import asyncio

from tradebot.exchanges.paper import PaperExchange
from tradebot.models import OrderRequest, OrderStatus, Side, TimeInForce

from conftest import D, book


def make(balances, fee="0.001"):
    ex = PaperExchange("p", balances={k: D(v) for k, v in balances.items()}, taker_fee=D(fee), quote_currency="USDT")
    ex.set_order_book(book("p", "BTC/USDT", [(100, 1), (99, 1)], [(101, 1), (102, 1)]))
    return ex


def test_buy_walks_book_and_charges_fee():
    ex = make({"USDT": 1000})
    o = asyncio.run(ex.place_order(OrderRequest("p", "BTC/USDT", Side.BUY, D("1.5"), D(102))))
    assert o.status is OrderStatus.FILLED
    assert o.filled == D("1.5")
    cost = D(101) + D("0.5") * D(102)
    assert o.fee == cost * D("0.001")
    assert ex.balances["BTC"] == D("1.5")
    assert ex.balances["USDT"] == D(1000) - cost - o.fee


def test_limit_caps_fill_and_ioc_reports_partial():
    ex = make({"USDT": 1000})
    o = asyncio.run(ex.place_order(OrderRequest("p", "BTC/USDT", Side.BUY, D(2), D(101))))
    assert o.filled == D(1) and o.status is OrderStatus.CANCELED


def test_fok_rejects_partial():
    ex = make({"USDT": 1000})
    o = asyncio.run(ex.place_order(OrderRequest("p", "BTC/USDT", Side.BUY, D(2), D(101), TimeInForce.FOK)))
    assert o.status is OrderStatus.REJECTED and ex.balances["USDT"] == D(1000)


def test_never_overspends_or_oversells():
    ex = make({"USDT": 50, "BTC": D("0.2")}, fee="0")
    buy = asyncio.run(ex.place_order(OrderRequest("p", "BTC/USDT", Side.BUY, D(1), None)))
    assert buy.filled * D(101) <= D(50) and ex.balances["USDT"] >= 0
    sell = asyncio.run(ex.place_order(OrderRequest("p", "BTC/USDT", Side.SELL, D(5), None)))
    assert sell.filled == D("0.2") + buy.filled and ex.balances["BTC"] == 0
