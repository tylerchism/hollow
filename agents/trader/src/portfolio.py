"""
Portfolio value tracker stub.
V0: returns zeroed snapshot. V1 will query Alpaca/Binance for live balances.
"""
from datetime import datetime, timezone


def get_portfolio_value(config: dict) -> dict:
    """
    Stub: returns an empty portfolio snapshot.
    Will be populated in V1 with live balance queries.
    """
    return {
        "snapshot_at": datetime.now(tz=timezone.utc).isoformat(),
        "mode": config["mode"],
        "total_value_usd": 0.0,
        "cash_usd": 0.0,
        "positions": {},
        "source": "stub",
    }


def get_crypto_portfolio(exchange, config: dict) -> dict:
    """
    Stub: will query Binance balance in V1.
    """
    return {
        "snapshot_at": datetime.now(tz=timezone.utc).isoformat(),
        "mode": config["mode"],
        "total_value_usd": 0.0,
        "cash_usd": 0.0,
        "positions": {},
        "source": "binance",
    }


def get_equity_portfolio(trading_client, config: dict) -> dict:
    """
    Stub: will query Alpaca account balance in V1.
    """
    return {
        "snapshot_at": datetime.now(tz=timezone.utc).isoformat(),
        "mode": config["mode"],
        "total_value_usd": 0.0,
        "cash_usd": 0.0,
        "positions": {},
        "source": "alpaca",
    }
