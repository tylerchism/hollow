#!/usr/bin/env python3
"""
hollow-trader V0 — entry point.

Usage:
    python main.py --config configs/paper.json          # paper trading (safe default)
    python main.py --config configs/paper.json --once   # single ingestion pass, then exit
    python main.py --config configs/live.json --mode=live  # LIVE — requires explicit flag

The --mode=live flag is a hard guard: the bot refuses to run with live.json
unless you pass --mode=live explicitly. This makes it impossible to
accidentally trade live capital when you meant to run paper.
"""
import argparse
import json
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("trader.main")

# Resolve src/ relative to this file, not cwd
_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))

from src import db as trader_db  # noqa: E402
from src import ingestion_loop  # noqa: E402


def load_config(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def _guard_live_mode(config: dict, cli_mode_flag: str | None) -> None:
    """
    Refuse to run in live mode unless --mode=live was explicitly passed.
    This is the hard safety gate between paper and live capital.
    """
    config_mode = config.get("mode", "paper")
    if config_mode == "live":
        if cli_mode_flag != "live":
            logger.error(
                "SAFETY ABORT: config file has mode='live' but --mode=live "
                "was not passed on the command line. "
                "Re-run with --mode=live if you truly intend to trade live capital."
            )
            sys.exit(1)
        logger.warning(
            "*** LIVE TRADING MODE ACTIVE — real capital at risk ***"
        )
    elif cli_mode_flag == "live":
        logger.error(
            "SAFETY ABORT: --mode=live passed but config file has mode='%s'. "
            "Use configs/live.json with --mode=live.",
            config_mode,
        )
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="hollow-trader V0 data layer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--config",
        default=str(_HERE / "configs" / "paper.json"),
        help="Path to config file (default: configs/paper.json)",
    )
    parser.add_argument(
        "--mode",
        choices=["paper", "live"],
        default=None,
        help="Must be 'live' to activate live trading config. "
             "Paper is the default and requires no flag.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single ingestion pass and exit (useful for cron / testing)",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Override db_path from config (useful for testing)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    logging.getLogger().setLevel(args.log_level)

    config = load_config(args.config)
    _guard_live_mode(config, args.mode)

    db_path = args.db or config["storage"]["db_path"]
    conn = trader_db.get_db(db_path)
    logger.info("Database ready at %s", Path(db_path).expanduser())

    if args.once:
        summary = ingestion_loop.run_once(config, conn)
        logger.info(
            "Done. crypto=%d equity=%d errors=%d",
            summary["crypto_rows"],
            summary["equity_rows"],
            len(summary["errors"]),
        )
        if summary["errors"]:
            for err in summary["errors"]:
                logger.warning("  %s", err)
    else:
        ingestion_loop.run_loop(config, conn)


if __name__ == "__main__":
    main()
