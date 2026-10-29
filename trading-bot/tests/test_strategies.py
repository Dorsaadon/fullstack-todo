from tradebot.models import MarketSnapshot, Order, OrderStatus, Side
from tradebot.strategies import CrossExchangeArbitrage, MeanReversion, PredictionMarketArbitrage, TriangularArbitrage

from conftest import D, book


def snap(books, balances):
    return MarketSnapshot({(b.venue, b.symbol): b for b in books}, balances)


def cross(min_edge=10):
    return CrossExchangeArbitrage(
        "x", "BTC/USDT", ["a", "b"], fees={"a": D("0.001"), "b": D("0.001")},
        min_edge_bps=D(min_edge), max_size=D(1), quote_currency="USDT",
    )


def test_cross_arb_finds_profitable_direction_and_sizes_by_depth():
    s = snap(
        [book("a", "BTC/USDT", [(99, 5)], [(100, "0.3"), (100.5, "0.3"), (110, 5)]),
         book("b", "BTC/USDT", [(101, 5)], [(102, 5)])],
        {"a": {"USDT": D(10_000)}, "b": {"BTC": D(1)}},
    )
    [plan] = cross().on_snapshot(s)
    buy, sell = plan.legs
    assert (buy.venue, buy.side, sell.venue, sell.side) == ("a", Side.BUY, "b", Side.SELL)
    assert buy.size == sell.size == D("0.6")  # stops before the 110 level kills the edge
    assert buy.price == D("100.5") and sell.price == D(101)
    assert plan.expected_profit > 0


def test_cross_arb_respects_fees_and_inventory():
    books = [book("a", "BTC/USDT", [(99, 5)], [(100, 5)]), book("b", "BTC/USDT", [(100.15, 5)], [(102, 5)])]
    # 15 bps gross spread < 20 bps of fees
    assert cross(min_edge=0).on_snapshot(snap(books, {"a": {"USDT": D(1e6)}, "b": {"BTC": D(5)}})) == []
    books[1] = book("b", "BTC/USDT", [(101, 5)], [(102, 5)])
    # profitable, but no BTC on the sell venue
    assert cross().on_snapshot(snap(books, {"a": {"USDT": D(1e6)}, "b": {}})) == []


def test_prediction_basket_buys_when_asks_sum_below_one():
    st = PredictionMarketArbitrage("pm", "poly", ["YES", "NO"], min_edge_bps=D(50), max_size=D(100))
    s = snap(
        [book("poly", "YES", [(0.40, 500)], [(0.45, 60), (0.47, 500)]),
         book("poly", "NO", [(0.50, 500)], [(0.52, 500)])],
        {"poly": {"USDC": D(1000)}},
    )
    [plan] = st.on_snapshot(s)
    assert all(l.side is Side.BUY for l in plan.legs)
    assert plan.legs[0].size == D(100)
    assert plan.expected_profit == D(100) - (D(60) * D("0.45") + D(40) * D("0.47") + D(100) * D("0.52"))


def test_prediction_basket_ignores_fair_market_and_sells_only_with_inventory():
    st = PredictionMarketArbitrage("pm", "poly", ["YES", "NO"], min_edge_bps=D(50))
    fair = [book("poly", "YES", [(0.49, 500)], [(0.51, 500)]), book("poly", "NO", [(0.49, 500)], [(0.51, 500)])]
    assert st.on_snapshot(snap(fair, {"poly": {"USDC": D(1000)}})) == []
    rich = [book("poly", "YES", [(0.55, 500)], [(0.56, 500)]), book("poly", "NO", [(0.50, 500)], [(0.51, 500)])]
    assert st.on_snapshot(snap(rich, {"poly": {"USDC": D(1000)}})) == []
    [plan] = st.on_snapshot(snap(rich, {"poly": {"YES": D(10), "NO": D(10)}}))
    assert all(l.side is Side.SELL and l.size == D(10) for l in plan.legs)


def test_triangular_detects_mispriced_cross_rate():
    st = TriangularArbitrage(
        "tri", "v", ["USDT", "BTC", "ETH"], ["BTC/USDT", "ETH/USDT", "ETH/BTC"],
        fees={"v": D("0.001")}, min_edge_bps=D(10), max_start_amount=D(1000),
    )
    books = [
        book("v", "BTC/USDT", [(59990, 10)], [(60000, 10)]),
        book("v", "ETH/USDT", [(3100, 100)], [(3101, 100)]),
        book("v", "ETH/BTC", [(0.0499, 100)], [(0.05, 100)]),  # implies ETH = 3000: cheap
    ]
    bal = {"v": {"USDT": D(10_000), "BTC": D(1), "ETH": D(10)}}
    [plan] = st.on_snapshot(snap(books, bal))
    assert plan.reason == "USDT -> BTC -> ETH -> USDT"
    assert [(l.symbol, l.side) for l in plan.legs] == [
        ("BTC/USDT", Side.BUY), ("ETH/BTC", Side.BUY), ("ETH/USDT", Side.SELL),
    ]
    assert plan.expected_profit > D(25)  # ~3.3% edge minus fees on $1000


def test_mean_reversion_enters_and_exits():
    st = MeanReversion("mr", "alp", "AAPL", window=5, entry_z=1.5, exit_z=0.0, order_size=D(2))
    plans = []
    for i, px in enumerate([100, 100.1, 99.9, 100, 97]):
        b = book("alp", "AAPL", [(px - 0.01, 100)], [(px + 0.01, 100)], ts=float(i))
        plans = st.on_snapshot(snap([b], {"alp": {"USD": D(10_000)}}))
    [entry] = plans
    assert entry.legs[0].side is Side.BUY and entry.legs[0].size == D(2)
    st.on_orders(entry, [Order(entry.legs[0], "1", OrderStatus.FILLED, filled=D(2), avg_price=D(97))])
    b = book("alp", "AAPL", [(101, 100)], [(101.02, 100)], ts=10.0)
    # The account holds 50 more AAPL that the strategy didn't buy; it sells only its 2.
    [exit_] = st.on_snapshot(snap([b], {"alp": {"USD": D(9_800), "AAPL": D(52)}}))
    assert exit_.legs[0].side is Side.SELL and exit_.legs[0].size == D(2)
