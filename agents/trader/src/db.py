"""
SQLite setup and schema for hollow-trader.
Uses idempotent migration — safe to call on every startup.
"""
import sqlite3
from pathlib import Path


def get_db(db_path: str) -> sqlite3.Connection:
    path = Path(db_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    # Enable WAL mode for better concurrent read performance
    conn.execute("PRAGMA journal_mode=WAL")
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS ohlcv (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_class TEXT NOT NULL,
            symbol TEXT NOT NULL,
            exchange TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume REAL,
            timeframe TEXT,
            fetched_at TEXT DEFAULT (datetime('now')),
            UNIQUE(symbol, exchange, timestamp, timeframe)
        );

        CREATE TABLE IF NOT EXISTS portfolio_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_at TEXT NOT NULL,
            mode TEXT NOT NULL,
            total_value_usd REAL,
            cash_usd REAL,
            positions TEXT DEFAULT '{}',
            source TEXT
        );

        CREATE TABLE IF NOT EXISTS trade_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            logged_at TEXT NOT NULL,
            mode TEXT NOT NULL,
            asset_class TEXT NOT NULL,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            quantity REAL,
            price REAL,
            order_id TEXT,
            status TEXT,
            pdt_count INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS pdt_tracker (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            symbol TEXT NOT NULL,
            day_trade_count INTEGER DEFAULT 0,
            account_equity_usd REAL,
            pdt_restricted INTEGER DEFAULT 0,
            UNIQUE(date, symbol)
        );
    """)
    conn.commit()


def insert_ohlcv(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """
    Insert OHLCV rows, ignoring duplicates (UNIQUE constraint on
    symbol, exchange, timestamp, timeframe).
    Returns number of rows actually inserted.
    """
    sql = """
        INSERT OR IGNORE INTO ohlcv
            (asset_class, symbol, exchange, timestamp,
             open, high, low, close, volume, timeframe)
        VALUES
            (:asset_class, :symbol, :exchange, :timestamp,
             :open, :high, :low, :close, :volume, :timeframe)
    """
    cur = conn.executemany(sql, rows)
    conn.commit()
    return cur.rowcount


def insert_portfolio_snapshot(conn: sqlite3.Connection, snapshot: dict) -> int:
    import json
    sql = """
        INSERT INTO portfolio_snapshots
            (snapshot_at, mode, total_value_usd, cash_usd, positions, source)
        VALUES
            (:snapshot_at, :mode, :total_value_usd, :cash_usd, :positions, :source)
    """
    row = dict(snapshot)
    if isinstance(row.get("positions"), dict):
        row["positions"] = json.dumps(row["positions"])
    cur = conn.execute(sql, row)
    conn.commit()
    return cur.lastrowid


def log_trade(conn: sqlite3.Connection, trade: dict) -> int:
    """
    Append-only trade log. Must be called BEFORE order submission.
    """
    sql = """
        INSERT INTO trade_log
            (logged_at, mode, asset_class, symbol, side,
             quantity, price, order_id, status, pdt_count)
        VALUES
            (:logged_at, :mode, :asset_class, :symbol, :side,
             :quantity, :price, :order_id, :status, :pdt_count)
    """
    cur = conn.execute(sql, trade)
    conn.commit()
    return cur.lastrowid


def upsert_pdt(conn: sqlite3.Connection, record: dict) -> None:
    sql = """
        INSERT INTO pdt_tracker (date, symbol, day_trade_count, account_equity_usd, pdt_restricted)
        VALUES (:date, :symbol, :day_trade_count, :account_equity_usd, :pdt_restricted)
        ON CONFLICT(date, symbol) DO UPDATE SET
            day_trade_count = excluded.day_trade_count,
            account_equity_usd = excluded.account_equity_usd,
            pdt_restricted = excluded.pdt_restricted
    """
    conn.execute(sql, record)
    conn.commit()
