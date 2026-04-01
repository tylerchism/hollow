# Trader2 Paper/Live Boundary Review

**Date:** 2026-03-31
**Reviewer:** Tarn (adversarial pass)
**Verdict:** FLAG — 3 issues must be resolved before live wiring

---

## What's In Place (Good)

1. `paper_mode` derived from config: `self._mode = config.get("mode", "paper")` — single source of truth
2. KalshiExecutor picks DEMO vs LIVE API URL based on `paper_mode`: separate base URLs
3. Kill switch implemented via `kill_switch.path_env`
4. Polymarket disabled by default (`"enabled": false`) — no orders possible in Phase 1
5. Risk limits in config (max_daily_loss, max_single_trade, etc.)
6. Data feed uses live prices even in paper mode (intentional and correct)

---

## FLAG: Issues to Resolve Before Live Wiring

### 1. No TRADER2_LIVE_CONFIRMED env var guard (CRITICAL)
The V1 bot (bots/trader) requires `TRADER_LIVE_CONFIRMED=true` env var as a second factor before live mode runs. Trader2 has NO such guard — `"mode": "live"` in the config file is sufficient to go live. One JSON field change away from real orders. Add the env var guard before live wiring.

**Fix:** Copy the V1 bot pattern: if `config["mode"] == "live"` and `os.environ.get("TRADER2_LIVE_CONFIRMED") != "true"`, abort with a clear error.

### 2. Single API key env var for demo AND live (HIGH)
`KALSHI_API_KEY` is used regardless of mode. Kalshi has separate demo credentials from live credentials. If someone configures with a live API key for live trading, paper mode would attempt to use that same key against the demo API (likely failing, but the key is exposed). More importantly, if paper_mode is True but KALSHI_API_KEY is a live key, there's no validation that the right key is in use.

**Fix:** Use `KALSHI_DEMO_API_KEY` in paper mode and `KALSHI_LIVE_API_KEY` in live mode. KalshiExecutor selects env var name based on `paper_mode`.

### 3. Kill switch behavior when env var not set is unknown (MEDIUM)
`"path_env": "TRADER2_KILL_SWITCH_PATH"` — if this env var isn't set, the kill switch path resolves to what? If it silently disables the kill switch in live mode, that's unacceptable. Needs verification: undefined env var must either use a safe default path or abort.

**Fix:** Verify the kill switch implementation handles missing env var safely. If undefined, default to `~/.local/share/hollow-trader2/KILL` (same as V1 pattern).

---

## Secondary: Live Config Template Improvements

When a `live.json` is eventually created for Trader2, add `"_WARNING"` and `"_GUARD"` JSON comments identical to the V1 bot's `live.json.example`:
```json
{
  "_WARNING": "THIS IS LIVE TRADING. Set TRADER2_LIVE_CONFIRMED=true env var to use.",
  "_GUARD": "The bot will refuse to run in live mode unless env var TRADER2_LIVE_CONFIRMED=true is set.",
  "mode": "live",
  ...
}
```

---

## Summary

Live wiring is BLOCKED until issues 1-3 are resolved. Issue 1 (no live guard) is the critical one — it's the only thing standing between a config file change and real orders. Issues 2-3 are lower risk but should be fixed in the same PR.
