"""
Alpaca paper trading equity price feed.
Fetches historical OHLCV bars and returns rows ready for db.insert_ohlcv().
"""
from datetime import datetime, timezone, timedelta


def get_client(config: dict):
    """
    Build and return an Alpaca StockHistoricalDataClient.
    Paper and live both use the same historical data client;
    the paper/live distinction applies to the trading client (orders), not data.
    """
    from alpaca.data.historical import StockHistoricalDataClient  # noqa: E402

    return StockHistoricalDataClient(
        api_key=config["equities"]["api_key"],
        secret_key=config["equities"]["api_secret"],
    )


def fetch_bars(
    client,
    symbol: str,
    timeframe_str: str = "1Day",
    lookback_days: int = 30,
) -> list:
    """
    Fetch historical bars for a single equity symbol.
    timeframe_str: '1Min' | '5Min' | '15Min' | '1Hour' | '1Day'
    Returns a list of alpaca Bar objects.
    """
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    _tf_map = {
        "1Min": TimeFrame(1, TimeFrameUnit.Minute),
        "5Min": TimeFrame(5, TimeFrameUnit.Minute),
        "15Min": TimeFrame(15, TimeFrameUnit.Minute),
        "1Hour": TimeFrame(1, TimeFrameUnit.Hour),
        "1Day": TimeFrame(1, TimeFrameUnit.Day),
    }
    tf = _tf_map.get(timeframe_str, TimeFrame(1, TimeFrameUnit.Day))

    end = datetime.now(tz=timezone.utc)
    start = end - timedelta(days=lookback_days)

    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=tf,
        start=start,
        end=end,
    )
    bars_response = client.get_stock_bars(req)
    # bars_response[symbol] returns a list of Bar objects
    return bars_response[symbol] if symbol in bars_response else []


def bars_to_rows(
    bars: list,
    symbol: str,
    exchange_name: str = "alpaca",
    timeframe: str = "1Day",
) -> list[dict]:
    """
    Convert Alpaca Bar objects to dicts ready for db.insert_ohlcv().
    """
    rows = []
    for bar in bars:
        ts = bar.timestamp.isoformat() if hasattr(bar.timestamp, "isoformat") else str(bar.timestamp)
        rows.append({
            "asset_class": "equity",
            "symbol": symbol,
            "exchange": exchange_name,
            "timestamp": ts,
            "open": float(bar.open),
            "high": float(bar.high),
            "low": float(bar.low),
            "close": float(bar.close),
            "volume": float(bar.volume),
            "timeframe": timeframe,
        })
    return rows


def fetch_and_convert(
    client,
    symbol: str,
    timeframe_str: str = "1Day",
    lookback_days: int = 30,
) -> list[dict]:
    """Convenience wrapper: fetch + convert in one call."""
    bars = fetch_bars(client, symbol, timeframe_str, lookback_days)
    return bars_to_rows(bars, symbol, timeframe=timeframe_str)
