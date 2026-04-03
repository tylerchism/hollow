"""Risk manager — approves or rejects trades based on position and daily limits."""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

from src.models import DivergenceSignal, RiskState

log = logging.getLogger(__name__)


class RiskManager:
    """
    Stateful risk manager. Tracks daily limits, position counts, and exposure.
    All approval functions return (bool, reason_str).

    RiskState is persisted to SQLite on every mutation so that a bot restart
    does not reset daily_deployed_usd / open_positions / total_exposure_usd to
    zero (which would allow risk limits to be bypassed after a crash).
    """

    def __init__(self, config: dict, db=None):
        """
        Args:
            config: bot config dict.
            db: optional StateDB instance for RiskState persistence.
                If None, state is in-memory only (acceptable for paper/test).
        """
        self._db = db
        self._state_lock = threading.Lock()
        risk_cfg = config.get("risk", {})
        self._max_single_trade_usd: float = risk_cfg.get("max_single_trade_usd", 500.0)
        self._max_event_arb_trade_usd: float = risk_cfg.get("max_event_arb_trade_usd", 300.0)
        self._max_daily_loss_usd: float = risk_cfg.get("max_daily_loss_usd", 500.0)
        self._max_daily_deployed_usd: float = risk_cfg.get("max_daily_deployed_usd", 3000.0)
        self._max_open_positions: int = risk_cfg.get("max_open_positions", 5)
        self._max_exposure_per_market_usd: float = risk_cfg.get("max_exposure_per_market_usd", 2000.0)
        self._max_total_exposure_usd: float = risk_cfg.get("max_total_exposure_usd", 5000.0)
        self._max_event_arb_positions: int = risk_cfg.get("max_event_arb_positions", 2)
        self._max_signal_age_seconds: float = risk_cfg.get("max_signal_age_seconds", 30.0)

        ks_cfg = config.get("kill_switch", {})
        ks_env = ks_cfg.get("path_env", "TRADER2_KILL_SWITCH_PATH")
        default_ks_path = str(
            Path.home() / ".local" / "share" / "hollow-trader2" / "KILL"
        )
        self._kill_switch_path = Path(
            os.environ.get(ks_env, default_ks_path)
        )

        self._paper_mode: bool = config.get("mode", "paper").strip().lower() != "live"

        self._state = RiskState()
        self._restore_state_from_db()
        self._reset_daily_if_needed()

    def _restore_state_from_db(self) -> None:
        """Restore RiskState from SQLite on startup (if db is configured)."""
        if self._db is None:
            return
        try:
            row = self._db.load_risk_state()
            if row is None:
                return
            self._state.daily_loss_usd = float(row.get("daily_loss_usd", 0.0))
            self._state.daily_deployed_usd = float(row.get("daily_deployed_usd", 0.0))
            self._state.open_positions = int(row.get("open_positions", 0))
            self._state.total_exposure_usd = float(row.get("total_exposure_usd", 0.0))
            self._state.exposure_by_market = dict(row.get("exposure_by_market", {}))
            self._state.event_arb_positions = int(row.get("event_arb_positions", 0))
            self._state.consecutive_failures = int(row.get("consecutive_failures", 0))
            self._state.last_reset_date = row.get("last_reset_date", "")
            log.info(
                "risk: restored state from DB — open_positions=%d total_exposure=%.2f "
                "daily_deployed=%.2f last_reset=%s",
                self._state.open_positions,
                self._state.total_exposure_usd,
                self._state.daily_deployed_usd,
                self._state.last_reset_date,
            )
        except Exception as e:
            log.error("risk: failed to restore state from DB: %s", e)
            if not self._paper_mode:
                raise RuntimeError(
                    f"risk: aborting startup — cannot restore state from DB in live mode: {e}"
                ) from e

    def _persist(self) -> None:
        """Persist current RiskState to SQLite (no-op if db is not configured)."""
        if self._db is None:
            return
        try:
            self._db.save_risk_state(self._state)
        except Exception as e:
            log.error("risk: failed to persist state to DB: %s", e)

    def _kill_switch_active(self) -> bool:
        return self._kill_switch_path.exists()

    def _reset_daily_if_needed(self) -> None:
        today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        if self._state.last_reset_date != today:
            log.info("risk: daily reset for %s", today)
            with self._state_lock:
                self._state.daily_loss_usd = 0.0
                self._state.daily_deployed_usd = 0.0
                self._state.last_reset_date = today
            self._persist()

    def _signal_stale(self, signal: DivergenceSignal) -> bool:
        return (time.time() - signal.timestamp) > self._max_signal_age_seconds

    def approve_stat_arb(
        self, signal: DivergenceSignal, size_usd: float
    ) -> Tuple[bool, str]:
        """Approve or reject a statistical arbitrage trade."""
        self._reset_daily_if_needed()

        if self._kill_switch_active():
            return False, "kill_switch"

        if self._signal_stale(signal):
            return False, "signal_stale"

        if self._state.daily_loss_usd >= self._max_daily_loss_usd:
            return False, "daily_loss_limit"

        if self._state.open_positions >= self._max_open_positions:
            return False, "max_positions"

        if self._state.total_exposure_usd + size_usd > self._max_total_exposure_usd:
            return False, "total_exposure"

        mkt_a_exp = self._state.exposure_by_market.get(signal.market_a, 0.0)
        if mkt_a_exp + size_usd > self._max_exposure_per_market_usd:
            return False, f"market_exposure_{signal.market_a}"

        if size_usd > self._max_single_trade_usd:
            return False, "trade_size"

        if self._state.daily_deployed_usd + size_usd > self._max_daily_deployed_usd:
            return False, "daily_deployed_limit"

        if self._state.consecutive_failures >= 3:
            return False, "consecutive_failures"

        return True, "approved"

    def approve_event_arb(self, market: str, size_usd: float) -> Tuple[bool, str]:
        """Approve or reject an event-speed arb trade."""
        self._reset_daily_if_needed()

        if self._kill_switch_active():
            return False, "kill_switch"

        if self._state.daily_loss_usd >= self._max_daily_loss_usd:
            return False, "daily_loss_limit"

        if self._state.event_arb_positions >= self._max_event_arb_positions:
            return False, "max_event_arb_positions"

        if size_usd > self._max_event_arb_trade_usd:
            return False, "event_arb_trade_size"

        if self._state.total_exposure_usd + size_usd > self._max_total_exposure_usd:
            return False, "total_exposure"

        return True, "approved"

    def record_trade_open(self, market: str, size_usd: float, is_event_arb: bool = False) -> None:
        """Update state when a trade opens."""
        with self._state_lock:
            self._state.open_positions += 1
            self._state.total_exposure_usd += size_usd
            self._state.daily_deployed_usd += size_usd
            self._state.exposure_by_market[market] = (
                self._state.exposure_by_market.get(market, 0.0) + size_usd
            )
            if is_event_arb:
                self._state.event_arb_positions += 1
            self._state.consecutive_failures = 0
        self._persist()
        log.info(
            "risk: trade open market=%s size=%.2f positions=%d total_exposure=%.2f",
            market,
            size_usd,
            self._state.open_positions,
            self._state.total_exposure_usd,
        )

    def record_trade_close(self, market: str, size_usd: float, pnl_usd: float, is_event_arb: bool = False) -> None:
        """Update state when a trade closes."""
        with self._state_lock:
            self._state.open_positions = max(0, self._state.open_positions - 1)
            self._state.total_exposure_usd = max(0.0, self._state.total_exposure_usd - size_usd)
            self._state.exposure_by_market[market] = max(
                0.0, self._state.exposure_by_market.get(market, 0.0) - size_usd
            )
            if is_event_arb:
                self._state.event_arb_positions = max(0, self._state.event_arb_positions - 1)
            if pnl_usd < 0:
                self._state.daily_loss_usd += abs(pnl_usd)
        self._persist()
        log.info(
            "risk: trade close market=%s pnl=%.2f daily_loss=%.2f positions=%d",
            market,
            pnl_usd,
            self._state.daily_loss_usd,
            self._state.open_positions,
        )

    def record_execution_failure(self) -> None:
        with self._state_lock:
            self._state.consecutive_failures += 1
        self._persist()
        log.warning("risk: consecutive failures=%d", self._state.consecutive_failures)

    @property
    def state(self) -> RiskState:
        return self._state

    @property
    def kill_switch_active(self) -> bool:
        return self._kill_switch_active()

    def check_kill_conditions(self) -> Tuple[bool, str]:
        """
        Check all automatic kill conditions.
        Returns (should_kill, reason).
        """
        self._reset_daily_if_needed()

        if self._kill_switch_active():
            return True, "kill_switch_file"

        if self._state.daily_loss_usd >= self._max_daily_loss_usd:
            return True, f"daily_loss_limit={self._state.daily_loss_usd:.2f}"

        if self._state.total_exposure_usd >= self._max_total_exposure_usd:
            return True, f"total_exposure={self._state.total_exposure_usd:.2f}"

        if self._state.consecutive_failures >= 3:
            return True, f"consecutive_failures={self._state.consecutive_failures}"

        return False, ""
