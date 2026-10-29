"""Command line entry point: `tradebot run` and `tradebot check`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

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
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    load_dotenv()
    try:
        asyncio.run(_run(args) if args.cmd == "run" else _check(args))
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    return 0
