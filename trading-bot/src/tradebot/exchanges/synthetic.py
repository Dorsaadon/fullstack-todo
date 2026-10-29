"""Synthetic market data for demos and tests: no network, no keys.

One SyntheticMarket holds a "fair value" random walk per asset (and per
prediction-market outcome set). Each SyntheticExchange quotes around fair value
with its own noise, and occasionally a large dislocation, which is exactly what
arbitrage strategies feed on. Cross pairs (ETH/BTC) are derived from USD prices
so triangular cycles stay mostly consistent.
"""

from __future__ import annotations

import asyncio
import math
import random
import time
import zlib
from collections.abc import AsyncIterator
from decimal import Decimal

from ..models import STABLE_ASSETS, Order, OrderBook, OrderRequest
from .base import Exchange


class SyntheticMarket:
    def __init__(
        self,
        assets: dict[str, float],
        outcome_sets: list[list[str]] | None = None,
        seed: int = 0,
        step_s: float = 1.0,
        vol_bps: float = 5.0,
        clock=time.time,
    ):
        self.usd = {a: float(p) for a, p in assets.items()}
        self.outcomes: dict[str, tuple[int, int]] = {}  # token -> (set index, position)
        self.logits: list[list[float]] = []
        for i, tokens in enumerate(outcome_sets or []):
            self.logits.append([0.0] * len(tokens))
            for j, t in enumerate(tokens):
                self.outcomes[t] = (i, j)
        self.seed = seed
        self.step_s = step_s
        self.vol = vol_bps / 10_000
        self.clock = clock
        self.t0 = clock()
        self.step = 0
        self._rng = random.Random(seed)

    def advance(self) -> int:
        target = int((self.clock() - self.t0) / self.step_s)
        while self.step < target:
            self.step += 1
            for a in self.usd:
                self.usd[a] *= math.exp(self._rng.gauss(0, self.vol))
            for lg in self.logits:
                for j in range(len(lg)):
                    lg[j] += self._rng.gauss(0, self.vol * 20)
        return self.step

    def fair(self, symbol: str) -> float:
        if symbol in self.outcomes:
            i, j = self.outcomes[symbol]
            exps = [math.exp(x) for x in self.logits[i]]
            return exps[j] / sum(exps)
        base, quote = symbol.split("/") if "/" in symbol else (symbol, "USD")
        return self.usd_price(base) / self.usd_price(quote)

    def usd_price(self, asset: str) -> float:
        return 1.0 if asset in STABLE_ASSETS else self.usd[asset]


class SyntheticExchange(Exchange):
    """Market-data-only venue; wrap it in PaperExchange to trade against it."""

    def __init__(
        self,
        name: str,
        market: SyntheticMarket,
        noise_bps: float = 3.0,
        spread_bps: float = 4.0,
        dislocation_prob: float = 0.05,
        dislocation_bps: float = 40.0,
        levels: int = 5,
        level_notional_usd: float = 2_000.0,
        quote_currency: str = "USD",
    ):
        super().__init__(name, Decimal("0"), quote_currency)
        self.market = market
        self.noise = noise_bps / 10_000
        self.half_spread = spread_bps / 20_000
        self.dislocation_prob = dislocation_prob
        self.dislocation = dislocation_bps / 10_000
        self.levels = levels
        self.level_notional = level_notional_usd

    async def fetch_order_book(self, symbol: str, depth: int = 10) -> OrderBook:
        step = self.market.advance()
        # Deterministic per (venue, symbol, step) so re-polls within a step agree.
        rng = random.Random(zlib.crc32(f"{self.market.seed}:{self.name}:{symbol}:{step}".encode()))
        fair = self.market.fair(symbol)
        shock = rng.gauss(0, self.noise)
        if rng.random() < self.dislocation_prob:
            shock += rng.choice((-1, 1)) * self.dislocation
        is_outcome = symbol in self.market.outcomes
        mid = fair + shock if is_outcome else fair * (1 + shock)
        _, quote = self.assets(symbol)
        size_unit = self.level_notional / (fair * self.market.usd_price(quote) if not is_outcome else max(fair, 0.05))
        step_px = mid * self.half_spread if not is_outcome else 0.001

        def px(x: float) -> Decimal:
            if is_outcome:
                return Decimal(f"{min(max(x, 0.001), 0.999):.3f}")
            return Decimal(f"{x:.6g}")

        half = mid * self.half_spread if not is_outcome else 0.005
        bids = [(px(mid - half - i * step_px), Decimal(f"{size_unit * rng.uniform(0.5, 1.5):.6g}")) for i in range(self.levels)]
        asks = [(px(mid + half + i * step_px), Decimal(f"{size_unit * rng.uniform(0.5, 1.5):.6g}")) for i in range(self.levels)]
        book = OrderBook.from_raw(self.name, symbol, bids, asks)
        return OrderBook(self.name, symbol, book.bids[:depth], book.asks[:depth], book.ts)

    async def watch_order_books(self, symbols: list[str], depth: int = 10) -> AsyncIterator[OrderBook | None]:
        """Push a fresh book for every symbol each time the market steps."""
        while True:
            for s in symbols:
                yield await self.fetch_order_book(s, depth)
            m = self.market
            next_step = m.t0 + (m.step + 1) * m.step_s
            await asyncio.sleep(max(next_step - m.clock(), 0.001))

    async def place_order(self, req: OrderRequest) -> Order:
        raise NotImplementedError("synthetic venues are data-only; run them in paper mode")

    async def cancel_order(self, order_id: str, symbol: str) -> None:
        raise NotImplementedError

    async def fetch_balances(self) -> dict[str, Decimal]:
        return {}
