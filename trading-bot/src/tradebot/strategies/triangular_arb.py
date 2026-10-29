"""Triangular arbitrage on a single venue, e.g. USDT -> BTC -> ETH -> USDT.

Legs fire simultaneously from inventory held in every currency on the path
(rather than chained, which waits on each fill and lets prices move). Both
directions of the cycle are checked. Sizing uses top-of-book liquidity only.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import ROUND_DOWN, Decimal

from ..models import ONE, MarketSnapshot, OrderRequest, Side, TradePlan
from .base import Strategy, bps


class TriangularArbitrage(Strategy):
    def __init__(
        self,
        name: str,
        venue: str,
        path: list[str],
        markets: list[str],
        fees: Mapping[str, Decimal] | None = None,
        min_edge_bps: Decimal = Decimal("10"),
        max_start_amount: Decimal = Decimal("1000"),
        size_step: Decimal = Decimal("0.00000001"),
    ):
        super().__init__(name, fees)
        self.size_step = size_step
        if len(path) != 3:
            raise ValueError("path is three currencies, e.g. ['USDT', 'BTC', 'ETH']")
        self.venue = venue
        self.path = path
        self.markets = markets
        self.min_edge_bps = min_edge_bps
        self.max_start_amount = max_start_amount
        for cycle in self._cycles():
            for a, b in zip(cycle, cycle[1:]):
                self._hop(a, b)  # fail fast on a missing market

    def subscriptions(self) -> list[tuple[str, str]]:
        return [(self.venue, m) for m in self.markets]

    def _cycles(self) -> list[list[str]]:
        a, b, c = self.path
        return [[a, b, c, a], [a, c, b, a]]

    def _hop(self, frm: str, to: str) -> tuple[str, Side]:
        if f"{to}/{frm}" in self.markets:
            return f"{to}/{frm}", Side.BUY  # spend quote `frm`, receive base `to`
        if f"{frm}/{to}" in self.markets:
            return f"{frm}/{to}", Side.SELL  # spend base `frm`, receive quote `to`
        raise ValueError(f"no market converts {frm} -> {to} in {self.markets}")

    def on_snapshot(self, snap: MarketSnapshot) -> list[TradePlan]:
        plans = [p for cycle in self._cycles() if (p := self._evaluate(snap, cycle))]
        return [max(plans, key=lambda p: p.expected_profit)] if plans else []

    def _evaluate(self, snap: MarketSnapshot, cycle: list[str]) -> TradePlan | None:
        fee = self.fee(self.venue)
        hops = []  # (symbol, side, price, rate, capacity in start currency)
        cumulative = ONE  # start currency -> current currency
        for frm, to in zip(cycle, cycle[1:]):
            symbol, side = self._hop(frm, to)
            book = snap.book(self.venue, symbol)
            top = book and (book.best_ask if side is Side.BUY else book.best_bid)
            if not top:
                return None
            rate = ONE / (top.price * (ONE + fee)) if side is Side.BUY else top.price * (ONE - fee)
            capacity_in_frm = top.size * top.price * (ONE + fee) if side is Side.BUY else top.size
            capacity = min(capacity_in_frm, snap.balance(self.venue, frm)) / cumulative
            hops.append((symbol, side, top.price, cumulative, capacity))
            cumulative *= rate

        edge = cumulative - ONE
        if bps(edge) < self.min_edge_bps:
            return None
        start = min([self.max_start_amount] + [h[4] for h in hops])
        if start <= 0:
            return None

        legs = []
        for symbol, side, price, to_frm, _ in hops:
            amount_in = start * to_frm  # in this hop's `frm` currency
            size = amount_in / (price * (ONE + fee)) if side is Side.BUY else amount_in
            size = size.quantize(self.size_step, rounding=ROUND_DOWN)
            if size <= 0:
                return None
            legs.append(OrderRequest(self.venue, symbol, side, size, price, strategy=self.name))
        return TradePlan(self.name, tuple(legs), start * edge, reason=" -> ".join(cycle))
