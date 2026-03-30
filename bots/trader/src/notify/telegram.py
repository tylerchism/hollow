"""Telegram notifications for hollow-trader.

Provides:
    - send_message()          : fire-and-forget single message
    - send_daily_summary()    : structured daily performance report
    - notify_kill_switch()    : critical alert on kill switch engagement
    - notify_trade()          : alert on each paper fill

Reuses the existing Hollow Telegram env var pattern:
    TELEGRAM_BOT_TOKEN   — bot token from BotFather
    TELEGRAM_CHAT_ID     — Tyler's Telegram ID (8604539164) or a channel ID

Messages use plain text (no markdown) for reliability.
Failures are logged but never raise — notifications must not crash the engine.

Config keys (under config["notifications"]):
    telegram_env            str     env var name for bot token (default TELEGRAM_BOT_TOKEN)
    telegram_chat_id_env    str     env var name for chat ID   (default TELEGRAM_CHAT_ID)
    on_error                bool    default true
    on_trade                bool    default true
    daily_summary_hour_utc  int     default 6   (06:00 UTC)
"""

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

log = logging.getLogger("trader.notify.telegram")

_TELEGRAM_API = "https://api.telegram.org"


class TelegramNotifier:
    def __init__(self, config: dict) -> None:
        self.config = config
        notify_cfg = config.get("notifications", {})

        token_env  = notify_cfg.get("telegram_env", "TELEGRAM_BOT_TOKEN")
        chat_env   = notify_cfg.get("telegram_chat_id_env", "TELEGRAM_CHAT_ID")

        self._token   = os.environ.get(token_env, "")
        self._chat_id = os.environ.get(chat_env, "")
        self._on_error = bool(notify_cfg.get("on_error", True))
        self._on_trade = bool(notify_cfg.get("on_trade", True))
        self._summary_hour = int(notify_cfg.get("daily_summary_hour_utc", 6))

        self._last_summary_date: str | None = None  # "YYYY-MM-DD" of last sent summary

        if not self._token:
            log.warning(
                "TELEGRAM_BOT_TOKEN not set — Telegram notifications disabled. "
                "Set env var %s to enable.", token_env,
            )
        if not self._chat_id:
            log.warning(
                "TELEGRAM_CHAT_ID not set — Telegram notifications disabled. "
                "Set env var %s to enable.", chat_env,
            )

    @property
    def enabled(self) -> bool:
        return bool(self._token and self._chat_id)

    # ------------------------------------------------------------------
    # Core send
    # ------------------------------------------------------------------

    def send_message(self, text: str) -> bool:
        """
        Send a plain-text Telegram message. Returns True on success.
        Never raises — failures are logged only.
        """
        if not self.enabled:
            log.debug("Telegram disabled — skipping message: %s", text[:80])
            return False

        url = f"{_TELEGRAM_API}/bot{self._token}/sendMessage"
        payload = json.dumps({
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "",
        }).encode()

        req = urllib.request.Request(url, data=payload, method="POST")
        req.add_header("Content-Type", "application/json")

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
                if data.get("ok"):
                    log.debug("Telegram message sent (len=%d)", len(text))
                    return True
                else:
                    log.error("Telegram API error: %s", data)
                    return False
        except (urllib.error.URLError, OSError) as exc:
            log.error("Telegram send failed: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Structured notifications
    # ------------------------------------------------------------------

    def notify_trade(self, fill, pnl_usd: float | None = None) -> None:
        """
        Send a notification for a completed paper fill.

        Args:
            fill:     FillResult from PaperSimulator
            pnl_usd:  Realized PnL if this is a close (sell). None for buys.
        """
        if not self._on_trade:
            return

        emoji = "BUY" if fill.side == "buy" else "SELL"
        lines = [
            f"[PAPER {emoji}] {fill.side.upper()} filled",
            f"  Qty:        {fill.qty:.6f}",
            f"  Fill price: ${fill.fill_price:,.2f}",
            f"  Intent:     ${fill.intent_price:,.2f}",
            f"  Slippage:   {fill.slippage_pct*100:.3f}%",
            f"  Value:      ${fill.fill_value_usd:,.2f}",
        ]
        if pnl_usd is not None:
            lines.append(f"  PnL:        ${pnl_usd:+,.2f}")
        if fill.notes:
            lines.append(f"  Note:       {fill.notes}")

        self.send_message("\n".join(lines))

    def notify_kill_switch(self, reason: str) -> None:
        """Send a critical kill-switch alert."""
        msg = (
            "*** KILL SWITCH ENGAGED ***\n"
            f"Reason: {reason}\n"
            f"Time:   {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
            "Trading halted. Manual restart required."
        )
        log.critical("Notifying kill switch: %s", reason)
        self.send_message(msg)

    def notify_error(self, context: str, error: str) -> None:
        """Send an error notification."""
        if not self._on_error:
            return
        msg = (
            f"[TRADER ERROR]\n"
            f"Context: {context}\n"
            f"Error:   {error}\n"
            f"Time:    {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        )
        self.send_message(msg)

    def notify_drawdown_halt(self, drawdown_pct: float, portfolio_value: float) -> None:
        """Send a drawdown halt notification."""
        msg = (
            f"[DAILY DRAWDOWN HALT]\n"
            f"Drawdown: {drawdown_pct:.2f}%\n"
            f"Portfolio: ${portfolio_value:,.2f}\n"
            f"Trading halted for today. Will resume tomorrow.\n"
            f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        )
        self.send_message(msg)

    # ------------------------------------------------------------------
    # Daily summary
    # ------------------------------------------------------------------

    def maybe_send_daily_summary(self, conn, simulator) -> None:
        """
        Send the daily summary if it's time (summary_hour_utc) and hasn't been
        sent today yet. Call this once per engine cycle.
        """
        now = datetime.now(timezone.utc)
        today = now.date().isoformat()

        # Already sent today?
        if self._last_summary_date == today:
            return

        # Is it the right hour?
        if now.hour != self._summary_hour:
            return

        self._last_summary_date = today
        self.send_daily_summary(conn, simulator)

    def send_daily_summary(self, conn, simulator) -> None:
        """
        Build and send the daily performance summary.

        Pulls data from portfolio_snapshots and paper_trades for today.
        """
        today = datetime.now(timezone.utc).date().isoformat()
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        # Portfolio value
        portfolio_value = simulator.portfolio_value()
        initial_cash = simulator._initial_cash
        total_pnl_usd = portfolio_value - initial_cash
        total_pnl_pct = (total_pnl_usd / initial_cash * 100.0) if initial_cash else 0.0

        # Today's trades
        try:
            today_trades = conn.execute("""
                SELECT side, COUNT(*) as count, SUM(fill_value_usd) as total_value
                FROM paper_trades
                WHERE date(logged_at) = ? AND status = 'filled'
                GROUP BY side
            """, (today,)).fetchall()
        except Exception as exc:
            log.error("Failed to query today's trades: %s", exc)
            today_trades = []

        buy_count = 0
        sell_count = 0
        for row in today_trades:
            if row["side"] == "buy":
                buy_count = int(row["count"])
            elif row["side"] == "sell":
                sell_count = int(row["count"])

        # Today's realized PnL (closed positions today)
        try:
            today_pnl = conn.execute("""
                SELECT COALESCE(SUM(pnl_usd), 0) as realized_pnl
                FROM paper_positions
                WHERE date(closed_at) = ? AND status = 'closed'
            """, (today,)).fetchone()
            realized_pnl = float(today_pnl["realized_pnl"]) if today_pnl else 0.0
        except Exception as exc:
            log.error("Failed to query realized PnL: %s", exc)
            realized_pnl = 0.0

        # Open positions summary
        open_positions = list(simulator.open_positions.values())

        # Latest RSI signal
        try:
            latest_signal = conn.execute("""
                SELECT rsi, signal, candle_ts FROM signals
                ORDER BY generated_at DESC LIMIT 1
            """).fetchone()
        except Exception as exc:
            log.error("Failed to query latest signal: %s", exc)
            latest_signal = None

        # Build message
        pnl_arrow = "+" if total_pnl_usd >= 0 else ""
        lines = [
            f"=== Hollow Trader Daily Summary ===",
            f"Date:        {today}",
            f"Time:        {now_iso}",
            f"",
            f"PORTFOLIO",
            f"  Value:     ${portfolio_value:,.2f}",
            f"  Cash:      ${simulator.cash:,.2f}",
            f"  Total PnL: {pnl_arrow}${total_pnl_usd:,.2f} ({pnl_arrow}{total_pnl_pct:.2f}%)",
            f"",
            f"TODAY",
            f"  Buys:      {buy_count}",
            f"  Sells:     {sell_count}",
            f"  Realized:  ${realized_pnl:+,.2f}",
            f"",
            f"POSITIONS ({len(open_positions)} open)",
        ]

        for pos in open_positions:
            lines.append(f"  {pos.symbol}  qty={pos.qty:.6f}  entry=${pos.entry_price:,.2f}")

        if latest_signal:
            rsi_val = f"{float(latest_signal['rsi']):.1f}" if latest_signal["rsi"] else "N/A"
            lines += [
                f"",
                f"LATEST SIGNAL",
                f"  RSI:    {rsi_val}",
                f"  Signal: {latest_signal['signal'].upper()}",
                f"  Candle: {latest_signal['candle_ts']}",
            ]

        lines += [
            f"",
            f"Mode: PAPER (no real capital at risk)",
        ]

        self.send_message("\n".join(lines))
        log.info("Daily summary sent for %s", today)
