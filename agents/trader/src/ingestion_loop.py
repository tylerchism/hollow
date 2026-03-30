"""
Ingestion loop: periodically fetches OHLCV data from all configured sources
and stores it in the SQLite database.
"""
import time
import logging
from datetime import datetime, timezone

from . import db as trader_db
from . import fetcher_crypto
from . import fetcher_equity
from . import portfolio as portfolio_tracker

logger = logging.getLogger(__name__)


def run_once(config: dict, conn) -> dict:
    """
    Run a single ingestion pass across all configured symbols.
    Returns a summary dict with counts.
    """
    summary = {
        "run_at": datetime.now(tz=timezone.utc).isoformat(),
        "crypto_rows": 0,
        "equity_rows": 0,
        "errors": [],
    }

    # --- Crypto (CCXT / Binance) ---
    crypto_cfg = config.get("crypto", {})
    if crypto_cfg.get("api_key") or crypto_cfg.get("testnet", True):
        try:
            exchange = fetcher_crypto.get_exchange(config)
            symbols = crypto_cfg.get("symbols", [])
            timeframes = crypto_cfg.get("timeframes", ["1h"])
            lookback = config.get("ingestion", {}).get("lookback_days", 30)
            # Approximate bar count from lookback + timeframe
            limit_map = {"1m": lookback * 24 * 60, "5m": lookback * 24 * 12,
                         "15m": lookback * 24 * 4, "1h": lookback * 24,
                         "4h": lookback * 6, "1d": lookback}
            for symbol in symbols:
                for tf in timeframes:
                    limit = min(limit_map.get(tf, lookback * 24), 1000)
                    try:
                        rows = fetcher_crypto.fetch_and_convert(
                            exchange, symbol, tf, limit
                        )
                        inserted = trader_db.insert_ohlcv(conn, rows)
                        summary["crypto_rows"] += inserted
                        logger.info(
                            "crypto %s %s: fetched %d bars, inserted %d",
                            symbol, tf, len(rows), inserted,
                        )
                    except Exception as e:
                        msg = f"crypto {symbol}/{tf}: {e}"
                        logger.error(msg)
                        summary["errors"].append(msg)
        except Exception as e:
            msg = f"crypto exchange init: {e}"
            logger.error(msg)
            summary["errors"].append(msg)

    # --- Equities (Alpaca) ---
    eq_cfg = config.get("equities", {})
    if eq_cfg.get("api_key"):
        try:
            client = fetcher_equity.get_client(config)
            symbols = eq_cfg.get("symbols", [])
            lookback = config.get("ingestion", {}).get("lookback_days", 30)
            for symbol in symbols:
                try:
                    rows = fetcher_equity.fetch_and_convert(
                        client, symbol, "1Day", lookback
                    )
                    inserted = trader_db.insert_ohlcv(conn, rows)
                    summary["equity_rows"] += inserted
                    logger.info(
                        "equity %s: fetched %d bars, inserted %d",
                        symbol, len(rows), inserted,
                    )
                except Exception as e:
                    msg = f"equity {symbol}: {e}"
                    logger.error(msg)
                    summary["errors"].append(msg)
        except Exception as e:
            msg = f"equity client init: {e}"
            logger.error(msg)
            summary["errors"].append(msg)
    else:
        logger.debug("Skipping equity ingestion — no Alpaca API key configured")

    # --- Portfolio snapshot ---
    try:
        snap = portfolio_tracker.get_portfolio_value(config)
        trader_db.insert_portfolio_snapshot(conn, snap)
    except Exception as e:
        msg = f"portfolio snapshot: {e}"
        logger.error(msg)
        summary["errors"].append(msg)

    return summary


def run_loop(config: dict, conn) -> None:
    """
    Continuous ingestion loop. Runs run_once() then sleeps for
    config['ingestion']['interval_seconds'].
    Ctrl-C to stop.
    """
    interval = config.get("ingestion", {}).get("interval_seconds", 300)
    logger.info(
        "Starting ingestion loop (mode=%s, interval=%ds)",
        config["mode"], interval,
    )

    while True:
        try:
            summary = run_once(config, conn)
            logger.info(
                "Ingestion pass complete: crypto=%d equity=%d errors=%d",
                summary["crypto_rows"],
                summary["equity_rows"],
                len(summary["errors"]),
            )
            if summary["errors"]:
                for err in summary["errors"]:
                    logger.warning("  error: %s", err)
        except Exception as e:
            logger.error("Unexpected error in ingestion loop: %s", e)

        logger.debug("Sleeping %ds until next pass...", interval)
        time.sleep(interval)
