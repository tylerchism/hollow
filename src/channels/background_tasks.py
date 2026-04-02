"""BackgroundTaskManager — tracks long-running agent tasks and delivers results.

When a channel handler's initial wait window (e.g. 900s) expires before the
agent finishes, the in-flight asyncio.Task is handed to this manager instead
of being cancelled.  The manager:

  1. Attaches a done-callback that fires the delivery function when the task
     completes.
  2. Applies a hard upper timeout (default 30 min) so tasks never run forever.
  3. Persists task records to SQLite so that tasks in-flight at restart time can
     be recovered — the original message is re-run and the result delivered to
     the correct channel when the process comes back up.
  4. Logs unhandled task exceptions rather than silently swallowing them.

Restart recovery
----------------
asyncio.Task objects are process-local and die with the process.  On restart,
``load_pending()`` returns records for tasks that were in-flight when the
process died.  The caller (typically ``_recover_pending_tasks()`` in main.py)
re-runs ``agent.reply(original_message, chat_id=channel_id)`` for each record
and delivers the result when it completes.  From Tyler's perspective the work
disappears briefly (restart) then the answer arrives.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

import aiosqlite

log = logging.getLogger(__name__)

# Hard upper bound on how long any background task may run (seconds).
# A 30-minute ceiling is generous for any realistic agent task; tasks that
# exceed it almost certainly indicate a stuck Claude API call or infinite loop.
BACKGROUND_TASK_MAX_SECS = 1800  # 30 minutes


@dataclass
class _TaskRecord:
    task_id: str
    channel_type: str       # "discord" | "telegram"
    channel_id: str
    original_message: str
    task: asyncio.Task
    deliver_fn: Callable[[str], Awaitable[None]]
    error_fn: Callable[[Exception], Awaitable[None]]
    watchdog: asyncio.Task | None = field(default=None, repr=False)


@dataclass
class PendingTask:
    """Lightweight record returned by ``load_pending()`` for restart recovery."""
    task_id: str
    channel_type: str
    channel_id: str
    original_message: str
    started_at: float


class BackgroundTaskManager:
    """Register background agent tasks and fire delivery callbacks on completion."""

    def __init__(self, data_dir: Path | None = None) -> None:
        self._data_dir = data_dir
        self._db: aiosqlite.Connection | None = None
        self._tasks: dict[str, _TaskRecord] = {}
        self._counter: int = 0

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def initialize(self) -> None:
        """Open the SQLite database and create the pending_tasks table.

        Must be awaited before ``register()`` or ``load_pending()`` are called.
        Safe to call even when ``data_dir`` is None (no-op in that case).
        """
        if self._data_dir is None:
            return
        db_path = self._data_dir / "pending_tasks.db"
        self._db = await aiosqlite.connect(str(db_path))
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_tasks (
                task_id         TEXT PRIMARY KEY,
                channel_type    TEXT NOT NULL,
                channel_id      TEXT NOT NULL,
                original_message TEXT NOT NULL,
                started_at      REAL NOT NULL
            )
            """
        )
        await self._db.commit()
        log.info("BackgroundTaskManager: SQLite pending_tasks initialised at %s", db_path)

    async def close(self) -> None:
        """Close the SQLite connection (called on shutdown)."""
        if self._db is not None:
            try:
                await self._db.close()
            except Exception:
                log.exception("BackgroundTaskManager: error closing SQLite")
            self._db = None

    # ── Public API ─────────────────────────────────────────────────────────────

    def register(
        self,
        task: asyncio.Task,
        deliver_fn: Callable[[str], Awaitable[None]],
        error_fn: Callable[[Exception], Awaitable[None]],
        channel_type: str,
        channel_id: str,
        original_message: str = "",
    ) -> str:
        """Register *task* for background delivery.

        Returns the internal task_id (for logging).  Both *deliver_fn* and
        *error_fn* must be async callables — they will be called from within
        asyncio via create_task so they run on the event loop safely.

        *original_message* is persisted to SQLite so the task can be replayed
        after a restart.  Pass the verbatim user message that triggered the
        agent call.
        """
        self._counter += 1
        task_id = f"bg{self._counter}"
        started_at = time.time()

        watchdog = asyncio.create_task(
            self._watchdog(task_id, task),
            name=f"bg_watchdog_{task_id}",
        )

        record = _TaskRecord(
            task_id=task_id,
            channel_type=channel_type,
            channel_id=channel_id,
            original_message=original_message,
            task=task,
            deliver_fn=deliver_fn,
            error_fn=error_fn,
            watchdog=watchdog,
        )
        self._tasks[task_id] = record

        # Attach a synchronous done-callback that schedules an async handler.
        # asyncio callbacks are synchronous; we bridge via create_task.
        task.add_done_callback(
            lambda t: asyncio.create_task(
                self._on_task_done(task_id, t),
                name=f"bg_deliver_{task_id}",
            )
        )

        # Persist to SQLite (fire-and-forget; failure is logged but non-fatal)
        asyncio.create_task(
            self._persist(task_id, channel_type, channel_id, original_message, started_at),
            name=f"bg_persist_{task_id}",
        )

        log.info(
            "BackgroundTaskManager: registered task %s for %s/%s",
            task_id, channel_type, channel_id,
        )
        return task_id

    async def load_pending(self) -> list[PendingTask]:
        """Return all tasks that were registered but never delivered.

        Called at startup to recover tasks that were in-flight when the process
        died.  Returns an empty list if no database is configured.
        """
        if self._db is None:
            return []
        try:
            cursor = await self._db.execute(
                "SELECT task_id, channel_type, channel_id, original_message, started_at "
                "FROM pending_tasks ORDER BY started_at ASC"
            )
            rows = await cursor.fetchall()
            return [
                PendingTask(
                    task_id=row[0],
                    channel_type=row[1],
                    channel_id=row[2],
                    original_message=row[3],
                    started_at=row[4],
                )
                for row in rows
            ]
        except Exception:
            log.exception("BackgroundTaskManager: failed to load pending tasks")
            return []

    async def shutdown(self) -> None:
        """Cancel all in-flight tasks gracefully (called on shutdown)."""
        if not self._tasks:
            return

        log.info(
            "BackgroundTaskManager: cancelling %d in-flight task(s) on shutdown",
            len(self._tasks),
        )
        for record in list(self._tasks.values()):
            if record.watchdog and not record.watchdog.done():
                record.watchdog.cancel()
            if not record.task.done():
                record.task.cancel()

        # Brief wait so cancellations propagate
        await asyncio.sleep(0)
        await self.close()

    # ── Internal ───────────────────────────────────────────────────────────────

    async def _persist(
        self,
        task_id: str,
        channel_type: str,
        channel_id: str,
        original_message: str,
        started_at: float,
    ) -> None:
        """Write a pending task record to SQLite."""
        if self._db is None:
            return
        try:
            await self._db.execute(
                "INSERT OR REPLACE INTO pending_tasks "
                "(task_id, channel_type, channel_id, original_message, started_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (task_id, channel_type, channel_id, original_message, started_at),
            )
            await self._db.commit()
        except Exception:
            log.exception("BackgroundTaskManager: failed to persist task %s", task_id)

    async def _delete_pending(self, task_id: str) -> None:
        """Remove a completed task record from SQLite."""
        if self._db is None:
            return
        try:
            await self._db.execute(
                "DELETE FROM pending_tasks WHERE task_id = ?", (task_id,)
            )
            await self._db.commit()
        except Exception:
            log.exception("BackgroundTaskManager: failed to delete pending task %s", task_id)

    async def _watchdog(self, task_id: str, task: asyncio.Task) -> None:
        """Cancel the task after BACKGROUND_TASK_MAX_SECS if it hasn't finished."""
        try:
            await asyncio.sleep(BACKGROUND_TASK_MAX_SECS)
        except asyncio.CancelledError:
            return  # normal path: task finished before watchdog fired

        if not task.done():
            log.warning(
                "BackgroundTaskManager: task %s exceeded %ds hard limit — cancelling",
                task_id, BACKGROUND_TASK_MAX_SECS,
            )
            task.cancel()

    async def _on_task_done(self, task_id: str, task: asyncio.Task) -> None:
        """Called when the background task finishes (success, error, or cancel)."""
        record = self._tasks.pop(task_id, None)
        if record is None:
            return  # Already cleaned up (e.g. shutdown race)

        # Cancel the watchdog — it's no longer needed
        if record.watchdog and not record.watchdog.done():
            record.watchdog.cancel()

        # Always remove the SQLite record regardless of outcome — if delivery
        # fails we don't want a stale record triggering a spurious recovery.
        await self._delete_pending(task_id)

        if task.cancelled():
            log.info(
                "BackgroundTaskManager: task %s was cancelled (restart or timeout)",
                task_id,
            )
            # No delivery — task was cancelled (shutdown or watchdog).
            return

        exc = task.exception()
        if exc is not None:
            log.error(
                "BackgroundTaskManager: task %s raised %s: %s",
                task_id, type(exc).__name__, exc,
            )
            try:
                await record.error_fn(exc)
            except Exception:
                log.exception(
                    "BackgroundTaskManager: error_fn for task %s raised",
                    task_id,
                )
            return

        result = task.result()
        log.info(
            "BackgroundTaskManager: task %s completed (%d chars) — delivering to %s/%s",
            task_id, len(result or ""), record.channel_type, record.channel_id,
        )
        try:
            await record.deliver_fn(result or "")
        except Exception:
            log.exception(
                "BackgroundTaskManager: deliver_fn for task %s raised — result lost",
                task_id,
            )
