"""Trader2 main process — orchestrates all components.

Thread model:
  main        — event loop, orchestrates, handles signals
  ws_thread   — Kalshi WebSocket listener (blocking IO)
  scheduler   — periodic polling: CME/FRED (30s), Polymarket (10s), daily summary

Phase 1: Kalshi + CME data sources.
Phase 2: Polymarket (Polygon) order execution. Enable via config["polymarket"]["enabled"].
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import queue
import signal
import sys
import time
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


def _setup_logging(log_dir: str, debug: bool = False) -> None:
    log_dir_path = Path(log_dir).expanduser()
    log_dir_path.mkdir(parents=True, exist_ok=True)

    level = logging.DEBUG if debug else logging.INFO
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(level)

    # Console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    root.addHandler(ch)

    # Rotating file handler — main log
    fh = logging.handlers.TimedRotatingFileHandler(
        str(log_dir_path / "trader2.log"),
        when="midnight",
        utc=True,
        backupCount=30,
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)

    # Errors-only log
    eh = logging.handlers.RotatingFileHandler(
        str(log_dir_path / "errors.log"),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
    )
    eh.setLevel(logging.ERROR)
    eh.setFormatter(fmt)
    root.addHandler(eh)


def _resolve_db_path(config: dict) -> str:
    storage_cfg = config.get("storage", {})
    env_var = storage_cfg.get("db_path_env", "TRADER2_DB_PATH")
    default = str(Path.home() / ".local" / "share" / "hollow-trader2" / "trader2.db")
    return os.environ.get(env_var, default)


def _resolve_log_dir(config: dict) -> str:
    storage_cfg = config.get("storage", {})
    env_var = storage_cfg.get("log_dir_env", "TRADER2_LOG_DIR")
    default = str(Path.home() / ".local" / "share" / "hollow-trader2" / "logs")
    return os.environ.get(env_var, default)


class Trader2:
    """Main bot process. Owns all components and threads."""

    def __init__(self, config: dict, dry_run: bool = False):
        self._config = config
        self._dry_run = dry_run
        # Normalise mode string to lowercase so "Live", "LIVE", "live" all work.
        self._mode = config.get("mode", "paper").strip().lower()
        self._paper_mode = self._mode != "live"

        log_dir = _resolve_log_dir(config)
        _setup_logging(log_dir, debug=dry_run)
        log.info("trader2: starting in %s mode (dry_run=%s)", self._mode, dry_run)

        # Explicit WARNING-level confirmation of operating mode — easy to spot in logs.
        if self._paper_mode:
            log.warning("PAPER MODE ACTIVE — no real orders will be placed")
        else:
            log.warning("LIVE MODE ACTIVE — REAL MONEY — real orders will be placed")

        # Load event configs
        config_dir = Path(__file__).parent.parent / "config"
        with open(config_dir / "event_map.json") as f:
            event_map_data = json.load(f)
        self._event_map = [
            e for e in event_map_data.get("events", []) if e.get("active")
        ]

        # Initialize components
        from src.data.snapshot_store import SnapshotStore
        from src.data.kalshi import KalshiClient
        from src.data.cme import CMEClient
        from src.data.treasury import TreasuryYieldClient
        from src.data.polymarket import PolymarketClient
        from src.divergence.engine import DivergenceEngine
        from src.event_arb.calendar import EventCalendar
        from src.event_arb.spike_detector import KalshiSpikeDetector
        from src.event_arb.rate_spike_detector import RateSpikeDetector
        from src.event_arb.fast_path import EventArbFastPath
        from src.risk.manager import RiskManager
        from src.execution.kalshi_executor import KalshiExecutor
        from src.execution.paper_executor import PaperExecutor
        from src.execution.polymarket_executor import PolymarketExecutor
        from src.execution.two_leg import TwoLegExecutor
        from src.state.db import StateDB
        from src.notify.discord import DiscordNotifier

        stale_cfg = config.get("data", {}).get("stale_threshold_seconds", {
            "kalshi": 60, "polymarket": 20, "cme": 90
        })
        self._snapshot_store = SnapshotStore(stale_cfg)

        db_path = _resolve_db_path(config)
        self._db = StateDB(db_path, mode=self._mode)

        self._notifier = DiscordNotifier(config)

        trade_log_path = str(
            Path(_resolve_log_dir(config)) / "trades.log"
        )
        self._paper_executor = PaperExecutor(config, trade_log_path)
        self._two_leg = TwoLegExecutor(config, self._paper_executor)

        # Authenticated KalshiExecutor created at startup so _shutdown() can reuse it.
        # Creating a new instance at shutdown risks missing credentials (e.g. KALSHI_LIVE_API_KEY unset).
        self._kalshi_executor = KalshiExecutor(config, paper_mode=self._paper_mode)

        # PredictIt signal generator — only initialized when enabled in config
        pi_cfg = config.get("predictit", {})
        if pi_cfg.get("enabled", False):
            from src.data.predictit import PredictItClient
            from src.signal.pi_signal import PISignalGenerator
            pi_poll_interval = int(pi_cfg.get("poll_interval_seconds", 30))
            self._predictit: Optional[PredictItClient] = PredictItClient(poll_interval=pi_poll_interval)
            self._pi_signal: Optional[PISignalGenerator] = PISignalGenerator(
                config=config,
                snapshot_store=self._snapshot_store,
            )
            log.info("trader2: PredictIt signal feed enabled (poll=%ds, threshold=%.1fpp)",
                     pi_poll_interval, pi_cfg.get("signal_threshold_pp", 8.0))
        else:
            self._predictit = None
            self._pi_signal = None
            log.info("trader2: PredictIt signal feed disabled — set config[predictit][enabled]=true to enable")

        # Polymarket executor — only initialized when enabled in config
        pm_cfg = config.get("polymarket", {})
        if pm_cfg.get("enabled", False):
            self._polymarket_executor: Optional[PolymarketExecutor] = PolymarketExecutor(
                config, paper_mode=self._paper_mode
            )
            log.info("trader2: Polymarket executor initialized (paper_mode=%s)", self._paper_mode)
        else:
            self._polymarket_executor = None
            log.info("trader2: Polymarket executor disabled — set config[polymarket][enabled]=true to enable")

        self._risk = RiskManager(config, db=self._db)
        self._divergence_engine = DivergenceEngine(config, self._snapshot_store)

        self._calendar = EventCalendar(str(config_dir / "event_calendar.json"))
        self._spike_detector = KalshiSpikeDetector(config, self._calendar)
        # Rate spike detector: 2Y yield moves → fire on Kalshi (corrected signal direction)
        self._rate_spike_detector = RateSpikeDetector(config, self._calendar)
        self._fast_path = EventArbFastPath(
            config=config,
            snapshot_store=self._snapshot_store,
            risk_manager=self._risk,
            paper_executor=self._paper_executor,
            calendar=self._calendar,
            notifier=self._notifier,
        )

        # Always use live Kalshi API for market data — real prices even in paper mode.
        # paper_mode only controls order execution (KalshiExecutor), not data feeds.
        self._kalshi = KalshiClient(
            config=config,
            on_snapshot=self._on_kalshi_snapshot,
            paper_mode=False,
        )
        self._cme = CMEClient(config, self._event_map)
        # Treasury 2Y yield client — real-time rate signal for event-speed arb
        self._treasury = TreasuryYieldClient(config)
        self._polymarket = PolymarketClient(config, self._event_map)

        # Signal / control
        self._stop_event = threading.Event()
        self._start_time = time.time()

        # Scheduling state
        self._last_cme_poll: float = 0.0
        self._last_pm_poll: float = 0.0
        self._last_pi_poll: float = 0.0
        self._last_yield_poll: float = 0.0
        self._last_snapshot_sample: float = 0.0
        self._last_daily_summary_date: str = ""

        # Near-miss tracking for daily summary
        self._near_misses: list = []  # (divergence_pp,) tuples
        self._daily_stat_arb_count: int = 0
        self._daily_event_arb_count: int = 0
        self._daily_stat_arb_pnl: float = 0.0
        self._daily_event_arb_pnl: float = 0.0

        log.info("trader2: all components initialized")

    def _on_kalshi_snapshot(self, snap) -> None:
        """Called from WS thread on each Kalshi price update."""
        self._snapshot_store.update(snap)

        # Spike detection during event windows
        spike = self._spike_detector.on_price_update(snap)
        if spike:
            self._fast_path.on_spike_detected(spike)

    def _poll_cme(self) -> None:
        """Fetch CME snapshots and update store."""
        snapshots = self._cme.fetch_snapshots()
        for snap in snapshots:
            self._snapshot_store.update(snap)
            log.debug("cme: %s %s prob=%.3f", snap.event_id, snap.ticker, snap.mid_prob)

    def _poll_treasury_yield(self) -> None:
        """
        Fetch the 2Y Treasury yield and run rate spike detection.

        Polls at an elevated frequency during active event windows (every ~5s)
        and at the normal CME poll interval otherwise.  The rate spike detector
        fires a SpikeEvent with source="rate_yield" when the yield moves
        more than the configured threshold — EventArbFastPath then targets
        the lagging Kalshi market.
        """
        in_event_window = self._calendar.active_event_window() is not None

        yield_val = self._treasury.fetch_yield()
        if yield_val is None:
            log.debug("treasury: no yield data available")
            return

        log.debug("treasury: 2Y yield=%.4f%% (event_window=%s)", yield_val, in_event_window)

        # Feed into rate spike detector
        spike = self._rate_spike_detector.on_yield_update(yield_val)
        if spike:
            log.info(
                "treasury: rate spike → event_arb: event=%s dir=%s mag=%.1fpp",
                spike.event_id, spike.direction, spike.magnitude_pp,
            )
            self._fast_path.on_spike_detected(spike)

    def _poll_polymarket(self) -> None:
        """Fetch Polymarket snapshots and update store (Phase 2 reads)."""
        snapshots = self._polymarket.fetch_snapshots()
        for snap in snapshots:
            self._snapshot_store.update(snap)

    def _poll_predictit(self) -> None:
        """Fetch PredictIt markets and run divergence signal scan against Kalshi."""
        if self._predictit is None or self._pi_signal is None:
            return
        pi_cfg = self._config.get("predictit", {})
        enabled_types = pi_cfg.get("markets", ["fed", "cpi", "nfp"])
        markets = self._predictit.get_all_markets()
        filtered = [m for m in markets if m.market_type in enabled_types]
        if not filtered:
            log.debug("predictit: no matching markets (types=%s)", enabled_types)
            return

        signals = self._pi_signal.scan(filtered)
        for sig in signals:
            log.info(
                "predictit: signal event=%s ticker=%s PI=%.1f%% Kalshi=%.1f%% div=%.1fpp dir=%s",
                sig.event_id, sig.kalshi_ticker,
                sig.pi_prob * 100, sig.kalshi_prob * 100,
                sig.divergence_pp, sig.direction,
            )
            # Record to DB
            try:
                self._db.record_predictit_signal(
                    event_id=sig.event_id,
                    kalshi_ticker=sig.kalshi_ticker,
                    predictit_market_id=sig.predictit_market_id,
                    predictit_market_name=sig.predictit_market_name,
                    predictit_prob=sig.pi_prob,
                    kalshi_mid_prob=sig.kalshi_prob,
                    divergence_pp=sig.divergence_pp,
                    direction=sig.direction,
                )
            except Exception as db_err:
                log.debug("predictit: DB write failed: %s", db_err)

            # Notify via Discord
            mode_tag = "[PAPER]" if self._paper_mode else "[LIVE]"
            self._notifier.send_trade_alert(
                strategy="predictit_signal",
                event_id=sig.event_id,
                details=(
                    f"PredictIt {sig.pi_prob*100:.0f}% vs Kalshi {sig.kalshi_prob*100:.0f}%, "
                    f"+{sig.divergence_pp:.1f}pp divergence. Direction: {sig.direction}. "
                    f"PI market: {sig.predictit_market_name[:60]} {mode_tag} [predictit_signaled]"
                ),
            )

    def _run_divergence_scan(self) -> None:
        """Scan all events for divergences and execute if approved."""
        signals = self._divergence_engine.scan_all_events()

        for signal in signals:
            size_usd = self._compute_size(signal)

            approved, reason = self._risk.approve_stat_arb(signal, size_usd)
            if not approved:
                log.info(
                    "divergence: risk rejected %s vs %s event=%s reason=%s",
                    signal.market_a,
                    signal.market_b,
                    signal.event_id,
                    reason,
                )
                continue

            if self._dry_run:
                log.info(
                    "DRY RUN: would execute stat_arb event=%s %s vs %s net=%.2fpp size=%.2f",
                    signal.event_id,
                    signal.market_a,
                    signal.market_b,
                    signal.net_divergence,
                    size_usd,
                )
                continue

            # Execute
            kalshi_snap = signal.snapshot_a if signal.market_a == "kalshi" else signal.snapshot_b
            pm_snap = None  # Phase 2: Polymarket execution wired but stat-arb cross-market not yet active

            result = self._two_leg.execute(
                signal=signal,
                kalshi_snap=kalshi_snap,
                polymarket_snap=pm_snap,
                size_usd=size_usd,
            )

            self._risk.record_trade_open(signal.market_a, size_usd)
            self._daily_stat_arb_count += 1

            self._notifier.send_trade_alert(
                strategy="stat_arb",
                event_id=signal.event_id,
                details=(
                    f"Kalshi {signal.prob_a*100:.0f}% vs CME {signal.prob_b*100:.0f}%, "
                    f"+{signal.net_divergence:.1f}pp net. WOULD place ${size_usd:.0f}/side."
                ),
            )

            # Record to DB
            self._db.record_trade_intent(
                trade_id=result.trade_id,
                strategy="stat_arb",
                event_id=signal.event_id,
                legs=[
                    {"market": signal.market_a, "ticker": signal.snapshot_a.ticker,
                     "side": "yes", "size_usd": size_usd},
                    {"market": signal.market_b, "ticker": signal.snapshot_b.ticker,
                     "side": "no", "size_usd": size_usd},
                ],
                divergence_pp=signal.net_divergence,
            )

        # Track near-misses (divergences below threshold)
        for event_id in self._snapshot_store.all_event_ids():
            k_snap = self._snapshot_store.get(event_id, "kalshi")
            c_snap = self._snapshot_store.get(event_id, "cme")
            if k_snap and c_snap and not k_snap.is_stale and not c_snap.is_stale:
                raw = abs(k_snap.mid_prob - c_snap.mid_prob) * 100.0
                if 2.0 <= raw < 5.0:
                    self._near_misses.append(raw)

    def _compute_size(self, signal) -> float:
        """Compute trade size in USD from config."""
        sizing = self._config.get("sizing", {})
        base = sizing.get("base_contracts", 200)
        max_single = sizing.get("max_single_trade_usd", 500.0)

        if sizing.get("scale_with_divergence", False):
            target = sizing.get("target_divergence_pp", 8.0)
            scale = min(signal.net_divergence / target, 2.0)
            base = int(base * scale)

        return min(float(base), max_single)

    def _check_daily_summary(self) -> None:
        """Post daily summary at the configured UTC hour."""
        notif_cfg = self._config.get("notifications", {})
        hour = notif_cfg.get("daily_summary_hour_utc", 6)
        today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        now_hour = datetime.now(tz=timezone.utc).hour

        if now_hour == hour and self._last_daily_summary_date != today:
            self._last_daily_summary_date = today

            daily_data = self._db.get_daily_pnl(today)
            cumulative = self._db.get_cumulative_simulated_pnl()
            kalshi_balance = None
            try:
                from src.execution.kalshi_executor import KalshiExecutor
                ke = KalshiExecutor(self._config, self._paper_mode)
                kalshi_balance = ke.get_balance()
            except Exception:
                pass

            top_missed = max(self._near_misses) if self._near_misses else None
            uptime_hours = (time.time() - self._start_time) / 3600.0

            net_pnl = (
                daily_data.get("realized_pnl_usd", 0.0)
                + daily_data.get("simulated_pnl_usd", 0.0)
            )

            self._notifier.send_daily_summary(
                date=today,
                trades_today=daily_data.get("trade_count", 0),
                stat_arb_count=self._daily_stat_arb_count,
                stat_arb_pnl=self._daily_stat_arb_pnl,
                event_arb_count=self._daily_event_arb_count,
                event_arb_pnl=self._daily_event_arb_pnl,
                net_pnl=net_pnl,
                cumulative_pnl=cumulative,
                kalshi_balance=kalshi_balance,
                top_missed_divergence=top_missed,
                uptime_hours=uptime_hours,
            )

            # Reset daily counters
            self._daily_stat_arb_count = 0
            self._daily_event_arb_count = 0
            self._daily_stat_arb_pnl = 0.0
            self._daily_event_arb_pnl = 0.0
            self._near_misses = []

    def _check_kill_conditions(self) -> None:
        should_kill, reason = self._risk.check_kill_conditions()
        if should_kill:
            log.critical("KILL CONDITION: %s — halting bot", reason)
            self._notifier.send_kill_alert(reason)
            self._stop_event.set()

    def _check_ws_health(self) -> None:
        if not self._kalshi.ws_is_healthy:
            log.warning("kalshi: WebSocket appears unhealthy (no messages in 5m)")
            self._notifier.send_error("Kalshi WebSocket disconnected for >5 minutes")

    def run(self) -> None:
        """Main event loop. Blocks until stop_event is set."""
        # Resolve active Kalshi tickers
        self._kalshi.resolve_active_tickers(self._event_map)

        # Start Kalshi WS thread
        if not self._dry_run:
            self._kalshi.start_websocket()
        else:
            log.info("DRY RUN: skipping Kalshi WebSocket connection")

        # Setup signal handlers
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGHUP, self._handle_sighup)

        log.info("trader2: event loop started")
        if self._dry_run:
            log.info("DRY RUN: will scan for divergences but not execute trades")

        # Startup notification
        self._notifier.send(
            f"Trader2 started [{self._mode.upper()}]. Phase 1: Kalshi + CME divergence monitoring."
        )

        cme_interval = self._config.get("data", {}).get("cme_poll_interval_seconds", 30)
        pm_interval = self._config.get("data", {}).get("polymarket_poll_interval_seconds", 10)
        pi_interval = int(self._config.get("predictit", {}).get("poll_interval_seconds", 30))
        # Treasury yield: poll every 5s during event windows, every 30s otherwise
        yield_interval_event: float = self._config.get("data", {}).get(
            "yield_poll_interval_event_seconds", 5.0
        )
        yield_interval_idle: float = self._config.get("data", {}).get(
            "yield_poll_interval_idle_seconds", 30.0
        )

        # Do an initial CME poll and yield fetch
        try:
            self._poll_cme()
        except Exception as e:
            log.error("Initial CME poll failed: %s", e)
        try:
            self._poll_treasury_yield()
            self._last_yield_poll = time.time()
        except Exception as e:
            log.error("Initial treasury yield poll failed: %s", e)

        loop_count = 0
        while not self._stop_event.is_set():
            now = time.time()
            loop_count += 1

            try:
                # CME polling
                if now - self._last_cme_poll >= cme_interval:
                    self._poll_cme()
                    self._last_cme_poll = now

                # Treasury 2Y yield polling — rate signal for event-speed arb
                in_event_window = self._calendar.active_event_window() is not None
                yield_interval = yield_interval_event if in_event_window else yield_interval_idle
                if now - self._last_yield_poll >= yield_interval:
                    self._poll_treasury_yield()
                    self._last_yield_poll = now

                # Polymarket polling (Phase 1: only if condition IDs are configured)
                if now - self._last_pm_poll >= pm_interval:
                    self._poll_polymarket()
                    self._last_pm_poll = now

                # PredictIt signal polling (every 30s when enabled)
                if self._predictit is not None and now - self._last_pi_poll >= pi_interval:
                    self._poll_predictit()
                    self._last_pi_poll = now

                # Divergence scan (every loop iteration — fast)
                self._run_divergence_scan()

                # Daily summary check
                self._check_daily_summary()

                # Kill condition check
                self._check_kill_conditions()

                # WS health check (every ~60 iterations ≈ 1 min at 1s loop)
                if loop_count % 60 == 0:
                    self._check_ws_health()

                # Sample snapshots to DB every 5 minutes
                if now - self._last_snapshot_sample >= 300:
                    self._sample_snapshots_to_db()
                    self._last_snapshot_sample = now

            except Exception as e:
                tb = traceback.format_exc()
                log.error("Main loop error: %s\n%s", e, tb)
                self._notifier.send_error(str(e), tb)

            self._stop_event.wait(timeout=1.0)

        log.info("trader2: event loop stopped")
        self._shutdown()
        self._notifier.send("Trader2 stopped.")

    def _sample_snapshots_to_db(self) -> None:
        """Periodically record current snapshots to the DB for historical analysis."""
        for event_id in self._snapshot_store.all_event_ids():
            for market in ("kalshi", "polymarket", "cme"):
                snap = self._snapshot_store.get(event_id, market)
                if snap and not snap.is_stale:
                    try:
                        self._db.record_price_snapshot(
                            event_id=snap.event_id,
                            market=snap.market,
                            ticker=snap.ticker,
                            yes_prob=snap.yes_prob,
                            bid_prob=snap.bid_prob,
                            ask_prob=snap.ask_prob,
                            timestamp=snap.timestamp,
                        )
                    except Exception as e:
                        log.debug("snapshot DB write error: %s", e)

    def _shutdown(self) -> None:
        """Orderly shutdown: cancel open Kalshi orders, then stop WebSocket."""
        # Cancel any resting limit orders so they don't fill after the bot stops.
        # Reuse the authenticated KalshiExecutor created at startup to avoid credential issues.
        if not self._dry_run and not self._paper_mode:
            try:
                n = self._kalshi_executor.cancel_all_open_orders()
                log.warning("shutdown: cancelled %d open Kalshi orders (live mode)", n)
            except Exception as e:
                log.error("shutdown: error cancelling open Kalshi orders: %s", e)
        elif not self._dry_run:
            # Paper mode — cancel demo orders too for cleanliness
            try:
                n = self._kalshi_executor.cancel_all_open_orders()
                if n:
                    log.info("shutdown: cancelled %d open Kalshi orders (paper mode)", n)
            except Exception as e:
                log.debug("shutdown: error cancelling paper Kalshi orders: %s", e)
        self._kalshi.stop()

    def _handle_signal(self, signum, frame) -> None:
        log.info("trader2: received signal %d, shutting down", signum)
        self._stop_event.set()

    def _handle_sighup(self, signum, frame) -> None:
        log.info("trader2: SIGHUP — reloading event_map and calendar")
        try:
            config_dir = Path(__file__).parent.parent / "config"
            with open(config_dir / "event_map.json") as f:
                event_map_data = json.load(f)
            self._event_map = [
                e for e in event_map_data.get("events", []) if e.get("active")
            ]
            self._kalshi.resolve_active_tickers(self._event_map)
            self._calendar.reload()
            log.info("trader2: reload complete")
        except Exception as e:
            log.error("trader2: reload failed: %s", e)


def run(config: dict, dry_run: bool = False) -> None:
    """Entry point called from the top-level main.py."""
    bot = Trader2(config, dry_run=dry_run)
    bot.run()
