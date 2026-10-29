"""US equity market calendar: regular sessions, holidays and early closes.

Holidays follow NYSE rules and are computed, so no data files or network
calls are needed:
  * fixed-date holidays falling on Saturday are observed Friday, on Sunday
    Monday, except New Year's Day on a Saturday, which is not observed;
  * 1:00 pm ET early closes on July 3 (when July 4 is Tue-Fri), the day
    after Thanksgiving, and Christmas Eve (Mon-Thu).

Unscheduled closures (national days of mourning, emergencies) can't be
predicted; add them with `extra_holidays`. Only regular hours are modelled,
not pre-market or after-hours.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as dtime
from functools import lru_cache
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")
OPEN, CLOSE, EARLY_CLOSE = dtime(9, 30), dtime(16, 0), dtime(13, 0)
MON, TUE, THU, FRI, SAT, SUN = 0, 1, 3, 4, 5, 6


@dataclass(frozen=True)
class Session:
    open: datetime  # tz-aware
    close: datetime

    @property
    def open_ts(self) -> float:
        return self.open.timestamp()

    @property
    def close_ts(self) -> float:
        return self.close.timestamp()

    @property
    def day(self) -> date:
        return self.open.date()

    def minutes_since_open(self, ts: float) -> float:
        return (ts - self.open_ts) / 60

    def minutes_to_close(self, ts: float) -> float:
        return (self.close_ts - ts) / 60


def easter(year: int) -> date:
    """Gregorian Easter Sunday (anonymous Gregorian algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    last = (date(year, month + 1, 1) if month < 12 else date(year + 1, 1, 1)) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(d: date) -> date:
    return d - timedelta(days=1) if d.weekday() == SAT else d + timedelta(days=1) if d.weekday() == SUN else d


@lru_cache(maxsize=64)
def nyse_holidays(year: int) -> frozenset[date]:
    days = {
        _nth_weekday(year, 1, MON, 3),  # Martin Luther King Jr. Day
        _nth_weekday(year, 2, MON, 3),  # Washington's Birthday
        easter(year) - timedelta(days=2),  # Good Friday
        _last_weekday(year, 5, MON),  # Memorial Day
        _observed(date(year, 7, 4)),  # Independence Day
        _nth_weekday(year, 9, MON, 1),  # Labor Day
        _nth_weekday(year, 11, THU, 4),  # Thanksgiving
        _observed(date(year, 12, 25)),  # Christmas
    }
    if date(year, 1, 1).weekday() != SAT:  # a Saturday New Year's is not observed
        days.add(_observed(date(year, 1, 1)))
    if year >= 2022:
        days.add(_observed(date(year, 6, 19)))  # Juneteenth
    return frozenset(days)


@lru_cache(maxsize=64)
def nyse_early_closes(year: int) -> frozenset[date]:
    days = {_nth_weekday(year, 11, THU, 4) + timedelta(days=1)}  # day after Thanksgiving
    if date(year, 7, 4).weekday() in (TUE, 2, THU, FRI):
        days.add(date(year, 7, 3))
    if date(year, 12, 24).weekday() in (MON, TUE, 2, THU):
        days.add(date(year, 12, 24))
    return frozenset(days)


class MarketCalendar:
    """Regular-hours sessions for US equities (NYSE / Nasdaq)."""

    def __init__(self, extra_holidays=(), extra_early_closes=(), clock=time.time):
        self.extra_holidays = {date.fromisoformat(str(d)) for d in extra_holidays}
        self.extra_early_closes = {date.fromisoformat(str(d)) for d in extra_early_closes}
        self.clock = clock

    def is_trading_day(self, d: date) -> bool:
        return d.weekday() < SAT and d not in nyse_holidays(d.year) and d not in self.extra_holidays

    def session(self, d: date) -> Session | None:
        if not self.is_trading_day(d):
            return None
        early = d in nyse_early_closes(d.year) or d in self.extra_early_closes
        return Session(
            datetime.combine(d, OPEN, NEW_YORK), datetime.combine(d, EARLY_CLOSE if early else CLOSE, NEW_YORK)
        )

    def session_at(self, ts: float | None = None) -> Session | None:
        """The session in progress at `ts`, or None if the market is closed."""
        ts = self.clock() if ts is None else ts
        s = self.session(datetime.fromtimestamp(ts, NEW_YORK).date())
        return s if s and s.open_ts <= ts < s.close_ts else None

    def is_open(self, ts: float | None = None) -> bool:
        return self.session_at(ts) is not None

    def next_session(self, ts: float | None = None) -> Session:
        """The session in progress, or the next one to open."""
        ts = self.clock() if ts is None else ts
        d = datetime.fromtimestamp(ts, NEW_YORK).date()
        for _ in range(15):  # the longest closure streaks are far shorter
            s = self.session(d)
            if s and ts < s.close_ts:
                return s
            d += timedelta(days=1)
        raise RuntimeError("no trading session in the next 15 days")


class RollingSessionCalendar(MarketCalendar):
    """Demo/test calendar: back-to-back sessions of `session_minutes`, the
    first opening when the calendar is created. Lets intraday strategies run
    end to end at any hour."""

    def __init__(self, session_minutes: float = 5.0, gap_minutes: float = 0.0, clock=time.time):
        super().__init__(clock=clock)
        self.length = session_minutes * 60
        self.period = self.length + gap_minutes * 60
        self.t0 = clock()

    def session_at(self, ts: float | None = None) -> Session | None:
        ts = self.clock() if ts is None else ts
        if ts < self.t0:
            return None
        start = self.t0 + ((ts - self.t0) // self.period) * self.period
        if ts >= start + self.length:
            return None
        return Session(datetime.fromtimestamp(start, NEW_YORK), datetime.fromtimestamp(start + self.length, NEW_YORK))

    def next_session(self, ts: float | None = None) -> Session:
        ts = self.clock() if ts is None else ts
        s = self.session_at(ts)
        if s:
            return s
        start = self.t0 + (max(ts - self.t0, 0) // self.period + (ts >= self.t0)) * self.period
        return Session(datetime.fromtimestamp(start, NEW_YORK), datetime.fromtimestamp(start + self.length, NEW_YORK))
