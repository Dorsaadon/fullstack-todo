"""The trading loop: snapshot -> strategies -> risk -> execution -> repeat."""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal

from .exchanges.base import Exchange
from .journal import Journal
from .models import MarketSnapshot, Order, OrderRequest, OrderStatus, Side, TimeInForce, TradePlan
from .risk import RiskManager, equity, usd_prices
from .strategies.base import Strategy

log = logging.getLogger(__name__)

DUST = Decimal("1e-9")


def fmt(x: Decimal | None) -> str:
    return "-" if x is None else f"{x:.10g}"


class Engine:
    def __init__(
        self,
        exchanges: dict[str, Exchange],
        strategies: list[Strategy],
        risk: RiskManager,
        journal: Journal | None = None,
        poll_interval_s: float = 2.0,
        book_depth: int = 10,
        unwind_slippage_bps: Decimal = Decimal("50"),
    ):
        self.exchanges = exchanges
        self.strategies = strategies
        self.risk = risk
        self.journal = journal or Journal(None)
        self.poll_interval_s = poll_interval_s
        self.book_depth = book_depth
        self.unwind_slippage = unwind_slippage_bps / Decimal("10000")
        self._stop = asyncio.Event()
        for s in strategies:
            for venue, _ in s.subscriptions():
                if venue not in exchanges:
                    raise ValueError(f"strategy {s.name!r} uses unknown venue {venue!r}")

    def assets_of(self, venue: str, symbol: str) -> tuple[str, str]:
        return self.exchanges[venue].assets(symbol)

    async def start(self) -> None:
        await asyncio.gather(*(ex.connect() for ex in self.exchanges.values()))

    async def close(self) -> None:
        await asyncio.gather(*(ex.close() for ex in self.exchanges.values()), return_exceptions=True)

    def stop(self) -> None:
        self._stop.set()

    async def run(self, max_ticks: int | None = None) -> None:
        ticks = 0
        while not self._stop.is_set() and (max_ticks is None or ticks < max_ticks):
            try:
                await self.tick()
            except Exception:
                log.exception("tick failed")
            ticks += 1
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval_s)
            except asyncio.TimeoutError:
                pass

    async def snapshot(self) -> MarketSnapshot:
        subs = sorted({sub for s in self.strategies for sub in s.subscriptions()})
        venues = list(self.exchanges)
        results = await asyncio.gather(
            *(self.exchanges[v].fetch_order_book(sym, self.book_depth) for v, sym in subs),
            *(self.exchanges[v].fetch_balances() for v in venues),
            return_exceptions=True,
        )
        books, balances = {}, {}
        for (v, sym), res in zip(subs, results[: len(subs)]):
            if isinstance(res, BaseException):
                log.warning("book %s:%s unavailable: %s", v, sym, res)
            else:
                books[(v, sym)] = res
        for v, res in zip(venues, results[len(subs):]):
            if isinstance(res, BaseException):
                log.warning("balances %s unavailable: %s", v, res)
            balances[v] = {} if isinstance(res, BaseException) else res
        return MarketSnapshot(books, balances)

    async def tick(self) -> list[tuple[TradePlan, list[Order]]]:
        snap = await self.snapshot()
        prices = usd_prices(snap, self.assets_of)
        eq, unpriced = equity(snap, prices)
        if unpriced:
            log.debug("equity excludes unpriced assets: %s", sorted(unpriced))
        self.risk.update_equity(eq)

        executed = []
        for strat in self.strategies:
            try:
                plans = strat.on_snapshot(snap)
            except Exception:
                log.exception("strategy %s failed", strat.name)
                continue
            for plan in plans:
                reason = self.risk.check(plan, snap, prices)
                if reason:
                    log.info("[%s] rejected: %s", strat.name, reason)
                    self.journal.write("rejected", strategy=strat.name, reason=reason, plan=plan)
                    continue
                orders = await self.execute(plan)
                strat.on_orders(plan, orders)
                executed.append((plan, orders))
                # The next strategy must see post-trade inventory.
                touched = {leg.venue for leg in plan.legs}
                for v in touched:
                    try:
                        snap.balances[v] = await self.exchanges[v].fetch_balances()
                    except Exception as e:
                        log.warning("balance refresh %s failed: %s", v, e)
        return executed

    async def execute(self, plan: TradePlan) -> list[Order]:
        log.info("[%s] executing %s (expected %+.4f): %s", plan.strategy, len(plan.legs), plan.expected_profit, plan.reason)
        self.journal.write("plan", plan=plan)
        self.risk.record_orders(len(plan.legs))
        orders = list(await asyncio.gather(*(self.exchanges[l.venue].place_order(l) for l in plan.legs)))
        for o in orders:
            self.journal.write("order", order=o)
            if o.status is OrderStatus.REJECTED:
                log.warning("  %s %s %s on %s rejected: %s", o.request.side.value, fmt(o.request.size), o.request.symbol, o.request.venue, o.error)
            else:
                log.info("  %s %s/%s %s on %s @ %s", o.request.side.value, fmt(o.filled), fmt(o.request.size), o.request.symbol, o.request.venue, fmt(o.avg_price))
        if plan.atomic and len(plan.legs) > 1:
            await self._unwind(plan, orders)
        return orders

    async def _unwind(self, plan: TradePlan, orders: list[Order]) -> None:
        """If legs filled unevenly, flatten each leg's excess over the least-filled leg."""
        floor = min(o.fill_ratio for o in orders)
        for o in orders:
            excess = o.filled - o.request.size * floor
            if excess <= DUST:
                continue
            req = o.request
            ex = self.exchanges[req.venue]
            book = await ex.fetch_order_book(req.symbol, self.book_depth)
            side = req.side.opposite
            top = book.best_bid if side is Side.SELL else book.best_ask
            if top is None:
                self.risk.halt(f"cannot unwind {excess} {req.symbol} on {req.venue}: empty book")
                continue
            px = top.price * (1 - self.unwind_slippage if side is Side.SELL else 1 + self.unwind_slippage)
            fix = OrderRequest(req.venue, req.symbol, side, excess, px, TimeInForce.IOC, strategy=f"{plan.strategy}:unwind")
            log.warning("  legging: unwinding %s %s on %s", excess, req.symbol, req.venue)
            res = await ex.place_order(fix)
            self.journal.write("unwind", order=res)
            if res.filled + DUST < excess:
                self.risk.halt(f"unwind incomplete on {req.venue}:{req.symbol} ({res.filled}/{excess}); fix manually")
