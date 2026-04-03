"""Snapshot helpers — persist and read pre-restart agent state.

The snapshot captures:
  - Active background tasks (IDs, channel type/ID, started_at)
  - Cron queue state (job names and next run times)

Written synchronously to SQLite before SIGTERM is sent by restart-agent,
so startup can read a fresh snapshot and build a richer continuity message.

Snapshot TTL: 5 minutes.  Snapshots older than this are ignored on startup
(they reflect state from a much earlier run, not the one just killed).
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

log = logging.getLogger(__name__)

SNAPSHOT_TTL_SECS = 300  # 5 minutes


def _db_path(data_dir: Path) -> Path:
    return data_dir / "snapshot.db"


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_snapshot (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            saved_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS snapshot_bg_tasks (
            task_id      TEXT PRIMARY KEY,
            channel_type TEXT NOT NULL,
            channel_id   TEXT NOT NULL,
            original_message TEXT NOT NULL,
            started_at   REAL NOT NULL,
            saved_at     REAL NOT NULL
        )
        """
    )
    conn.commit()


def write_snapshot(
    data_dir: Path,
    bg_tasks: list[dict],
    cron_jobs: list[dict],
) -> None:
    """Write a pre-restart snapshot synchronously to SQLite.

    Args:
        data_dir: Agent data directory (e.g. data/tarn/).
        bg_tasks: List of dicts with keys: task_id, channel_type, channel_id,
                  original_message, started_at.
        cron_jobs: List of dicts with keys: name, next_run_time (ISO string or None).
    """
    import json

    db_file = _db_path(data_dir)
    try:
        conn = sqlite3.connect(str(db_file))
        _ensure_schema(conn)

        now = time.time()

        # Write cron state as a JSON blob
        conn.execute(
            "INSERT OR REPLACE INTO agent_snapshot (key, value, saved_at) VALUES (?, ?, ?)",
            ("cron_jobs", json.dumps(cron_jobs), now),
        )

        # Overwrite background tasks table
        conn.execute("DELETE FROM snapshot_bg_tasks")
        for task in bg_tasks:
            conn.execute(
                "INSERT OR REPLACE INTO snapshot_bg_tasks "
                "(task_id, channel_type, channel_id, original_message, started_at, saved_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    task.get("task_id", ""),
                    task.get("channel_type", ""),
                    task.get("channel_id", ""),
                    task.get("original_message", ""),
                    task.get("started_at", now),
                    now,
                ),
            )

        conn.commit()
        conn.close()
        log.info(
            "Snapshot written: %d bg tasks, %d cron jobs",
            len(bg_tasks),
            len(cron_jobs),
        )
    except Exception:
        log.exception("Failed to write snapshot (non-fatal)")


def read_snapshot(data_dir: Path) -> dict | None:
    """Read the most recent snapshot if it is < SNAPSHOT_TTL_SECS old.

    Returns a dict with keys:
        bg_tasks: list of dicts (task_id, channel_type, channel_id, original_message, started_at)
        cron_jobs: list of dicts (name, next_run_time)
        saved_at: float (unix timestamp)

    Returns None if no snapshot exists or it is too old.
    """
    import json

    db_file = _db_path(data_dir)
    if not db_file.exists():
        return None

    try:
        conn = sqlite3.connect(str(db_file))
        _ensure_schema(conn)

        now = time.time()

        # Check snapshot age
        cursor = conn.execute(
            "SELECT value, saved_at FROM agent_snapshot WHERE key = 'cron_jobs'"
        )
        row = cursor.fetchone()
        if row is None:
            conn.close()
            return None

        cron_json, saved_at = row
        if now - saved_at > SNAPSHOT_TTL_SECS:
            log.info(
                "Snapshot is %.0f seconds old (TTL=%d) — ignoring",
                now - saved_at,
                SNAPSHOT_TTL_SECS,
            )
            conn.close()
            return None

        cron_jobs = json.loads(cron_json)

        cursor = conn.execute(
            "SELECT task_id, channel_type, channel_id, original_message, started_at "
            "FROM snapshot_bg_tasks"
        )
        bg_rows = cursor.fetchall()
        bg_tasks = [
            {
                "task_id": r[0],
                "channel_type": r[1],
                "channel_id": r[2],
                "original_message": r[3],
                "started_at": r[4],
            }
            for r in bg_rows
        ]

        conn.close()
        return {
            "bg_tasks": bg_tasks,
            "cron_jobs": cron_jobs,
            "saved_at": saved_at,
        }

    except Exception:
        log.exception("Failed to read snapshot (non-fatal)")
        return None
