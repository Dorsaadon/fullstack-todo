"""Pre-trade risk checks and circuit breakers.

Every TradePlan passes through `check` before any order is sent. Orders on a
venue with a market calendar (US equities) are refused outside its session,
whatever the strategy thinks. A sell that would take a position below zero is
refused unless the venue is configured for shorting, so a strategy bug can't
open an accidental short on a margin account. The manager halts all trading (until restarted) when the daily loss limit is hit, when the
kill-switch file appears, or when the engine reports an unrecoverable leg.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from ..market_hours import MarketCalendar
from ..models import ZERO, MarketSnapshot, Side, TradePlan
from .portfolio import AssetsOf

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RiskLimits:
    max_order_notional: Decimal = Decimal("1000")  # USD per order
    max_position_notional: Decimal = Decimal("5000")  # USD per asset per venue
    max_daily_loss: Decimal = Decimal("250")  # USD drawdown from start-of-day equity
    max_orders_per_minute: int = 30
    max_book_age_s: float = 5.0
    min_plan_profit: Decimal = ZERO  # USD-ish; atomic plans must expect at least this
    kill_switch_file: str | None = "KILL"


class RiskManager:
    def __init__(
        self,
        limits: RiskLimits,
        assets_of: AssetsOf,
        clock=time.time,
        calendars: dict[str, MarketCalendar] | None = None,
        short_venues: set[str] | None = None,
    ):
        self.limits = limits
        self.calendars = calendars or {}  # venue -> trading hours; absent = 24/7
        self.short_venues = short_venues or set()
        self.on_halt: list = []  # callbacks(reason), e.g. alerts and an immediate state save
        self.assets_of = assets_of
        self.clock = clock
        self.halted = False
        self.halt_reason = ""
        self._order_times: deque[float] = deque()
        self._day: str | None = None
        self.day_start_equity: Decimal | None = None
        self.equity: Decimal | None = None

    def halt(self, reason: str) -> None:
        if not self.halted:
            log.critical("TRADING HALTED: %s", reason)
            self.halted, self.halt_reason = True, reason
            for callback in self.on_halt:
                callback(reason)

    def clear_halt(self) -> None:
        self.halted, self.halt_reason = False, ""

    def get_state(self) -> dict:
        return {
            "day": self._day,
            "day_start_equity": str(self.day_start_equity) if self.day_start_equity is not None else None,
            "halted": self.halted,
            "halt_reason": self.halt_reason,
        }

    def set_state(self, state: dict) -> None:
        self._day = state.get("day")
        dse = state.get("day_start_equity")
        self.day_start_equity = Decimal(dse) if dse is not None else None
        self.halted = state.get("halted", False)
        self.halt_reason = state.get("halt_reason", "")

    def update_equity(self, equity: Decimal) -> None:
        day = datetime.fromtimestamp(self.clock(), timezone.utc).date().isoformat()
        if day != self._day:
            self._day, self.day_start_equity = day, equity
        self.equity = equity
        loss = self.day_start_equity - equity
        if loss >= self.limits.max_daily_loss:
            self.halt(f"daily loss {loss:.2f} >= limit {self.limits.max_daily_loss}")

    @property
    def daily_pnl(self) -> Decimal:
        if self.equity is None or self.day_start_equity is None:
            return ZERO
        return self.equity - self.day_start_equity

    def record_orders(self, n: int) -> None:
        now = self.clock()
        self._order_times.extend([now] * n)

    def check(self, plan: TradePlan, snap: MarketSnapshot, prices: dict[str, Decimal]) -> str | None:
        """Return None if the plan may trade, else the reason it may not."""
        if self.limits.kill_switch_file and Path(self.limits.kill_switch_file).exists():
            self.halt(f"kill switch file {self.limits.kill_switch_file!r} present")
        if self.halted:
            return f"halted: {self.halt_reason}"

        now = self.clock()
        while self._order_times and now - self._order_times[0] > 60:
            self._order_times.popleft()
        if len(self._order_times) + len(plan.legs) > self.limits.max_orders_per_minute:
            return "order rate limit"

        if plan.atomic and plan.expected_profit < self.limits.min_plan_profit:
            return f"expected profit {plan.expected_profit:.4f} below minimum"

        for leg in plan.legs:
            cal = self.calendars.get(leg.venue)
            if cal is not None and not cal.is_open(now):
                return f"{leg.venue} market closed"
            book = snap.book(leg.venue, leg.symbol)
            if book is None:
                return f"no book for {leg.venue}:{leg.symbol}"
            age = snap.data_age(leg.venue, leg.symbol, now)
            if age > self.limits.max_book_age_s:
                return f"stale book {leg.venue}:{leg.symbol} ({age:.1f}s old)"
            base, quote = self.assets_of(leg.venue, leg.symbol)
            if quote not in prices:
                return f"cannot value {quote}"
            ref = leg.price if leg.price is not None else book.mid
            if ref is None:
                return f"no reference price for {leg.venue}:{leg.symbol}"
            notional = leg.size * ref * prices[quote]
            if notional > self.limits.max_order_notional:
                return f"order notional {notional:.2f} > {self.limits.max_order_notional}"
            held = snap.balance(leg.venue, base)
            after = held + leg.size if leg.side is Side.BUY else held - leg.size
            if after < 0 and leg.venue not in self.short_venues:
                return f"sell would open a short in {leg.venue}:{base}; shorting is not enabled there"
            # Trades that shrink a position (stops, covers) are always allowed.
            if abs(after) > abs(held) and base in prices:
                exposure = abs(after) * prices[base]
                if exposure > self.limits.max_position_notional:
                    return f"position {leg.venue}:{base} would be {exposure:.2f} > {self.limits.max_position_notional}"
        return None
