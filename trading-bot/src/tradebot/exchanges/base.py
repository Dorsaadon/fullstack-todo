"""The one interface every venue adapter implements.

Strategies and the engine only ever talk to `Exchange`, so adding a venue means
writing one adapter, and paper trading is just a different adapter.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal

from ..models import Order, OrderBook, OrderRequest, split_symbol


class Exchange(ABC):
    def __init__(self, name: str, taker_fee: Decimal = Decimal("0"), quote_currency: str = "USD"):
        self.name = name
        self.taker_fee = taker_fee  # fraction, e.g. 0.001 = 10 bps
        self.quote_currency = quote_currency  # used for symbols without a "/"

    def assets(self, symbol: str) -> tuple[str, str]:
        return split_symbol(symbol, self.quote_currency)

    async def connect(self) -> None:
        """Open sessions, load markets, authenticate. Optional."""

    async def close(self) -> None:
        """Release network resources. Optional."""

    @abstractmethod
    async def fetch_order_book(self, symbol: str, depth: int = 10) -> OrderBook: ...

    @abstractmethod
    async def place_order(self, req: OrderRequest) -> Order:
        """Submit an order and return its state.

        For IOC/FOK orders the returned Order must be terminal (filled amount
        known), because the engine uses it to detect legging risk.
        """

    @abstractmethod
    async def cancel_order(self, order_id: str, symbol: str) -> None: ...

    @abstractmethod
    async def fetch_balances(self) -> dict[str, Decimal]:
        """Free balance per asset, including share/token positions."""
