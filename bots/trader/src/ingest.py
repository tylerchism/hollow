"""Ingestion loop — fetches crypto + equity OHLCV and writes to DB."""

import logging
import time

from .db import get_conn, init_db
from .fetchers.crypto import CryptoFetcher
from .fetchers.equities import EquityFetcher
from .storage.ohlcv import store_ohlcv
from .portfolio import PortfolioTracker

log = logging.getLogger("trader.ingest")


def _write_heartbeat(conn, source: str, symbol: str, status: str, message: str = "") -> None:
    sql = """
        INSERT INTO ingest_heartbeat (source, symbol, status, message)
        VALUES (?, ?, ?, ?)
    """
    with conn:
        conn.execute(sql, (source, symbol, status, message or ""))


class IngestLoop:
    """Runs the ingestion cycle for crypto and equity data."""

    def __init__(self, config: dict) -> None:
        self.config = config
        self.crypto_cfg = config.get("crypto", {})
        self.eq_cfg = config.get("equities", {})
        self.ingest_cfg = config.get("ingestion", {})
        self.interval = self.ingest_cfg.get("interval_seconds", 60)
        self.retry_attempts = self.ingest_cfg.get("retry_attempts", 3)
        self.retry_delay = self.ingest_cfg.get("retry_delay_seconds", 5)

        self._crypto_fetcher = CryptoFetcher(config)
        self._equity_fetcher = EquityFetcher(config)
        self._portfolio = PortfolioTracker(config)

        # Determine exchange label for DB rows
        exchange_id = self.crypto_cfg.get("exchange", "binance")
        testnet = self.crypto_cfg.get("testnet", True)
        self.crypto_exchange_label = f"{exchange_id}_testnet" if testnet else exchange_id

        eq_paper = self.eq_cfg.get("paper", True)
        self.eq_exchange_label = "alpaca_paper" if eq_paper else "alpaca_live"

    def _fetch_with_retry(self, fn, *args, label=""):
        """Call fn(*args), retrying up to retry_attempts times on failure."""
        last_exc = None
        for attempt in range(1, self.retry_attempts + 1):
            try:
                return fn(*args)
            except Exception as exc:
                last_exc = exc
                log.warning(
                    "%s attempt %d/%d failed: %s",
                    label, attempt, self.retry_attempts, exc,
                )
                if attempt < self.retry_attempts:
                    time.sleep(self.retry_delay)
        raise last_exc

    def run_once(self) -> None:
        """Run a single ingestion cycle: fetch all symbols, store to DB, write heartbeat."""
        conn = get_conn(self.config)
        try:
            # --- Crypto ---
            crypto_symbols = self.crypto_cfg.get("symbols", [])
            crypto_timeframes = self.crypto_cfg.get("timeframes", ["1m"])
            ohlcv_limit = self.crypto_cfg.get("ohlcv_limit", 500)

            for symbol in crypto_symbols:
                for tf in crypto_timeframes:
                    label = f"crypto/{symbol}/{tf}"
                    try:
                        rows = self._fetch_with_retry(
                            self._crypto_fetcher.fetch_ohlcv,
                            symbol, tf, ohlcv_limit,
                            label=label,
                        )
                        stored = store_ohlcv(
                            conn, rows,
                            asset_class="crypto",
                            exchange=self.crypto_exchange_label,
                            symbol=symbol,
                            timeframe=tf,
                        )
                        _write_heartbeat(
                            conn, self.crypto_exchange_label, symbol,
                            "ok", f"stored {stored} rows ({tf})",
                        )
                        log.info("Ingested %d crypto rows: %s/%s", stored, symbol, tf)
                    except Exception as exc:
                        _write_heartbeat(
                            conn, self.crypto_exchange_label, symbol,
                            "error", str(exc),
                        )
                        log.error("Crypto ingest failed for %s/%s: %s", symbol, tf, exc)

            # --- Equities ---
            eq_symbols = self.eq_cfg.get("symbols", [])
            eq_timeframe = self.eq_cfg.get("timeframe", "1Min")

            for symbol in eq_symbols:
                label = f"equity/{symbol}/{eq_timeframe}"
                try:
                    rows = self._fetch_with_retry(
                        self._equity_fetcher.fetch_bars,
                        symbol, eq_timeframe, 500,
                        label=label,
                    )
                    stored = store_ohlcv(
                        conn, rows,
                        asset_class="equity",
                        exchange=self.eq_exchange_label,
                        symbol=symbol,
                        timeframe=eq_timeframe,
                    )
                    _write_heartbeat(
                        conn, self.eq_exchange_label, symbol,
                        "ok", f"stored {stored} bars",
                    )
                    log.info("Ingested %d equity bars: %s", stored, symbol)
                except Exception as exc:
                    _write_heartbeat(
                        conn, self.eq_exchange_label, symbol,
                        "error", str(exc),
                    )
                    log.error("Equity ingest failed for %s: %s", symbol, exc)

            # --- Portfolio snapshot ---
            try:
                self._portfolio.snapshot(conn)
            except Exception as exc:
                log.error("Portfolio snapshot failed: %s", exc)

        finally:
            conn.close()

    def run(self) -> None:
        """Run the ingestion loop continuously, sleeping interval_seconds between cycles."""
        log.info(
            "IngestLoop starting — interval=%ds, crypto_symbols=%s, eq_symbols=%s",
            self.interval,
            self.crypto_cfg.get("symbols", []),
            self.eq_cfg.get("symbols", []),
        )
        while True:
            start = time.monotonic()
            try:
                self.run_once()
            except Exception as exc:
                log.exception("Unhandled error in run_once: %s", exc)
            elapsed = time.monotonic() - start
            sleep_for = max(0, self.interval - elapsed)
            log.debug("Cycle complete in %.1fs, sleeping %.1fs", elapsed, sleep_for)
            time.sleep(sleep_for)
