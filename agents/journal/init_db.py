"""Initialize the journal SQLite database at data/journal/journal.db."""

import sqlite3
from pathlib import Path

HOLLOW_ROOT = Path(__file__).parent.parent.parent
DB_PATH = HOLLOW_ROOT / "data" / "journal" / "journal.db"

DDL = """
CREATE TABLE IF NOT EXISTS entries (
    id              TEXT PRIMARY KEY,
    timestamp       TEXT NOT NULL,
    date            TEXT NOT NULL,
    entry_type      TEXT NOT NULL,
    raw_text        TEXT NOT NULL,
    structured_data TEXT DEFAULT '{}',
    tags            TEXT DEFAULT '[]',
    created_at      TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_entries_date       ON entries(date);
CREATE INDEX IF NOT EXISTS idx_entries_type_date  ON entries(entry_type, date);
CREATE INDEX IF NOT EXISTS idx_entries_timestamp  ON entries(timestamp);
"""


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.executescript(DDL)
    conn.commit()
    conn.close()
    print(f"Journal DB initialized at {DB_PATH}")


if __name__ == "__main__":
    init_db()
