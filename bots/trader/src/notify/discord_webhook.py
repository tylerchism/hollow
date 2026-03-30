"""Discord webhook notifications for hollow-trader.

Posts trade alerts and daily summaries to a Discord channel via webhook URL.
Appears as "Flux" (or whatever display_name is configured) rather than the
bot user, so notifications are clearly identified in #trader-bot.

Uses only stdlib urllib — no new dependencies.

Config keys (under config["notifications"]):
    discord_webhook_url     str     Webhook URL for #trader-bot
    discord_display_name    str     Username shown on messages (default "Flux")
    on_error                bool    default true
    on_trade                bool    default true
"""

import json
import logging
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone

log = logging.getLogger("trader.notify.discord_webhook")

_DISCORD_MSG_LIMIT = 2000


class DiscordWebhookNotifier:
    def __init__(self, config: dict) -> None:
        self.config = config
        notify_cfg = config.get("notifications", {})

        self._webhook_url = notify_cfg.get("discord_webhook_url", "").strip()
        self._display_name = notify_cfg.get("discord_display_name", "Flux").strip()
        self._on_error = bool(notify_cfg.get("on_error", True))
        self._on_trade = bool(notify_cfg.get("on_trade", True))
        self._summary_hour = int(notify_cfg.get("daily_summary_hour_utc", 6))
        self._last_summary_date: str | None = None

        if not self._webhook_url:
            log.warning(
                "discord_webhook_url not set in notifications config — "
                "Discord webhook notifications disabled."
            )

    @property
    def enabled(self) -> bool:
        return bool(self._webhook_url)

    # ------------------------------------------------------------------
    # Core send
    # ------------------------------------------------------------------

    def send_message(self, text: str) -> bool:
        """Send a plain-text message via webhook. Returns True on success."""
        if not self.enabled:
            log.debug("Discord webhook disabled — skipping message: %s", text[:80])
            return False

        text = str(text)
        # Split at Discord's character limit
        chunks = [text[i: i + _DISCORD_MSG_LIMIT] for i in range(0, len(text), _DISCORD_MSG_LIMIT)] if text else [""]

        all_ok = True
        for chunk in chunks:
            ok = self._post_chunk(chunk)
            if not ok:
                all_ok = False

        return all_ok

    def _post_chunk(self, text: str) -> bool:
        payload = json.dumps({"content": text, "username": self._display_name}).encode()
        req = urllib.request.Request(self._webhook_url, data=payload, method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                # Discord webhooks return 204 No Content on success
                status = resp.status
                if status in (200, 204):
                    log.debug("Discord webhook message sent (len=%d)", len(text))
                    return True
                else:
                    body = resp.read().decode()
                    log.error("Discord webhook unexpected status %d: %s", status, body)
                    return False
        except urllib.error.HTTPError as exc:
            log.error("Discord webhook HTTP error: %s %s", exc.code, exc.reason)
            return False
        except (urllib.error.URLError, OSError) as exc:
            log.error("Discord webhook send failed: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Structured notifications (mirrors TelegramNotifier API)
    # ------------------------------------------------------------------

    def notify_trade(self, fill, pnl_usd: float | None = None) -> None:
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
        msg = (
            "*** KILL SWITCH ENGAGED ***\n"
            f"Reason: {reason}\n"
            f"Time:   {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
            "Trading halted. Manual restart required."
        )
        log.critical("Notifying kill switch: %s", reason)
        self.send_message(msg)

    def notify_error(self, context: str, error: str) -> None:
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
        now = datetime.now(timezone.utc)
        today = now.date().isoformat()

        if self._last_summary_date == today:
            return

        if now.hour != self._summary_hour:
            return

        self._last_summary_date = today
        self.send_daily_summary(conn, simulator)

    def send_daily_summary(self, conn, simulator) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        portfolio_value = simulator.portfolio_value()
        initial_cash = simulator._initial_cash
        total_pnl_usd = portfolio_value - initial_cash
        total_pnl_pct = (total_pnl_usd / initial_cash * 100.0) if initial_cash else 0.0

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

        open_positions = list(simulator.open_positions.values())

        try:
            latest_signal = conn.execute("""
                SELECT rsi, signal, candle_ts FROM signals
                ORDER BY generated_at DESC LIMIT 1
            """).fetchone()
        except Exception as exc:
            log.error("Failed to query latest signal: %s", exc)
            latest_signal = None

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
