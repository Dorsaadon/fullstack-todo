"""Core domain types shared by every venue, strategy and the engine.

All prices and sizes are Decimal: float rounding errors are unacceptable when
the edge you are hunting is a few basis points.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

ZERO = Decimal("0")
ONE = Decimal("1")

# Quote currencies treated as worth exactly 1 USD when valuing a portfolio.
STABLE_ASSETS = frozenset({"USD", "USDT", "USDC", "BUSD", "DAI", "FDUSD"})


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"

    @property
    def opposite(self) -> Side:
        return Side.SELL if self is Side.BUY else Side.BUY


class OrderType(str, Enum):
    LIMIT = "limit"
    MARKET = "market"


class TimeInForce(str, Enum):
    GTC = "gtc"  # rest on the book until cancelled
    IOC = "ioc"  # fill what you can immediately, cancel the rest
    FOK = "fok"  # fill everything immediately or nothing


class OrderStatus(str, Enum):
    NEW = "new"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"

    @property
    def is_terminal(self) -> bool:
        return self in (OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED)


def split_symbol(symbol: str, default_quote: str) -> tuple[str, str]:
    """'BTC/USDT' -> ('BTC', 'USDT'); 'AAPL' -> ('AAPL', default_quote)."""
    if "/" in symbol:
        base, quote = symbol.split("/", 1)
        return base, quote
    return symbol, default_quote


@dataclass(frozen=True)
class Level:
    price: Decimal
    size: Decimal


@dataclass(frozen=True)
class OrderBook:
    venue: str
    symbol: str
    bids: tuple[Level, ...]  # best (highest) first
    asks: tuple[Level, ...]  # best (lowest) first
    ts: float = field(default_factory=time.time)

    @classmethod
    def from_raw(cls, venue: str, symbol: str, bids, asks, ts: float | None = None) -> OrderBook:
        """Build a book from any iterable of (price, size) pairs, in any order."""
        def levels(raw, reverse: bool) -> tuple[Level, ...]:
            parsed = [Level(Decimal(str(p)), Decimal(str(s))) for p, s in raw]
            parsed = [lv for lv in parsed if lv.size > 0 and lv.price > 0]
            return tuple(sorted(parsed, key=lambda lv: lv.price, reverse=reverse))

        return cls(venue, symbol, levels(bids, True), levels(asks, False), ts if ts is not None else time.time())

    @property
    def best_bid(self) -> Level | None:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> Level | None:
        return self.asks[0] if self.asks else None

    @property
    def mid(self) -> Decimal | None:
        if not self.bids or not self.asks:
            return None
        return (self.bids[0].price + self.asks[0].price) / 2

    def age(self, now: float | None = None) -> float:
        return (now if now is not None else time.time()) - self.ts

    def levels_for(self, side: Side) -> tuple[Level, ...]:
        """The levels a taker on `side` consumes: buyers lift asks, sellers hit bids."""
        return self.asks if side is Side.BUY else self.bids

    def sweep(self, side: Side, size: Decimal, limit: Decimal | None = None) -> tuple[Decimal, Decimal]:
        """Simulate taking up to `size` as a `side` taker, never trading through `limit`.

        Returns (filled_size, total_cost) where cost is in quote currency.
        """
        remaining, filled, cost = size, ZERO, ZERO
        for lv in self.levels_for(side):
            if remaining <= 0:
                break
            if limit is not None and (lv.price > limit if side is Side.BUY else lv.price < limit):
                break
            take = min(remaining, lv.size)
            filled += take
            cost += take * lv.price
            remaining -= take
        return filled, cost

    def worst_price(self, side: Side, size: Decimal) -> Decimal | None:
        """Price of the deepest level a sweep of `size` touches: the IOC limit to use."""
        remaining = size
        for lv in self.levels_for(side):
            remaining -= lv.size
            if remaining <= 0:
                return lv.price
        return None

    def cumulative_sizes(self, side: Side) -> list[Decimal]:
        """Sizes at which a sweep moves to the next price level."""
        out, total = [], ZERO
        for lv in self.levels_for(side):
            total += lv.size
            out.append(total)
        return out

    def vwap(self, side: Side, size: Decimal) -> Decimal | None:
        """Average price to take `size` right now, or None if the book is too thin."""
        filled, cost = self.sweep(side, size)
        if filled < size or filled == 0:
            return None
        return cost / filled


@dataclass(frozen=True)
class OrderRequest:
    venue: str
    symbol: str
    side: Side
    size: Decimal
    price: Decimal | None = None  # None => market order
    tif: TimeInForce = TimeInForce.IOC
    strategy: str = ""
    client_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])

    @property
    def order_type(self) -> OrderType:
        return OrderType.MARKET if self.price is None else OrderType.LIMIT

    def notional(self, ref_price: Decimal | None = None) -> Decimal:
        px = self.price if self.price is not None else ref_price
        return self.size * px if px is not None else ZERO


@dataclass
class Order:
    request: OrderRequest
    id: str
    status: OrderStatus = OrderStatus.NEW
    filled: Decimal = ZERO
    avg_price: Decimal | None = None
    fee: Decimal = ZERO  # in quote currency
    error: str | None = None
    ts: float = field(default_factory=time.time)

    @property
    def fill_ratio(self) -> Decimal:
        return self.filled / self.request.size if self.request.size else ZERO


@dataclass(frozen=True)
class TradePlan:
    """One or more orders a strategy wants executed together.

    When `atomic` is True the engine treats the legs as a single trade: if they
    fill unevenly it unwinds the excess so you are not left holding naked
    inventory (the main way "risk-free" arbitrage loses money).
    """

    strategy: str
    legs: tuple[OrderRequest, ...]
    expected_profit: Decimal = ZERO  # in quote currency, after fees
    atomic: bool = True
    reason: str = ""


@dataclass
class MarketSnapshot:
    """Everything a strategy sees on one tick."""

    books: dict[tuple[str, str], OrderBook]
    balances: dict[str, dict[str, Decimal]]  # venue -> asset -> free amount
    ts: float = field(default_factory=time.time)
    # Streaming only: when each feed last proved alive. A quiet market's book
    # can be old yet still current while its connection is healthy.
    alive: dict[tuple[str, str], float] = field(default_factory=dict)

    def data_age(self, venue: str, symbol: str, now: float | None = None) -> float:
        now = now if now is not None else time.time()
        book = self.books.get((venue, symbol))
        last = max(book.ts if book else 0.0, self.alive.get((venue, symbol), 0.0))
        return now - last

    def book(self, venue: str, symbol: str) -> OrderBook | None:
        return self.books.get((venue, symbol))

    def balance(self, venue: str, asset: str) -> Decimal:
        return self.balances.get(venue, {}).get(asset, ZERO)
