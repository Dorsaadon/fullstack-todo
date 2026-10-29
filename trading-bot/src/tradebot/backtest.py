"""Replay recorded market data through the real engine on a simulated clock.

The same Engine, strategies and RiskManager that trade live run here; only
the clock and the venues change. Venues are PaperExchanges with no network.
Updates are coalesced into ticks the way the live streaming loop does it
(`min_tick_interval_s`), and only strategies whose books changed run.

Latency: with `latency_ms > 0`, an order decided at time t fills against each
book as it stood at t + latency, read ahead from the recording. Zero latency
fills against the very book that triggered the trade, which flatters every
strategy, arbitrage most of all.

Still not modelled: queue position and market impact. Your orders never move
the recorded prices, so results are an upper bound for size that is large
relative to the book.
"""

from __future__ import annotations

import csv
import json
import logging
import math
import statistics
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field, fields
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from .config import build_engine
from .engine import Engine
from .models import MarketSnapshot, OrderBook
from .recording import Event, read_events
from .risk import equity, usd_prices

log = logging.getLogger(__name__)


class SimClock:
    def __init__(self, now: float = 0.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


@dataclass
class BacktestResult:
    start: float
    end: float
    events: int
    ticks: int
    initial_equity: Decimal
    final_equity: Decimal
    max_drawdown: Decimal  # USD, peak to trough
    max_drawdown_pct: float
    daily_pnl: dict[str, Decimal]
    sharpe: float | None  # annualized from daily P&L; None with < 2 days
    plans: Counter
    fills: Counter
    fees: Decimal
    borrow_fees: Decimal
    halted: bool
    halt_reason: str
    trades: list[dict] = field(default_factory=list)
    equity_curve: list[tuple[float, Decimal]] = field(default_factory=list)

    @property
    def pnl(self) -> Decimal:
        return self.final_equity - self.initial_equity

    @property
    def return_pct(self) -> float:
        return float(self.pnl / self.initial_equity * 100) if self.initial_equity else 0.0

    def summary(self) -> str:
        def ts(t):
            return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        lines = [
            f"Period        {ts(self.start)} -> {ts(self.end)}",
            f"Events        {self.events:,} book updates, {self.ticks:,} ticks",
            f"Equity        ${self.initial_equity:,.2f} -> ${self.final_equity:,.2f}",
            f"P&L           ${self.pnl:+,.2f} ({self.return_pct:+.2f}%)",
            f"Max drawdown  ${self.max_drawdown:,.2f} ({self.max_drawdown_pct:.2f}%)",
            f"Sharpe        {self.sharpe:.2f}" if self.sharpe is not None else "Sharpe        n/a (needs 2+ days)",
            f"Fees          ${self.fees:,.2f} trading, ${self.borrow_fees:,.2f} borrow",
        ]
        if self.halted:
            lines.append(f"HALTED        {self.halt_reason}")
        lines.append("Per strategy  plans executed / orders filled")
        for name in sorted(set(self.plans) | set(self.fills)):
            lines.append(f"  {name:<24} {self.plans[name]:>6} / {self.fills[name]}")
        if len(self.daily_pnl) > 1:
            lines.append("Daily P&L")
            lines += [f"  {d}  {p:+,.2f}" for d, p in sorted(self.daily_pnl.items())]
        return "\n".join(lines)

    def save(self, out_dir: str | Path) -> Path:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        # Not dataclasses.asdict: it rebuilds Counters from (key, value) pairs, mangling them.
        report = {f.name: getattr(self, f.name) for f in fields(self) if f.name not in ("trades", "equity_curve")}
        report.update(plans=dict(self.plans), fills=dict(self.fills), pnl=self.pnl, return_pct=self.return_pct)
        (out / "report.json").write_text(json.dumps(report, default=str, indent=2))
        with (out / "trades.csv").open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["time", "strategy", "venue", "symbol", "side", "size", "price", "fee"])
            w.writeheader()
            w.writerows(self.trades)
        with (out / "equity.csv").open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["time", "equity"])
            w.writerows([datetime.fromtimestamp(t, timezone.utc).isoformat(), str(e)] for t, e in self.equity_curve)
        return out


class Backtester:
    def __init__(
        self,
        config_path: str | Path,
        data_dir: str | Path,
        start: date | None = None,
        end: date | None = None,
        latency_ms: float = 0.0,
        equity_sample_s: float = 60.0,
    ):
        self.config_path = config_path
        self.data_dir = data_dir
        self.start = start
        self.end = end
        self.latency = latency_ms / 1000
        self.equity_sample_s = equity_sample_s
        self.clock = SimClock()
        self.engine: Engine | None = None
        self.books: dict[tuple[str, str], OrderBook] = {}
        self.alive: dict[tuple[str, str], float] = {}
        self._ahead: deque[Event] = deque()  # read but not yet applied

    async def run(self) -> BacktestResult:
        events = read_events(self.data_dir, self.start, self.end)
        first = next(events, None)
        if first is None:
            raise ValueError(f"no recorded data in {self.data_dir} for the requested dates")
        self._events = events
        self._ahead.append(first)
        self.clock.now = first.t  # before building: demo calendars anchor to "now"
        engine = self.engine = build_engine(self.config_path, clock=self.clock, offline=True)
        subs = set(engine.subscriptions())
        venue_symbols: dict[str, list[str]] = defaultdict(list)
        for v, s in subs:
            venue_symbols[v].append(s)
        for v, ex in engine.exchanges.items():
            ex.fill_book = self._fill_book_for(v)
        await engine.refresh_balances(list(engine.exchanges))

        start, n_events, ticks = first.t, 0, 0
        initial: Decimal | None = None
        peak = max_dd = Decimal(0)
        max_dd_pct = 0.0
        curve: list[tuple[float, Decimal]] = []
        day_close: dict[str, Decimal] = {}
        plans: Counter = Counter()
        last_balance_refresh = first.t
        missing = set(subs)

        while self._ahead or self._read_until(self.clock.now):
            # One tick: the first pending update, plus everything that arrives
            # within min_tick_interval_s after it (as the live loop coalesces).
            head = self._ahead.popleft()
            window_end = head.t + engine.min_tick_interval_s
            self._read_until(window_end)
            batch = [head]
            while self._ahead and self._ahead[0].t <= window_end:
                batch.append(self._ahead.popleft())
            self.clock.now = window_end
            dirty = self._apply(batch, subs, venue_symbols)
            n_events += sum(1 for e in batch if e.symbol is not None)
            if missing:
                missing -= set(self.books)
                if missing:
                    continue  # warm-up: no valuation until every book has arrived, like live
            if self.clock.now - last_balance_refresh > engine.balance_refresh_s:
                await engine.refresh_balances(list(engine.exchanges))
                last_balance_refresh = self.clock.now
            self._read_until(self.clock.now + self.latency)

            snap = MarketSnapshot(dict(self.books), engine._balances, ts=self.clock.now, alive=dict(self.alive))
            for plan, _ in await engine.tick(snap, only=dirty):
                plans[plan.strategy] += 1
            ticks += 1

            eq = engine.risk.equity
            if eq is None:
                continue
            if initial is None:
                initial = peak = eq
            peak = max(peak, eq)
            if peak - eq > max_dd:
                max_dd = peak - eq
                max_dd_pct = float(max_dd / peak * 100) if peak else 0.0
            if not curve or self.clock.now - curve[-1][0] >= self.equity_sample_s:
                curve.append((self.clock.now, eq))
            day_close[datetime.fromtimestamp(self.clock.now, timezone.utc).date().isoformat()] = eq

        if initial is None:
            raise ValueError(f"recording never had a book for every subscription; missing {sorted(missing)}")
        # Final mark-to-market, after the last trades.
        await engine.refresh_balances(list(engine.exchanges))
        final_snap = MarketSnapshot(dict(self.books), engine._balances, ts=self.clock.now)
        final, _ = equity(final_snap, usd_prices(final_snap, engine.assets_of))
        curve.append((self.clock.now, final))
        if day_close:
            day_close[max(day_close)] = final

        daily, prev = {}, initial  # close to close, so overnight moves count
        for d in sorted(day_close):
            daily[d], prev = day_close[d] - prev, day_close[d]
        return self._result(start, n_events, ticks, initial, final, max_dd, max_dd_pct, daily, curve, plans)

    def _read_until(self, t: float) -> bool:
        """Read ahead until the recording passes `t`. False when exhausted."""
        while not self._ahead or self._ahead[-1].t <= t:
            e = next(self._events, None)
            if e is None:
                return bool(self._ahead)
            self._ahead.append(e)
        return True

    def _apply(self, batch: list[Event], subs, venue_symbols) -> set[tuple[str, str]]:
        dirty = set()
        for e in batch:
            if e.symbol is None:  # heartbeat: every book on that venue is still current
                for s in venue_symbols.get(e.venue, []):
                    self.alive[(e.venue, s)] = e.t
                continue
            key = (e.venue, e.symbol)
            if key not in subs:
                continue
            book = e.book()
            self.books[key] = book
            self.alive[key] = e.t
            self.engine.exchanges[e.venue].set_order_book(book)
            dirty.add(key)
        return dirty

    def _fill_book_for(self, venue: str):
        def fill_book(symbol: str) -> OrderBook | None:
            """The book for `symbol` as of now + latency."""
            if not self.latency:
                return None
            horizon = self.clock.now + self.latency
            found = None
            for e in self._ahead:
                if e.t > horizon:
                    break
                if e.venue == venue and e.symbol == symbol:
                    found = e
            return found.book() if found else None

        return fill_book

    def _result(self, start, n_events, ticks, initial, final, max_dd, max_dd_pct, daily, curve, plans) -> BacktestResult:
        engine = self.engine
        trades, fills, fees = [], Counter(), Decimal(0)
        for ex in engine.exchanges.values():
            for o in ex.orders:
                if o.filled <= 0:
                    continue
                strat = o.request.strategy.split(":")[0]
                fills[strat] += 1
                fees += o.fee
                trades.append({
                    "time": datetime.fromtimestamp(o.ts, timezone.utc).isoformat(),
                    "strategy": o.request.strategy, "venue": o.request.venue, "symbol": o.request.symbol,
                    "side": o.request.side.value, "size": str(o.filled), "price": str(o.avg_price), "fee": str(o.fee),
                })
        trades.sort(key=lambda t: t["time"])
        sharpe = None
        if len(daily) >= 2 and initial:
            rets = [float(p / initial) for _, p in sorted(daily.items())]
            sd = statistics.stdev(rets)
            sharpe = statistics.fmean(rets) / sd * math.sqrt(252) if sd > 0 else None
        return BacktestResult(
            start=start, end=self.clock.now, events=n_events, ticks=ticks,
            initial_equity=initial, final_equity=final, max_drawdown=max_dd, max_drawdown_pct=max_dd_pct,
            daily_pnl=daily, sharpe=sharpe, plans=plans, fills=fills, fees=fees,
            borrow_fees=sum((getattr(ex, "borrow_fees_paid", 0) for ex in engine.exchanges.values()), Decimal(0)),
            halted=engine.risk.halted, halt_reason=engine.risk.halt_reason,
            trades=trades, equity_curve=curve,
        )
