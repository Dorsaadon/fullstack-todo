"""Outcome-basket arbitrage for prediction markets (Polymarket, Kalshi...).

If the outcomes are mutually exclusive and exhaustive, exactly one share in the
basket pays $1. So:
  * sum of best asks < $1 (after fees) -> buy one of each: locked-in profit at
    resolution (or immediately, by merging the set back into USDC on Polymarket)
  * sum of best bids > $1 and you hold the set -> sell one of each.

For a binary market the basket is just [YES, NO]. For multi-outcome markets,
list every outcome token; missing one ("Other") turns this into a gamble.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

from ..models import ONE, MarketSnapshot, OrderRequest, Side, TradePlan
from .base import Strategy, bps


class PredictionMarketArbitrage(Strategy):
    def __init__(
        self,
        name: str,
        venue: str,
        outcomes: list[str],
        fees: Mapping[str, Decimal] | None = None,
        min_edge_bps: Decimal = Decimal("50"),
        max_size: Decimal = Decimal("100"),
        collateral: str = "USDC",
    ):
        super().__init__(name, fees)
        if len(outcomes) < 2:
            raise ValueError("an outcome basket needs at least two outcomes")
        self.venue = venue
        self.outcomes = outcomes
        self.min_edge_bps = min_edge_bps
        self.max_size = max_size
        self.collateral = collateral

    def subscriptions(self) -> list[tuple[str, str]]:
        return [(self.venue, o) for o in self.outcomes]

    def on_snapshot(self, snap: MarketSnapshot) -> list[TradePlan]:
        books = [snap.book(self.venue, o) for o in self.outcomes]
        if any(b is None for b in books):
            return []
        plan = self._basket(snap, books, Side.BUY) or self._basket(snap, books, Side.SELL)
        return [plan] if plan else []

    def _basket(self, snap: MarketSnapshot, books, side: Side) -> TradePlan | None:
        fee = self.fee(self.venue)
        cap = self.max_size
        if side is Side.SELL:
            cap = min([cap] + [snap.balance(self.venue, o) for o in self.outcomes])
        candidates = sorted({s for b in books for s in b.cumulative_sizes(side) if s < cap} | {cap})

        best: tuple[Decimal, Decimal] | None = None
        for size in candidates:
            if size <= 0:
                continue
            sweeps = [b.sweep(side, size) for b in books]
            if any(filled < size for filled, _ in sweeps):
                continue
            total = sum(cost for _, cost in sweeps)
            if side is Side.BUY:
                spend = total * (ONE + fee)
                if spend > snap.balance(self.venue, self.collateral):
                    break
                profit, base = size - spend, spend  # each set redeems for $1
            else:
                profit, base = total * (ONE - fee) - size, size  # vs. holding to $1
            if bps(profit / base) >= self.min_edge_bps and (best is None or profit > best[0]):
                best = (profit, size)
        if best is None:
            return None

        profit, size = best
        legs = tuple(
            OrderRequest(self.venue, o, side, size, b.worst_price(side, size), strategy=self.name)
            for o, b in zip(self.outcomes, books)
        )
        what = "asks" if side is Side.BUY else "bids"
        return TradePlan(self.name, legs, profit, reason=f"{side.value} basket: sum of {what} off $1")
