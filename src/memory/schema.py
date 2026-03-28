"""SQLite schema for memory system."""

import sqlite3
from pathlib import Path

DDL = """
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    mtime REAL NOT NULL,
    indexed_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    text TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    embedding BLOB,
    person TEXT,
    platform TEXT,
    source_type TEXT,
    source_title TEXT,
    source_url TEXT,
    date TEXT
);

CREATE INDEX IF NOT EXISTS idx_chunks_file_path ON chunks(file_path);
CREATE INDEX IF NOT EXISTS idx_chunks_content_hash ON chunks(content_hash);
CREATE INDEX IF NOT EXISTS idx_chunks_person ON chunks(person);
CREATE INDEX IF NOT EXISTS idx_chunks_source_type ON chunks(source_type);
CREATE INDEX IF NOT EXISTS idx_chunks_date ON chunks(date);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text,
    content='chunks',
    content_rowid='id'
);

-- Triggers to keep FTS in sync
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;

CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;

CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;

CREATE TABLE IF NOT EXISTS embedding_cache (
    content_hash TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    embedding BLOB NOT NULL
);
"""


_METADATA_MIGRATIONS = [
    "ALTER TABLE chunks ADD COLUMN person TEXT",
    "ALTER TABLE chunks ADD COLUMN platform TEXT",
    "ALTER TABLE chunks ADD COLUMN source_type TEXT",
    "ALTER TABLE chunks ADD COLUMN source_title TEXT",
    "ALTER TABLE chunks ADD COLUMN source_url TEXT",
    "ALTER TABLE chunks ADD COLUMN date TEXT",
]


def _run_migrations(conn: sqlite3.Connection) -> None:
    """Apply WAL-safe ADD COLUMN migrations for existing databases.

    This is a no-op on fresh databases (chunks table doesn't exist yet).
    """
    # Check if the chunks table exists at all; skip on fresh DBs.
    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chunks'"
    ).fetchone()
    if not table_exists:
        return

    existing = {
        row[1]
        for row in conn.execute("PRAGMA table_info(chunks)").fetchall()
    }
    changed = False
    for stmt in _METADATA_MIGRATIONS:
        # Extract column name from "ALTER TABLE chunks ADD COLUMN <name> <type>"
        col_name = stmt.split()[-2]
        if col_name not in existing:
            conn.execute(stmt)
            changed = True
    if changed:
        conn.commit()


def ensure_schema(db_path: Path) -> sqlite3.Connection:
    """Create database with WAL mode and run DDL. Returns connection.

    Order of operations:
    1. Run ADD COLUMN migrations so existing DBs have the metadata columns
       before the DDL tries to create indexes on those columns.
    2. Run the full DDL (all CREATE IF NOT EXISTS statements are idempotent).
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _run_migrations(conn)
    conn.executescript(DDL)
    conn.row_factory = sqlite3.Row
    return conn
