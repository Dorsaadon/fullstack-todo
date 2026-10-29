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
from collections import Counter, defaultdict
from datetime import datetime
from datetime import time as dtime
from decimal import Decimal

from .alerts import Alerter
from .exchanges.base import Exchange
from .exchanges.paper import PaperExchange
from .journal import Journal
from .market_hours import NEW_YORK
from .marketdata import MarketDataHub
from .models import ZERO, MarketSnapshot, Order, OrderRequest, OrderStatus, Side, TimeInForce, TradePlan
from .risk import RiskManager, equity, usd_prices
from .state import StateStore
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
        self.recorder = None  # recording.Recorder; set by config when [recording] enabled
        self.alerts = Alerter()  # logs only, until [alerts] configures channels
        self.state: StateStore | None = None  # set by config ([engine] state_file)
        self.state_save_s = 10.0
        self.daily_summary_time: dtime | None = None  # America/New_York
        self._last_save = 0.0
        self._last_tick = time.monotonic()
        self._plans_today: Counter = Counter()
        self._summary_day = None
        risk.on_halt.append(self._on_halt)
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
        self.restore_state()  # first: a corrupt state file should stop us before we connect
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
        await self.alerts.start()
        stuck_after = max(60.0, 10 * self.heartbeat_s, 5 * self.poll_interval_s)
        self.alerts.alive = lambda: time.monotonic() - self._last_tick < stuck_after
        if self.hub:
            self.hub.on_status = self._on_venue_status
        try:
            if self.hub:
                await self._run_streaming(max_ticks)
            else:
                await self._run_polling(max_ticks)
        finally:
            self.save_state()
            self.alerts.send("info", "stopped")
            await self.alerts.close()

    # --- state, reconciliation, alerts -------------------------------------------

    def restore_state(self) -> None:
        """Load saved state, if any. Call before trading (start() does)."""
        if self.state is None:
            return
        data = self.state.load()
        if not data:
            log.info("no saved state in %s; starting fresh", self.state.path)
            return
        self.risk.set_state(data.get("risk", {}))
        saved = data.get("strategies", {})
        for s in self.strategies:
            if s.name in saved:
                s.set_state(saved[s.name])
        for name, ex in self.exchanges.items():
            if isinstance(ex, PaperExchange) and name in data.get("paper", {}):
                ex.set_state(data["paper"][name])
        log.info("restored state saved %.0fs ago", time.time() - data.get("saved_at", time.time()))

    def collect_state(self) -> dict:
        return {
            "risk": self.risk.get_state(),
            "strategies": {s.name: st for s in self.strategies if (st := s.get_state()) is not None},
            "paper": {n: ex.get_state() for n, ex in self.exchanges.items() if isinstance(ex, PaperExchange)},
        }

    def save_state(self) -> None:
        if self.state is None:
            return
        try:
            self.state.save(self.collect_state())
            self._last_save = time.monotonic()
        except Exception:
            log.exception("could not save state to %s", self.state.path)

    async def reconcile(self) -> list[str]:
        """Check positions the strategies must still close against the broker.

        Where the broker holds less than the strategies think (filled while the
        bot was down, closed by hand...), shrink the tracked positions to match,
        so the bot never tries to sell shares that aren't there. Extra shares
        at the broker are left alone: they may be yours. Call after balances
        are fresh and before trading.
        """
        tracked: dict[tuple[str, str], list] = defaultdict(list)
        for s in self.strategies:
            for (venue, symbol), qty in s.positions().items():
                if qty:
                    tracked[(venue, self.assets_of(venue, symbol)[0])].append((s, symbol, qty))
        issues = []
        for (venue, asset), holders in tracked.items():
            total = sum(q for _, _, q in holders)
            actual = self._balances.get(venue, {}).get(asset, ZERO)
            if len({q > 0 for _, _, q in holders}) > 1:
                self.risk.halt(f"strategies hold opposite positions in {venue}:{asset}; resolve by hand")
                continue
            long = total > 0
            if (actual >= total) if long else (actual <= total):
                continue
            remaining = max(actual, ZERO) if long else min(actual, ZERO)
            for s, symbol, q in holders:
                new = min(q, remaining) if long else max(q, remaining)
                remaining -= new
                s.set_position(venue, symbol, new)
            msg = f"{venue}:{asset}: strategies tracked {fmt(total)} but the account holds {fmt(actual)}; adjusted to match"
            issues.append(msg)
            self.alerts.send("warning", f"position mismatch at startup. {msg}")
        if issues:
            self.save_state()
        return issues

    async def _prepare(self) -> None:
        """Between first balances and first trade: reconcile and announce."""
        await self.reconcile()
        if self.risk.halted:
            self.alerts.send(
                "critical", f"started while HALTED ({self.risk.halt_reason}); "
                "trading stays off until you run `tradebot state --clear-halt`",
            )
        names = ", ".join(s.name for s in self.strategies)
        self.alerts.send("info", f"started: {len(self.strategies)} strategies ({names})")
        now_et = datetime.now(NEW_YORK)
        if self.daily_summary_time and now_et.time() >= self.daily_summary_time:
            self._summary_day = now_et.date()  # today's summary time already passed

    def _on_halt(self, reason: str) -> None:
        self.alerts.send("critical", f"TRADING HALTED: {reason}")
        self.alerts.fail(reason)
        self.save_state()

    def _on_venue_status(self, venue: str, ok: bool, detail: str) -> None:
        if ok:
            self.alerts.send("warning", f"{venue}: {detail}")
        else:
            self.alerts.send("critical", f"{venue} market data DOWN: {detail}")

    def _after_tick(self) -> None:
        self._last_tick = time.monotonic()
        if self.state and time.monotonic() - self._last_save >= self.state_save_s:
            self.save_state()
        if self.daily_summary_time:
            now_et = datetime.now(NEW_YORK)
            if now_et.time() >= self.daily_summary_time and self._summary_day != now_et.date():
                self._summary_day = now_et.date()
                self.alerts.send("summary", self.summary())
                self._plans_today.clear()

    def summary(self) -> str:
        r = self.risk
        plans = ", ".join(f"{k} {v}" for k, v in sorted(self._plans_today.items())) or "none"
        eq = f"${r.equity:,.2f}" if r.equity is not None else "n/a"
        status = f"HALTED: {r.halt_reason}" if r.halted else "trading"
        return f"daily summary: equity {eq}, P&L today ${r.daily_pnl:+,.2f}, trades: {plans}; {status}"

    async def _run_streaming(self, max_ticks: int | None) -> None:
        subs = self.subscriptions()
        if self.recorder:
            self.hub.listeners.append(self.recorder.on_event)
            self.recorder.start()
        await self.hub.start(subs)
        # Don't value the account (or set the daily-loss baseline) until every
        # book has arrived; a partial first valuation would hide later losses.
        missing = await self.hub.warm_up(subs, self.warmup_timeout_s)
        if missing:
            log.warning("no data yet for %s; strategies using them wait", sorted(missing))
        await self.refresh_balances(list(self.exchanges))
        await self._prepare()
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
                    await self.refresh_balances(list(self.exchanges))
                    last_balance_refresh = time.monotonic()
                try:
                    await self.tick(self.stream_snapshot(), only=dirty)
                except Exception:
                    log.exception("tick failed")
                self._after_tick()
                ticks += 1
        finally:
            await self.hub.stop()
            if self.recorder:
                await self.recorder.close()

    def stream_snapshot(self) -> MarketSnapshot:
        # Balances are shared, not copied, so post-trade refreshes update the cache.
        return MarketSnapshot(dict(self.hub.books), self._balances, alive=dict(self.hub.alive))

    async def refresh_balances(self, venues: list[str]) -> None:
        results = await asyncio.gather(*(self.exchanges[v].fetch_balances() for v in venues), return_exceptions=True)
        for v, res in zip(venues, results):
            if isinstance(res, BaseException):
                log.warning("balances %s unavailable: %s", v, res)
                self._balances.setdefault(v, {})
            else:
                self._balances[v] = res

    async def _run_polling(self, max_ticks: int | None) -> None:
        if self.recorder:
            self.recorder.start()
        try:
            await self._poll_loop(max_ticks)
        finally:
            if self.recorder:
                await self.recorder.close()

    async def _poll_loop(self, max_ticks: int | None) -> None:
        await self.refresh_balances(list(self.exchanges))
        await self._prepare()
        ticks = 0
        while not self._stop.is_set() and (max_ticks is None or ticks < max_ticks):
            try:
                await self.tick()
            except Exception:
                log.exception("tick failed")
            self._after_tick()
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
                if self.recorder:
                    self.recorder.on_event(v, res, time.time())
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
                self._plans_today[strat.name] += 1
                self.save_state()  # positions changed: persist before anything else can go wrong
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
            self.alerts.send("warning", f"{plan.strategy}: uneven fill, unwinding {fmt(excess)} {req.symbol} on {req.venue}")
            res = await ex.place_order(fix)
            self.journal.write("unwind", order=res)
            if res.filled + DUST < excess:
                self.risk.halt(f"unwind incomplete on {req.venue}:{req.symbol} ({res.filled}/{excess}); fix manually")
