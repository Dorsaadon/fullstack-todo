import asyncio
from datetime import datetime
from pathlib import Path

import pytest

from tradebot.config import ConfigError, build_engine
from tradebot.exchanges.paper import PaperExchange
from tradebot.market_hours import NEW_YORK, MarketCalendar
from tradebot.models import MarketSnapshot, OrderRequest, OrderStatus, Side, TimeInForce, TradePlan
from tradebot.risk import RiskLimits, RiskManager
from tradebot.strategies import OpeningRangeBreakout

from conftest import D, book

DEMO = Path(__file__).parent.parent / "config" / "demo.toml"


def run(coro):
    return asyncio.run(coro)


def venue(cash=1000, clock=None, **kw):
    ex = PaperExchange("p", balances={"USD": D(cash)}, allow_short=True, clock=clock or (lambda: 0.0), **kw)
    ex.set_order_book(book("p", "XYZ", [(100, 1000)], [(100, 1000)]))
    return ex


def sell(ex, size, tif=TimeInForce.IOC, symbol="XYZ", price=None):
    return run(ex.place_order(OrderRequest("p", symbol, Side.SELL, D(size), price, tif)))


def buy(ex, size, symbol="XYZ", price=None):
    return run(ex.place_order(OrderRequest("p", symbol, Side.BUY, D(size), price)))


# --- paper exchange ----------------------------------------------------------

def test_short_sale_credits_proceeds_and_goes_negative():
    ex = venue(cash=10_000)
    o = sell(ex, 10)
    assert o.status is OrderStatus.FILLED
    assert ex.balances["XYZ"] == D(-10) and ex.balances["USD"] == D(11_000)
    assert ex.short_value() == D(1000)


def test_initial_margin_caps_short_size():
    ex = venue(cash=1000)  # 1000 + V >= 1.5 V  ->  V <= 2000  ->  20 shares
    o = sell(ex, 30)
    assert o.status is OrderStatus.CANCELED  # IOC partial
    assert abs(o.filled - 20) < D("1e-9")
    assert ex.balances["USD"] >= ex.short_value() * D("1.5")
    assert sell(venue(cash=1000), 30, TimeInForce.FOK).status is OrderStatus.REJECTED


def test_cover_realizes_pnl():
    ex = venue(cash=10_000)
    sell(ex, 10)
    ex.set_order_book(book("p", "XYZ", [(90, 1000)], [(90, 1000)]))
    o = buy(ex, 10)
    assert o.filled == D(10) and ex.balances["XYZ"] == 0
    assert ex.balances["USD"] == D(10_100)  # sold 1000, bought back 900


def test_borrow_fee_accrues_on_short_value():
    t = [0.0]
    ex = venue(cash=10_000, clock=lambda: t[0], borrow_rate_annual=D("0.365"))
    sell(ex, 10)  # 1000 short value
    t[0] = 86_400.0  # one day: 1000 * 0.365 / 365 = 1
    run(ex.fetch_balances())
    assert abs(ex.balances["USD"] - D(10_999)) < D("1e-9")
    assert abs(ex.borrow_fees_paid - 1) < D("1e-9")


def test_margin_also_limits_buys_while_short():
    ex = venue(cash=1000)
    ex.set_order_book(book("p", "ABC", [(50, 1000)], [(50, 1000)]))
    sell(ex, 10)  # cash 2000, needs 1500
    o = buy(ex, 100, symbol="ABC")  # only 500 spare
    assert abs(o.filled - 10) < D("1e-9")


def test_long_only_venue_never_goes_negative():
    ex = PaperExchange("p", balances={"USD": D(1000), "XYZ": D(5)})
    ex.set_order_book(book("p", "XYZ", [(100, 1000)], [(100, 1000)]))
    o = sell(ex, 10)
    assert o.filled == D(5) and ex.balances["XYZ"] == 0


def test_cannot_short_through_cross_pairs():
    ex = PaperExchange("p", balances={"USDT": D(1000), "BTC": D("0.01")}, allow_short=True, quote_currency="USDT")
    ex.set_order_book(book("p", "ETH/BTC", [("0.05", 100)], [("0.05", 100)]))
    assert sell(ex, 1, symbol="ETH/BTC").status is OrderStatus.REJECTED  # not quoted in USDT
    o = buy(ex, 1, symbol="ETH/BTC")  # paying in BTC must not go negative either
    assert abs(o.filled - D("0.2")) < D("1e-9") and ex.balances["BTC"] >= 0


# --- risk manager -------------------------------------------------------------

def risk(short_venues=()):
    return RiskManager(
        RiskLimits(max_order_notional=D(100_000), max_position_notional=D(5000), kill_switch_file=None),
        assets_of=lambda v, s: (s, "USD"),
        short_venues=set(short_venues),
    )


def plan(side, size):
    return TradePlan("s", (OrderRequest("p", "XYZ", side, D(size), D(100)),), atomic=False)


def snap(held):
    b = book("p", "XYZ", [(100, 1000)], [(100, 1000)])
    return MarketSnapshot({("p", "XYZ"): b}, {"p": {"USD": D(100_000), "XYZ": D(held)}})


PRICES = {"USD": D(1), "XYZ": D(100)}


def test_risk_refuses_accidental_shorts():
    assert "shorting is not enabled" in risk().check(plan(Side.SELL, 11), snap(10), PRICES)
    assert risk().check(plan(Side.SELL, 10), snap(10), PRICES) is None  # selling down to flat is fine
    assert risk({"p"}).check(plan(Side.SELL, 11), snap(10), PRICES) is None


def test_position_cap_applies_to_shorts_but_never_blocks_covering():
    r = risk({"p"})
    assert "position" in r.check(plan(Side.SELL, 60), snap(0), PRICES)  # 6000 short > 5000
    assert r.check(plan(Side.SELL, 40), snap(0), PRICES) is None
    assert r.check(plan(Side.BUY, 70), snap(-80), PRICES) is None  # covering: exposure shrinks


# --- ORB short side -----------------------------------------------------------

def at(hh, mm):
    return datetime(2026, 10, 29, hh, mm, tzinfo=NEW_YORK).timestamp()


def orb(**kw):
    opts = dict(range_minutes=15, breakout_buffer_bps=D(0), min_range_bps=D(10), risk_per_trade=D(100),
                max_position_notional=D(100_000), take_profit_r=D(2), max_slippage_bps=D(0), allow_short=True)
    opts.update(kw)
    return OpeningRangeBreakout("orb", "p", ["XYZ"], calendar=MarketCalendar(), **opts)


def msnap(t, mid, cash=D(100_000)):
    b = book("p", "XYZ", [(mid - D("0.01"), 1000)], [(mid + D("0.01"), 1000)], ts=t)
    return MarketSnapshot({("p", "XYZ"): b}, {"p": {"USD": cash}}, ts=t)


def build_range(s):
    for minute, px in [(30, 100), (35, 101), (40, 99), (44, 100)]:
        assert s.on_snapshot(msnap(at(9, minute), D(px))) == []


def test_orb_shorts_breakdown_and_covers_at_target():
    s = orb()
    build_range(s)
    [entry] = s.on_snapshot(msnap(at(10, 0), D("98.51")))
    leg = entry.legs[0]
    assert leg.side is Side.SELL and leg.price == D("98.50") and "short" in entry.reason
    assert leg.size == D(40)  # $100 risk / (101 stop - 98.50)
    ex = PaperExchange("p", balances={"USD": D(100_000)}, allow_short=True)
    ex.set_order_book(msnap(at(10, 0), D("98.51")).book("p", "XYZ"))
    s.on_orders(entry, [run(ex.place_order(leg))])
    st = s.days["XYZ"]
    assert st.position == D(-40) and st.stop == D(101) and st.target == D("93.50")

    assert s.on_snapshot(msnap(at(10, 30), D(96))) == []
    [cover] = s.on_snapshot(msnap(at(11, 0), D("93.4")))
    assert cover.legs[0].side is Side.BUY and cover.legs[0].size == D(40) and "target" in cover.reason
    ex.set_order_book(msnap(at(11, 0), D("93.4")).book("p", "XYZ"))
    s.on_orders(cover, [run(ex.place_order(cover.legs[0]))])
    assert st.position == 0 and ex.balances["XYZ"] == 0
    assert ex.balances["USD"] == D(100_000) + 40 * (D("98.50") - D("93.41"))
    assert s.on_snapshot(msnap(at(11, 15), D(90))) == []  # one trade per day


def test_orb_short_stop_and_flatten():
    s = orb(take_profit_r=D(0))
    build_range(s)
    entry = s.on_snapshot(msnap(at(10, 0), D(98)))[0]
    from tradebot.models import Order

    s.on_orders(entry, [Order(entry.legs[0], "1", OrderStatus.FILLED, filled=entry.legs[0].size, avg_price=D("97.99"))])
    [stop] = s.on_snapshot(msnap(at(10, 5), D("101.0")))
    assert stop.legs[0].side is Side.BUY and "stop" in stop.reason
    [flat] = s.on_snapshot(msnap(at(15, 55), D(97)))
    assert "flatten" in flat.reason


def test_orb_without_allow_short_ignores_breakdowns():
    s = orb(allow_short=False)
    build_range(s)
    assert s.on_snapshot(msnap(at(10, 0), D(97))) == []


def test_config_rejects_short_strategy_on_long_only_venue(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text(DEMO.read_text().replace("allow_short = true            # paper margin", "allow_short = false            # paper margin"))
    with pytest.raises(ConfigError, match="lacks allow_short"):
        build_engine(cfg)
