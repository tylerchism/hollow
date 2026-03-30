"""OHLCV storage — upsert and query from the ohlcv table."""

import logging
import sqlite3
from datetime import datetime, timezone

log = logging.getLogger("trader.storage.ohlcv")


def store_ohlcv(
    conn: sqlite3.Connection,
    rows: list[list],
    asset_class: str,
    exchange: str,
    symbol: str,
    timeframe: str,
) -> int:
    """
    Upsert OHLCV rows into the ohlcv table.

    Args:
        conn:       Open sqlite3 connection.
        rows:       List of [timestamp_ms, open, high, low, close, volume] rows.
        asset_class: 'crypto' or 'equity'.
        exchange:   Exchange identifier (e.g. 'binance_testnet', 'alpaca_paper').
        symbol:     Ticker symbol (e.g. 'BTC/USDT', 'AAPL').
        timeframe:  Candle timeframe (e.g. '1m', '1Min').

    Returns:
        Number of rows inserted or replaced.
    """
    if not rows:
        return 0

    sql = """
        INSERT OR REPLACE INTO ohlcv
            (symbol, asset_class, exchange, timeframe, ts, open, high, low, close, volume)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    count = 0
    with conn:
        for row in rows:
            ts_ms, open_, high, low, close, volume = row[:6]
            # Convert epoch ms to ISO8601
            ts_iso = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()
            conn.execute(sql, (
                symbol, asset_class, exchange, timeframe,
                ts_iso, open_, high, low, close, volume,
            ))
            count += 1
    log.debug("Stored %d OHLCV rows for %s/%s/%s", count, symbol, exchange, timeframe)
    return count


def query_ohlcv(
    conn: sqlite3.Connection,
    symbol: str,
    timeframe: str,
    limit: int = 500,
    exchange: str | None = None,
) -> list[sqlite3.Row]:
    """
    Query OHLCV rows for a symbol and timeframe, newest first.

    Args:
        conn:      Open sqlite3 connection.
        symbol:    Ticker symbol.
        timeframe: Candle timeframe.
        limit:     Max rows to return.
        exchange:  Optional exchange filter.

    Returns:
        List of sqlite3.Row objects.
    """
    if exchange is not None:
        sql = """
            SELECT * FROM ohlcv
            WHERE symbol = ? AND timeframe = ? AND exchange = ?
            ORDER BY ts DESC
            LIMIT ?
        """
        rows = conn.execute(sql, (symbol, timeframe, exchange, limit)).fetchall()
    else:
        sql = """
            SELECT * FROM ohlcv
            WHERE symbol = ? AND timeframe = ?
            ORDER BY ts DESC
            LIMIT ?
        """
        rows = conn.execute(sql, (symbol, timeframe, limit)).fetchall()

    log.debug(
        "query_ohlcv(%s, %s, exchange=%s) -> %d rows",
        symbol, timeframe, exchange, len(rows),
    )
    return rows
