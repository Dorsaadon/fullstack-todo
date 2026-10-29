"""Z-score mean reversion: a directional strategy, typically for US equities.

Buys when price is `entry_z` standard deviations below its rolling mean and
exits when it reverts to within `exit_z`. Long only, one position at a time.
Unlike the arbitrage strategies this takes market risk; backtest before use.
"""

from __future__ import annotations

import statistics
from collections import deque
from collections.abc import Mapping
from decimal import Decimal

from ..models import ZERO, MarketSnapshot, OrderRequest, Side, TradePlan
from .base import Strategy


class MeanReversion(Strategy):
    def __init__(
        self,
        name: str,
        venue: str,
        symbol: str,
        fees: Mapping[str, Decimal] | None = None,
        window: int = 60,
        entry_z: float = 2.0,
        exit_z: float = 0.0,
        order_size: Decimal = Decimal("1"),
    ):
        super().__init__(name, fees)
        self.venue = venue
        self.symbol = symbol
        self.window = window
        self.entry_z = entry_z
        self.exit_z = exit_z
        self.order_size = order_size
        self.mids: deque[float] = deque(maxlen=window)
        self._last_ts: float | None = None

    def subscriptions(self) -> list[tuple[str, str]]:
        return [(self.venue, self.symbol)]

    def zscore(self) -> float | None:
        if len(self.mids) < self.window:
            return None
        sd = statistics.pstdev(self.mids)
        return (self.mids[-1] - statistics.fmean(self.mids)) / sd if sd > 0 else None

    def on_snapshot(self, snap: MarketSnapshot) -> list[TradePlan]:
        book = snap.book(self.venue, self.symbol)
        if not book or book.mid is None:
            return []
        if book.ts != self._last_ts:  # only sample new quotes, not repeated polls
            self._last_ts = book.ts
            self.mids.append(float(book.mid))
        z = self.zscore()
        if z is None:
            return []

        position = snap.balance(self.venue, self.symbol)
        if position == ZERO and z <= -self.entry_z and book.best_ask:
            leg = OrderRequest(self.venue, self.symbol, Side.BUY, self.order_size, book.best_ask.price, strategy=self.name)
            return [TradePlan(self.name, (leg,), atomic=False, reason=f"entry z={z:.2f}")]
        if position > ZERO and z >= -self.exit_z and book.best_bid:
            leg = OrderRequest(self.venue, self.symbol, Side.SELL, position, book.best_bid.price, strategy=self.name)
            return [TradePlan(self.name, (leg,), atomic=False, reason=f"exit z={z:.2f}")]
        return []
