"""Hollow Trader — V0 entry point.

Usage:
    python main.py                         # paper trading (default)
    python main.py --config config/paper.json
    python main.py --config config/live.json  # requires TRADER_LIVE_CONFIRMED=true

LIVE MODE GUARD:
    If config mode == 'live', the env var TRADER_LIVE_CONFIRMED must be set to
    the exact string 'true'. The bot will refuse to start otherwise.
"""

import argparse
import json
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stdout,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("trader.main")

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

# When running from anywhere, resolve config paths relative to this file's dir.
BOT_DIR = Path(__file__).parent
DEFAULT_CONFIG = BOT_DIR / "config" / "paper.json"


def load_config(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Live mode guard
# ---------------------------------------------------------------------------

def check_live_guard(config: dict) -> None:
    """Abort with a clear error if live mode is requested without confirmation."""
    mode = config.get("mode", "paper")
    if mode != "live":
        return

    confirmed = os.environ.get("TRADER_LIVE_CONFIRMED", "").strip().lower()
    if confirmed != "true":
        print(
            "\n"
            "ERROR: Config mode is 'live' but TRADER_LIVE_CONFIRMED env var is not set.\n"
            "\n"
            "Live trading with real capital requires explicit confirmation.\n"
            "To proceed, set:\n"
            "\n"
            "    export TRADER_LIVE_CONFIRMED=true\n"
            "\n"
            "WARNING: Live mode will execute real orders with real money.\n"
            "Review config/live.json carefully before enabling.\n",
            file=sys.stderr,
        )
        sys.exit(1)

    log.warning(
        "LIVE MODE ACTIVE — real orders will be placed. "
        "TRADER_LIVE_CONFIRMED=true acknowledged."
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Hollow Trader — V0 data layer")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="Path to config JSON (default: config/paper.json)",
    )
    args = parser.parse_args()

    config_path = args.config
    if not config_path.is_absolute():
        config_path = BOT_DIR / config_path

    if not config_path.exists():
        print(f"ERROR: Config file not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    config = load_config(config_path)
    log.info("Loaded config from %s (mode=%s)", config_path, config.get("mode", "paper"))

    # --- Live mode guard (non-negotiable) ---
    check_live_guard(config)

    # --- Import src from the bot's own src/ directory ---
    src_dir = str(BOT_DIR / "src")
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)

    # Delay imports until after path setup, so src/ is resolvable regardless of cwd
    from src.db import init_db
    from src.ingest import IngestLoop

    # --- Initialize database ---
    init_db(config)

    # --- Run ingestion loop ---
    loop = IngestLoop(config)
    try:
        loop.run()
    except KeyboardInterrupt:
        log.info("Shutdown requested (KeyboardInterrupt). Exiting.")
        sys.exit(0)


if __name__ == "__main__":
    main()
