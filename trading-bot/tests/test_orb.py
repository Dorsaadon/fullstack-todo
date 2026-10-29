from datetime import datetime

from tradebot.market_hours import NEW_YORK, MarketCalendar
from tradebot.models import MarketSnapshot, Order, OrderRequest, OrderStatus, Side, TradePlan
from tradebot.risk import RiskLimits, RiskManager
from tradebot.strategies import OpeningRangeBreakout

from conftest import D, book

THU = (2026, 10, 29)
EARLY_CLOSE_DAY = (2026, 11, 27)  # day after Thanksgiving: closes 13:00


def at(hh, mm, day=THU):
    return datetime(*day, hh, mm, tzinfo=NEW_YORK).timestamp()


def make(**kw):
    opts = dict(
        range_minutes=15, entry_cutoff_minutes=120, flatten_minutes_before_close=5,
        breakout_buffer_bps=D(0), min_range_bps=D(10), max_range_bps=D(500),
        risk_per_trade=D(100), max_position_notional=D(100_000), take_profit_r=D(2), max_slippage_bps=D(0),
    )
    opts.update(kw)
    return OpeningRangeBreakout("orb", "alp", ["SPY"], calendar=MarketCalendar(), **opts)


def snap(t, mid, cash=D(100_000), spy=D(0)):
    b = book("alp", "SPY", [(mid - D("0.01"), 1000)], [(mid + D("0.01"), 1000)], ts=t)
    return MarketSnapshot({("alp", "SPY"): b}, {"alp": {"USD": cash, "SPY": spy}}, ts=t)


def fill(strat, plan):
    leg = plan.legs[0]
    strat.on_orders(plan, [Order(leg, "1", OrderStatus.FILLED, filled=leg.size, avg_price=leg.price)])


def build_range(strat, day=THU, low=99, high=101):
    for minute, px in [(30, 100), (35, high), (40, low), (44, 100)]:
        assert strat.on_snapshot(snap(at(9, minute, day), D(px))) == []


def test_full_day_entry_target_and_one_trade_per_day():
    s = make()
    build_range(s)
    assert s.on_snapshot(snap(at(9, 50), D("100.5"))) == []  # inside the range
    [entry] = s.on_snapshot(snap(at(10, 0), D("101.49")))
    leg = entry.legs[0]
    assert leg.side is Side.BUY and leg.price == D("101.50")
    assert leg.size == D(40)  # $100 risk / (101.50 entry - 99 stop)
    fill(s, entry)
    st = s.days["SPY"]
    assert st.stop == D(99) and st.target == D("106.50")  # 2R
    assert s.on_snapshot(snap(at(10, 30), D(103))) == []
    [exit_] = s.on_snapshot(snap(at(11, 0), D("106.6")))
    assert exit_.legs[0].side is Side.SELL and exit_.legs[0].size == D(40) and "target" in exit_.reason
    fill(s, exit_)
    assert s.on_snapshot(snap(at(11, 15), D(110))) == []  # no second trade


def test_stop_loss():
    s = make()
    build_range(s)
    fill(s, s.on_snapshot(snap(at(10, 0), D(102)))[0])
    [exit_] = s.on_snapshot(snap(at(10, 5), D("98.99")))
    assert "stop" in exit_.reason


def test_flatten_before_close():
    s = make(take_profit_r=D(0))
    build_range(s)
    fill(s, s.on_snapshot(snap(at(10, 0), D(102)))[0])
    assert s.on_snapshot(snap(at(15, 54), D(103))) == []
    [exit_] = s.on_snapshot(snap(at(15, 55), D(103)))
    assert "flatten" in exit_.reason


def test_flatten_respects_early_close():
    s = make(take_profit_r=D(0))
    build_range(s, day=EARLY_CLOSE_DAY)
    fill(s, s.on_snapshot(snap(at(10, 0, EARLY_CLOSE_DAY), D(102)))[0])
    assert s.on_snapshot(snap(at(12, 54, EARLY_CLOSE_DAY), D(103))) == []
    [exit_] = s.on_snapshot(snap(at(12, 55, EARLY_CLOSE_DAY), D(103)))
    assert "flatten" in exit_.reason


def test_only_sells_its_own_shares():
    s = make(take_profit_r=D(0))
    build_range(s)
    fill(s, s.on_snapshot(snap(at(10, 0), D(102)))[0])
    # The account also holds 500 SPY the strategy didn't buy.
    [exit_] = s.on_snapshot(snap(at(15, 55), D(103), spy=D(540)))
    assert exit_.legs[0].size == D(33)


def test_no_entry_after_cutoff_or_outside_hours():
    s = make()
    build_range(s)
    assert s.on_snapshot(snap(at(11, 31), D(102))) == []  # cutoff is 120 min after open
    assert s.on_snapshot(snap(at(17, 0), D(102))) == []  # after the close
    s2 = make()
    sat = (2026, 10, 31)
    assert s2.on_snapshot(snap(at(10, 0, sat), D(102))) == []


def test_range_filters_and_late_start():
    narrow = make(min_range_bps=D(300))
    build_range(narrow)
    assert narrow.on_snapshot(snap(at(10, 0), D(102))) == []
    assert "range" in narrow.days["SPY"].skipped

    late = make()  # bot started at 10:00, so it never saw the opening range
    assert late.on_snapshot(snap(at(10, 0), D(102))) == []
    assert late.days["SPY"].skipped == "started after the opening range"


def test_sizing_capped_by_cash_and_notional():
    s = make(max_position_notional=D(1000))
    build_range(s)
    [entry] = s.on_snapshot(snap(at(10, 0), D(102)))
    assert entry.legs[0].size == D(9)  # 1000 / 102.01
    s = make()
    build_range(s)
    [entry] = s.on_snapshot(snap(at(10, 0), D(102), cash=D(500)))
    assert entry.legs[0].size == D(4)


def test_new_day_resets_state():
    s = make()
    build_range(s)
    fill(s, s.on_snapshot(snap(at(10, 0), D(102)))[0])
    fill(s, s.on_snapshot(snap(at(15, 55), D(103)))[0])
    friday = (2026, 10, 30)
    build_range(s, day=friday)
    assert s.on_snapshot(snap(at(10, 0, friday), D(102)))  # trades again


def test_risk_refuses_equity_orders_outside_session(no_kill_file):
    t = [at(17, 0)]
    rm = RiskManager(
        RiskLimits(), assets_of=lambda v, sym: (sym, "USD"), clock=lambda: t[0], calendars={"alp": MarketCalendar()}
    )
    plan = TradePlan("x", (OrderRequest("alp", "SPY", Side.BUY, D(1), D(100)),), atomic=False)
    s = snap(t[0], D(100))
    prices = {"USD": D(1), "SPY": D(100)}
    assert rm.check(plan, s, prices) == "alp market closed"
    t[0] = at(10, 0)
    assert rm.check(plan, snap(t[0], D(100)), prices) is None
