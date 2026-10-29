import asyncio

from tradebot.engine import Engine
from tradebot.exchanges.paper import PaperExchange
from tradebot.models import OrderStatus, Side
from tradebot.risk import RiskLimits, RiskManager
from tradebot.strategies import CrossExchangeArbitrage

from conftest import D, book


def build(sell_venue_depth=5):
    a = PaperExchange("a", balances={"USDT": D(10_000)}, quote_currency="USDT")
    b = PaperExchange("b", balances={"BTC": D(1)}, quote_currency="USDT")
    a.set_order_book(book("a", "BTC/USDT", [(99, 5)], [(100, 5)]))
    b.set_order_book(book("b", "BTC/USDT", [(102, sell_venue_depth)], [(103, 5)]))
    strat = CrossExchangeArbitrage("arb", "BTC/USDT", ["a", "b"], min_edge_bps=D(10), max_size=D(1), quote_currency="USDT")
    exchanges = {"a": a, "b": b}
    risk = RiskManager(RiskLimits(kill_switch_file=None), assets_of=lambda v, s: exchanges[v].assets(s))
    return Engine(exchanges, [strat], risk, poll_interval_s=0), a, b


def test_tick_executes_arbitrage_end_to_end():
    engine, a, b = build()
    [(plan, orders)] = asyncio.run(engine.tick())
    assert all(o.status is OrderStatus.FILLED for o in orders)
    assert a.balances["BTC"] == D(1) and a.balances["USDT"] == D(9_900)
    assert b.balances["BTC"] == 0 and b.balances["USDT"] == D(102)


def test_uneven_fill_is_unwound():
    engine, a, b = build()

    real_place = b.place_order

    async def thin_sell(req):  # the sell venue's book moves before our order lands
        b.set_order_book(book("b", "BTC/USDT", [(102, D("0.4")), (90, 5)], [(103, 5)]))
        return await real_place(req)

    b.place_order = thin_sell
    [(plan, orders)] = asyncio.run(engine.tick())
    buy, sell = orders
    assert buy.filled == D(1) and sell.filled == D("0.4")
    # The 0.6 BTC excess bought on venue a was sold back so we stay flat.
    assert a.balances["BTC"] == D("0.4")
    unwind = a.orders[-1]
    assert unwind.request.side is Side.SELL and unwind.filled == D("0.6")
    assert not engine.risk.halted


def test_failed_unwind_halts_trading():
    engine, a, b = build()
    real_place = b.place_order

    async def thin_sell(req):
        b.set_order_book(book("b", "BTC/USDT", [(102, D("0.4"))], [(103, 5)]))
        a.set_order_book(book("a", "BTC/USDT", [(50, D("0.1"))], [(100, 5)]))  # nothing to unwind into
        return await real_place(req)

    b.place_order = thin_sell
    asyncio.run(engine.tick())
    assert engine.risk.halted and "unwind incomplete" in engine.risk.halt_reason
