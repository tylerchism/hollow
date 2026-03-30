"""V1 Trading Engine — RSI mean reversion on BTC/USDT 4h.

Wires together:
    - CryptoFetcher     (data ingestion — reuses V0 layer)
    - RSIStrategy       (signal generation)
    - RegimeDetector    (trending vs ranging — gates RSI signals)
    - RiskManager       (pre-trade guards, drawdown, kill switch)
    - PaperSimulator    (fill simulation with slippage)
    - TelegramNotifier  (trade alerts + daily summary)

Engine loop (runs every poll_interval_seconds):
    1. Exchange connectivity pre-check (fail loudly before entering any position)
    2. Fetch latest 4h OHLCV from exchange and store to DB
    3. Run regime detection — trending or ranging?
    4. Run RSI strategy → get Signal
       - BUY signals suppressed in trending regime (RSI false signals in trends)
       - SELL signals always permitted (allow closing open positions)
    5. Persist signal to DB (append-only, before acting)
    6. Check kill switch + risk controls
    7. Decide action:
       - BUY signal + ranging regime + no open position → size position, paper buy
       - SELL signal + open position exists              → paper sell all
       - HOLD or risk blocked or trending regime         → log and continue
    8. Send Telegram trade alert if fill occurred
    9. Maybe send daily summary (checks hour)

The engine does NOT manage the ingest loop from V0 — it runs its own focused
fetch so the 4h symbol/timeframe is always fresh without depending on V0's
multi-symbol ingest schedule.

Config keys (under config["engine"]):
    poll_interval_seconds   int     default 900   (15 min — sub-candle polling on 4h)
    symbol                  str     default "BTC/USDT"  (mirrors strategy.rsi.symbol)
    timeframe               str     default "4h"
    connectivity_check      bool    default true  (pre-check before each position entry)
"""

import logging
import time
import sqlite3
from datetime import datetime, timezone

log = logging.getLogger("trader.engine")


class TradingEngine:
    def __init__(self, config: dict) -> None:
        self.config = config
        engine_cfg = config.get("engine", {})

        self._poll_interval = int(engine_cfg.get("poll_interval_seconds", 900))
        self._symbol    = engine_cfg.get("symbol", config.get("strategy", {}).get("rsi", {}).get("symbol", "BTC/USDT"))
        self._timeframe = engine_cfg.get("timeframe", config.get("strategy", {}).get("rsi", {}).get("timeframe", "4h"))

        self._connectivity_check = bool(engine_cfg.get("connectivity_check", True))

        # Lazy-imported to keep startup fast
        self._strategy  = None
        self._regime    = None
        self._risk      = None
        self._simulator = None
        self._notifier  = None
        self._fetcher   = None

    def _build_components(self) -> None:
        """Construct all engine components. Called once before the loop starts."""
        from .strategy.rsi import RSIStrategy
        from .strategy.regime import RegimeDetector
        from .risk.manager import RiskManager
        from .paper.simulator import PaperSimulator
        from .notify.telegram import TelegramNotifier
        from .notify.discord_webhook import DiscordWebhookNotifier
        from .fetchers.crypto import CryptoFetcher

        self._strategy  = RSIStrategy(self.config)
        self._regime    = RegimeDetector(self.config)
        self._risk      = RiskManager(self.config)
        self._simulator = PaperSimulator(self.config)
        # Primary notifier: Discord webhook (#trader-bot as "Flux")
        self._notifier  = DiscordWebhookNotifier(self.config)
        # Fallback: Telegram (used if Discord webhook URL is not set)
        self._tg_notifier = TelegramNotifier(self.config)
        if not self._notifier.enabled:
            log.warning("Discord webhook notifier disabled — falling back to Telegram")
            self._notifier = self._tg_notifier
        self._fetcher   = CryptoFetcher(self.config)
        self._fetcher.connect()

        log.info(
            "Engine components built — symbol=%s timeframe=%s poll=%ds connectivity_check=%s",
            self._symbol, self._timeframe, self._poll_interval, self._connectivity_check,
        )

    def start(self) -> None:
        """
        Initialize state from DB and start the trading loop.
        Runs until KeyboardInterrupt or kill switch.
        """
        from .db import init_db, get_conn

        log.info("=== Hollow Trader V1 Engine starting ===")
        log.info("Mode: PAPER | Symbol: %s | Timeframe: %s", self._symbol, self._timeframe)

        # Initialize DB (idempotent)
        init_db(self.config)

        # Build components
        self._build_components()

        # Restore prior state
        conn = get_conn(self.config)
        try:
            self._simulator.restore_state(conn)
            portfolio_val = self._simulator.portfolio_value()
            self._risk.ensure_risk_state_row(conn, portfolio_val)
        finally:
            conn.close()

        log.info(
            "State restored — cash=$%.2f open_positions=%d",
            self._simulator.cash,
            len(self._simulator.open_positions),
        )

        self._notifier.send_message(
            f"Hollow Trader V1 started\n"
            f"Mode: PAPER | {self._symbol} {self._timeframe} | RSI mean reversion\n"
            f"Cash: ${self._simulator.cash:,.2f} | "
            f"Positions: {len(self._simulator.open_positions)}"
        )

        # Main loop
        while True:
            start_t = time.monotonic()
            try:
                self._run_cycle()
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                log.exception("Unhandled error in engine cycle: %s", exc)
                self._notifier.notify_error("engine cycle", str(exc))

            elapsed = time.monotonic() - start_t
            sleep_for = max(0, self._poll_interval - elapsed)
            log.debug("Cycle complete in %.1fs, sleeping %.0fs", elapsed, sleep_for)
            time.sleep(sleep_for)

    # ------------------------------------------------------------------
    # Single engine cycle
    # ------------------------------------------------------------------

    def _connectivity_precheck(self) -> bool:
        """
        Verify exchange auth + connectivity before entering any position.

        Returns True if exchange is reachable and authenticated.
        Logs loudly on failure — does not raise (caller decides whether to block).
        """
        if not self._connectivity_check:
            return True
        try:
            # fetch_ticker is a lightweight authenticated call
            ticker = self._fetcher.fetch_ticker(self._symbol)
            if ticker and ticker.get("last"):
                log.debug(
                    "Connectivity pre-check OK — %s last=$%.2f",
                    self._symbol, float(ticker["last"]),
                )
                return True
            else:
                log.error(
                    "Connectivity pre-check: ticker returned empty/invalid data for %s: %s",
                    self._symbol, ticker,
                )
                return False
        except Exception as exc:
            log.error("Connectivity pre-check FAILED: %s", exc)
            self._notifier.notify_error("connectivity pre-check", str(exc))
            return False

    def _run_cycle(self) -> None:
        from .db import get_conn
        from .strategy.rsi import SignalType, persist_signal, mark_signal_acted, mark_signal_expired
        from .strategy.regime import Regime
        from .risk.manager import RiskError
        from .storage.ohlcv import store_ohlcv

        conn = get_conn(self.config)
        try:
            # --- 1. Fetch fresh OHLCV ---
            candles = self._fetcher.fetch_ohlcv(
                self._symbol,
                self._timeframe,
                limit=self.config.get("strategy", {}).get("rsi", {}).get("candles_needed", 100),
            )
            exchange_label = self._fetcher.exchange_id
            if self._fetcher.testnet:
                exchange_label += "_testnet"

            stored = store_ohlcv(
                conn, candles,
                asset_class="crypto",
                exchange=exchange_label,
                symbol=self._symbol,
                timeframe=self._timeframe,
            )
            log.debug("Stored %d OHLCV candles for %s/%s", stored, self._symbol, self._timeframe)

            # --- 2. Regime detection ---
            regime_result = self._regime.classify(conn, self._symbol, self._timeframe)
            log.info(
                "Regime: %s (ADX=%s) — %s",
                regime_result.regime.value,
                f"{regime_result.adx:.1f}" if regime_result.adx is not None else "N/A",
                regime_result.notes,
            )

            # --- 3. Evaluate RSI signal ---
            signal = self._strategy.evaluate(conn)

            # --- 4. Suppress BUY signals in trending regime ---
            if signal.signal == SignalType.BUY and regime_result.regime == Regime.TRENDING:
                log.info(
                    "BUY signal suppressed — trending regime (ADX=%.1f). RSI=%.2f.",
                    regime_result.adx or 0.0,
                    signal.rsi if signal.rsi == signal.rsi else float("nan"),
                )
                # Persist the signal as expired (it was generated but not acted on)
                signal_id = persist_signal(conn, signal)
                mark_signal_expired(conn, signal_id)
                self._notifier.maybe_send_daily_summary(conn, self._simulator)
                return

            # --- 5. Persist signal BEFORE acting ---
            signal_id = persist_signal(conn, signal)

            # --- 6. HOLD → nothing to do ---
            if signal.signal == SignalType.HOLD:
                log.info(
                    "Signal HOLD — RSI=%.2f, no action",
                    signal.rsi if signal.rsi == signal.rsi else float("nan"),
                )
                self._notifier.maybe_send_daily_summary(conn, self._simulator)
                return

            # --- 7. Risk checks ---
            portfolio_value = self._simulator.portfolio_value(
                {self._symbol: signal.close_price}
            )

            open_pos_for_symbol = [
                pos for pos in self._simulator.open_positions.values()
                if pos.symbol == self._symbol
            ]

            try:
                self._risk.pre_trade_checks(
                    conn,
                    signal,
                    len(self._simulator.open_positions),
                    portfolio_value,
                )
            except RiskError as exc:
                log.warning("Risk check blocked trade: %s", exc)
                mark_signal_expired(conn, signal_id)

                # Check for kill switch specifically to send alert
                if "Kill switch" in str(exc):
                    self._notifier.notify_kill_switch(str(exc))
                elif "drawdown" in str(exc).lower():
                    self._notifier.notify_drawdown_halt(
                        self.config.get("risk", {}).get("max_drawdown_pct", 0.05) * 100,
                        portfolio_value,
                    )
                return

            # --- 8. Connectivity pre-check before entering a position ---
            if signal.signal == SignalType.BUY and not open_pos_for_symbol:
                if not self._connectivity_precheck():
                    log.warning(
                        "BUY blocked — exchange connectivity pre-check failed. "
                        "Skipping this cycle to avoid entering with stale data."
                    )
                    mark_signal_expired(conn, signal_id)
                    return

            # --- 9. Execute ---
            if signal.signal == SignalType.BUY:
                self._handle_buy(conn, signal, signal_id, portfolio_value, open_pos_for_symbol)

            elif signal.signal == SignalType.SELL:
                self._handle_sell(conn, signal, signal_id, open_pos_for_symbol)

            # --- 10. Maybe send daily summary ---
            self._notifier.maybe_send_daily_summary(conn, self._simulator)

        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Buy / Sell handlers
    # ------------------------------------------------------------------

    def _handle_buy(
        self,
        conn: sqlite3.Connection,
        signal,
        signal_id: int,
        portfolio_value: float,
        open_pos_for_symbol: list,
    ) -> None:
        from .strategy.rsi import mark_signal_acted, mark_signal_expired

        # Only one long position per symbol at a time
        if open_pos_for_symbol:
            log.info(
                "BUY signal but already have %d open position(s) for %s — skipping",
                len(open_pos_for_symbol), self._symbol,
            )
            mark_signal_expired(conn, signal_id)
            return

        # Size the position
        usd_amount = self._risk.position_size_usd(portfolio_value)
        if usd_amount < 1.0:
            log.warning("Position size too small ($%.2f) — skipping buy", usd_amount)
            mark_signal_expired(conn, signal_id)
            return

        log.info(
            "BUY signal: RSI=%.2f price=$%.2f sizing=$%.2f",
            signal.rsi, signal.close_price, usd_amount,
        )

        fill = self._simulator.buy(
            conn,
            symbol=self._symbol,
            intent_price=signal.close_price,
            usd_amount=usd_amount,
            signal_id=signal_id,
            strategy_id=self._strategy.STRATEGY_ID,
        )

        if fill.success:
            mark_signal_acted(conn, signal_id)
            log.info(
                "Buy filled: qty=%.6f @ $%.2f value=$%.2f",
                fill.qty, fill.fill_price, fill.fill_value_usd,
            )
            self._notifier.notify_trade(fill)
        else:
            log.warning("Buy failed: %s", fill.notes)
            mark_signal_expired(conn, signal_id)

    def _handle_sell(
        self,
        conn: sqlite3.Connection,
        signal,
        signal_id: int,
        open_pos_for_symbol: list,
    ) -> None:
        from .strategy.rsi import mark_signal_acted, mark_signal_expired

        if not open_pos_for_symbol:
            log.info("SELL signal but no open position for %s — skipping", self._symbol)
            mark_signal_expired(conn, signal_id)
            return

        acted = False
        for pos in open_pos_for_symbol:
            log.info(
                "SELL signal: RSI=%.2f price=$%.2f closing position %s (entry=$%.2f)",
                signal.rsi, signal.close_price, pos.position_id, pos.entry_price,
            )

            fill = self._simulator.sell(
                conn,
                position_id=pos.position_id,
                intent_price=signal.close_price,
                signal_id=signal_id,
                strategy_id=self._strategy.STRATEGY_ID,
            )

            if fill.success:
                acted = True
                # Compute realized PnL for notification
                pnl_usd = fill.fill_value_usd - pos.entry_value_usd
                log.info(
                    "Sell filled: qty=%.6f @ $%.2f pnl=$%+.2f",
                    fill.qty, fill.fill_price, pnl_usd,
                )
                self._notifier.notify_trade(fill, pnl_usd=pnl_usd)
            else:
                log.warning("Sell failed for position %s: %s", pos.position_id, fill.notes)

        if acted:
            mark_signal_acted(conn, signal_id)
        else:
            mark_signal_expired(conn, signal_id)
