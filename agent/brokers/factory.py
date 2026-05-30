"""
Broker factory — return the right BrokerClient based on config or env var.

Usage:
    broker = create_broker()           # reads BROKER env var or config
    broker = create_broker("alpaca")   # explicit
    broker = create_broker("robinhood")
"""
from __future__ import annotations

import os

from loguru import logger

from agent.config import CONFIG
from .base import BrokerClient


def create_broker(broker_name: str | None = None) -> BrokerClient:
    """
    Instantiate and return the configured broker client.

    Priority: explicit arg > BROKER env var > config broker.name > "alpaca"
    """
    name = (
        broker_name
        or os.getenv("BROKER")
        or CONFIG.get("broker", {}).get("name", "alpaca")
    ).lower()

    if name == "alpaca":
        from .alpaca_broker import AlpacaBroker
        logger.info("Broker: Alpaca Markets")
        return AlpacaBroker()

    if name == "robinhood":
        from .robinhood_broker import RobinhoodBroker
        logger.info("Broker: Robinhood (robin_stocks)")
        return RobinhoodBroker()

    raise ValueError(
        f"Unknown broker '{name}'. Supported: alpaca, robinhood. "
        "Set BROKER env var or config broker.name."
    )
