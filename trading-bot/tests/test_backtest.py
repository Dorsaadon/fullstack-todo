import asyncio
import gzip
from datetime import date, datetime, timedelta, timezone


from tradebot.backtest import Backtester
from tradebot.cli import main
from tradebot.engine import Engine
from tradebot.exchanges.paper import PaperExchange
from tradebot.market_hours import NEW_YORK
from tradebot.recording import Recorder, read_events, recording_files
from tradebot.risk import RiskLimits, RiskManager
from tradebot.strategies import CrossExchangeArbitrage

from conftest import D, book
from test_streaming import ScriptedFeed, until


def write(directory, events):
    """events: (t, venue, OrderBook | None)."""
    rec = Recorder(directory)
    for t, venue, b in events:
        rec.on_event(venue, b, t)
    rec._write(rec._take())


def quote(venue, symbol, mid, t):
    mid = D(mid)
    return book(venue, symbol, [(mid - D("0.01"), 1000)], [(mid + D("0.01"), 1000)], ts=t)


# --- recording -----------------------------------------------------------------

def test_recording_round_trips_exactly_and_splits_by_utc_day(tmp_path):
    midnight = datetime(2026, 10, 30, tzinfo=timezone.utc).timestamp()
    b = book("v", "BTC/USDT", [("65000.12345678", "0.00012"), ("64999", "1")], [("65001.5", "2")], ts=midnight - 1)
    write(tmp_path, [(midnight - 1, "v", b), (midnight - 0.5, "v", None), (midnight + 1, "v", b)])
    assert [p.name for p in recording_files(tmp_path)] == ["2026-10-29.jsonl.gz", "2026-10-30.jsonl.gz"]
    events = list(read_events(tmp_path))
    assert [(e.t, e.symbol) for e in events] == [(midnight - 1, "BTC/USDT"), (midnight - 0.5, None), (midnight + 1, "BTC/USDT")]
    back = events[0].book()
    assert back.bids == b.bids and back.asks == b.asks  # exact Decimals
    assert [e.t for e in read_events(tmp_path, start=date(2026, 10, 30))] == [midnight + 1]


def test_recorder_throttles_heartbeats_and_optional_downsampling(tmp_path):
    rec = Recorder(tmp_path, min_interval_s=1.0, heartbeat_interval_s=1.0)
    b = book("v", "X", [(1, 1)], [(2, 1)])
    for t in (0.0, 0.3, 0.6, 1.2):
        rec.on_event("v", b, t)
        rec.on_event("v", None, t)
    assert rec.records == 4  # books at 0 and 1.2, heartbeats at 0 and 1.2


def test_reader_survives_a_crash_truncated_file(tmp_path):
    write(tmp_path, [(1_800_000_000.0, "v", book("v", "X", [(1, 1)], [(2, 1)]))])
    path = recording_files(tmp_path)[0]
    data = path.read_bytes()
    with gzip.open(path, "ab") as f:  # a second member, cut off mid-write
        f.write(b'{"t": 1800000001, "v": "v", "s": "X", "b": [[')
    path.write_bytes(path.read_bytes()[: len(data) + 25])
    assert len(list(read_events(tmp_path))) == 1


def test_engine_records_while_streaming(tmp_path):
    async def main():
        feed = ScriptedFeed("a")
        a = PaperExchange("a", data_source=feed, balances={"USDT": D(1000)}, quote_currency="USDT")
        strat = CrossExchangeArbitrage("arb", "BTC/USDT", ["a", "a2"], quote_currency="USDT")
        a2 = PaperExchange("a2", data_source=ScriptedFeed("a2"), quote_currency="USDT")
        exchanges = {"a": a, "a2": a2}
        risk = RiskManager(RiskLimits(kill_switch_file=None), assets_of=lambda v, s: exchanges[v].assets(s))
        engine = Engine(exchanges, [strat], risk, heartbeat_s=0.05, warmup_timeout_s=0.1)
        engine.recorder = Recorder(tmp_path)
        task = asyncio.create_task(engine.run())
        await feed.queue.put(book("a", "BTC/USDT", [(99, 1)], [(101, 1)]))
        await feed.queue.put(None)
        await until(lambda: engine.recorder.records == 2)
        engine.stop()
        await asyncio.wait_for(task, 2)

    asyncio.run(main())
    events = list(read_events(tmp_path))
    assert [(e.venue, e.symbol) for e in events] == [("a", "BTC/USDT"), ("a", None)]


# --- backtesting -----------------------------------------------------------------

ORB_CONFIG = """
[engine]
mode = "paper"
min_tick_interval_s = 0

[risk]
max_order_notional = 100000
max_position_notional = 100000

[venues.alpaca]
kind = "alpaca"
api_key_env = "TRADEBOT_TEST_UNSET_KEY"   # offline backtests need no credentials
secret_env = "TRADEBOT_TEST_UNSET_SECRET"
paper_balances = { USD = 100000 }

[[strategies]]
kind = "opening_range_breakout"
name = "orb"
venue = "alpaca"
symbols = ["SPY"]
range_minutes = 15
breakout_buffer_bps = 0
min_range_bps = 10
risk_per_trade = 100
take_profit_r = 2
max_slippage_bps = 0
"""


def orb_day(directory, day=(2026, 10, 29)):
    """One minute-by-minute session: range 99-101, breakout at 10:00, target hit at 11:00."""
    def at(minute):
        return (datetime(*day, 9, 30, tzinfo=NEW_YORK) + timedelta(minutes=minute)).timestamp()

    path = [100, 101, 99, 100] + [100.5] * 26  # 9:30-9:59 (range = first 15 min)
    path += [101.49]  # 10:00 breakout: ask 101.50
    path += [101.5 + i * 4.5 / 59 for i in range(59)]  # 10:01-10:59 grind up, below target
    path += [106.6] * 300  # 11:00 target (bid 106.59 >= 106.50), then flat until the close
    path = path[:390]
    write(directory, [(at(m), "alpaca", quote("alpaca", "SPY", round(px, 4), at(m))) for m, px in enumerate(path)])


def test_backtest_replays_orb_day_with_exact_pnl(tmp_path):
    cfg = tmp_path / "orb.toml"
    cfg.write_text(ORB_CONFIG)
    orb_day(tmp_path / "rec")
    result = asyncio.run(Backtester(cfg, tmp_path / "rec").run())
    assert result.plans["orb"] == 2 and result.fills["orb"] == 2
    entry, exit_ = result.trades
    assert (entry["side"], entry["size"], entry["price"]) == ("buy", "40", "101.50")
    assert exit_["side"] == "sell" and exit_["price"] == "106.59"
    assert entry["time"].startswith("2026-10-29T14:00")  # 10:00 ET, in simulated time
    assert result.pnl == 40 * (D("106.59") - D("101.50"))
    assert not result.halted
    assert list(result.daily_pnl) == ["2026-10-29"]


def test_backtest_over_several_days_reports_daily_pnl_and_sharpe(tmp_path):
    cfg = tmp_path / "orb.toml"
    cfg.write_text(ORB_CONFIG)
    for d in [(2026, 10, 27), (2026, 10, 28), (2026, 10, 29)]:
        orb_day(tmp_path / "rec", day=d)
    result = asyncio.run(Backtester(cfg, tmp_path / "rec").run())
    assert result.plans["orb"] == 6
    assert sorted(result.daily_pnl) == ["2026-10-27", "2026-10-28", "2026-10-29"]
    assert result.pnl == 3 * 40 * (D("106.59") - D("101.50"))


ARB_CONFIG = """
[engine]
mode = "paper"
min_tick_interval_s = 0

[risk]
max_order_notional = 100000

[venues.a]
kind = "ccxt"
exchange_id = "binance"
paper_balances = { USDT = 10000 }

[venues.b]
kind = "ccxt"
exchange_id = "kraken"
paper_balances = { BTC = 1 }

[[strategies]]
kind = "cross_exchange_arb"
symbol = "BTC/USDT"
venues = ["a", "b"]
min_edge_bps = 10
max_size = 1
"""


def arb_recording(directory):
    t0 = 1_800_000_000.0
    write(directory, [
        (t0, "a", book("a", "BTC/USDT", [(99, 5)], [(100, 5)])),
        (t0, "b", book("b", "BTC/USDT", [(99.5, 5)], [(100.5, 5)])),
        (t0 + 1.0, "b", book("b", "BTC/USDT", [(102, 5)], [(103, 5)])),  # dislocation...
        (t0 + 1.08, "b", book("b", "BTC/USDT", [(99.5, 5)], [(100.5, 5)])),  # ...gone 80ms later
        (t0 + 2.0, "a", book("a", "BTC/USDT", [(99, 5)], [(100, 5)])),
    ])


def test_latency_turns_a_fleeting_arb_into_a_legging_loss(tmp_path):
    cfg = tmp_path / "arb.toml"
    cfg.write_text(ARB_CONFIG)
    arb_recording(tmp_path / "rec")
    instant = asyncio.run(Backtester(cfg, tmp_path / "rec", latency_ms=0).run())
    assert instant.pnl == D(2)  # bought 100, sold 102

    slow = asyncio.run(Backtester(cfg, tmp_path / "rec", latency_ms=100).run())
    # The sell lands after the dislocation closed: no fill, so the bought BTC is
    # unwound at the 99 bid. Exactly the risk paper P&L hides.
    assert slow.pnl == D(-1)
    assert slow.fills["cross_exchange_arb"] == 2  # buy + unwind; the sell never filled


def test_cli_backtest_writes_report(tmp_path, capsys):
    cfg = tmp_path / "orb.toml"
    cfg.write_text(ORB_CONFIG)
    orb_day(tmp_path / "rec")
    out = tmp_path / "out"
    assert main(["backtest", "-c", str(cfg), "--data", str(tmp_path / "rec"), "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "P&L           $+203.60" in printed
    assert {p.name for p in out.iterdir()} == {"report.json", "trades.csv", "equity.csv"}
    assert main(["backtest", "-c", str(cfg), "--data", str(tmp_path / "empty")]) == 2
