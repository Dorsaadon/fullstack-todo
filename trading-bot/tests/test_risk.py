from tradebot.models import MarketSnapshot, OrderRequest, Side, TradePlan
from tradebot.risk import RiskLimits, RiskManager

from conftest import D, book

PRICES = {"USDT": D(1), "BTC": D(100)}


class Clock:
    def __init__(self, t=1_700_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def setup(clock=None, **limits):
    clock = clock or Clock()
    rm = RiskManager(RiskLimits(**limits), assets_of=lambda v, s: tuple(s.split("/")), clock=clock)
    b = book("v", "BTC/USDT", [(99, 10)], [(101, 10)], ts=clock())
    return rm, MarketSnapshot({("v", "BTC/USDT"): b}, {"v": {"USDT": D(1000)}}), clock


def plan(size, side=Side.BUY, profit=D(1)):
    return TradePlan("s", (OrderRequest("v", "BTC/USDT", side, D(size), D(101)),), profit)


def test_order_and_position_limits(no_kill_file):
    rm, s, _ = setup(max_order_notional=D(500), max_position_notional=D(300))
    assert rm.check(plan(2), s, PRICES) is None
    assert "order notional" in rm.check(plan(6), s, PRICES)
    assert "position" in rm.check(plan(4), s, PRICES)


def test_stale_books_rejected(no_kill_file):
    rm, s, clock = setup(max_book_age_s=2)
    clock.t += 5
    assert "stale" in rm.check(plan(1), s, PRICES)


def test_rate_limit(no_kill_file):
    rm, s, _ = setup(max_orders_per_minute=3)
    rm.record_orders(3)
    assert rm.check(plan(1), s, PRICES) == "order rate limit"


def test_daily_loss_halts(no_kill_file):
    rm, s, clock = setup(max_daily_loss=D(100))
    rm.update_equity(D(10_000))
    rm.update_equity(D(9_950))
    assert not rm.halted and rm.daily_pnl == D(-50)
    rm.update_equity(D(9_899))
    assert rm.halted and "halted" in rm.check(plan(1), s, PRICES)


def test_kill_switch_file(no_kill_file, tmp_path):
    rm, s, _ = setup(kill_switch_file="KILL")
    (tmp_path / "KILL").touch()
    assert "halted" in rm.check(plan(1), s, PRICES)
