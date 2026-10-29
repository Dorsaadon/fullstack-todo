"""Command line entry point: `tradebot run | check | record | backtest`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import time
from datetime import date, datetime

from .config import ConfigError, build_engine, load_dotenv
from .risk import equity, usd_prices


async def _run(args) -> None:
    engine = build_engine(args.config, live=args.live)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, engine.stop)
        except NotImplementedError:  # Windows
            pass
    await engine.start()
    try:
        await engine.run(max_ticks=args.ticks)
    finally:
        await engine.close()
        r = engine.risk
        logging.info("stopped. equity=%.2f daily_pnl=%+.2f halted=%s %s", r.equity or 0, r.daily_pnl, r.halted, r.halt_reason)


async def _check(args) -> None:
    """Connect, take one snapshot, show what each strategy would do. Never trades."""
    engine = build_engine(args.config, live=args.live)
    await engine.start()
    try:
        snap = await engine.snapshot()
        prices = usd_prices(snap, engine.assets_of)
        print("\nOrder books:")
        for (v, sym), b in sorted(snap.books.items()):
            bid, ask = b.best_bid, b.best_ask
            print(f"  {v:>12} {sym:<24} bid {bid.price if bid else '-':>12}  ask {ask.price if ask else '-':>12}")
        print("\nBalances:")
        for v, bals in snap.balances.items():
            print(f"  {v:>12} " + ", ".join(f"{a}={amt}" for a, amt in bals.items()))
        eq, unpriced = equity(snap, prices)
        print(f"\nEquity ~ ${eq:,.2f}" + (f" (unpriced: {', '.join(sorted(unpriced))})" if unpriced else ""))
        print("\nStrategy decisions (not executed):")
        for s in engine.strategies:
            plans = s.on_snapshot(snap)
            if not plans:
                print(f"  {s.name}: no opportunity")
            for p in plans:
                verdict = engine.risk.check(p, snap, prices) or "would execute"
                print(f"  {s.name}: {p.reason}, expected {p.expected_profit:+.4f} -> {verdict}")
    finally:
        await engine.close()


def _on_signals(callback) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, callback)
        except NotImplementedError:  # Windows
            pass


async def _record(args) -> None:
    """Stream market data for every subscription to disk. Never trades."""
    from .marketdata import MarketDataHub
    from .recording import Recorder

    engine = build_engine(args.config)
    recorder = Recorder(args.out) if args.out else engine.recorder or Recorder("data/recordings")
    hub = engine.hub or MarketDataHub(engine.exchanges, engine.book_depth)
    stop = asyncio.Event()
    _on_signals(stop.set)
    await engine.start()
    hub.listeners.append(recorder.on_event)
    recorder.start()
    await hub.start(engine.subscriptions())
    logging.info("recording %d books to %s (Ctrl-C to stop)", len(engine.subscriptions()), recorder.dir)
    started = time.monotonic()
    try:
        while not stop.is_set():
            remaining = None if args.seconds is None else args.seconds - (time.monotonic() - started)
            if remaining is not None and remaining <= 0:
                break
            try:
                await asyncio.wait_for(stop.wait(), timeout=min(30.0, remaining or 30.0))
            except asyncio.TimeoutError:
                logging.info("%s records so far", f"{recorder.records:,}")
    finally:
        await hub.stop()
        await recorder.close()
        await engine.close()
        logging.info("stopped: %s records written to %s", f"{recorder.records:,}", recorder.dir)


async def _backtest(args) -> None:
    from .backtest import Backtester

    if not args.verbose:  # per-order logs would drown the report over long replays
        logging.getLogger("tradebot.engine").setLevel(logging.WARNING)
    bt = Backtester(
        args.config,
        args.data,
        start=date.fromisoformat(args.start) if args.start else None,
        end=date.fromisoformat(args.end) if args.end else None,
        latency_ms=args.latency_ms,
    )
    try:
        result = await bt.run()
    except ValueError as e:
        raise ConfigError(str(e)) from None
    print(result.summary())
    out = result.save(args.out or f"data/backtests/{datetime.now():%Y%m%d-%H%M%S}")
    print(f"\nReport, trades and equity curve saved to {out}/")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tradebot", description="Multi-venue, multi-strategy trading bot")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name, help_ in (("run", "run the trading loop"), ("check", "one dry-run snapshot, no orders")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("-c", "--config", required=True)
        p.add_argument("--live", action="store_true", help="trade real money (config must also say mode = 'live')")
        p.add_argument("-v", "--verbose", action="store_true")
        if name == "run":
            p.add_argument("--ticks", type=int, help="stop after N ticks")

    p = sub.add_parser("record", help="record market data for the config's subscriptions (no trading)")
    p.add_argument("-c", "--config", required=True)
    p.add_argument("--out", help="directory (default: [recording] path, or data/recordings)")
    p.add_argument("--seconds", type=float, help="stop after this long")
    p.add_argument("-v", "--verbose", action="store_true")

    p = sub.add_parser("backtest", help="replay recorded data through the strategies")
    p.add_argument("-c", "--config", required=True)
    p.add_argument("--data", default="data/recordings", help="recording directory")
    p.add_argument("--start", help="first UTC day, YYYY-MM-DD")
    p.add_argument("--end", help="last UTC day, YYYY-MM-DD")
    p.add_argument("--latency-ms", type=float, default=100.0,
                   help="fill against the book this long after the decision (default 100)")
    p.add_argument("--out", help="report directory (default data/backtests/<timestamp>)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    args.live = getattr(args, "live", False)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    load_dotenv()
    try:
        commands = {"run": _run, "check": _check, "record": _record, "backtest": _backtest}
        asyncio.run(commands[args.cmd](args))
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    return 0
