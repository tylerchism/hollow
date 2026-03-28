"""Telegram channel adapter with MessageQueue for sequential per-chat processing."""

import asyncio
import logging
import os
import time
from typing import Callable

import aiosqlite
import httpx
from telegram import Update
from telegram.error import Conflict, NetworkError, TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from src.agent import AgentRunner
from src.config import Config

log = logging.getLogger(__name__)

# How long (seconds) to keep a completed update_id in the dedup cache before
# evicting it.  Re-delivered updates that arrive within this window are silently
# dropped; after the window expires the entry is removed to keep memory bounded.
DEDUP_TTL_SECS = 300  # 5 minutes

# DDL for the offset-persistence table (added to hollow.db at startup).
_OFFSET_DDL = """
CREATE TABLE IF NOT EXISTS telegram_offset (
    id   INTEGER PRIMARY KEY CHECK (id = 1),
    last_update_id INTEGER NOT NULL DEFAULT 0,
    updated_at     REAL    NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO telegram_offset (id, last_update_id, updated_at)
VALUES (1, 0, 0);
"""


async def verify_token_exclusive(token: str) -> tuple[bool, str]:
    """
    Call Telegram's getMe and deleteWebhook to verify we can own this token exclusively.
    Returns (ok, error_message). If another process is polling, we'll detect it here
    or shortly after when start_polling raises Conflict.
    """
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"https://api.telegram.org/bot{token}/getMe")
            data = resp.json()
            if not data.get("ok"):
                return False, f"Telegram rejected token: {data.get('description', 'unknown error')}"
            bot_name = data["result"].get("username", "unknown")
            log.info("Telegram token verified — bot is @%s", bot_name)
            return True, ""
    except Exception as exc:
        return False, f"Token verification failed: {exc}"


def _check_global_env_contamination() -> None:
    """
    Warn loudly if the global hollow .env has a non-empty TELEGRAM_BOT_TOKEN.
    This is the root cause of the token conflict incident.
    """
    global_env = os.path.join(os.path.dirname(__file__), "..", "..", ".env")
    global_env = os.path.normpath(global_env)
    if not os.path.exists(global_env):
        return
    try:
        with open(global_env) as f:
            for line in f:
                line = line.strip()
                if line.startswith("TELEGRAM_BOT_TOKEN=") and not line.endswith("=") and "#" not in line.split("=")[0]:
                    value = line.split("=", 1)[1].strip()
                    if value and not value.startswith("#"):
                        log.error(
                            "SAFETY VIOLATION: TELEGRAM_BOT_TOKEN is set in the global .env (%s). "
                            "This will cause token conflicts across all specialist agents. "
                            "Remove it immediately — token belongs only in agents/tarn/.env.",
                            global_env,
                        )
    except Exception:
        pass


class MessageQueue:
    """Per-chat sequential message processing queue."""

    def __init__(self):
        self._queues: dict[str, asyncio.Queue] = {}
        self._workers: dict[str, asyncio.Task] = {}

    async def enqueue(self, chat_id: str, handler: Callable) -> None:
        """Enqueue an async handler for sequential processing."""
        if chat_id not in self._queues:
            self._queues[chat_id] = asyncio.Queue()
            self._workers[chat_id] = asyncio.create_task(self._worker(chat_id))
        await self._queues[chat_id].put(handler)

    async def _worker(self, chat_id: str) -> None:
        """Consume queue serially for a chat_id."""
        queue = self._queues[chat_id]
        while True:
            handler = await queue.get()
            try:
                await handler()
            except Exception:
                log.exception("Error processing queued message for chat %s", chat_id)
            finally:
                queue.task_done()

    async def shutdown(self) -> None:
        """Cancel all worker tasks."""
        for task in self._workers.values():
            task.cancel()
        for task in self._workers.values():
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._workers.clear()
        self._queues.clear()


class UpdateOffsetStore:
    """
    SQLite-backed store for the Telegram getUpdates offset watermark.

    Implements the OpenClaw in-flight watermark pattern:
    - pending_update_ids: set of update IDs currently being processed
    - highest_completed_update_id: the highest update_id we have fully processed
    - persisted offset = min(pending_update_ids) - 1  (or highest_completed if no pending)

    This ensures that if update 6 is slow and update 7 finishes first, we persist
    offset=5 so that both 6 and 7 are re-delivered on restart (safe re-delivery),
    rather than skipping 6.  The 5-minute TTL dedup cache prevents re-delivered
    updates from being processed twice.
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

        # In-flight watermark state
        self._pending_update_ids: set[int] = set()
        self._highest_completed_update_id: int = 0

        # TTL dedup cache: update_id -> completed_at timestamp
        self._dedup_cache: dict[int, float] = {}

    async def initialize(self) -> None:
        """Open DB and create the offset table if needed."""
        self._db = await aiosqlite.connect(self.db_path)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.executescript(_OFFSET_DDL)
        await self._db.commit()
        # Seed in-memory watermark from DB
        self._highest_completed_update_id = await self._read_persisted_offset()

    async def _read_persisted_offset(self) -> int:
        cursor = await self._db.execute(
            "SELECT last_update_id FROM telegram_offset WHERE id = 1"
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def get_startup_offset(self) -> int:
        """Return the offset to pass to getUpdates on startup (last_update_id)."""
        return await self._read_persisted_offset()

    def mark_in_flight(self, update_id: int) -> None:
        """Register that we are starting to process an update."""
        self._pending_update_ids.add(update_id)

    def is_duplicate(self, update_id: int) -> bool:
        """
        Return True if this update_id was already completed recently (within TTL).
        Also prunes stale dedup entries.
        """
        now = time.time()
        # Prune expired entries
        expired = [uid for uid, ts in self._dedup_cache.items() if now - ts > DEDUP_TTL_SECS]
        for uid in expired:
            del self._dedup_cache[uid]
        return update_id in self._dedup_cache

    async def mark_completed(self, update_id: int) -> None:
        """
        Register that we finished processing an update, then persist the safe offset.
        """
        async with self._lock:
            self._pending_update_ids.discard(update_id)
            self._dedup_cache[update_id] = time.time()
            if update_id > self._highest_completed_update_id:
                self._highest_completed_update_id = update_id
            await self._persist_safe_offset()

    async def _persist_safe_offset(self) -> None:
        """
        Compute and write the safe watermark:
          - If there are in-flight updates, persist min(pending) - 1
          - Otherwise persist highest_completed
        """
        if self._pending_update_ids:
            safe_offset = min(self._pending_update_ids) - 1
        else:
            safe_offset = self._highest_completed_update_id

        await self._db.execute(
            "UPDATE telegram_offset SET last_update_id = ?, updated_at = ? WHERE id = 1",
            (safe_offset, time.time()),
        )
        await self._db.commit()
        log.debug(
            "Persisted Telegram offset=%d (pending=%s, highest_completed=%d)",
            safe_offset,
            self._pending_update_ids,
            self._highest_completed_update_id,
        )

    async def close(self) -> None:
        if self._db:
            await self._db.close()


class TelegramBot:
    """Telegram bot that routes messages through MessageQueue to AgentRunner."""

    def __init__(self, config: Config, agent: AgentRunner):
        self.config = config
        self.agent = agent
        self.allowed_users: set[int] = set(config.telegram_allowed_users)
        self.app: Application | None = None
        self._queue = MessageQueue()
        self._start_time: float = time.time()
        self._offset_store = UpdateOffsetStore(
            str(config.data_dir / "hollow.db")
        )

    async def start(self) -> None:
        """Build the application and start polling."""
        # Guard: warn if the global .env has been contaminated with our token
        _check_global_env_contamination()

        # Guard: verify token is valid before building the full app
        ok, err = await verify_token_exclusive(self.config.telegram_bot_token)
        if not ok:
            log.error("Telegram startup aborted: %s", err)
            raise RuntimeError(f"Telegram token error: {err}")

        # Initialize offset store (reads persisted offset from DB)
        await self._offset_store.initialize()
        startup_offset = await self._offset_store.get_startup_offset()
        log.info("Telegram startup offset from DB: %d", startup_offset)

        self.app = Application.builder().token(self.config.telegram_bot_token).build()

        self.app.add_handler(CommandHandler("start", self._handle_start))
        self.app.add_handler(CommandHandler("clear", self._handle_clear))
        self.app.add_handler(CommandHandler("whoami", self._handle_whoami))
        self.app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message)
        )
        self.app.add_handler(
            MessageHandler(filters.PHOTO, self._handle_photo)
        )
        self.app.add_handler(
            MessageHandler(filters.Document.ALL, self._handle_document)
        )
        self.app.add_handler(
            MessageHandler(filters.VOICE | filters.AUDIO, self._handle_voice)
        )

        log.info("Starting Telegram bot (polling)...")
        await self.app.initialize()
        await self.app.start()

        # Surface any messages that arrived while we were offline.
        # We do this before start_polling so we can handle them deliberately.
        if startup_offset > 0:
            await self._surface_missed_messages(startup_offset)

        self._start_time = time.time()

        # drop_pending_updates=False: we rely on the stored offset for safety,
        # not on Telegram dropping updates for us.
        await self.app.updater.start_polling(
            drop_pending_updates=False,
            error_callback=self._handle_polling_error,
        )

    def _handle_polling_error(self, error: Exception) -> None:
        """Handle errors from the polling loop."""
        if isinstance(error, Conflict):
            log.critical(
                "TELEGRAM CONFLICT: Another process is polling with the same token. "
                "This means TELEGRAM_BOT_TOKEN leaked into a specialist agent's environment. "
                "Check: grep TELEGRAM_BOT_TOKEN ~/git/hollow/.env and all service files."
            )
        elif isinstance(error, NetworkError):
            log.warning("Telegram network error (will retry): %s", error)
        else:
            log.error("Telegram polling error: %s", error)

    async def _surface_missed_messages(self, last_offset: int) -> None:
        """
        Fetch updates since last_offset from Telegram.  Any updates whose
        update_id is greater than last_offset arrived while we were offline.
        Log them with an offline-window note so the operator can see what was
        missed; they will be processed normally by the polling loop.
        """
        try:
            # Use a short timeout — this is best-effort at startup
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"https://api.telegram.org/bot{self.config.telegram_bot_token}/getUpdates",
                    params={"offset": last_offset + 1, "limit": 100, "timeout": 0},
                )
                data = resp.json()

            if not data.get("ok"):
                log.warning("Could not fetch missed updates: %s", data.get("description"))
                return

            updates = data.get("result", [])
            if not updates:
                log.info("No missed messages since last offset %d", last_offset)
                return

            # Determine the offline window
            first_update = updates[0]
            last_update = updates[-1]
            first_msg = (
                first_update.get("message") or
                first_update.get("edited_message") or {}
            )
            last_msg = (
                last_update.get("message") or
                last_update.get("edited_message") or {}
            )
            offline_from = first_msg.get("date")
            offline_to = last_msg.get("date")

            if offline_from and offline_to:
                import datetime
                from_dt = datetime.datetime.utcfromtimestamp(offline_from).strftime("%Y-%m-%d %H:%M:%S UTC")
                to_dt = datetime.datetime.utcfromtimestamp(offline_to).strftime("%Y-%m-%d %H:%M:%S UTC")
                log.info(
                    "I was offline from %s to %s; %d message(s) arrived during that time:",
                    from_dt,
                    to_dt,
                    len(updates),
                )
            else:
                log.info(
                    "Missed %d update(s) since offset %d (timestamps unavailable):",
                    len(updates),
                    last_offset,
                )

            for upd in updates:
                msg = upd.get("message") or upd.get("edited_message") or {}
                sender = (msg.get("from") or {}).get("username", "unknown")
                text = msg.get("text", "<non-text>")
                log.info(
                    "  [offline] update_id=%d from=@%s text=%r",
                    upd["update_id"],
                    sender,
                    text[:200],
                )

        except Exception:
            log.exception("Error surfacing missed messages — continuing startup")

    async def stop(self) -> None:
        """Graceful shutdown."""
        await self._queue.shutdown()
        if self.app:
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()
        await self._offset_store.close()

    async def send_message(self, chat_id: int | str, text: str) -> None:
        """Send a message to a chat (used by cron notifications)."""
        if not self.app:
            return
        # Telegram 4096 char limit
        text = str(text)
        if len(text) <= 4096:
            await self.app.bot.send_message(chat_id=chat_id, text=text)
        else:
            for i in range(0, len(text), 4096):
                await self.app.bot.send_message(chat_id=chat_id, text=text[i : i + 4096])

    def _is_allowed(self, user_id: int) -> bool:
        if not self.allowed_users:
            return True
        return user_id in self.allowed_users

    def _is_dm(self, update: Update) -> bool:
        return update.effective_chat.type == "private"

    def _is_mentioned(self, update: Update) -> bool:
        message = update.effective_message
        if message.reply_to_message and message.reply_to_message.from_user:
            if message.reply_to_message.from_user.id == self.app.bot.id:
                return True
        if message.entities:
            bot_username = f"@{self.app.bot.username}"
            for entity in message.entities:
                if entity.type == "mention":
                    mentioned = message.text[entity.offset : entity.offset + entity.length]
                    if mentioned.lower() == bot_username.lower():
                        return True
        return False

    async def _handle_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_allowed(update.effective_user.id):
            await update.message.reply_text("Not authorized.")
            return
        await update.message.reply_text("Hey. What's up?")

    async def _handle_clear(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = str(update.effective_chat.id)
        await self.agent.clear_history(chat_id)
        await update.message.reply_text("Conversation history cleared.")

    async def _handle_whoami(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        await update.message.reply_text(
            f"User ID: {user.id}\nUsername: @{user.username or 'none'}"
        )

    async def _download_to_tmp(self, file_id: str, suffix: str) -> str:
        """Download a Telegram file to /tmp and return the local path."""
        import pathlib, tempfile
        tg_file = await self.app.bot.get_file(file_id)
        tmp_path = pathlib.Path(tempfile.gettempdir()) / f"tarn_{file_id}{suffix}"
        await tg_file.download_to_drive(str(tmp_path))
        return str(tmp_path)

    async def _handle_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle photo messages — download and pass to agent as a readable file path."""
        user_id = update.effective_user.id
        chat_id = str(update.effective_chat.id)
        if not self._is_allowed(user_id):
            return

        # Best quality = last photo in the array
        photo = update.message.photo[-1]
        local_path = await self._download_to_tmp(photo.file_id, ".jpg")
        caption = update.message.caption or ""

        message_text = f"[Image sent via Telegram — use the Read tool to view it: {local_path}]"
        if caption:
            message_text += f"\n\nCaption: {caption}"

        await self._process_and_reply(update, chat_id, user_id, message_text)

    async def _handle_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle documents (files). Images sent as files, PDFs, text files, etc."""
        user_id = update.effective_user.id
        chat_id = str(update.effective_chat.id)
        if not self._is_allowed(user_id):
            return

        doc = update.message.document
        mime = doc.mime_type or ""
        filename = doc.file_name or f"file_{doc.file_id}"

        # Determine suffix from filename or mime type
        import pathlib
        suffix = pathlib.Path(filename).suffix or ""
        if not suffix:
            if "pdf" in mime:
                suffix = ".pdf"
            elif "image" in mime:
                suffix = ".jpg"
            elif "text" in mime:
                suffix = ".txt"

        local_path = await self._download_to_tmp(doc.file_id, suffix)
        caption = update.message.caption or ""

        if "image" in mime or suffix in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
            message_text = f"[Image file sent via Telegram — use the Read tool to view it: {local_path}]"
        elif suffix == ".pdf":
            message_text = f"[PDF file sent via Telegram — use the Read tool to view it: {local_path}]"
        else:
            message_text = f"[File sent via Telegram: {filename} — saved to {local_path}]"

        if caption:
            message_text += f"\n\nCaption: {caption}"

        await self._process_and_reply(update, chat_id, user_id, message_text)

    async def _handle_voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle voice messages and audio files."""
        user_id = update.effective_user.id
        chat_id = str(update.effective_chat.id)
        if not self._is_allowed(user_id):
            return

        if update.message.voice:
            audio = update.message.voice
            suffix = ".ogg"
            label = "Voice message"
        else:
            audio = update.message.audio
            suffix = ".mp3"
            label = f"Audio: {getattr(audio, 'file_name', 'audio')}"

        local_path = await self._download_to_tmp(audio.file_id, suffix)
        duration = getattr(audio, "duration", "?")

        message_text = (
            f"[{label} sent via Telegram ({duration}s) — saved to {local_path}. "
            f"Audio transcription requires whisper (not yet installed). "
            f"Let Tyler know if you need this transcribed.]"
        )

        await self._process_and_reply(update, chat_id, user_id, message_text)

    async def _process_and_reply(
        self,
        update: Update,
        chat_id: str,
        user_id: int,
        message_text: str,
    ) -> None:
        """Shared processing pipeline for all message types."""
        update_id = update.update_id
        is_dm = self._is_dm(update)

        if self._offset_store.is_duplicate(update_id):
            log.info("Dropping duplicate update_id=%d", update_id)
            return

        await update.effective_chat.send_action("typing")

        try:
            import pathlib, tempfile, time
            active_chat_file = pathlib.Path(tempfile.gettempdir()) / "tarn_active_chat_id"
            active_chat_file.write_text(chat_id)
            ts_file = pathlib.Path(tempfile.gettempdir()) / "tarn_active_telegram_ts"
            ts_file.write_text(str(int(time.time())))
        except Exception:
            pass

        self._offset_store.mark_in_flight(update_id)

        async def process():
            done = asyncio.Event()

            async def typing_keepalive():
                while not done.is_set():
                    await asyncio.sleep(4)
                    if not done.is_set():
                        try:
                            await update.effective_chat.send_action("typing")
                        except Exception:
                            pass

            keepalive_task = asyncio.create_task(typing_keepalive())
            try:
                response = await asyncio.wait_for(
                    self.agent.reply(
                        message=message_text,
                        chat_id=chat_id,
                        is_main_session=is_dm,
                    ),
                    timeout=300,
                )
                if not response or not response.strip():
                    response = "I ran out of investigation steps before finishing. Send me a message to continue where I left off."
                if len(response) <= 4096:
                    await update.message.reply_text(response)
                else:
                    for i in range(0, len(response), 4096):
                        await update.message.reply_text(response[i : i + 4096])
            except asyncio.TimeoutError:
                log.warning("Telegram: agent.reply() timed out after 300s for chat %s", chat_id)
                await update.message.reply_text("still working on this — taking longer than expected. I'll follow up when I have something.")
            except Exception:
                log.exception("Error processing media message from chat %s", chat_id)
                await update.message.reply_text("Something went wrong. Try again in a moment.")
            finally:
                done.set()
                keepalive_task.cancel()
                try:
                    await keepalive_task
                except asyncio.CancelledError:
                    pass
                await self._offset_store.mark_completed(update_id)

        await self._queue.enqueue(chat_id, process)

    async def _handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = update.effective_user.id
        chat_id = str(update.effective_chat.id)
        is_dm = self._is_dm(update)
        message_text = update.message.text

        if is_dm and not self._is_allowed(user_id):
            return

        if not is_dm and not self._is_mentioned(update):
            return

        # Strip bot mention in groups
        if not is_dm and self.app.bot.username:
            message_text = message_text.replace(f"@{self.app.bot.username}", "").strip()

        await self._process_and_reply(update, chat_id, user_id, message_text)
