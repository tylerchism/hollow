"""Alpaca-based equity bar fetcher — targets paper trading by default."""

import logging
import os
from datetime import datetime, timezone

log = logging.getLogger("trader.fetchers.equities")

try:
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    _ALPACA_AVAILABLE = True
except ImportError:
    _ALPACA_AVAILABLE = False


def _require_alpaca() -> None:
    if not _ALPACA_AVAILABLE:
        raise ImportError(
            "alpaca-py is not installed. Install it via: "
            "pip install -r bots/trader/requirements.txt"
        )


def _parse_timeframe(tf_str: str):
    """Convert config timeframe string (e.g. '1Min') to alpaca TimeFrame object."""
    _require_alpaca()
    mapping = {
        "1Min": TimeFrame(1, TimeFrameUnit.Minute),
        "5Min": TimeFrame(5, TimeFrameUnit.Minute),
        "15Min": TimeFrame(15, TimeFrameUnit.Minute),
        "1Hour": TimeFrame(1, TimeFrameUnit.Hour),
        "1Day": TimeFrame(1, TimeFrameUnit.Day),
    }
    if tf_str not in mapping:
        raise ValueError(
            f"Unknown timeframe '{tf_str}'. Known: {list(mapping.keys())}"
        )
    return mapping[tf_str]


class EquityFetcher:
    """Fetches OHLCV bar data from Alpaca Markets."""

    def __init__(self, config: dict) -> None:
        self.config = config
        self.eq_cfg = config.get("equities", {})
        self.paper = self.eq_cfg.get("paper", True)
        self.api_key_env = self.eq_cfg.get("api_key_env", "ALPACA_PAPER_API_KEY")
        self.api_secret_env = self.eq_cfg.get("api_secret_env", "ALPACA_PAPER_API_SECRET")
        self.base_url = self.eq_cfg.get("base_url", "https://paper-api.alpaca.markets")
        self._client = None

    def connect(self) -> None:
        """Create Alpaca StockHistoricalDataClient."""
        _require_alpaca()
        api_key = os.environ.get(self.api_key_env, "")
        api_secret = os.environ.get(self.api_secret_env, "")
        self._client = StockHistoricalDataClient(
            api_key=api_key,
            secret_key=api_secret,
        )
        log.info("EquityFetcher connected (paper=%s, base_url=%s)", self.paper, self.base_url)

    def fetch_bars(self, symbol: str, timeframe: str, limit: int = 500) -> list[list]:
        """
        Fetch OHLCV bars for an equity symbol.

        Returns a list of [timestamp_ms, open, high, low, close, volume] rows,
        matching the same format as CryptoFetcher.fetch_ohlcv for unified storage.
        """
        _require_alpaca()
        if self._client is None:
            self.connect()

        tf = _parse_timeframe(timeframe)
        request = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=tf,
            limit=limit,
        )
        try:
            bars_response = self._client.get_stock_bars(request)
            bars = bars_response[symbol] if symbol in bars_response else []
            rows = []
            for bar in bars:
                ts_ms = int(bar.timestamp.timestamp() * 1000)
                rows.append([ts_ms, bar.open, bar.high, bar.low, bar.close, bar.volume])
            log.debug("Fetched %d bars for %s/%s", len(rows), symbol, timeframe)
            return rows
        except Exception as exc:
            log.error("fetch_bars(%s, %s) failed: %s", symbol, timeframe, exc)
            raise

    def fetch_quote(self, symbol: str) -> dict:
        """
        Fetch the latest quote for an equity symbol.

        Returns a dict with 'symbol', 'ask_price', 'bid_price', 'last_price'.
        """
        _require_alpaca()
        if self._client is None:
            self.connect()

        try:
            from alpaca.data.requests import StockLatestQuoteRequest
            request = StockLatestQuoteRequest(symbol_or_symbols=symbol)
            quotes = self._client.get_stock_latest_quote(request)
            quote = quotes.get(symbol)
            if quote is None:
                raise ValueError(f"No quote returned for {symbol}")
            result = {
                "symbol": symbol,
                "ask_price": quote.ask_price,
                "bid_price": quote.bid_price,
                "last_price": (quote.ask_price + quote.bid_price) / 2
                if quote.ask_price and quote.bid_price else None,
                "timestamp": quote.timestamp.isoformat() if quote.timestamp else None,
            }
            log.debug("Fetched quote for %s: %s", symbol, result)
            return result
        except Exception as exc:
            log.error("fetch_quote(%s) failed: %s", symbol, exc)
            raise
