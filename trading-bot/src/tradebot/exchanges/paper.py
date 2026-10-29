"""Paper trading: real (or synthetic) market data, simulated execution.

Wrap a live adapter as the data source to paper trade against real order books
with zero risk. Fills walk the visible book, so slippage on size is modelled;
queue position and latency are not, so treat paper P&L as an upper bound.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import AsyncIterator
from decimal import Decimal

from ..models import ZERO, Order, OrderBook, OrderRequest, OrderStatus, Side, TimeInForce
from .base import Exchange


class PaperExchange(Exchange):
    def __init__(
        self,
        name: str,
        data_source: Exchange | None = None,
        balances: dict[str, Decimal] | None = None,
        taker_fee: Decimal = Decimal("0"),
        quote_currency: str = "USD",
    ):
        super().__init__(name, taker_fee, quote_currency)
        self.data_source = data_source
        self.balances: dict[str, Decimal] = defaultdict(lambda: ZERO, balances or {})
        self._books: dict[str, OrderBook] = {}
        self.orders: list[Order] = []

    def set_order_book(self, book: OrderBook) -> None:
        """Inject a book directly (tests, demos, replaying recorded data)."""
        self._books[book.symbol] = book

    async def connect(self) -> None:
        if self.data_source:
            await self.data_source.connect()

    async def close(self) -> None:
        if self.data_source:
            await self.data_source.close()

    async def fetch_order_book(self, symbol: str, depth: int = 10) -> OrderBook:
        if self.data_source:
            book = await self.data_source.fetch_order_book(symbol, depth)
            # Re-label so the rest of the system sees this venue's name.
            self._books[symbol] = OrderBook(self.name, symbol, book.bids, book.asks, book.ts)
        if symbol not in self._books:
            raise KeyError(f"{self.name}: no order book for {symbol}")
        return self._books[symbol]

    async def watch_order_books(self, symbols: list[str], depth: int = 10) -> AsyncIterator[OrderBook | None]:
        if self.data_source is None:
            async for item in super().watch_order_books(symbols, depth):
                yield item
            return
        self.data_source.poll_interval_s = self.poll_interval_s
        async for book in self.data_source.watch_order_books(symbols, depth):
            if book is None:
                yield None
                continue
            # Streamed books drive simulated fills, exactly like fetched ones.
            mine = OrderBook(self.name, book.symbol, book.bids, book.asks, book.ts)
            self._books[book.symbol] = mine
            yield mine

    async def fetch_balances(self) -> dict[str, Decimal]:
        return {k: v for k, v in self.balances.items() if v != 0}

    async def cancel_order(self, order_id: str, symbol: str) -> None:
        return None  # orders never rest in the simulator

    async def place_order(self, req: OrderRequest) -> Order:
        order = Order(request=req, id=f"paper-{uuid.uuid4().hex[:12]}")
        self.orders.append(order)
        book = self._books.get(req.symbol)
        if book is None:
            return self._reject(order, "no market data")

        base, quote = self.assets(req.symbol)
        size = req.size
        # Cap size by what we can afford so the simulator never goes negative.
        if req.side is Side.SELL:
            size = min(size, self.balances[base])
        filled, cost = book.sweep(req.side, size, req.price)
        if req.side is Side.BUY and filled > 0:
            budget = self.balances[quote]
            if cost * (1 + self.taker_fee) > budget:
                filled, cost = self._affordable(book, req, budget)

        if filled == 0:
            return self._reject(order, "insufficient balance or no liquidity at limit")
        if req.tif is TimeInForce.FOK and filled < req.size:
            return self._reject(order, "FOK not fully fillable")

        fee = cost * self.taker_fee
        if req.side is Side.BUY:
            self.balances[base] += filled
            self.balances[quote] -= cost + fee
        else:
            self.balances[base] -= filled
            self.balances[quote] += cost - fee

        order.filled = filled
        order.avg_price = cost / filled
        order.fee = fee
        order.status = OrderStatus.FILLED if filled >= req.size else OrderStatus.CANCELED
        return order

    def _affordable(self, book: OrderBook, req: OrderRequest, budget: Decimal) -> tuple[Decimal, Decimal]:
        filled, cost = ZERO, ZERO
        for lv in book.asks:
            if req.price is not None and lv.price > req.price:
                break
            room = (budget / (1 + self.taker_fee)) - cost
            take = min(lv.size, req.size - filled, room / lv.price)
            if take <= 0:
                break
            filled += take
            cost += take * lv.price
        return filled, cost

    @staticmethod
    def _reject(order: Order, why: str) -> Order:
        order.status = OrderStatus.REJECTED
        order.error = why
        return order
