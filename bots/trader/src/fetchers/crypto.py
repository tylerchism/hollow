"""CCXT-based crypto OHLCV fetcher — targets Binance testnet by default."""

import logging
import os

log = logging.getLogger("trader.fetchers.crypto")

try:
    import ccxt
    _CCXT_AVAILABLE = True
except ImportError:
    _CCXT_AVAILABLE = False


def _require_ccxt() -> None:
    if not _CCXT_AVAILABLE:
        raise ImportError(
            "ccxt is not installed. Install it via: "
            "pip install -r bots/trader/requirements.txt"
        )


class CryptoFetcher:
    """Fetches OHLCV data from a crypto exchange via CCXT."""

    def __init__(self, config: dict) -> None:
        self.config = config
        self.crypto_cfg = config.get("crypto", {})
        self.exchange_id = self.crypto_cfg.get("exchange", "binance")
        self.testnet = self.crypto_cfg.get("testnet", True)
        self.api_key_env = self.crypto_cfg.get("api_key_env", "BINANCE_TESTNET_API_KEY")
        self.api_secret_env = self.crypto_cfg.get("api_secret_env", "BINANCE_TESTNET_API_SECRET")
        self._exchange = None

    def connect(self) -> None:
        """Create CCXT exchange object. Applies testnet flag if requested."""
        _require_ccxt()
        exchange_class = getattr(ccxt, self.exchange_id)
        self._exchange = exchange_class({
            "apiKey": os.environ.get(self.api_key_env, ""),
            "secret": os.environ.get(self.api_secret_env, ""),
            "enableRateLimit": True,
        })
        if self.testnet:
            if hasattr(self._exchange, "set_sandbox_mode"):
                self._exchange.set_sandbox_mode(True)
                log.info("CCXT %s: sandbox/testnet mode enabled", self.exchange_id)
            else:
                log.warning(
                    "CCXT %s: set_sandbox_mode not available; verify testnet URL manually",
                    self.exchange_id,
                )
        log.info("CryptoFetcher connected to %s (testnet=%s)", self.exchange_id, self.testnet)

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 500) -> list[list]:
        """
        Fetch OHLCV candles for a symbol.

        Returns a list of [timestamp_ms, open, high, low, close, volume] rows.
        """
        _require_ccxt()
        if self._exchange is None:
            self.connect()
        try:
            rows = self._exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
            log.debug("Fetched %d OHLCV rows for %s/%s", len(rows), symbol, timeframe)
            return rows
        except Exception as exc:
            log.error("fetch_ohlcv(%s, %s) failed: %s", symbol, timeframe, exc)
            raise

    def fetch_ticker(self, symbol: str) -> dict:
        """
        Fetch current ticker for a symbol.

        Returns the raw CCXT ticker dict (contains 'last', 'bid', 'ask', etc.).
        """
        _require_ccxt()
        if self._exchange is None:
            self.connect()
        try:
            ticker = self._exchange.fetch_ticker(symbol)
            log.debug("Fetched ticker for %s: last=%s", symbol, ticker.get("last"))
            return ticker
        except Exception as exc:
            log.error("fetch_ticker(%s) failed: %s", symbol, exc)
            raise
