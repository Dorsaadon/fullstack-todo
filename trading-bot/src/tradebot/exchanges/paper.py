"""Paper trading: real (or synthetic) market data, simulated execution.

Wrap a live adapter as the data source to paper trade against real order books
with zero risk. Fills walk the visible book, so slippage on size is modelled;
queue position and latency are not, so treat paper P&L as an upper bound.

Short selling (`allow_short=True`) follows how a US margin account works:
  * selling more than you hold opens a short; the proceeds are credited to cash;
  * Reg T initial margin: cash must cover (1 + `short_initial_margin`) times
    the value of all open shorts, 150% by default. Orders that would breach it
    are cut down (IOC) or rejected (FOK). Long holdings are not counted as
    collateral, which is stricter than a real broker;
  * a borrow fee accrues on short value at `borrow_rate_annual`, charged to cash;
  * buying covers the short first.
Only assets quoted in this venue's `quote_currency` can be shorted. Forced
liquidation on a maintenance-margin breach is not modelled; the risk manager's
position and daily-loss limits stop you well before that.
"""

from __future__ import annotations

import time
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
        allow_short: bool = False,
        short_initial_margin: Decimal = Decimal("0.5"),
        borrow_rate_annual: Decimal = Decimal("0"),
        clock=time.time,
    ):
        super().__init__(name, taker_fee, quote_currency)
        self.data_source = data_source
        self.balances: dict[str, Decimal] = defaultdict(lambda: ZERO, balances or {})
        self._books: dict[str, OrderBook] = {}
        self.orders: list[Order] = []
        self.allow_short = allow_short
        self.short_initial_margin = short_initial_margin
        self.borrow_rate_annual = borrow_rate_annual
        self.borrow_fees_paid = ZERO
        self.clock = clock
        self._last_px: dict[str, Decimal] = {}  # asset -> last fill price, for valuing shorts
        self._accrued_to = clock()

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
        self._accrue_borrow()
        return {k: v for k, v in self.balances.items() if v != 0}

    async def cancel_order(self, order_id: str, symbol: str) -> None:
        return None  # orders never rest in the simulator

    async def place_order(self, req: OrderRequest) -> Order:
        order = Order(request=req, id=f"paper-{uuid.uuid4().hex[:12]}")
        self.orders.append(order)
        book = self._books.get(req.symbol)
        if book is None:
            return self._reject(order, "no market data")

        self._accrue_borrow()
        base, quote = self.assets(req.symbol)
        if req.side is Side.SELL and self.allow_short and quote != self.quote_currency:
            return self._reject(order, f"can only short assets quoted in {self.quote_currency}")
        size = req.size
        if req.side is Side.SELL and not self.allow_short:
            size = min(size, max(self.balances[base], ZERO))  # long only: never sell what you don't own
        filled, cost = book.sweep(req.side, size, req.price)
        if filled > 0 and not self._feasible(self._after(req.side, base, quote, filled, cost), quote):
            filled, cost = self._largest_feasible(book, req, base, quote, filled)

        if filled == 0:
            return self._reject(order, "insufficient balance, margin or liquidity at limit")
        if req.tif is TimeInForce.FOK and filled < req.size:
            return self._reject(order, "FOK not fully fillable")

        fee = cost * self.taker_fee
        if req.side is Side.BUY:
            self.balances[base] += filled
            self.balances[quote] -= cost + fee
        else:
            self.balances[base] -= filled
            self.balances[quote] += cost - fee

        self._last_px[base] = cost / filled
        order.filled = filled
        order.avg_price = cost / filled
        order.fee = fee
        order.status = OrderStatus.FILLED if filled >= req.size else OrderStatus.CANCELED
        return order

    # --- balance and margin checks -------------------------------------------

    def _price(self, asset: str) -> Decimal:
        """Current value of one unit of `asset` in the quote currency."""
        for book in self._books.values():
            base, quote = self.assets(book.symbol)
            if base == asset and quote == self.quote_currency and book.mid is not None:
                return book.mid
        return self._last_px.get(asset, ZERO)

    def short_value(self, balances: dict[str, Decimal] | None = None) -> Decimal:
        b = self.balances if balances is None else balances
        return sum((-amt * self._price(a) for a, amt in b.items() if amt < 0), ZERO)

    def _feasible(self, balances: dict[str, Decimal], paid_in: str) -> bool:
        """The currency a trade pays in never goes negative (shorts only come
        from selling), nothing goes negative without shorting, and open
        shorts stay within initial margin."""
        if balances.get(paid_in, ZERO) < 0 or balances.get(self.quote_currency, ZERO) < 0:
            return False
        if not self.allow_short:
            return all(amt >= 0 for amt in balances.values())
        return balances.get(self.quote_currency, ZERO) >= self.short_value(balances) * (1 + self.short_initial_margin)

    def _after(self, side: Side, base: str, quote: str, filled: Decimal, cost: Decimal) -> dict[str, Decimal]:
        """Balances as they would be after this fill."""
        after = dict(self.balances)
        fee = cost * self.taker_fee
        sign = 1 if side is Side.BUY else -1
        after[base] = after.get(base, ZERO) + sign * filled
        after[quote] = after.get(quote, ZERO) - sign * cost - fee
        return after

    def _largest_feasible(
        self, book: OrderBook, req: OrderRequest, base: str, quote: str, upper: Decimal
    ) -> tuple[Decimal, Decimal]:
        """Biggest fill that keeps balances valid. Feasibility shrinks
        monotonically with size, so bisect on it."""
        lo, hi = ZERO, upper
        for _ in range(60):
            mid = (lo + hi) / 2
            _, cost = book.sweep(req.side, mid, req.price)
            if self._feasible(self._after(req.side, base, quote, mid, cost), quote):
                lo = mid
            else:
                hi = mid
        return book.sweep(req.side, lo, req.price)

    def _accrue_borrow(self) -> None:
        now = self.clock()
        elapsed, self._accrued_to = now - self._accrued_to, now
        if self.borrow_rate_annual and elapsed > 0:
            fee = self.short_value() * self.borrow_rate_annual * Decimal(elapsed) / Decimal(365 * 86400)
            if fee:
                self.balances[self.quote_currency] -= fee
                self.borrow_fees_paid += fee

    @staticmethod
    def _reject(order: Order, why: str) -> Order:
        order.status = OrderStatus.REJECTED
        order.error = why
        return order
