"""Database connection and schema management for hollow-trader."""

import logging
import os
import sqlite3
from pathlib import Path

log = logging.getLogger("trader.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS ohlcv (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT NOT NULL,
    asset_class TEXT NOT NULL,
    exchange    TEXT NOT NULL,
    timeframe   TEXT NOT NULL,
    ts          TEXT NOT NULL,
    open        REAL NOT NULL,
    high        REAL NOT NULL,
    low         REAL NOT NULL,
    close       REAL NOT NULL,
    volume      REAL NOT NULL,
    ingested_at TEXT DEFAULT (datetime('now')),
    UNIQUE(symbol, exchange, timeframe, ts)
);

CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL DEFAULT (datetime('now')),
    mode            TEXT NOT NULL,
    total_value_usd REAL,
    cash_usd        REAL,
    positions       TEXT DEFAULT '{}',
    pnl_usd         REAL,
    pnl_pct         REAL
);

CREATE TABLE IF NOT EXISTS trade_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    logged_at   TEXT NOT NULL DEFAULT (datetime('now')),
    mode        TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    asset_class TEXT NOT NULL,
    side        TEXT NOT NULL,
    qty         REAL NOT NULL,
    price       REAL,
    order_type  TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    exchange    TEXT NOT NULL,
    strategy_id TEXT,
    notes       TEXT
);

CREATE TABLE IF NOT EXISTS pdt_tracker (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date            TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    round_trip_count INTEGER DEFAULT 0,
    account_value   REAL,
    pdt_restricted  INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS ingest_heartbeat (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL DEFAULT (datetime('now')),
    source      TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    status      TEXT NOT NULL,
    message     TEXT
);
"""


def get_db_path(config: dict) -> Path:
    """Resolve and expand the database path from config."""
    raw = config.get("storage", {}).get("db_path", "~/.local/share/hollow-trader/trader.db")
    expanded = os.path.expanduser(raw)
    return Path(expanded)


def init_db(config: dict) -> None:
    """Create all tables (idempotent). Creates parent directories as needed."""
    db_path = get_db_path(config)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(SCHEMA)
        conn.commit()
        log.info("DB initialized at %s", db_path)
    finally:
        conn.close()


def get_conn(config: dict) -> sqlite3.Connection:
    """Return an open sqlite3 connection. Caller is responsible for closing."""
    db_path = get_db_path(config)
    if not db_path.exists():
        raise FileNotFoundError(
            f"Database not found at {db_path}. Run init_db(config) first."
        )
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn
