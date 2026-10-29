"""Streaming market data: keeps the latest book for every subscription.

One task per venue consumes `Exchange.watch_order_books`, reconnecting with
exponential backoff when the stream drops. The engine waits on `wait()` and
asks which (venue, symbol) pairs changed, so strategies run on fresh data the
moment it arrives instead of on a fixed timer.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict

from .exchanges.base import Exchange
from .models import OrderBook

log = logging.getLogger(__name__)


class MarketDataHub:
    def __init__(
        self,
        exchanges: dict[str, Exchange],
        depth: int = 10,
        initial_backoff_s: float = 1.0,
        max_backoff_s: float = 30.0,
        clock=time.time,
    ):
        self.exchanges = exchanges
        self.depth = depth
        self.initial_backoff_s = initial_backoff_s
        self.max_backoff_s = max_backoff_s
        self.clock = clock
        self.books: dict[tuple[str, str], OrderBook] = {}
        self.alive: dict[tuple[str, str], float] = {}
        self.reconnects: dict[str, int] = defaultdict(int)
        self._dirty: set[tuple[str, str]] = set()
        self._event = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    async def start(self, subscriptions: list[tuple[str, str]]) -> None:
        by_venue: dict[str, list[str]] = defaultdict(list)
        for venue, symbol in sorted(set(subscriptions)):
            by_venue[venue].append(symbol)
        for venue, symbols in by_venue.items():
            self._tasks.append(asyncio.create_task(self._run_venue(venue, symbols), name=f"md:{venue}"))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def warm_up(self, subscriptions: list[tuple[str, str]], timeout: float) -> set[tuple[str, str]]:
        """Wait until every subscription has a first book. Returns those still missing."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        missing = set(subscriptions) - set(self.books)
        while missing and loop.time() < deadline:
            await self.wait(min(0.1, max(deadline - loop.time(), 0)))
            missing = set(subscriptions) - set(self.books)
        return missing

    def wake(self) -> None:
        """Release a pending wait() early (used on shutdown)."""
        self._event.set()

    async def wait(self, timeout: float) -> bool:
        """Block until some book changes or `timeout` passes. True if data changed."""
        try:
            await asyncio.wait_for(self._event.wait(), timeout)
        except asyncio.TimeoutError:
            pass
        self._event.clear()
        return bool(self._dirty)

    def take_dirty(self) -> set[tuple[str, str]]:
        dirty, self._dirty = self._dirty, set()
        return dirty

    def _on_book(self, venue: str, book: OrderBook) -> None:
        key = (venue, book.symbol)
        self.books[key] = book
        self.alive[key] = self.clock()
        self._dirty.add(key)
        self._event.set()

    async def _run_venue(self, venue: str, symbols: list[str]) -> None:
        ex = self.exchanges[venue]
        backoff = self.initial_backoff_s
        while True:
            try:
                async for item in ex.watch_order_books(symbols, self.depth):
                    backoff = self.initial_backoff_s  # healthy again
                    if item is None:
                        now = self.clock()
                        for s in symbols:
                            self.alive[(venue, s)] = now
                    elif item.symbol in symbols:
                        self._on_book(venue, item)
                log.warning("%s: stream ended; reconnecting", venue)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("%s: stream error (%s: %s); reconnecting in %.1fs", venue, type(e).__name__, e, backoff)
            self.reconnects[venue] += 1
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self.max_backoff_s)
