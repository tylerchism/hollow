"""SQLite init and CRUD for journal entries."""

import json
import os
import sqlite3
from pathlib import Path

DB_DIR = Path(os.path.expanduser("~/.local/share/hollow-journal"))
DB_PATH = DB_DIR / "journal.db"

DDL = """
CREATE TABLE IF NOT EXISTS entries (
  id          TEXT PRIMARY KEY,
  date        TEXT NOT NULL,
  timestamp   TEXT NOT NULL,
  entry_type  TEXT NOT NULL,
  score       INTEGER,
  hours       REAL,
  meal_type   TEXT,
  tags        TEXT DEFAULT '[]',
  notes       TEXT,
  entities    TEXT DEFAULT '{}',
  sentiment   REAL,
  created_at  TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_entries_date ON entries(date);
CREATE INDEX IF NOT EXISTS idx_entries_type_date ON entries(entry_type, date);
CREATE INDEX IF NOT EXISTS idx_entries_score ON entries(entry_type, score);
"""


def get_conn() -> sqlite3.Connection:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(DDL)


def insert_entry(entry: dict) -> None:
    sql = """
    INSERT INTO entries
      (id, date, timestamp, entry_type, score, hours, meal_type, tags, notes, entities, sentiment)
    VALUES
      (:id, :date, :timestamp, :entry_type, :score, :hours, :meal_type, :tags, :notes, :entities, :sentiment)
    """
    with get_conn() as conn:
        conn.execute(sql, entry)
        conn.commit()


def get_entries_by_date(date: str) -> list[dict]:
    sql = "SELECT * FROM entries WHERE date = ? ORDER BY timestamp"
    with get_conn() as conn:
        rows = conn.execute(sql, (date,)).fetchall()
    return [dict(r) for r in rows]
