from datetime import date, datetime

import pytest

from tradebot.market_hours import NEW_YORK, MarketCalendar, RollingSessionCalendar, easter, nyse_early_closes, nyse_holidays


def ts(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=NEW_YORK).timestamp()


def iso(days):
    return sorted(d.isoformat() for d in days)


# Published NYSE schedules.
@pytest.mark.parametrize("year,holidays,early", [
    (2024, ["2024-01-01", "2024-01-15", "2024-02-19", "2024-03-29", "2024-05-27", "2024-06-19", "2024-07-04",
            "2024-09-02", "2024-11-28", "2024-12-25"], ["2024-07-03", "2024-11-29", "2024-12-24"]),
    (2025, ["2025-01-01", "2025-01-20", "2025-02-17", "2025-04-18", "2025-05-26", "2025-06-19", "2025-07-04",
            "2025-09-01", "2025-11-27", "2025-12-25"], ["2025-07-03", "2025-11-28", "2025-12-24"]),
    (2026, ["2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03",
            "2026-09-07", "2026-11-26", "2026-12-25"], ["2026-11-27", "2026-12-24"]),
    # New Year's 2022 fell on a Saturday and was not observed; Juneteenth on Sunday -> Monday.
    (2022, ["2022-01-17", "2022-02-21", "2022-04-15", "2022-05-30", "2022-06-20", "2022-07-04", "2022-09-05",
            "2022-11-24", "2022-12-26"], ["2022-11-25"]),
])
def test_nyse_holidays_match_published_calendar(year, holidays, early):
    assert iso(nyse_holidays(year)) == holidays
    assert iso(nyse_early_closes(year)) == early


def test_easter():
    assert [easter(y) for y in (2024, 2025, 2026, 2038)] == [
        date(2024, 3, 31), date(2025, 4, 20), date(2026, 4, 5), date(2038, 4, 25)]


def test_regular_and_early_sessions_across_dst():
    cal = MarketCalendar()
    winter = cal.session(date(2026, 1, 5))
    summer = cal.session(date(2026, 7, 1))
    assert winter.open.utcoffset().total_seconds() == -5 * 3600
    assert summer.open.utcoffset().total_seconds() == -4 * 3600
    assert (winter.close - winter.open).seconds == 6.5 * 3600
    early = cal.session(date(2026, 11, 27))
    assert early.close.hour == 13


def test_is_open_boundaries_weekends_and_holidays():
    cal = MarketCalendar()
    assert not cal.is_open(ts(2026, 10, 29, 9, 29))
    assert cal.is_open(ts(2026, 10, 29, 9, 30))
    assert cal.is_open(ts(2026, 10, 29, 15, 59))
    assert not cal.is_open(ts(2026, 10, 29, 16, 0))
    assert not cal.is_open(ts(2026, 10, 31, 12, 0))  # Saturday
    assert not cal.is_open(ts(2026, 11, 26, 12, 0))  # Thanksgiving
    assert not cal.is_open(ts(2026, 11, 27, 13, 30))  # after the early close


def test_next_session_skips_weekend_and_holiday():
    cal = MarketCalendar()
    # Friday after the close, before a Monday that is MLK Day
    nxt = cal.next_session(ts(2026, 1, 16, 17, 0))
    assert nxt.day == date(2026, 1, 20)
    assert cal.next_session(ts(2026, 1, 20, 10, 0)).day == date(2026, 1, 20)  # in progress


def test_extra_holidays_and_early_closes():
    cal = MarketCalendar(extra_holidays=["2025-01-09"], extra_early_closes=["2026-10-30"])
    assert not cal.is_open(ts(2025, 1, 9, 12, 0))  # day of mourning
    assert cal.session(date(2026, 10, 30)).close.hour == 13


def test_rolling_demo_sessions():
    t = [1000.0]
    cal = RollingSessionCalendar(session_minutes=1, gap_minutes=1, clock=lambda: t[0])
    s = cal.session_at(1030)
    assert s.open_ts == 1000 and s.close_ts == 1060
    assert cal.session_at(1090) is None
    assert cal.next_session(1090).open_ts == 1120
    assert cal.session_at(1150).open_ts == 1120
