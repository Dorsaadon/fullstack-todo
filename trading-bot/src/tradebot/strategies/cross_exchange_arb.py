"""Cross-exchange arbitrage: buy where it's cheap, sell where it's rich, at once.

Uses the pre-funded inventory model: hold quote currency and the asset on every
venue, and fire both legs simultaneously. No transfers during the trade, so no
withdrawal delays; rebalance inventory periodically instead.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from itertools import permutations

from ..models import ONE, MarketSnapshot, OrderRequest, Side, TradePlan, split_symbol
from .base import Strategy, bps


class CrossExchangeArbitrage(Strategy):
    def __init__(
        self,
        name: str,
        symbol: str,
        venues: list[str],
        fees: Mapping[str, Decimal] | None = None,
        min_edge_bps: Decimal = Decimal("10"),
        max_size: Decimal = Decimal("1"),
        quote_currency: str = "USD",
    ):
        super().__init__(name, fees)
        if len(venues) < 2:
            raise ValueError("cross-exchange arbitrage needs at least two venues")
        self.symbol = symbol
        self.venues = venues
        self.min_edge_bps = min_edge_bps
        self.max_size = max_size
        self.base, self.quote = split_symbol(symbol, quote_currency)

    def subscriptions(self) -> list[tuple[str, str]]:
        return [(v, self.symbol) for v in self.venues]

    def on_snapshot(self, snap: MarketSnapshot) -> list[TradePlan]:
        plans = [p for buy, sell in permutations(self.venues, 2) if (p := self._evaluate(snap, buy, sell))]
        return [max(plans, key=lambda p: p.expected_profit)] if plans else []

    def _evaluate(self, snap: MarketSnapshot, buy_venue: str, sell_venue: str) -> TradePlan | None:
        buy_book, sell_book = snap.book(buy_venue, self.symbol), snap.book(sell_venue, self.symbol)
        if not buy_book or not sell_book or not buy_book.best_ask or not sell_book.best_bid:
            return None
        fb, fs = self.fee(buy_venue), self.fee(sell_venue)
        # Cheap rejection on top of book before walking depth.
        if sell_book.best_bid.price * (ONE - fs) <= buy_book.best_ask.price * (ONE + fb):
            return None

        cap = min(self.max_size, snap.balance(sell_venue, self.base))
        budget = snap.balance(buy_venue, self.quote)
        candidates = sorted(
            {s for s in buy_book.cumulative_sizes(Side.BUY) + sell_book.cumulative_sizes(Side.SELL) if s < cap} | {cap}
        )
        best: tuple[Decimal, Decimal] | None = None  # (profit, size)
        for size in candidates:
            if size <= 0:
                continue
            got_b, cost = buy_book.sweep(Side.BUY, size)
            got_s, proceeds = sell_book.sweep(Side.SELL, size)
            if got_b < size or got_s < size:
                continue
            spend = cost * (ONE + fb)
            if spend > budget:
                break
            profit = proceeds * (ONE - fs) - spend
            if bps(profit / spend) >= self.min_edge_bps and (best is None or profit > best[0]):
                best = (profit, size)
        if best is None:
            return None

        profit, size = best
        legs = (
            OrderRequest(buy_venue, self.symbol, Side.BUY, size, buy_book.worst_price(Side.BUY, size), strategy=self.name),
            OrderRequest(sell_venue, self.symbol, Side.SELL, size, sell_book.worst_price(Side.SELL, size), strategy=self.name),
        )
        return TradePlan(self.name, legs, profit, reason=f"buy {buy_venue} / sell {sell_venue}")
