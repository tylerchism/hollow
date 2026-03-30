"""RSI mean reversion strategy — BTC/USDT 4h.

Strategy logic:
    - RSI(14) computed on 4h close prices (no external deps — pure Python)
    - BUY signal:  RSI crosses below or is under the oversold threshold (default 30)
    - SELL signal: RSI crosses above or is over the overbought threshold (default 70)
                   OR RSI crosses back above exit_rsi (default 50) from below
    - HOLD:        everything else

Signal freshness: callers should check signal.candle_ts against wall clock.
A signal is stale if wall clock > candle_ts + timeframe_seconds + max_signal_age_seconds.
The risk manager enforces stale-signal protection before any order is placed.

No external libraries required — RSI is computed from raw OHLCV rows.
"""

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

log = logging.getLogger("trader.strategy.rsi")


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

class SignalType(str, Enum):
    BUY  = "buy"
    SELL = "sell"
    HOLD = "hold"


@dataclass
class Signal:
    strategy_id: str
    symbol: str
    timeframe: str
    signal: SignalType
    rsi: float
    close_price: float
    candle_ts: str          # ISO8601 UTC timestamp of the candle
    generated_at: str       # ISO8601 UTC timestamp when signal was computed
    notes: str = ""


# ---------------------------------------------------------------------------
# RSI computation — pure Python, no numpy dependency
# ---------------------------------------------------------------------------

def _rsi(closes: list[float], period: int = 14) -> float | None:
    """
    Compute RSI(period) on a list of close prices using Wilder's smoothing.

    Requires at least period+1 values. Returns None if insufficient data.
    """
    if len(closes) < period + 1:
        return None

    # Use only the last (period * 3) candles for speed; more than enough for accuracy
    closes = closes[-(period * 3):]

    gains: list[float] = []
    losses: list[float] = []

    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        if diff > 0:
            gains.append(diff)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(abs(diff))

    # Seed with simple average of first `period` changes
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    # Wilder smoothing for remaining values
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------

class RSIStrategy:
    """
    RSI mean reversion strategy.

    Config keys (under config["strategy"]["rsi"]):
        symbol          str     default "BTC/USDT"
        timeframe       str     default "4h"
        period          int     default 14
        oversold        float   default 30.0   — buy signal threshold
        overbought      float   default 70.0   — sell signal threshold (entry overbought)
        exit_rsi        float   default 50.0   — close long when RSI returns to this level
        candles_needed  int     default 100    — number of recent candles to fetch
    """

    STRATEGY_ID = "rsi_mean_reversion_v1"

    def __init__(self, config: dict) -> None:
        self.config = config
        rsi_cfg = config.get("strategy", {}).get("rsi", {})

        self.symbol       = rsi_cfg.get("symbol", "BTC/USDT")
        self.timeframe    = rsi_cfg.get("timeframe", "4h")
        self.period       = int(rsi_cfg.get("period", 14))
        self.oversold     = float(rsi_cfg.get("oversold", 30.0))
        self.overbought   = float(rsi_cfg.get("overbought", 70.0))
        self.exit_rsi     = float(rsi_cfg.get("exit_rsi", 50.0))
        self.candles_needed = int(rsi_cfg.get("candles_needed", 100))

        self._prev_rsi: float | None = None  # track previous RSI for crossover detection

    def evaluate(self, conn: sqlite3.Connection) -> Signal:
        """
        Read recent OHLCV from DB, compute RSI, return a Signal.

        Note: this does NOT persist the signal — the engine does that after
        risk checks so the append-only log is clean.
        """
        from ..storage.ohlcv import query_ohlcv

        rows = query_ohlcv(
            conn,
            symbol=self.symbol,
            timeframe=self.timeframe,
            limit=self.candles_needed,
        )

        if not rows:
            log.warning(
                "RSIStrategy: no OHLCV data for %s/%s — returning HOLD",
                self.symbol, self.timeframe,
            )
            return self._hold_signal("no data in DB")

        # query_ohlcv returns newest first — reverse for chronological order
        rows_asc = list(reversed(rows))
        closes = [float(row["close"]) for row in rows_asc]
        latest = rows_asc[-1]
        candle_ts = latest["ts"]
        close_price = float(latest["close"])

        rsi_value = _rsi(closes, period=self.period)
        if rsi_value is None:
            log.warning(
                "RSIStrategy: insufficient data (%d candles) for RSI(%d) on %s/%s",
                len(closes), self.period, self.symbol, self.timeframe,
            )
            return self._hold_signal(f"only {len(closes)} candles, need {self.period + 1}")

        now_iso = datetime.now(timezone.utc).isoformat()
        signal_type = self._classify(rsi_value)

        log.info(
            "RSI(%d) = %.2f on %s/%s close=%.2f → %s",
            self.period, rsi_value, self.symbol, self.timeframe, close_price, signal_type.value,
        )

        sig = Signal(
            strategy_id=self.STRATEGY_ID,
            symbol=self.symbol,
            timeframe=self.timeframe,
            signal=signal_type,
            rsi=rsi_value,
            close_price=close_price,
            candle_ts=candle_ts,
            generated_at=now_iso,
            notes=f"RSI({self.period})={rsi_value:.2f}",
        )

        self._prev_rsi = rsi_value
        return sig

    def _classify(self, rsi: float) -> SignalType:
        """Map RSI value to a signal, considering previous RSI for crossover logic."""
        # Oversold → BUY
        if rsi <= self.oversold:
            return SignalType.BUY

        # Overbought → SELL (direct entry exit, also initial short trigger — V1 skips shorts)
        if rsi >= self.overbought:
            return SignalType.SELL

        # RSI crossed back above exit_rsi from below (long position exit signal)
        # Only relevant if a position is open — engine decides whether to act
        if (
            self._prev_rsi is not None
            and self._prev_rsi < self.exit_rsi
            and rsi >= self.exit_rsi
        ):
            return SignalType.SELL

        return SignalType.HOLD

    def _hold_signal(self, reason: str) -> Signal:
        now_iso = datetime.now(timezone.utc).isoformat()
        return Signal(
            strategy_id=self.STRATEGY_ID,
            symbol=self.symbol,
            timeframe=self.timeframe,
            signal=SignalType.HOLD,
            rsi=float("nan"),
            close_price=float("nan"),
            candle_ts=now_iso,
            generated_at=now_iso,
            notes=reason,
        )


# ---------------------------------------------------------------------------
# DB persistence helpers
# ---------------------------------------------------------------------------

def persist_signal(conn: sqlite3.Connection, sig: Signal) -> int:
    """
    Insert a signal into the signals table.

    Returns the rowid of the inserted row.
    Caller must ensure this is written BEFORE the engine acts on it.
    """
    sql = """
        INSERT INTO signals
            (strategy_id, symbol, timeframe, signal, rsi, close_price, candle_ts,
             generated_at, acted_on, expired, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?)
    """
    rsi_val = None if (sig.rsi != sig.rsi) else sig.rsi          # nan → None
    close_val = None if (sig.close_price != sig.close_price) else sig.close_price

    with conn:
        cursor = conn.execute(sql, (
            sig.strategy_id,
            sig.symbol,
            sig.timeframe,
            sig.signal.value,
            rsi_val,
            close_val,
            sig.candle_ts,
            sig.generated_at,
            sig.notes,
        ))
        return cursor.lastrowid


def mark_signal_acted(conn: sqlite3.Connection, signal_id: int) -> None:
    with conn:
        conn.execute("UPDATE signals SET acted_on = 1 WHERE id = ?", (signal_id,))


def mark_signal_expired(conn: sqlite3.Connection, signal_id: int) -> None:
    with conn:
        conn.execute("UPDATE signals SET expired = 1 WHERE id = ?", (signal_id,))
