# hollow-trader V0 — Data Layer

Standalone data ingestion layer for the AI trading bot. Not integrated into the Hollow agent system.

## Setup

Create a dedicated Python venv (do NOT use the hollow venv):

```bash
python3 -m venv ~/.local/share/hollow-trader/venv
source ~/.local/share/hollow-trader/venv/bin/activate
pip install --require-hashes -r requirements.txt
```

## Configuration

Copy and fill in your API keys:

```bash
# Paper trading (safe — Binance testnet + Alpaca paper)
cp configs/paper.json configs/paper.local.json
# edit configs/paper.local.json — add api_key / api_secret
```

Paper mode requires no special flags. Live mode requires an explicit `--mode=live` flag to prevent accidental live execution.

## Running

```bash
# Single ingestion pass (good for cron / testing)
python main.py --config configs/paper.local.json --once

# Continuous loop (polls every 300s by default)
python main.py --config configs/paper.local.json

# Override DB path for testing
python main.py --config configs/paper.json --once --db /tmp/test-trader.db
```

## Directory layout

```
agents/trader/
├── configs/
│   ├── paper.json          # paper trading — safe defaults, testnet=true
│   └── live.json           # LIVE capital — requires --mode=live CLI flag
├── src/
│   ├── db.py               # SQLite schema + helpers (idempotent migrations)
│   ├── fetcher_crypto.py   # CCXT/Binance OHLCV fetcher
│   ├── fetcher_equity.py   # Alpaca historical bar fetcher
│   ├── ingestion_loop.py   # fetch + store loop
│   └── portfolio.py        # portfolio snapshot stub (V0)
├── main.py                 # entry point
└── requirements.txt        # pinned versions + sha256 hashes
```

## Schema

Four tables in `~/.local/share/hollow-trader/trader.db`:

- `ohlcv` — price bars for both crypto and equities
- `portfolio_snapshots` — periodic account value snapshots
- `trade_log` — append-only trade record (written before order submission)
- `pdt_tracker` — day-trade counter for PDT rule awareness

## PDT rule awareness

The `pdt_tracker` table tracks day trades per symbol per date. The config `pdt.max_day_trades_per_5_days` defaults to 3 (the regulatory limit for accounts under $25k). Strategy logic in V1 must check `pdt_restricted` before placing intraday trades.

## Safety guards

- `configs/live.json` has `"mode": "live"` and `"live_capital": true`
- `main.py` refuses to start if config mode is `live` and `--mode=live` was not passed
- All trades are logged to `trades.jsonl` (append-only) before order submission, not after
