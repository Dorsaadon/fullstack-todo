from tradebot.models import Side, split_symbol

from conftest import D, book


def test_from_raw_sorts_and_drops_empty_levels():
    b = book("x", "BTC/USDT", [(99, 1), (100, 2), (98, 0)], [(102, 1), (101, 3)])
    assert [lv.price for lv in b.bids] == [D(100), D(99)]
    assert [lv.price for lv in b.asks] == [D(101), D(102)]
    assert b.mid == D("100.5")


def test_sweep_walks_levels_and_respects_limit():
    b = book("x", "S", [(100, 1)], [(101, 1), (102, 2), (103, 5)])
    assert b.sweep(Side.BUY, D(2)) == (D(2), D(203))
    assert b.sweep(Side.BUY, D(10), limit=D(102)) == (D(3), D(305))
    assert b.vwap(Side.BUY, D(3)) == D(305) / 3
    assert b.vwap(Side.BUY, D(100)) is None  # too thin
    assert b.worst_price(Side.BUY, D("1.5")) == D(102)
    assert b.cumulative_sizes(Side.BUY) == [D(1), D(3), D(8)]


def test_split_symbol():
    assert split_symbol("ETH/BTC", "USD") == ("ETH", "BTC")
    assert split_symbol("AAPL", "USD") == ("AAPL", "USD")
