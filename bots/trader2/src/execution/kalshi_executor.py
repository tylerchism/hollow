"""Kalshi order execution — REST API client for order placement.

Phase 1: Places orders against the Kalshi demo API (paper mode).
All order placement is real (demo API = real mechanics, fake money).
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
import uuid
from typing import Optional, Tuple

log = logging.getLogger(__name__)

DEMO_REST_BASE = "https://demo-api.kalshi.co/trade-api/v2"
LIVE_REST_BASE = "https://api.elections.kalshi.com/trade-api/v2"


class KalshiExecutor:
    """
    Places and manages Kalshi orders via REST API.
    Paper mode uses demo-api.kalshi.co.
    """

    def __init__(self, config: dict, paper_mode: bool = True):
        self._paper_mode = paper_mode
        self._rest_base = DEMO_REST_BASE if paper_mode else LIVE_REST_BASE
        # Use mode-specific key: KALSHI_DEMO_API_KEY (paper) or KALSHI_LIVE_API_KEY (live).
        # KALSHI_API_KEY is kept as a last-resort fallback for backwards compatibility.
        if paper_mode:
            demo_key = os.environ.get("KALSHI_DEMO_API_KEY", "")
            fallback_key = os.environ.get("KALSHI_API_KEY", "")
            if not demo_key and fallback_key:
                log.warning(
                    "kalshi: KALSHI_DEMO_API_KEY not set — falling back to KALSHI_API_KEY for paper mode"
                )
            self._api_key = demo_key or fallback_key
        else:
            live_key = os.environ.get("KALSHI_LIVE_API_KEY", "")
            fallback_key = os.environ.get("KALSHI_API_KEY", "")
            if not live_key and fallback_key:
                log.warning(
                    "kalshi: KALSHI_LIVE_API_KEY not set — falling back to KALSHI_API_KEY for live mode"
                )
            self._api_key = live_key or fallback_key

        exec_cfg = config.get("execution", {})
        self._fill_timeout_seconds: float = exec_cfg.get("kalshi_fill_timeout_seconds", 120.0)
        self._fill_poll_seconds: float = exec_cfg.get("kalshi_fill_poll_seconds", 5.0)

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self._api_key:
            h["Authorization"] = f"Bearer {self._api_key}"
        return h

    def _request(
        self,
        method: str,
        path: str,
        data: Optional[dict] = None,
    ) -> Tuple[Optional[dict], Optional[str]]:
        url = self._rest_base + path
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(url, data=body, method=method, headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode()), None
        except urllib.error.HTTPError as e:
            body_text = e.read().decode() if e.fp else ""
            return None, f"HTTP {e.code}: {body_text}"
        except Exception as e:
            return None, str(e)

    def place_order(
        self,
        ticker: str,
        action: str,
        side: str,
        count: int,
        yes_price_cents: int,
    ) -> Tuple[Optional[str], Optional[str]]:
        """
        Place a limit order on Kalshi.

        ticker: market ticker (e.g., "FED-26MAR-T4.25")
        action: "buy" | "sell"
        side: "yes" | "no"
        count: number of contracts
        yes_price_cents: integer 1-99

        Returns (order_id, error_str).
        """
        client_order_id = f"trader2-{uuid.uuid4().hex}"
        payload = {
            "ticker": ticker,
            "action": action,
            "side": side,
            "type": "limit",
            "count": count,
            "yes_price": yes_price_cents,
            "client_order_id": client_order_id,
        }

        log.info(
            "kalshi: placing %s %s %s count=%d price=%d [%s]",
            action,
            side,
            ticker,
            count,
            yes_price_cents,
            "PAPER" if self._paper_mode else "LIVE",
        )

        data, err = self._request("POST", "/portfolio/orders", payload)
        if err:
            log.error("kalshi: place_order failed: %s", err)
            return None, err

        order = data.get("order", {})
        order_id = order.get("order_id")
        log.info("kalshi: order placed order_id=%s status=%s", order_id, order.get("status"))
        return order_id, None

    def poll_fill(self, order_id: str) -> Tuple[str, int, Optional[str]]:
        """
        Poll order status until filled, cancelled, expired, or timeout.

        Returns (status, filled_count, error_str).
        status: "filled" | "cancelled" | "expired" | "timeout" | "error"
        """
        deadline = time.time() + self._fill_timeout_seconds

        while time.time() < deadline:
            data, err = self._request("GET", f"/portfolio/orders/{order_id}")
            if err:
                return "error", 0, err

            order = data.get("order", {})
            status = order.get("status", "")
            filled_count = order.get("filled_count", 0)

            if status in ("filled", "cancelled", "expired"):
                log.info("kalshi: order %s status=%s filled=%d", order_id, status, filled_count)
                return status, filled_count, None

            time.sleep(self._fill_poll_seconds)

        # Timeout — cancel the order
        log.warning("kalshi: order %s timed out, cancelling", order_id)
        self.cancel_order(order_id)
        return "timeout", 0, None

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an order. Returns True on success."""
        _, err = self._request("DELETE", f"/portfolio/orders/{order_id}")
        if err:
            log.error("kalshi: cancel_order %s failed: %s", order_id, err)
            return False
        log.info("kalshi: cancelled order %s", order_id)
        return True

    def get_balance(self) -> Optional[float]:
        """Get current demo/live balance in USD."""
        data, err = self._request("GET", "/portfolio/balance")
        if err:
            log.warning("kalshi: get_balance failed: %s", err)
            return None
        return data.get("balance", {}).get("available_balance_cents", 0) / 100.0

    def get_open_order_ids(self) -> list:
        """Return list of order_id strings for all resting open orders."""
        data, err = self._request("GET", "/portfolio/orders", )
        if err:
            log.warning("kalshi: get_open_order_ids failed: %s", err)
            return []
        orders = data.get("orders", []) if data else []
        return [
            o["order_id"]
            for o in orders
            if o.get("status") in ("resting", "open", "pending")
            and o.get("order_id")
        ]

    def cancel_all_open_orders(self) -> int:
        """Cancel all resting open orders on Kalshi. Returns number cancelled."""
        order_ids = self.get_open_order_ids()
        if not order_ids:
            log.info("kalshi: no open orders to cancel")
            return 0
        cancelled = 0
        for order_id in order_ids:
            if self.cancel_order(order_id):
                cancelled += 1
        log.info("kalshi: cancelled %d / %d open orders", cancelled, len(order_ids))
        return cancelled
