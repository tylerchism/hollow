"""Portfolio tracker — V0 stub. Records snapshots but does not query live positions."""

import json
import logging
import sqlite3

log = logging.getLogger("trader.portfolio")


class PortfolioTracker:
    """
    Tracks portfolio value over time.

    V0: stub implementation — records the fact that a snapshot was requested
    but does not retrieve real position data from brokers. Strategy logic and
    real position tracking will be added in a future version.
    """

    def __init__(self, config: dict) -> None:
        self.config = config
        self.mode = config.get("mode", "paper")

    def snapshot(self, conn: sqlite3.Connection) -> int:
        """
        Write a stub portfolio snapshot to the portfolio_snapshots table.

        In V0, all position values are None/empty — this establishes the
        table row structure and confirms the snapshot cadence is working.

        Returns:
            The rowid of the inserted snapshot.
        """
        sql = """
            INSERT INTO portfolio_snapshots
                (mode, total_value_usd, cash_usd, positions, pnl_usd, pnl_pct)
            VALUES (?, NULL, NULL, ?, NULL, NULL)
        """
        with conn:
            cursor = conn.execute(sql, (self.mode, json.dumps({})))
            rowid = cursor.lastrowid

        log.info(
            "Portfolio snapshot recorded (V0 stub) — rowid=%d, mode=%s",
            rowid, self.mode,
        )
        return rowid
