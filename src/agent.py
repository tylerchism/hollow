"""Agent runner — wraps Claude Code SDK with SQLite-backed persistent conversation history."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

import aiosqlite
import claude_code_sdk._internal.client as _sdk_client
import claude_code_sdk._internal.message_parser as _sdk_parser
import claude_code_sdk._internal.query as _sdk_query
from claude_code_sdk import (
    AssistantMessage,
    ClaudeCodeOptions,
    ResultMessage,
    SystemMessage,
    TextBlock,
    query,
)

# ---------------------------------------------------------------------------
# Monkey-patches for claude-code-sdk compatibility
# ---------------------------------------------------------------------------

_original_parse = _sdk_parser.parse_message


def _patched_parse(data):
    try:
        return _original_parse(data)
    except Exception:
        return SystemMessage(subtype=data.get("type", "unknown"), data=data)


_sdk_parser.parse_message = _patched_parse
_sdk_client.parse_message = _patched_parse

_original_handle_control = _sdk_query.Query._handle_control_request


async def _patched_handle_control(self, request):
    try:
        await _original_handle_control(self, request)
    except Exception:
        pass


_sdk_query.Query._handle_control_request = _patched_handle_control

if TYPE_CHECKING:
    from src.config import Config
    from src.memory import MemoryManager

log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 50
NATIVE_TOOLS = ["Bash", "Read", "Write", "Edit", "Glob", "Grep", "Agent", "WebSearch", "WebFetch"]
HISTORY_CHAR_BUDGET = 500_000

# SQL for chat_sessions table
_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS chat_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at REAL NOT NULL,
    session_key TEXT DEFAULT ''
);
"""
_CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_chat_sessions_chat_id
ON chat_sessions(chat_id, session_key);
"""


class PersistentConversationHistory:
    """SQLite-backed conversation history."""

    def __init__(self, db_path: str, char_budget: int = HISTORY_CHAR_BUDGET):
        self.db_path = db_path
        self.char_budget = char_budget
        self._db: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        """Open the database and ensure schema exists."""
        self._db = await aiosqlite.connect(self.db_path)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute(_CREATE_TABLE)
        await self._db.execute(_CREATE_INDEX)
        await self._db.commit()

    async def add(self, chat_id: str, role: str, content: str) -> None:
        """Insert a message and commit immediately."""
        await self._db.execute(
            "INSERT INTO chat_sessions (chat_id, role, content, created_at, session_key) VALUES (?, ?, ?, ?, '')",
            (chat_id, role, content, time.time()),
        )
        await self._db.commit()

    async def get_messages(self, chat_id: str) -> list[dict]:
        """Load active messages for a chat_id, applying char budget (drop oldest first)."""
        cursor = await self._db.execute(
            "SELECT role, content FROM chat_sessions WHERE chat_id = ? AND session_key = '' ORDER BY created_at ASC",
            (chat_id,),
        )
        rows = await cursor.fetchall()

        messages = [{"role": r[0], "content": r[1]} for r in rows]
        if not messages:
            return messages

        # Walk from newest to oldest, keep what fits in budget
        result = []
        total_chars = 0
        for msg in reversed(messages):
            msg_chars = len(msg["content"])
            if total_chars + msg_chars > self.char_budget and result:
                break
            result.insert(0, msg)
            total_chars += msg_chars

        return result

    async def clear(self, chat_id: str) -> None:
        """Tombstone all active rows for a chat_id."""
        ts = time.time()
        await self._db.execute(
            "UPDATE chat_sessions SET session_key = ? WHERE chat_id = ? AND session_key = ''",
            (f"cleared_{ts}", chat_id),
        )
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()


class AgentRunner:
    """Calls Claude via the Claude Code SDK with persistent history."""

    def __init__(
        self,
        config: Config,
        memory_manager: MemoryManager,
    ):
        self.config = config
        self.memory = memory_manager
        self.history: PersistentConversationHistory | None = None

    async def initialize(self) -> None:
        """Initialize the persistent history database."""
        db_path = str(self.config.data_dir / "hollow.db")
        self.history = PersistentConversationHistory(
            db_path, char_budget=self.config.history_char_budget
        )
        await self.history.initialize()

    def _model_id(self) -> str:
        model = self.config.primary_model
        if "/" in model:
            return model.split("/", 1)[1]
        return model

    def _build_prompt(self, messages: list[dict], current_message: str) -> str:
        """Format conversation history + current message into a single prompt."""
        if not messages:
            return current_message

        # Exclude the last message if it's the current user message we just added
        prior = messages[:-1] if messages else []
        if not prior:
            return current_message

        parts = ["<conversation_history>"]
        for msg in prior:
            role = msg["role"]
            content = msg["content"]
            parts.append(f"<{role}>{content}</{role}>")
        parts.append("</conversation_history>")
        parts.append("")
        parts.append(current_message)
        return "\n".join(parts)

    async def _compact_history(self, chat_id: str) -> None:
        """Compact conversation history when it exceeds the char budget.

        Summarizes old turns via claude-haiku-3-5, tombstones originals,
        inserts summary + last 4 turns as new active rows.
        """
        cursor = await self.history._db.execute(
            "SELECT id, role, content FROM chat_sessions WHERE chat_id = ? AND session_key = '' ORDER BY created_at ASC",
            (chat_id,),
        )
        rows = await cursor.fetchall()
        total_chars = sum(len(r[2]) for r in rows)

        if total_chars <= self.config.history_char_budget:
            return

        log.info("Compacting history for chat_id=%s (%d chars, %d messages)", chat_id, total_chars, len(rows))

        # Build history text for summarization
        history_text = "\n".join(f"[{r[1]}]: {r[2][:2000]}" for r in rows)
        if len(history_text) > 100_000:
            history_text = history_text[:100_000] + "\n[...truncated for summarization...]"

        compaction_prompt = (
            "You are summarizing a conversation for context compression. "
            "Produce a concise summary that captures: key decisions made, active tasks and their status, "
            "important context about the user and project, and any unresolved threads. "
            "Format as a brief narrative, not a list. Keep it under 500 words.\n\n"
            f"Conversation to summarize:\n{history_text}"
        )

        try:
            summary = ""

            async def _compaction_stream():
                yield {
                    "type": "user",
                    "message": {"role": "user", "content": compaction_prompt},
                }

            options = ClaudeCodeOptions(
                model="claude-haiku-4-5",
                max_turns=1,
                permission_mode="bypassPermissions",
            )
            async for msg in query(prompt=_compaction_stream(), options=options):
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            summary += block.text

            if not summary:
                log.warning("Compaction returned empty summary — skipping")
                return

            # Tombstone old active rows
            ts = time.time()
            await self.history._db.execute(
                "UPDATE chat_sessions SET session_key = ? WHERE chat_id = ? AND session_key = ''",
                (f"compacted_{ts}", chat_id),
            )

            # Insert summary as new active pair
            now = time.time()
            await self.history._db.execute(
                "INSERT INTO chat_sessions (chat_id, role, content, created_at, session_key) VALUES (?, ?, ?, ?, '')",
                (chat_id, "user", "[Context summary of prior conversation]", now),
            )
            await self.history._db.execute(
                "INSERT INTO chat_sessions (chat_id, role, content, created_at, session_key) VALUES (?, ?, ?, ?, '')",
                (chat_id, "assistant", summary, now + 0.001),
            )

            # Re-insert last 4 turns (up to 8 messages: 4 user + 4 assistant)
            last_turns = rows[-8:] if len(rows) >= 8 else rows
            for i, row in enumerate(last_turns):
                await self.history._db.execute(
                    "INSERT INTO chat_sessions (chat_id, role, content, created_at, session_key) VALUES (?, ?, ?, ?, '')",
                    (chat_id, row[1], row[2], now + 0.01 + i * 0.001),
                )

            await self.history._db.commit()

            # Verify compaction result
            new_msgs = await self.history.get_messages(chat_id)
            new_chars = sum(len(m["content"]) for m in new_msgs)
            log.info("Compaction complete: %d chars → %d chars, %d messages", total_chars, new_chars, len(new_msgs))

        except Exception:
            log.exception("Compaction failed — continuing with original history")

    async def reply(
        self,
        message: str,
        chat_id: str,
        is_main_session: bool = True,
        context_injection: str = "",
        allowed_tools: list[str] | None = None,
    ) -> str:
        """Process a user message and return the assistant's response.

        Args:
            message: The user's message.
            chat_id: Session identifier (stable per user/agent pair for delegation).
            is_main_session: Whether to load full memory context (memory.md etc.).
            context_injection: Optional context from a delegating agent (e.g. Tarn
                passing conversation background to a sub-agent). Appended to the
                system prompt inside <delegation_context> tags.
            allowed_tools: Override the default NATIVE_TOOLS allowlist for this
                call.  Pass a restricted list to block dangerous tools (e.g.
                exclude Bash during read-only startup sequences).
        """
        # 0. Run compaction if history is too large (before adding new message)
        try:
            await self._compact_history(chat_id)
        except Exception:
            log.exception("Pre-reply compaction check failed — continuing")

        # 1. Persist user message
        await self.history.add(chat_id, "user", message)

        # 2. Build system prompt via memory manager
        context = await self.memory.get_context(
            query=message, is_main_session=is_main_session
        )

        # 2a. Append delegated context if provided
        if context_injection:
            context += (
                f"\n\n<delegation_context>\n{context_injection}\n</delegation_context>"
            )
            log.debug("Context injection applied (%d chars) for chat_id=%s", len(context_injection), chat_id)

        # 3. Load history
        messages = await self.history.get_messages(chat_id)

        # 4. Build prompt with history
        prompt = self._build_prompt(messages, message)

        # 5. Call Claude Code SDK — use streaming input mode to avoid
        #    Linux MAX_ARG_STRLEN (128 KB) limit on individual CLI arguments.
        async def _prompt_stream():
            yield {
                "type": "user",
                "message": {"role": "user", "content": prompt},
            }

        options = ClaudeCodeOptions(
            system_prompt=context,
            max_turns=MAX_TOOL_ROUNDS,
            model=self._model_id(),
            permission_mode="bypassPermissions",
            allowed_tools=allowed_tools if allowed_tools is not None else NATIVE_TOOLS,
        )

        reply_text = ""
        is_error_response = False
        try:
            async for msg in query(prompt=_prompt_stream(), options=options):
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            reply_text += block.text
                elif isinstance(msg, ResultMessage):
                    if msg.is_error and not reply_text:
                        reply_text = f"Error: {msg.result or 'unknown error'}"
                    log.info(
                        "SDK query complete: %d turns, %dms",
                        msg.num_turns,
                        msg.duration_ms,
                    )
        except BaseException as e:
            if isinstance(e, asyncio.CancelledError):
                raise
            # Detect shutdown-related errors: SIGTERM causes the Claude Code SDK
            # subprocess to exit with code 143, surfaced as a plain Exception with
            # "exit code 143" in the message. Also catch CancelledError subclasses
            # by name (e.g. from trio/anyio interop) and stop_event if available.
            _is_shutdown = (
                "exit code 143" in str(e)
                or type(e).__name__ == "CancelledError"
                or (hasattr(self, "stop_event") and self.stop_event is not None and self.stop_event.is_set())
            )
            if _is_shutdown:
                log.info(
                    "SDK stream interrupted by shutdown signal (suppressing error message): [%s] %s",
                    type(e).__name__,
                    e or "(empty message)",
                )
                # Silently swallow — the new process will send its own startup message
                return reply_text or ""
            if reply_text:
                log.debug("SDK stream ended with error (response already received): %s", e)
            else:
                log.error(
                    "Claude Code SDK query failed: [%s] %s",
                    type(e).__name__,
                    e or "(empty message)",
                )
                reply_text = "Sorry, I encountered an error processing your message."
                is_error_response = True

                # Fire-and-forget operator alert via Telegram
                async def _send_operator_alert(
                    _token=self.config.telegram_bot_token,
                    _chat_id=self.config.heartbeat_chat_id,
                    _exc_type=type(e).__name__,
                    _user_chat_id=chat_id,
                    _user_msg=message,
                ):
                    if not _token or not _chat_id:
                        return
                    truncated = _user_msg[:200] + ("..." if len(_user_msg) > 200 else "")
                    alert = (
                        f"[AGENT ERROR] {_exc_type}\n"
                        f"chat_id: {_user_chat_id}\n"
                        f"message: {truncated}"
                    )
                    try:
                        import httpx
                        async with httpx.AsyncClient(timeout=10) as client:
                            await client.post(
                                f"https://api.telegram.org/bot{_token}/sendMessage",
                                json={"chat_id": _chat_id, "text": alert},
                            )
                    except Exception:
                        log.debug("Operator alert send failed (non-fatal)", exc_info=True)

                asyncio.create_task(_send_operator_alert())

        # 6. Persist assistant response (skip if it's an error fallback string)
        if not is_error_response:
            await self.history.add(chat_id, "assistant", reply_text)

        return reply_text

    async def clear_history(self, chat_id: str) -> None:
        """Clear conversation history for a chat."""
        await self.history.clear(chat_id)
