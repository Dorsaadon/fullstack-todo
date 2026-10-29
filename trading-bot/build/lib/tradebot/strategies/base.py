"""Strategy interface.

A strategy is a pure decision function: it sees a MarketSnapshot and returns
TradePlans. It never places orders itself, so every order passes the risk
manager, and the same strategy code runs in demo, paper and live modes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from decimal import Decimal

from ..models import MarketSnapshot, Order, TradePlan


class Strategy(ABC):
    def __init__(self, name: str, fees: Mapping[str, Decimal] | None = None):
        self.name = name
        self.fees = dict(fees or {})  # venue -> taker fee fraction

    def fee(self, venue: str) -> Decimal:
        return self.fees.get(venue, Decimal("0"))

    @abstractmethod
    def subscriptions(self) -> list[tuple[str, str]]:
        """(venue, symbol) pairs whose order books this strategy needs."""

    @abstractmethod
    def on_snapshot(self, snap: MarketSnapshot) -> list[TradePlan]: ...

    def on_orders(self, plan: TradePlan, orders: list[Order]) -> None:
        """Called after a plan executes. Override to track state."""

    # --- persistence (see state.py) ------------------------------------------

    def get_state(self) -> dict | None:
        """JSON-serializable state to survive a restart. None if stateless."""
        return None

    def set_state(self, state: dict) -> None:
        """Restore what get_state returned."""

    def positions(self) -> dict[tuple[str, str], Decimal]:
        """Positions this strategy opened and must still close: (venue, symbol)
        -> signed size in base units. Checked against the broker at startup."""
        return {}

    def set_position(self, venue: str, symbol: str, size: Decimal) -> None:
        """Correct a tracked position after reconciliation found less at the broker."""


def bps(x: Decimal) -> Decimal:
    return x * Decimal("10000")
