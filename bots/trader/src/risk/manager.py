"""Risk manager — V1.

Enforces the full risk control checklist from docs/spike-ai-trader-bot.md:

    [x] Per-trade size limit       — max 2-3% of portfolio per position
    [x] Daily drawdown limit       — halt trading if portfolio drops >5% in a calendar day
    [x] Max concurrent positions   — explicit cap (default 3)
    [x] Stale signal protection    — don't act on signals older than N seconds
    [x] Kill switch                — single flag that halts all signal generation/execution
    [x] Separate paper/live guard  — enforced in main.py (not here)
    [x] Append-only trade log      — enforced in PaperSimulator (writes before fill)
    [x] Order fill verification    — enforced in PaperSimulator (FillResult.success check)

The kill switch state is persisted in the risk_state DB table so a kill survives restarts.
A file-based kill switch at {kill_switch_path} is also checked every cycle — drop a file
there from any terminal to kill without touching the DB.

Config keys (under config["risk"]):
    max_position_pct        float   default 0.03    (3% of portfolio per trade)
    max_drawdown_pct        float   default 0.05    (5% daily drawdown → halt)
    max_open_positions      int     default 3
    max_signal_age_seconds  int     default 900     (15 min — 4h candle + buffer)
    kill_switch_path        str     default "~/.local/share/hollow-trader/KILL"
"""

import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("trader.risk.manager")


class RiskError(Exception):
    """Raised when a risk check blocks an action. Not a bug — expected flow."""


class RiskManager:
    def __init__(self, config: dict) -> None:
        self.config = config
        risk_cfg = config.get("risk", {})

        self.max_position_pct    = float(risk_cfg.get("max_position_pct", 0.03))
        self.max_drawdown_pct    = float(risk_cfg.get("max_drawdown_pct", 0.05))
        self.max_open_positions  = int(risk_cfg.get("max_open_positions", 3))
        self.max_signal_age_secs = int(risk_cfg.get("max_signal_age_seconds", 900))

        raw_kill_path = risk_cfg.get(
            "kill_switch_path", "~/.local/share/hollow-trader/KILL"
        )
        self.kill_switch_path = Path(os.path.expanduser(raw_kill_path))
        self.kill_switch_path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Kill switch
    # ------------------------------------------------------------------

    def is_killed(self, conn: sqlite3.Connection) -> bool:
        """Return True if the kill switch is active (file OR DB flag)."""
        # File-based kill (fast check first)
        if self.kill_switch_path.exists():
            log.warning("KILL SWITCH FILE detected at %s", self.kill_switch_path)
            # Sync to DB so restarts remember
            self._set_killed_in_db(conn, "kill switch file detected")
            return True

        # DB-based kill
        row = conn.execute("SELECT killed FROM risk_state WHERE id = 1").fetchone()
        if row and int(row["killed"]) == 1:
            return True

        return False

    def engage_kill_switch(self, conn: sqlite3.Connection, reason: str) -> None:
        """
        Engage the kill switch: set DB flag, create the file, log loudly.
        Does NOT cancel live orders (paper mode — nothing to cancel).
        In live mode, the caller is responsible for cancelling open orders.
        """
        log.critical("KILL SWITCH ENGAGED: %s", reason)
        self._set_killed_in_db(conn, reason)
        # Also create the file so any watcher can see it
        try:
            self.kill_switch_path.write_text(f"KILLED at {datetime.now(timezone.utc).isoformat()}: {reason}\n")
        except OSError as exc:
            log.error("Could not write kill switch file: %s", exc)

    def clear_kill_switch(self, conn: sqlite3.Connection) -> None:
        """
        Manually clear the kill switch (for testing / manual restart).
        Requires human intervention — not called automatically.
        """
        log.warning("Clearing kill switch (manual override)")
        try:
            if self.kill_switch_path.exists():
                self.kill_switch_path.unlink()
        except OSError as exc:
            log.error("Could not remove kill switch file: %s", exc)
        with conn:
            conn.execute(
                "UPDATE risk_state SET killed = 0, killed_reason = NULL, killed_at = NULL "
                "WHERE id = 1"
            )

    def _set_killed_in_db(self, conn: sqlite3.Connection, reason: str) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        with conn:
            conn.execute("""
                INSERT INTO risk_state (id, killed, killed_at, killed_reason, last_updated)
                VALUES (1, 1, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    killed = 1,
                    killed_at = excluded.killed_at,
                    killed_reason = excluded.killed_reason,
                    last_updated = excluded.last_updated
            """, (now_iso, reason, now_iso))

    # ------------------------------------------------------------------
    # Daily drawdown
    # ------------------------------------------------------------------

    def check_drawdown(
        self,
        conn: sqlite3.Connection,
        current_portfolio_value: float,
    ) -> None:
        """
        Compare current portfolio value against day-start value.
        If drawdown exceeds max_drawdown_pct, engage kill switch and raise RiskError.

        Day-start value is set once per calendar day on the first call.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        today = datetime.now(timezone.utc).date().isoformat()

        row = conn.execute("SELECT * FROM risk_state WHERE id = 1").fetchone()

        if row is None:
            # First time — seed the risk_state row
            with conn:
                conn.execute("""
                    INSERT INTO risk_state
                        (id, day_start_value_usd, halt_date, last_updated)
                    VALUES (1, ?, ?, ?)
                """, (current_portfolio_value, today, now_iso))
            log.info("Risk state seeded. Day-start value: $%.2f", current_portfolio_value)
            return

        # Check if already halted
        if int(row["daily_drawdown_halted"]) == 1:
            if row["halt_date"] == today:
                raise RiskError(
                    f"Daily drawdown halt active (halted on {row['halt_date']}). "
                    "Manual restart required."
                )
            else:
                # New day — reset the halt
                log.info("New day (%s) — resetting daily drawdown halt", today)
                with conn:
                    conn.execute("""
                        UPDATE risk_state
                        SET daily_drawdown_halted = 0,
                            halt_date = NULL,
                            day_start_value_usd = ?,
                            last_updated = ?
                        WHERE id = 1
                    """, (current_portfolio_value, now_iso))
                return

        # Update day-start value if we're in a new day
        if row["halt_date"] != today:
            log.info(
                "New day (%s) — updating day-start value to $%.2f",
                today, current_portfolio_value,
            )
            with conn:
                conn.execute("""
                    UPDATE risk_state
                    SET day_start_value_usd = ?, halt_date = ?, last_updated = ?
                    WHERE id = 1
                """, (current_portfolio_value, today, now_iso))
            return

        day_start = float(row["day_start_value_usd"] or current_portfolio_value)
        if day_start == 0:
            return

        drawdown = (day_start - current_portfolio_value) / day_start
        if drawdown >= self.max_drawdown_pct:
            msg = (
                f"Daily drawdown limit breached: {drawdown*100:.2f}% "
                f"(limit={self.max_drawdown_pct*100:.0f}%) "
                f"day_start=${day_start:.2f} current=${current_portfolio_value:.2f}"
            )
            log.critical(msg)
            with conn:
                conn.execute("""
                    UPDATE risk_state
                    SET daily_drawdown_halted = 1, halt_date = ?, last_updated = ?
                    WHERE id = 1
                """, (today, now_iso))
            self.engage_kill_switch(conn, msg)
            raise RiskError(msg)

        log.debug(
            "Drawdown check OK: current=$%.2f day_start=$%.2f drawdown=%.2f%%",
            current_portfolio_value, day_start, drawdown * 100,
        )

    # ------------------------------------------------------------------
    # Position sizing
    # ------------------------------------------------------------------

    def position_size_usd(self, portfolio_value: float) -> float:
        """
        Calculate the USD amount to risk on a new position.

        Returns max_position_pct * portfolio_value, floored at 0.
        Hard-coded, not runtime-configurable without restart.
        """
        size = portfolio_value * self.max_position_pct
        log.debug(
            "Position size: %.2f%% of $%.2f = $%.2f",
            self.max_position_pct * 100, portfolio_value, size,
        )
        return max(0.0, size)

    # ------------------------------------------------------------------
    # Pre-trade checks (call before every order attempt)
    # ------------------------------------------------------------------

    def pre_trade_checks(
        self,
        conn: sqlite3.Connection,
        signal,                     # strategy.rsi.Signal
        open_position_count: int,
        portfolio_value: float,
    ) -> None:
        """
        Run all pre-trade risk checks. Raises RiskError if any check fails.

        Checks performed:
            1. Kill switch engaged
            2. Daily drawdown halt
            3. Max concurrent positions
            4. Stale signal
        """
        # 1. Kill switch
        if self.is_killed(conn):
            raise RiskError("Kill switch is active — trading halted.")

        # 2. Drawdown (also seeds/updates day-start value)
        self.check_drawdown(conn, portfolio_value)

        # 3. Max concurrent positions (only blocks BUY; SELL always allowed)
        if signal.signal.value == "buy":
            if open_position_count >= self.max_open_positions:
                raise RiskError(
                    f"Max open positions reached ({open_position_count}/"
                    f"{self.max_open_positions}). Cannot open new position."
                )

        # 4. Stale signal check
        self._check_signal_freshness(signal)

    def _check_signal_freshness(self, signal) -> None:
        """Raise RiskError if the signal's candle is too old."""
        from ..strategy.rsi import SignalType
        if signal.signal == SignalType.HOLD:
            return  # HOLD signals don't expire — they're not acted on anyway

        try:
            generated = datetime.fromisoformat(signal.generated_at)
            if generated.tzinfo is None:
                generated = generated.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            age_seconds = (now - generated).total_seconds()
            if age_seconds > self.max_signal_age_secs:
                raise RiskError(
                    f"Stale signal: generated {age_seconds:.0f}s ago "
                    f"(max={self.max_signal_age_secs}s). Skipping."
                )
        except (ValueError, TypeError) as exc:
            raise RiskError(f"Could not parse signal timestamp: {exc}") from exc

    # ------------------------------------------------------------------
    # State initialization
    # ------------------------------------------------------------------

    def ensure_risk_state_row(self, conn: sqlite3.Connection, initial_portfolio_value: float) -> None:
        """
        Ensure the risk_state singleton row exists.
        Safe to call on every startup (idempotent).
        """
        today = datetime.now(timezone.utc).date().isoformat()
        now_iso = datetime.now(timezone.utc).isoformat()
        with conn:
            conn.execute("""
                INSERT INTO risk_state (id, day_start_value_usd, halt_date, last_updated)
                VALUES (1, ?, ?, ?)
                ON CONFLICT(id) DO NOTHING
            """, (initial_portfolio_value, today, now_iso))
