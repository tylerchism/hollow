"""
CCXT / Binance testnet OHLCV fetcher.
Returns rows ready for insert_ohlcv().
"""
from datetime import datetime, timezone


def get_exchange(config: dict):
    """
    Build and return a CCXT Binance exchange instance.
    Enables sandbox (testnet) mode when config['crypto']['testnet'] is True.
    """
    import ccxt  # imported here so the module is importable without ccxt installed

    exchange = ccxt.binance({
        "apiKey": config["crypto"]["api_key"],
        "secret": config["crypto"]["api_secret"],
        "enableRateLimit": True,
    })
    if config["crypto"].get("testnet", True):
        exchange.set_sandbox_mode(True)
    return exchange


def fetch_ohlcv(exchange, symbol: str, timeframe: str = "1h", limit: int = 100) -> list:
    """
    Fetch raw OHLCV bars from the exchange.
    Returns list of [timestamp_ms, open, high, low, close, volume].
    """
    return exchange.fetch_ohlcv(symbol, timeframe, limit=limit)


def ohlcv_to_rows(
    raw: list,
    symbol: str,
    exchange_name: str,
    timeframe: str,
) -> list[dict]:
    """
    Convert raw CCXT OHLCV list to dicts ready for db.insert_ohlcv().
    """
    rows = []
    for bar in raw:
        ts_ms, open_, high, low, close, volume = bar
        ts = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()
        rows.append({
            "asset_class": "crypto",
            "symbol": symbol,
            "exchange": exchange_name,
            "timestamp": ts,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "timeframe": timeframe,
        })
    return rows


def fetch_and_convert(
    exchange,
    symbol: str,
    timeframe: str = "1h",
    limit: int = 100,
) -> list[dict]:
    """Convenience wrapper: fetch + convert in one call."""
    raw = fetch_ohlcv(exchange, symbol, timeframe, limit)
    return ohlcv_to_rows(raw, symbol, exchange.id, timeframe)
