"""The trading loop: snapshot -> strategies -> risk -> execution -> repeat.

Two data modes:
  * "stream" (default): a MarketDataHub pushes book updates and the engine
    reacts within milliseconds, running only strategies whose books changed.
  * "poll": fetch every book and balance over REST every `poll_interval_s`.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from decimal import Decimal

from .exchanges.base import Exchange
from .journal import Journal
from .marketdata import MarketDataHub
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
        data_mode: str = "stream",
        heartbeat_s: float = 1.0,
        min_tick_interval_s: float = 0.05,
        balance_refresh_s: float = 30.0,
        warmup_timeout_s: float = 15.0,
    ):
        if data_mode not in ("stream", "poll"):
            raise ValueError("data_mode must be 'stream' or 'poll'")
        self.exchanges = exchanges
        self.strategies = strategies
        self.risk = risk
        self.journal = journal or Journal(None)
        self.poll_interval_s = poll_interval_s
        self.book_depth = book_depth
        self.unwind_slippage = unwind_slippage_bps / Decimal("10000")
        self.data_mode = data_mode
        self.heartbeat_s = heartbeat_s  # re-check risk/equity at least this often
        self.min_tick_interval_s = min_tick_interval_s  # coalesce bursts of updates
        self.balance_refresh_s = balance_refresh_s
        self.warmup_timeout_s = warmup_timeout_s
        self.hub = MarketDataHub(exchanges, book_depth) if data_mode == "stream" else None
        self._balances: dict[str, dict[str, Decimal]] = {}
        self._last_rejection: dict[str, str] = {}
        self._stop = asyncio.Event()
        for ex in exchanges.values():
            ex.poll_interval_s = poll_interval_s  # cadence for venues without WebSockets
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
        if self.hub:
            self.hub.wake()

    def subscriptions(self) -> list[tuple[str, str]]:
        return sorted({sub for s in self.strategies for sub in s.subscriptions()})

    async def run(self, max_ticks: int | None = None) -> None:
        if self.hub:
            await self._run_streaming(max_ticks)
        else:
            await self._run_polling(max_ticks)

    async def _run_streaming(self, max_ticks: int | None) -> None:
        subs = self.subscriptions()
        await self.hub.start(subs)
        # Don't value the account (or set the daily-loss baseline) until every
        # book has arrived; a partial first valuation would hide later losses.
        missing = await self.hub.warm_up(subs, self.warmup_timeout_s)
        if missing:
            log.warning("no data yet for %s; strategies using them wait", sorted(missing))
        await self._refresh_balances(list(self.exchanges))
        last_balance_refresh = time.monotonic()
        ticks = 0
        try:
            while not self._stop.is_set() and (max_ticks is None or ticks < max_ticks):
                changed = await self.hub.wait(self.heartbeat_s)
                if self._stop.is_set():
                    break
                if changed and self.min_tick_interval_s:
                    await asyncio.sleep(self.min_tick_interval_s)  # let a burst settle
                dirty = self.hub.take_dirty()
                if time.monotonic() - last_balance_refresh > self.balance_refresh_s:
                    await self._refresh_balances(list(self.exchanges))
                    last_balance_refresh = time.monotonic()
                try:
                    await self.tick(self.stream_snapshot(), only=dirty)
                except Exception:
                    log.exception("tick failed")
                ticks += 1
        finally:
            await self.hub.stop()

    def stream_snapshot(self) -> MarketSnapshot:
        # Balances are shared, not copied, so post-trade refreshes update the cache.
        return MarketSnapshot(dict(self.hub.books), self._balances, alive=dict(self.hub.alive))

    async def _refresh_balances(self, venues: list[str]) -> None:
        results = await asyncio.gather(*(self.exchanges[v].fetch_balances() for v in venues), return_exceptions=True)
        for v, res in zip(venues, results):
            if isinstance(res, BaseException):
                log.warning("balances %s unavailable: %s", v, res)
                self._balances.setdefault(v, {})
            else:
                self._balances[v] = res

    async def _run_polling(self, max_ticks: int | None) -> None:
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
        subs = self.subscriptions()
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

    async def tick(
        self, snap: MarketSnapshot | None = None, only: set[tuple[str, str]] | None = None
    ) -> list[tuple[TradePlan, list[Order]]]:
        """Evaluate strategies once. `only` limits it to strategies watching those books."""
        snap = snap if snap is not None else await self.snapshot()
        prices = usd_prices(snap, self.assets_of)
        eq, unpriced = equity(snap, prices)
        if unpriced:
            log.debug("equity excludes unpriced assets: %s", sorted(unpriced))
        self.risk.update_equity(eq)

        executed = []
        for strat in self.strategies:
            if only is not None and not only.intersection(strat.subscriptions()):
                continue
            try:
                plans = strat.on_snapshot(snap)
            except Exception:
                log.exception("strategy %s failed", strat.name)
                continue
            for plan in plans:
                reason = self.risk.check(plan, snap, prices)
                if reason:
                    # Streaming re-evaluates constantly; only report a reason when it changes.
                    key = re.sub(r"[-+]?\d[\d.,]*", "#", reason)  # ignore changing numbers
                    if self._last_rejection.get(strat.name) != key:
                        log.info("[%s] rejected: %s", strat.name, reason)
                        self.journal.write("rejected", strategy=strat.name, reason=reason, plan=plan)
                    self._last_rejection[strat.name] = key
                    continue
                self._last_rejection.pop(strat.name, None)
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
