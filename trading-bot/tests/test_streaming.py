import asyncio
import json
from pathlib import Path

import pytest

from tradebot.config import build_engine
from tradebot.engine import Engine
from tradebot.exchanges.base import Exchange
from tradebot.exchanges.paper import PaperExchange
from tradebot.marketdata import MarketDataHub
from tradebot.models import MarketSnapshot, OrderRequest, Side, TradePlan
from tradebot.risk import RiskLimits, RiskManager
from tradebot.strategies import CrossExchangeArbitrage

from conftest import D, book

DEMO = Path(__file__).parent.parent / "config" / "demo.toml"


class ScriptedFeed(Exchange):
    """Data-only venue whose stream is driven by the test through a queue."""

    def __init__(self, name):
        super().__init__(name, quote_currency="USDT")
        self.queue: asyncio.Queue = asyncio.Queue()
        self.connects = 0
        self.latest = {}

    async def watch_order_books(self, symbols, depth=10):
        self.connects += 1
        while True:
            item = await self.queue.get()
            if isinstance(item, Exception):
                raise item
            if item is not None:
                self.latest[item.symbol] = item
            yield item

    async def fetch_order_book(self, symbol, depth=10):
        return self.latest[symbol]

    async def place_order(self, req):
        raise NotImplementedError

    async def cancel_order(self, order_id, symbol):
        pass

    async def fetch_balances(self):
        return {}


async def until(cond, timeout=2.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not cond():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.005)


def test_hub_tracks_books_heartbeats_and_reconnects():
    async def main():
        feed = ScriptedFeed("a")
        hub = MarketDataHub({"a": feed}, initial_backoff_s=0.01)
        await hub.start([("a", "BTC/USDT")])
        await feed.queue.put(book("a", "BTC/USDT", [(100, 1)], [(101, 1)], ts=1.0))
        assert await hub.wait(1.0)
        assert hub.take_dirty() == {("a", "BTC/USDT")}
        first_alive = hub.alive[("a", "BTC/USDT")]

        await asyncio.sleep(0.01)
        await feed.queue.put(None)  # heartbeat: fresher, but not a change
        await until(lambda: hub.alive[("a", "BTC/USDT")] > first_alive)
        assert hub.take_dirty() == set()

        await feed.queue.put(ConnectionError("socket dropped"))
        await until(lambda: feed.connects == 2)
        assert hub.reconnects["a"] == 1
        await feed.queue.put(book("a", "BTC/USDT", [(100, 1)], [(102, 1)]))
        await until(lambda: hub.books[("a", "BTC/USDT")].best_ask.price == D(102))
        await hub.stop()

    asyncio.run(main())


def test_quiet_book_on_live_stream_is_not_stale():
    rm = RiskManager(RiskLimits(max_book_age_s=2, kill_switch_file=None), assets_of=lambda v, s: tuple(s.split("/")))
    b = book("v", "BTC/USDT", [(99, 10)], [(101, 10)], ts=0.0)  # last changed long ago
    plan = TradePlan("s", (OrderRequest("v", "BTC/USDT", Side.BUY, D(1), D(101)),), D(1))
    prices = {"USDT": D(1), "BTC": D(100)}
    stale = MarketSnapshot({("v", "BTC/USDT"): b}, {"v": {"USDT": D(1000)}})
    assert "stale" in rm.check(plan, stale, prices)
    import time

    alive = MarketSnapshot({("v", "BTC/USDT"): b}, {"v": {"USDT": D(1000)}}, alive={("v", "BTC/USDT"): time.time()})
    assert rm.check(plan, alive, prices) is None


def test_engine_reacts_to_streamed_dislocation():
    async def main():
        fa, fb = ScriptedFeed("a"), ScriptedFeed("b")
        a = PaperExchange("a", data_source=fa, balances={"USDT": D(10_000)}, quote_currency="USDT")
        b = PaperExchange("b", data_source=fb, balances={"BTC": D(1)}, quote_currency="USDT")
        arb = CrossExchangeArbitrage("arb", "BTC/USDT", ["a", "b"], min_edge_bps=D(10), max_size=D(1), quote_currency="USDT")
        exchanges = {"a": a, "b": b}
        risk = RiskManager(RiskLimits(kill_switch_file=None), assets_of=lambda v, s: exchanges[v].assets(s))
        engine = Engine(exchanges, [arb], risk, heartbeat_s=0.05, min_tick_interval_s=0)
        task = asyncio.create_task(engine.run())
        await asyncio.sleep(0.05)
        assert risk.equity is None  # warming up: nothing valued before all books arrive

        await fa.queue.put(book("a", "BTC/USDT", [(99, 5)], [(100, 5)]))
        await fb.queue.put(book("b", "BTC/USDT", [(99.5, 5)], [(100.5, 5)]))  # no edge yet
        await asyncio.sleep(0.1)
        assert a.orders == [] and b.orders == []
        assert risk.day_start_equity == D(10_000) + D("99.5")  # baseline includes the BTC

        await fb.queue.put(book("b", "BTC/USDT", [(102, 5)], [(103, 5)]))  # b jumps
        await until(lambda: len(b.orders) == 1)
        assert a.balances["BTC"] == D(1) and b.balances["USDT"] == D(102)

        engine.stop()
        await asyncio.wait_for(task, 2)

    asyncio.run(main())


def test_demo_runs_in_streaming_mode(no_kill_file):
    engine = build_engine(DEMO)
    assert engine.data_mode == "stream"

    async def main():
        await engine.start()
        await asyncio.wait_for(engine.run(max_ticks=5), 10)
        await engine.close()
        assert len(engine.hub.books) == 10

    asyncio.run(main())


# --- Polymarket ------------------------------------------------------------

from tradebot.exchanges import polymarket as pm  # noqa: E402


def test_polymarket_book_and_deltas():
    books = {}
    assert pm.apply_market_message(books, {"event_type": "price_change", "price_changes": [
        {"asset_id": "Y", "price": "0.5", "size": "10", "side": "BUY"}]}) == []  # before snapshot
    pm.apply_market_message(books, {
        "event_type": "book", "asset_id": "Y",
        "bids": [{"price": "0.48", "size": "100"}, {"price": "0.47", "size": "50"}],
        "asks": [{"price": "0.52", "size": "80"}],
    })
    changed = pm.apply_market_message(books, {"event_type": "price_change", "price_changes": [
        {"asset_id": "Y", "price": "0.49", "size": "20", "side": "BUY"},
        {"asset_id": "Y", "price": "0.47", "size": "0", "side": "BUY"},
        {"asset_id": "Y", "price": "0.51", "size": "5", "side": "SELL"},
    ]})
    assert changed == ["Y"]
    bids, asks = books["Y"]
    assert bids == {D("0.48"): D(100), D("0.49"): D(20)}
    assert asks == {D("0.52"): D(80), D("0.51"): D(5)}
    # legacy shape
    pm.apply_market_message(books, {"event_type": "price_change", "asset_id": "Y",
                                    "changes": [{"price": "0.52", "size": "0", "side": "SELL"}]})
    assert books["Y"][1] == {D("0.51"): D(5)}


def test_polymarket_stream_against_local_server(monkeypatch):
    from websockets.asyncio.server import serve

    monkeypatch.setattr(pm, "PING_INTERVAL_S", 0.05)
    got_subscribe, got_ping = [], []

    async def handler(ws):
        got_subscribe.append(json.loads(await ws.recv()))
        await ws.send(json.dumps([
            {"event_type": "book", "asset_id": "YES", "bids": [{"price": "0.40", "size": "10"}], "asks": [{"price": "0.45", "size": "10"}]},
            {"event_type": "book", "asset_id": "NO", "bids": [{"price": "0.50", "size": "10"}], "asks": [{"price": "0.55", "size": "10"}]},
        ]))
        await ws.send(json.dumps({"event_type": "price_change", "price_changes": [
            {"asset_id": "YES", "price": "0.44", "size": "7", "side": "SELL"}]}))
        async for msg in ws:
            if msg == "PING":
                got_ping.append(msg)
                await ws.send("PONG")

    async def main():
        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            ex = pm.PolymarketExchange("poly", ws_url=f"ws://127.0.0.1:{port}")
            seen = []
            async for item in ex.watch_order_books(["YES", "NO"]):
                seen.append(item)
                if item is None:  # PONG heartbeat
                    break
            books = [b for b in seen if b is not None]
            assert [b.symbol for b in books] == ["YES", "NO", "YES"]
            assert books[-1].best_ask.price == D("0.44") and books[-1].best_ask.size == D(7)
            assert got_subscribe == [{"assets_ids": ["YES", "NO"], "type": "market"}] and got_ping

    asyncio.run(main())


# --- Alpaca ----------------------------------------------------------------

def _alpaca_server(auth_ok=True):
    received = []

    async def handler(ws):
        await ws.send(json.dumps([{"T": "success", "msg": "connected"}]))
        received.append(json.loads(await ws.recv()))
        if not auth_ok:
            await ws.send(json.dumps([{"T": "error", "code": 402, "msg": "auth failed"}]))
            return
        await ws.send(json.dumps([{"T": "success", "msg": "authenticated"}]))
        received.append(json.loads(await ws.recv()))
        await ws.send(json.dumps([{"T": "subscription", "quotes": ["AAPL"]}]))
        await ws.send(json.dumps([
            {"T": "q", "S": "AAPL", "bp": 230.1, "bs": 3, "ap": 230.2, "as": 5, "t": "2026-10-29T14:30:00.123456789Z"},
            {"T": "q", "S": "AAPL", "bp": 230.15, "bs": 1, "ap": 230.18, "as": 2, "t": "2026-10-29T14:30:00.223456789Z"},
        ]))
        await ws.wait_closed()

    return handler, received


def _alpaca(port):
    pytest.importorskip("alpaca")
    from tradebot.exchanges.alpaca import AlpacaExchange

    return AlpacaExchange("alp", "PKTEST000000000000000", "s" * 40, stream_url=f"ws://127.0.0.1:{port}")


def test_alpaca_stream_against_local_server():
    from websockets.asyncio.server import serve

    handler, received = _alpaca_server()

    async def main():
        async with serve(handler, "127.0.0.1", 0) as server:
            ex = _alpaca(server.sockets[0].getsockname()[1])
            books = []
            async for item in ex.watch_order_books(["AAPL"]):
                if item is not None:
                    books.append(item)
                if len(books) == 2:
                    break
            assert books[1].best_bid.price == D("230.15") and books[1].best_ask.size == D(2)
            assert received[0]["action"] == "auth" and received[1] == {"action": "subscribe", "quotes": ["AAPL"]}

    asyncio.run(main())


def test_alpaca_auth_failure_raises():
    from websockets.asyncio.server import serve

    handler, _ = _alpaca_server(auth_ok=False)

    async def main():
        async with serve(handler, "127.0.0.1", 0) as server:
            ex = _alpaca(server.sockets[0].getsockname()[1])
            with pytest.raises(ConnectionError, match="auth failed"):
                async for _ in ex.watch_order_books(["AAPL"]):
                    pass

    asyncio.run(main())


# --- ccxt.pro ----------------------------------------------------------------

def test_ccxt_merges_symbol_streams_and_propagates_errors():
    pytest.importorskip("ccxt")
    from tradebot.exchanges.ccxt_exchange import CcxtExchange

    async def main():
        ex = CcxtExchange("binance", "binance")
        assert ex.streaming
        calls = {"BTC/USDT": 0, "ETH/USDT": 0}

        async def fake_watch(symbol, limit=None, params=None):
            calls[symbol] += 1
            await asyncio.sleep(0.001)
            if symbol == "ETH/USDT" and calls[symbol] == 3:
                raise ConnectionError("ws closed")
            px = 100 if symbol == "BTC/USDT" else 10
            bids = [[px - 1 - i / 100, 1, 0] for i in range(30)]  # ccxt levels may carry extra fields
            return {"bids": bids, "asks": [[px + 1, 1, 0]], "timestamp": None}

        ex.client.watch_order_book = fake_watch
        seen = []
        with pytest.raises(ConnectionError, match="ws closed"):
            async for b in ex.watch_order_books(["BTC/USDT", "ETH/USDT"], depth=5):
                seen.append(b)
        assert {b.symbol for b in seen} == {"BTC/USDT", "ETH/USDT"}
        assert all(len(b.bids) == 5 for b in seen)  # trimmed to depth
        await ex.close()

    asyncio.run(main())
