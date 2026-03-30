"""Paper trading simulator — V1.

Simulates order execution with realistic slippage.
Tracks cash and open positions in memory and syncs to DB.

Key design constraints (from spike doc):
    - Paper mode uses identical code path as live; only the "place order" step
      differs (simulator vs real exchange).
    - All trades are written to paper_trades BEFORE the fill is applied.
    - Slippage: 0.1% per side (market order assumption).
    - Position sizing: controlled by RiskManager before this class is called.
"""

import logging
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

log = logging.getLogger("trader.paper.simulator")

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

@dataclass
class Position:
    position_id: str
    strategy_id: str
    symbol: str
    qty: float
    entry_price: float
    entry_value_usd: float
    opened_at: str
    db_id: int | None = None  # paper_positions.id after DB insert

    @property
    def current_value(self, price: float = 0.0) -> float:
        return self.qty * price

    def unrealized_pnl(self, current_price: float) -> tuple[float, float]:
        """Return (pnl_usd, pnl_pct)."""
        value = self.qty * current_price
        pnl_usd = value - self.entry_value_usd
        pnl_pct = (pnl_usd / self.entry_value_usd) * 100.0 if self.entry_value_usd else 0.0
        return pnl_usd, pnl_pct


@dataclass
class FillResult:
    success: bool
    side: str
    qty: float
    intent_price: float
    fill_price: float
    slippage_pct: float
    fill_value_usd: float
    position_id: str
    trade_id: int | None = None
    notes: str = ""


# ---------------------------------------------------------------------------
# Simulator
# ---------------------------------------------------------------------------

class PaperSimulator:
    """
    Paper trading simulator.

    State lives in memory (cash, open_positions dict) and is persisted to SQLite.
    On startup, restores state from the DB so restarts are safe.

    Config keys (under config["paper"]):
        initial_cash_usd    float   default 10000.0
        slippage_pct        float   default 0.001 (0.1% per side)
    """

    def __init__(self, config: dict) -> None:
        self.config = config
        paper_cfg = config.get("paper", {})
        self._initial_cash = float(paper_cfg.get("initial_cash_usd", 10_000.0))
        self._slippage_pct = float(paper_cfg.get("slippage_pct", 0.001))
        self._strategy_id = config.get("strategy", {}).get("rsi", {}).get("symbol", "rsi_mean_reversion_v1")

        self.cash: float = self._initial_cash
        self.open_positions: dict[str, Position] = {}  # position_id → Position
        self._initialized = False

    def restore_state(self, conn: sqlite3.Connection) -> None:
        """
        Restore cash and open positions from DB.
        Called once at engine startup.
        """
        # Get most recent portfolio snapshot for cash balance
        snap = conn.execute(
            "SELECT cash_usd FROM portfolio_snapshots WHERE mode = 'paper' ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        if snap and snap["cash_usd"] is not None:
            self.cash = float(snap["cash_usd"])
            log.info("Restored paper cash from DB: $%.2f", self.cash)
        else:
            self.cash = self._initial_cash
            log.info("No prior snapshot found — starting with initial cash: $%.2f", self.cash)

        # Restore open positions
        rows = conn.execute(
            "SELECT * FROM paper_positions WHERE status = 'open'"
        ).fetchall()
        for row in rows:
            pos = Position(
                position_id=row["position_id"],
                strategy_id=row["strategy_id"],
                symbol=row["symbol"],
                qty=float(row["qty"]),
                entry_price=float(row["entry_price"]),
                entry_value_usd=float(row["entry_value_usd"]),
                opened_at=row["opened_at"],
                db_id=int(row["id"]),
            )
            self.open_positions[pos.position_id] = pos

        log.info(
            "Restored %d open paper positions from DB",
            len(self.open_positions),
        )
        self._initialized = True

    # ------------------------------------------------------------------
    # Order execution
    # ------------------------------------------------------------------

    def buy(
        self,
        conn: sqlite3.Connection,
        symbol: str,
        intent_price: float,
        usd_amount: float,
        signal_id: int | None = None,
        strategy_id: str = "rsi_mean_reversion_v1",
    ) -> FillResult:
        """
        Execute a paper buy order.

        Args:
            conn:           Open DB connection.
            symbol:         Trading pair, e.g. "BTC/USDT".
            intent_price:   Price at signal time.
            usd_amount:     USD amount to spend (from risk manager sizing).
            signal_id:      FK to signals table row.
            strategy_id:    Strategy identifier.

        Returns:
            FillResult describing the simulated fill.
        """
        if usd_amount <= 0:
            return FillResult(
                success=False, side="buy", qty=0, intent_price=intent_price,
                fill_price=intent_price, slippage_pct=0, fill_value_usd=0,
                position_id="", notes="usd_amount <= 0",
            )

        if usd_amount > self.cash:
            return FillResult(
                success=False, side="buy", qty=0, intent_price=intent_price,
                fill_price=intent_price, slippage_pct=0, fill_value_usd=0,
                position_id="",
                notes=f"insufficient cash: have ${self.cash:.2f}, need ${usd_amount:.2f}",
            )

        # Slippage: buy fills slightly higher
        fill_price = intent_price * (1.0 + self._slippage_pct)
        qty = usd_amount / fill_price
        fill_value = qty * fill_price
        position_id = str(uuid.uuid4())
        now_iso = datetime.now(timezone.utc).isoformat()

        # --- Append-only trade log BEFORE applying state ---
        trade_id = self._log_paper_trade(
            conn, strategy_id, symbol, "buy", qty,
            intent_price, fill_price, self._slippage_pct,
            fill_value, "filled", signal_id, position_id,
        )

        # --- Apply state ---
        self.cash -= fill_value

        pos = Position(
            position_id=position_id,
            strategy_id=strategy_id,
            symbol=symbol,
            qty=qty,
            entry_price=fill_price,
            entry_value_usd=fill_value,
            opened_at=now_iso,
        )
        db_id = self._persist_position_open(conn, pos)
        pos.db_id = db_id
        self.open_positions[position_id] = pos

        self._snapshot_portfolio(conn, symbol, intent_price)

        log.info(
            "PAPER BUY  %s qty=%.6f @ fill=%.2f (intent=%.2f slippage=%.3f%%) "
            "cash_remaining=$%.2f",
            symbol, qty, fill_price, intent_price,
            self._slippage_pct * 100, self.cash,
        )

        return FillResult(
            success=True, side="buy", qty=qty,
            intent_price=intent_price, fill_price=fill_price,
            slippage_pct=self._slippage_pct,
            fill_value_usd=fill_value,
            position_id=position_id,
            trade_id=trade_id,
        )

    def sell(
        self,
        conn: sqlite3.Connection,
        position_id: str,
        intent_price: float,
        signal_id: int | None = None,
        strategy_id: str = "rsi_mean_reversion_v1",
    ) -> FillResult:
        """
        Close an open paper position.

        Args:
            conn:           Open DB connection.
            position_id:    UUID of the position to close.
            intent_price:   Current price at signal time.
            signal_id:      FK to signals table row.
            strategy_id:    Strategy identifier.

        Returns:
            FillResult describing the simulated fill.
        """
        pos = self.open_positions.get(position_id)
        if pos is None:
            return FillResult(
                success=False, side="sell", qty=0, intent_price=intent_price,
                fill_price=intent_price, slippage_pct=0, fill_value_usd=0,
                position_id=position_id,
                notes=f"position {position_id} not found in open_positions",
            )

        # Slippage: sell fills slightly lower
        fill_price = intent_price * (1.0 - self._slippage_pct)
        fill_value = pos.qty * fill_price
        pnl_usd = fill_value - pos.entry_value_usd
        pnl_pct = (pnl_usd / pos.entry_value_usd) * 100.0 if pos.entry_value_usd else 0.0
        now_iso = datetime.now(timezone.utc).isoformat()

        # --- Append-only trade log BEFORE applying state ---
        trade_id = self._log_paper_trade(
            conn, strategy_id, pos.symbol, "sell", pos.qty,
            intent_price, fill_price, self._slippage_pct,
            fill_value, "filled", signal_id, position_id,
        )

        # --- Apply state ---
        self.cash += fill_value
        del self.open_positions[position_id]
        self._persist_position_close(conn, position_id, fill_price, fill_value, pnl_usd, pnl_pct, now_iso)
        self._snapshot_portfolio(conn, pos.symbol, intent_price)

        log.info(
            "PAPER SELL %s qty=%.6f @ fill=%.2f (intent=%.2f slippage=%.3f%%) "
            "pnl=$%.2f (%.2f%%) cash=$%.2f",
            pos.symbol, pos.qty, fill_price, intent_price,
            self._slippage_pct * 100, pnl_usd, pnl_pct, self.cash,
        )

        return FillResult(
            success=True, side="sell", qty=pos.qty,
            intent_price=intent_price, fill_price=fill_price,
            slippage_pct=self._slippage_pct,
            fill_value_usd=fill_value,
            position_id=position_id,
            trade_id=trade_id,
            notes=f"pnl=${pnl_usd:.2f} ({pnl_pct:.2f}%)",
        )

    def portfolio_value(self, prices: dict[str, float] | None = None) -> float:
        """
        Compute total portfolio value: cash + mark-to-market open positions.

        Args:
            prices: dict of symbol → current price. If None, uses entry prices.
        """
        prices = prices or {}
        position_value = sum(
            pos.qty * prices.get(pos.symbol, pos.entry_price)
            for pos in self.open_positions.values()
        )
        return self.cash + position_value

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------

    def _log_paper_trade(
        self,
        conn: sqlite3.Connection,
        strategy_id: str,
        symbol: str,
        side: str,
        qty: float,
        intent_price: float,
        fill_price: float,
        slippage_pct: float,
        fill_value_usd: float,
        status: str,
        signal_id: int | None,
        position_id: str,
        notes: str = "",
    ) -> int:
        """Append-only trade record written BEFORE fill is applied."""
        sql = """
            INSERT INTO paper_trades
                (strategy_id, symbol, side, qty, intent_price, fill_price,
                 slippage_pct, fill_value_usd, status, signal_id, position_id, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        with conn:
            cursor = conn.execute(sql, (
                strategy_id, symbol, side, qty, intent_price, fill_price,
                slippage_pct, fill_value_usd, status, signal_id, position_id, notes,
            ))
            return cursor.lastrowid

    def _persist_position_open(self, conn: sqlite3.Connection, pos: Position) -> int:
        sql = """
            INSERT INTO paper_positions
                (position_id, strategy_id, symbol, side, qty,
                 entry_price, entry_value_usd, opened_at, status)
            VALUES (?, ?, ?, 'long', ?, ?, ?, ?, 'open')
        """
        with conn:
            cursor = conn.execute(sql, (
                pos.position_id, pos.strategy_id, pos.symbol,
                pos.qty, pos.entry_price, pos.entry_value_usd, pos.opened_at,
            ))
            return cursor.lastrowid

    def _persist_position_close(
        self, conn: sqlite3.Connection, position_id: str,
        exit_price: float, exit_value_usd: float,
        pnl_usd: float, pnl_pct: float, closed_at: str,
    ) -> None:
        sql = """
            UPDATE paper_positions
            SET status = 'closed', closed_at = ?, exit_price = ?,
                exit_value_usd = ?, pnl_usd = ?, pnl_pct = ?
            WHERE position_id = ?
        """
        with conn:
            conn.execute(sql, (closed_at, exit_price, exit_value_usd, pnl_usd, pnl_pct, position_id))

    def _snapshot_portfolio(
        self, conn: sqlite3.Connection, symbol: str, current_price: float
    ) -> None:
        """Write a portfolio_snapshots row with current state."""
        import json
        prices = {symbol: current_price}
        total = self.portfolio_value(prices)
        positions_json = json.dumps({
            pid: {
                "symbol": pos.symbol, "qty": pos.qty,
                "entry_price": pos.entry_price,
                "current_price": prices.get(pos.symbol, pos.entry_price),
            }
            for pid, pos in self.open_positions.items()
        })
        initial = self._initial_cash
        pnl_usd = total - initial
        pnl_pct = (pnl_usd / initial) * 100.0 if initial else 0.0
        sql = """
            INSERT INTO portfolio_snapshots
                (mode, total_value_usd, cash_usd, positions, pnl_usd, pnl_pct)
            VALUES ('paper', ?, ?, ?, ?, ?)
        """
        with conn:
            conn.execute(sql, (total, self.cash, positions_json, pnl_usd, pnl_pct))
