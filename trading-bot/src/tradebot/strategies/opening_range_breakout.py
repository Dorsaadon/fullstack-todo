"""Opening range breakout (ORB): the classic intraday equity strategy.

For each symbol, every trading day:
  1. Record the high and low of the first `range_minutes` (the opening range).
  2. Long: buy when the ask breaks above the range high (plus a small buffer).
     Short (if `allow_short`): sell short when the bid breaks below the range
     low. Entries only until `entry_cutoff_minutes` after the open.
  3. The stop is the other side of the range. Take profit at `take_profit_r`
     times the risk (0 = no target).
  4. Flatten everything `flatten_minutes_before_close` before the close, so
     no position is ever held overnight. Early closes are handled.

Sizing is risk-based: shares = risk_per_trade / |entry - stop|, capped by
`max_position_notional` and available cash. One trade per symbol per day:
whichever side breaks first.

The strategy only ever closes the position it opened itself, so it never
touches other holdings in the same account.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from ..market_hours import MarketCalendar
from ..models import ONE, ZERO, MarketSnapshot, Order, OrderRequest, Side, TradePlan
from .base import Strategy, bps

log = logging.getLogger(__name__)


@dataclass
class _Day:
    session_open: float  # identifies the session (several demo sessions share a date)
    high: Decimal | None = None
    low: Decimal | None = None
    position: Decimal = ZERO  # opened by this strategy: > 0 long, < 0 short
    entry: Decimal | None = None
    stop: Decimal | None = None
    target: Decimal | None = None
    traded: bool = False  # one round trip per day
    skipped: str = ""  # why today was skipped, if it was


class OpeningRangeBreakout(Strategy):
    def __init__(
        self,
        name: str,
        venue: str,
        symbols: list[str],
        fees: Mapping[str, Decimal] | None = None,
        calendar: MarketCalendar | None = None,
        range_minutes: float = 15,
        entry_cutoff_minutes: float = 120,
        flatten_minutes_before_close: float = 5,
        breakout_buffer_bps: Decimal = Decimal("5"),
        min_range_bps: Decimal = Decimal("20"),
        max_range_bps: Decimal = Decimal("300"),
        risk_per_trade: Decimal = Decimal("50"),
        max_position_notional: Decimal = Decimal("5000"),
        take_profit_r: Decimal = Decimal("2"),
        max_slippage_bps: Decimal = Decimal("10"),
        allow_short: bool = False,
        quote_currency: str = "USD",
    ):
        super().__init__(name, fees)
        if not range_minutes < entry_cutoff_minutes:
            raise ValueError("entry_cutoff_minutes must be after the opening range ends")
        self.venue = venue
        self.symbols = symbols
        self.calendar = calendar or MarketCalendar()
        self.range_minutes = range_minutes
        self.entry_cutoff_minutes = entry_cutoff_minutes
        self.flatten_minutes = flatten_minutes_before_close
        self.buffer = breakout_buffer_bps / 10_000
        self.min_range_bps = min_range_bps
        self.max_range_bps = max_range_bps
        self.risk_per_trade = risk_per_trade
        self.max_position_notional = max_position_notional
        self.take_profit_r = take_profit_r
        self.slippage = max_slippage_bps / 10_000
        self.allow_short = allow_short
        self.quote = quote_currency
        self.days: dict[str, _Day] = {}

    def subscriptions(self) -> list[tuple[str, str]]:
        return [(self.venue, s) for s in self.symbols]

    def on_snapshot(self, snap: MarketSnapshot) -> list[TradePlan]:
        session = self.calendar.session_at(snap.ts)
        if session is None:
            return []
        since_open = session.minutes_since_open(snap.ts)
        to_close = session.minutes_to_close(snap.ts)
        cash = snap.balance(self.venue, self.quote)
        plans = []
        for symbol in self.symbols:
            book = snap.book(self.venue, symbol)
            if book is None or book.mid is None:
                continue
            st = self.days.get(symbol)
            if st is None or st.session_open != session.open_ts:
                if st is not None and st.position != 0:
                    # Can't happen with a flatten window; flag it rather than forget the shares.
                    log.error("%s: %s still has a position of %s from the previous session", self.name, symbol, st.position)
                st = self.days[symbol] = _Day(session.open_ts)
                if since_open > self.range_minutes:
                    st.skipped = "started after the opening range"
            plan = self._evaluate(symbol, st, book, since_open, to_close, cash)
            if plan:
                plans.append(plan)
                cash -= plan.legs[0].notional()  # budget shorts like longs: conservative
        return plans

    def _evaluate(self, symbol, st: _Day, book, since_open, to_close, cash) -> TradePlan | None:
        if st.skipped:
            return None
        if since_open < self.range_minutes:
            mid = book.mid
            st.high = mid if st.high is None else max(st.high, mid)
            st.low = mid if st.low is None else min(st.low, mid)
            return None
        if st.high is None:
            st.skipped = "no prices during the opening range"
            return None

        if st.position != 0:
            return self._exit(symbol, st, book, to_close)
        if st.traded or since_open > self.entry_cutoff_minutes or to_close <= self.flatten_minutes:
            return None

        width = bps((st.high - st.low) / st.low)
        if not self.min_range_bps <= width <= self.max_range_bps:
            st.skipped = f"range {width:.0f} bps outside [{self.min_range_bps}, {self.max_range_bps}]"
            return None
        ask, bid = book.best_ask, book.best_bid
        if ask is not None and ask.price > st.high * (ONE + self.buffer):
            return self._enter(symbol, st, Side.BUY, ask.price, stop=st.low, cash=cash)
        if self.allow_short and bid is not None and bid.price < st.low * (ONE - self.buffer):
            return self._enter(symbol, st, Side.SELL, bid.price, stop=st.high, cash=cash)
        return None

    def _enter(self, symbol, st: _Day, side: Side, price: Decimal, stop: Decimal, cash: Decimal) -> TradePlan | None:
        limit = price * (ONE + self.slippage) if side is Side.BUY else price * (ONE - self.slippage)
        affordable = cash / (limit * (ONE + self.fee(self.venue)))
        shares = min(self.risk_per_trade / abs(price - stop), self.max_position_notional / limit, affordable)
        shares = shares.quantize(ONE, rounding=ROUND_DOWN)
        if shares <= 0:
            return None
        st.stop = stop
        leg = OrderRequest(self.venue, symbol, side, shares, limit, strategy=self.name)
        if side is Side.BUY:
            why = f"ORB long {symbol}: ask {price} > range high {st.high} (stop {stop})"
        else:
            why = f"ORB short {symbol}: bid {price} < range low {st.low} (stop {stop})"
        return TradePlan(self.name, (leg,), atomic=False, reason=why)

    def _exit(self, symbol, st: _Day, book, to_close) -> TradePlan | None:
        long = st.position > 0
        # A long exits by selling into the bid; a short covers by buying the ask.
        top = book.best_bid if long else book.best_ask
        if top is None:
            return None
        px = top.price
        if to_close <= self.flatten_minutes:
            why = f"flatten {to_close:.1f} min before close"
        elif (px <= st.stop) if long else (px >= st.stop):
            why = f"stop: {px} through {st.stop}"
        elif st.target is not None and ((px >= st.target) if long else (px <= st.target)):
            why = f"target: {px} reached {st.target}"
        else:
            return None
        side = Side.SELL if long else Side.BUY
        limit = px * (ONE - self.slippage) if long else px * (ONE + self.slippage)
        leg = OrderRequest(self.venue, symbol, side, abs(st.position), limit, strategy=self.name)
        return TradePlan(self.name, (leg,), atomic=False, reason=f"ORB {'exit' if long else 'cover'} {symbol}: {why}")

    def on_orders(self, plan: TradePlan, orders: list[Order]) -> None:
        for o in orders:
            st = self.days.get(o.request.symbol)
            if st is None or o.filled <= 0:
                continue
            opening = st.position == 0 and not st.traded
            st.position += o.filled if o.request.side is Side.BUY else -o.filled
            if opening:
                st.traded = True
                st.entry = o.avg_price or o.request.price
                if self.take_profit_r > 0:
                    # entry - stop is negative for a short, putting the target below entry.
                    st.target = st.entry + self.take_profit_r * (st.entry - st.stop)
