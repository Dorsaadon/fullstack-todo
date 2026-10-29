import asyncio
import json
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tradebot.alerts import Alerter
from tradebot.cli import main
from tradebot.engine import Engine
from tradebot.exchanges.paper import PaperExchange
from tradebot.market_hours import NEW_YORK, MarketCalendar
from tradebot.marketdata import MarketDataHub
from tradebot.models import MarketSnapshot, Order, OrderStatus
from tradebot.risk import RiskLimits, RiskManager
from tradebot.state import InstanceLock, StateError, StateStore
from tradebot.strategies import CrossExchangeArbitrage, MeanReversion, OpeningRangeBreakout

from conftest import D, book
from test_streaming import ScriptedFeed, until

DEMO = Path(__file__).parent.parent / "config" / "demo.toml"


# --- state store -------------------------------------------------------------

def test_state_round_trip_and_backup_fallback(tmp_path):
    store = StateStore(tmp_path / "state.json")
    assert store.load() == {}
    store.save({"risk": {"halted": False}})
    store.save({"risk": {"halted": True}})
    assert store.load()["risk"] == {"halted": True}
    store.path.write_text("{ torn write")  # crash mid-write of the main file
    assert store.load()["risk"] == {"halted": False}  # previous version from .bak
    store.backup.write_text("also bad")
    with pytest.raises(StateError, match="corrupt"):
        store.load()  # never silently start fresh: that could lift a halt


def test_instance_lock_blocks_a_second_bot(tmp_path):
    first = InstanceLock(tmp_path / "tradebot.lock")
    first.acquire()
    with pytest.raises(StateError, match="already running"):
        InstanceLock(tmp_path / "tradebot.lock").acquire()


# --- component state ---------------------------------------------------------

def at(hh, mm, day=(2026, 10, 29)):
    return datetime(*day, hh, mm, tzinfo=NEW_YORK).timestamp()


def orb():
    return OpeningRangeBreakout(
        "orb", "alp", ["SPY"], calendar=MarketCalendar(), breakout_buffer_bps=D(0), min_range_bps=D(10),
        risk_per_trade=D(100), max_position_notional=D(100_000), take_profit_r=D(0), max_slippage_bps=D(0),
    )


def snap(t, mid):
    mid = D(mid)
    b = book("alp", "SPY", [(mid - D("0.01"), 1000)], [(mid + D("0.01"), 1000)], ts=t)
    return MarketSnapshot({("alp", "SPY"): b}, {"alp": {"USD": D(100_000)}}, ts=t)


def enter(s, day=(2026, 10, 29)):
    for minute, px in [(30, 100), (35, 101), (40, 99), (44, 100)]:
        s.on_snapshot(snap(at(9, minute, day), px))
    [plan] = s.on_snapshot(snap(at(10, 0, day), 102))
    leg = plan.legs[0]
    s.on_orders(plan, [Order(leg, "1", OrderStatus.FILLED, filled=leg.size, avg_price=leg.price)])
    return leg.size


def test_orb_survives_restart_mid_trade():
    before = orb()
    shares = enter(before)
    saved = json.loads(json.dumps(before.get_state()))  # through JSON, like the real file
    after = orb()
    after.set_state(saved)
    assert after.positions() == {("alp", "SPY"): shares}
    [flat] = after.on_snapshot(snap(at(15, 55), 103))  # still knows to flatten
    assert flat.legs[0].size == shares and "flatten" in flat.reason


def test_orb_closes_position_left_over_from_a_previous_session():
    s = orb()
    shares = enter(s)  # bot "dies" before the 15:55 flatten
    [close] = s.on_snapshot(snap(at(9, 31, (2026, 10, 30)), 101))
    assert close.legs[0].size == shares and "previous session" in close.reason


def test_mean_reversion_and_risk_state_round_trip():
    mr = MeanReversion("mr", "alp", "AAPL", window=3)
    mr.shares, mr._last_ts = D(5), 12.0
    mr.mids.extend([1.0, 2.0])
    mr2 = MeanReversion("mr", "alp", "AAPL", window=3)
    mr2.set_state(json.loads(json.dumps(mr.get_state())))
    assert (mr2.shares, list(mr2.mids), mr2._last_ts) == (D(5), [1.0, 2.0], 12.0)

    rm = RiskManager(RiskLimits(), assets_of=lambda v, s: (s, "USD"))
    rm.update_equity(D(10_000))
    rm.halt("daily loss")
    rm2 = RiskManager(RiskLimits(), assets_of=lambda v, s: (s, "USD"))
    rm2.set_state(json.loads(json.dumps(rm.get_state())))
    assert rm2.halted and rm2.halt_reason == "daily loss" and rm2.day_start_equity == D(10_000)
    rm2.update_equity(D(9_990))  # same day: the baseline survives, no fresh allowance
    assert rm2.daily_pnl == D(-10)


# --- engine restart & reconciliation ------------------------------------------

def arb_engine(state_path):
    a = PaperExchange("a", balances={"USDT": D(10_000)}, quote_currency="USDT")
    b = PaperExchange("b", balances={"BTC": D(1)}, quote_currency="USDT")
    a.set_order_book(book("a", "BTC/USDT", [(99, 5)], [(100, 5)]))
    b.set_order_book(book("b", "BTC/USDT", [(102, 5)], [(103, 5)]))
    strat = CrossExchangeArbitrage("arb", "BTC/USDT", ["a", "b"], max_size=D(1), quote_currency="USDT")
    ex = {"a": a, "b": b}
    engine = Engine(ex, [strat], RiskManager(RiskLimits(kill_switch_file=None), assets_of=lambda v, s: ex[v].assets(s)))
    engine.state = StateStore(state_path)
    return engine


def test_engine_restart_restores_paper_accounts_and_halt(tmp_path):
    first = arb_engine(tmp_path / "state.json")
    asyncio.run(first.tick())  # trades and saves
    assert first.exchanges["a"].balances["BTC"] == D(1)
    first.risk.halt("unwind incomplete")  # halts save immediately

    second = arb_engine(tmp_path / "state.json")
    asyncio.run(second.start())
    assert second.exchanges["a"].balances["BTC"] == D(1)  # not the config's fresh balances
    assert second.exchanges["b"].balances["USDT"] == D(102)
    assert second.risk.halted
    assert asyncio.run(second.tick()) == []  # still halted after the "restart"


def mr_engine(held, broker):
    ex = PaperExchange("alp", balances={"USD": D(10_000), "AAPL": D(broker)})
    mr = MeanReversion("mr", "alp", "AAPL")
    mr.shares = D(held)
    engine = Engine({"alp": ex}, [mr], RiskManager(RiskLimits(), assets_of=lambda v, s: (s, "USD")))
    asyncio.run(engine.refresh_balances(["alp"]))
    return engine, mr


def test_reconcile_shrinks_positions_the_broker_doesnt_have():
    engine, mr = mr_engine(held=10, broker=4)
    [issue] = asyncio.run(engine.reconcile())
    assert mr.shares == D(4) and "tracked 10 but the account holds 4" in issue


def test_reconcile_leaves_extra_broker_shares_alone():
    engine, mr = mr_engine(held=10, broker=500)  # the other 490 are yours
    assert asyncio.run(engine.reconcile()) == []
    assert mr.shares == D(10)


def test_reconcile_halts_on_conflicting_strategies():
    engine, mr = mr_engine(held=10, broker=10)
    other = MeanReversion("mr2", "alp", "AAPL")
    other.shares = D(-3)
    engine.strategies.append(other)
    asyncio.run(engine.reconcile())
    assert engine.risk.halted and "opposite positions" in engine.risk.halt_reason


# --- alerts --------------------------------------------------------------------

class Sink:
    """Local HTTP server standing in for Telegram, Slack and healthchecks.io."""

    def __init__(self):
        self.requests = []
        sink = self

        class Handler(BaseHTTPRequestHandler):
            def _handle(self):
                n = int(self.headers.get("Content-Length") or 0)
                sink.requests.append((self.command, self.path, self.rfile.read(n).decode()))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")

            do_GET = do_POST = _handle

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def paths(self):
        return [p for _, p, _ in self.requests]


@pytest.fixture
def sink():
    s = Sink()
    yield s
    s.server.shutdown()


def alerter(sink, **kw):
    return Alerter(
        telegram_token="T0KEN", telegram_chat_id="42", slack_webhook=f"{sink.url}/slack",
        healthcheck_url=f"{sink.url}/hc", telegram_api=sink.url, healthcheck_interval_s=0.05, **kw,
    )


def test_alerts_deliver_filter_and_dedupe(sink):
    async def main():
        a = alerter(sink, min_level="warning")
        await a.start()
        a.send("info", "routine")  # below min_level: log only
        a.send("critical", "HALTED")
        a.send("critical", "HALTED")  # duplicate within dedupe window
        a.send("summary", "daily summary")  # always delivered
        await a.close()
        return a

    a = asyncio.run(main())
    tg = [json.loads(body) for _, p, body in sink.requests if p == "/botT0KEN/sendMessage"]
    slack = [json.loads(body) for _, p, body in sink.requests if p == "/slack"]
    assert [m["text"] for m in tg] == ["🚨 [tradebot] HALTED", "📊 [tradebot] daily summary"]
    assert all(m["chat_id"] == "42" for m in tg)
    assert [m["text"] for m in slack] == [m["text"] for m in tg]
    assert len(a.sent) == 2


def test_healthcheck_pings_only_while_alive_and_fails_on_halt(sink):
    async def main():
        a = alerter(sink)
        alive = [True]
        a.alive = lambda: alive[0]
        await a.start()
        await until(lambda: sink.paths().count("/hc") >= 2)
        alive[0] = False  # trading loop stuck: pings must stop so the service alerts you
        await asyncio.sleep(0.1)
        n = sink.paths().count("/hc")
        await asyncio.sleep(0.2)
        assert sink.paths().count("/hc") == n
        a.fail("unwind incomplete")
        await a.close()

    asyncio.run(main())
    assert "/hc/fail" in sink.paths()


def test_engine_halt_alerts_and_saves(sink, tmp_path):
    async def main():
        engine = arb_engine(tmp_path / "state.json")
        engine.alerts = alerter(sink)
        await engine.alerts.start()
        engine.risk.halt("daily loss 300 >= limit 250")
        await engine.alerts.close()
        return engine

    engine = asyncio.run(main())
    assert any("TRADING HALTED: daily loss" in m for m in engine.alerts.sent)
    assert "/hc/fail" in sink.paths()
    assert StateStore(tmp_path / "state.json").load()["risk"]["halted"]


def test_hub_reports_venue_down_and_recovered():
    async def main():
        feed = ScriptedFeed("a")
        hub = MarketDataHub({"a": feed}, initial_backoff_s=0.001)
        events = []
        hub.on_status = lambda v, ok, detail: events.append((v, ok))
        await hub.start([("a", "BTC/USDT")])
        for _ in range(3):
            await feed.queue.put(ConnectionError("refused"))
            await asyncio.sleep(0.02)
        await until(lambda: events == [("a", False)])
        await feed.queue.put(book("a", "BTC/USDT", [(1, 1)], [(2, 1)]))
        await until(lambda: events == [("a", False), ("a", True)])
        await hub.stop()

    asyncio.run(main())


# --- CLI -------------------------------------------------------------------------

def test_cli_state_show_clear_halt_reset_and_health(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    store = StateStore(tmp_path / "data" / "state.json")
    assert main(["health", "-c", str(DEMO)]) == 1  # never saved
    store.save({"risk": {"halted": True, "halt_reason": "daily loss", "day": "2026-10-29"}, "strategies": {}})
    assert main(["health", "-c", str(DEMO)]) == 0
    assert main(["state", "-c", str(DEMO)]) == 0
    assert "HALTED: daily loss" in capsys.readouterr().out
    assert main(["state", "-c", str(DEMO), "--clear-halt"]) == 0
    assert not store.load()["risk"]["halted"]
    assert main(["state", "-c", str(DEMO), "--reset"]) == 2  # needs --yes
    assert main(["state", "-c", str(DEMO), "--reset", "--yes"]) == 0
    assert store.load() == {}
