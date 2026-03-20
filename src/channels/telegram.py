"""Telegram channel adapter with MessageQueue for sequential per-chat processing."""

import asyncio
import logging
import os
import time
from typing import Callable

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

# How old (seconds) a pending update must be before we skip it on restart.
# Messages within this window will still be processed even after a restart.
PENDING_UPDATE_AGE_CUTOFF_SECS = 120


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


class TelegramBot:
    """Telegram bot that routes messages through MessageQueue to AgentRunner."""

    def __init__(self, config: Config, agent: AgentRunner):
        self.config = config
        self.agent = agent
        self.allowed_users: set[int] = set(config.telegram_allowed_users)
        self.app: Application | None = None
        self._queue = MessageQueue()
        self._start_time: float = time.time()

    async def start(self) -> None:
        """Build the application and start polling."""
        # Guard: warn if the global .env has been contaminated with our token
        _check_global_env_contamination()

        # Guard: verify token is valid before building the full app
        ok, err = await verify_token_exclusive(self.config.telegram_bot_token)
        if not ok:
            log.error("Telegram startup aborted: %s", err)
            raise RuntimeError(f"Telegram token error: {err}")

        self.app = Application.builder().token(self.config.telegram_bot_token).build()

        self.app.add_handler(CommandHandler("start", self._handle_start))
        self.app.add_handler(CommandHandler("clear", self._handle_clear))
        self.app.add_handler(CommandHandler("whoami", self._handle_whoami))
        self.app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message)
        )

        log.info("Starting Telegram bot (polling)...")
        await self.app.initialize()
        await self.app.start()

        # Use timestamp-based filtering instead of drop_pending_updates=True.
        # This means messages sent within the last 2 minutes will still be delivered
        # after a restart, rather than being silently dropped.
        self._start_time = time.time()
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

    async def stop(self) -> None:
        """Graceful shutdown."""
        await self._queue.shutdown()
        if self.app:
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()

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

    async def _handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = update.effective_user.id
        chat_id = str(update.effective_chat.id)
        is_dm = self._is_dm(update)
        message_text = update.message.text

        # Skip messages that pre-date our startup by more than the cutoff.
        # This replaces drop_pending_updates=True: we still process recent messages
        # (e.g. sent while restarting) but drop stale ones from extended downtime.
        if update.message.date:
            msg_ts = update.message.date.timestamp()
            age = self._start_time - msg_ts
            if age > PENDING_UPDATE_AGE_CUTOFF_SECS:
                log.info(
                    "Skipping stale message from %s (%.0fs before startup)", user_id, age
                )
                return

        if is_dm and not self._is_allowed(user_id):
            return

        if not is_dm and not self._is_mentioned(update):
            return

        # Strip bot mention in groups
        if not is_dm and self.app.bot.username:
            message_text = message_text.replace(f"@{self.app.bot.username}", "").strip()

        # Send typing immediately
        await update.effective_chat.send_action("typing")

        # Write active chat_id to a temp file so bin/send_tg can send interim messages mid-turn
        try:
            import pathlib, tempfile
            active_chat_file = pathlib.Path(tempfile.gettempdir()) / "tarn_active_chat_id"
            active_chat_file.write_text(chat_id)
        except Exception:
            pass

        # Build handler and enqueue
        async def process():
            # Typing keepalive — refresh every 4 seconds until done
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
                response = await self.agent.reply(
                    message=message_text,
                    chat_id=chat_id,
                    is_main_session=is_dm,
                )

                if len(response) <= 4096:
                    await update.message.reply_text(response)
                else:
                    for i in range(0, len(response), 4096):
                        await update.message.reply_text(response[i : i + 4096])

            except Exception:
                log.exception("Error processing message from chat %s", chat_id)
                await update.message.reply_text(
                    "Something went wrong. Try again in a moment."
                )
            finally:
                done.set()
                keepalive_task.cancel()
                try:
                    await keepalive_task
                except asyncio.CancelledError:
                    pass

        await self._queue.enqueue(chat_id, process)
