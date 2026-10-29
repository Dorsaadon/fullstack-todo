"""Record market data to disk and read it back for replay.

Format: one gzip file per UTC day, `<dir>/YYYY-MM-DD.jsonl.gz`, one JSON
object per line, in receive order:

  {"t": 1793300000.123, "v": "binance", "s": "BTC/USDT", "b": [["65000.1", "0.5"], ...], "a": [...]}
  {"t": 1793300001.000, "v": "polymarket", "hb": 1}

`t` is the local receive time (what the engine reacted to, so replay is
faithful to what the bot actually saw). Prices and sizes are strings so they
round-trip as exact Decimals. "hb" lines are feed-alive heartbeats, recorded at
most once per `heartbeat_interval_s` per venue, so quiet books aren't mistaken
for stale ones in replay. Venue names are the config's, so a backtest must use
the same names.

Files are appended in gzip members, so a crash loses at most the last
`flush_interval_s` of data and leaves earlier data readable.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from .models import OrderBook

log = logging.getLogger(__name__)


def _day(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).date().isoformat()


class Recorder:
    def __init__(
        self,
        directory: str | Path,
        min_interval_s: float = 0.0,
        flush_interval_s: float = 5.0,
        heartbeat_interval_s: float = 1.0,
    ):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.min_interval_s = min_interval_s  # >0 downsamples each book; replay then sees fewer updates
        self.flush_interval_s = flush_interval_s
        self.heartbeat_interval_s = heartbeat_interval_s
        self.records = 0
        self._buf: list[tuple[float, str]] = []
        self._last: dict[tuple[str, str], float] = {}
        self._last_hb: dict[str, float] = {}
        self._task: asyncio.Task | None = None

    def on_event(self, venue: str, book: OrderBook | None, t: float) -> None:
        """MarketDataHub listener; also called directly in poll mode."""
        if book is None:
            if t - self._last_hb.get(venue, float("-inf")) < self.heartbeat_interval_s:
                return
            self._last_hb[venue] = t
            line = {"t": round(t, 6), "v": venue, "hb": 1}
        else:
            key = (venue, book.symbol)
            if self.min_interval_s and t - self._last.get(key, float("-inf")) < self.min_interval_s:
                return
            self._last[key] = t
            line = {
                "t": round(t, 6),
                "v": venue,
                "s": book.symbol,
                "b": [[str(lv.price), str(lv.size)] for lv in book.bids],
                "a": [[str(lv.price), str(lv.size)] for lv in book.asks],
            }
        self._buf.append((t, json.dumps(line, separators=(",", ":"))))
        self.records += 1

    def start(self) -> None:
        self._task = asyncio.create_task(self._flush_loop(), name="recorder")

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self._write(self._take())

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self.flush_interval_s)
            # Compress off the event loop so recording never delays trading.
            await asyncio.to_thread(self._write, self._take())

    def _take(self) -> list[tuple[float, str]]:
        buf, self._buf = self._buf, []
        return buf

    def _write(self, buf: list[tuple[float, str]]) -> None:
        by_day: dict[str, list[str]] = defaultdict(list)
        for t, line in buf:
            by_day[_day(t)].append(line)
        for day, lines in by_day.items():
            with gzip.open(self.dir / f"{day}.jsonl.gz", "at") as f:
                f.write("\n".join(lines) + "\n")


@dataclass(frozen=True, slots=True)
class Event:
    t: float
    venue: str
    symbol: str | None  # None: heartbeat
    bids: list
    asks: list

    def book(self) -> OrderBook:
        return OrderBook.from_raw(self.venue, self.symbol, self.bids, self.asks, self.t)


def recording_files(directory: str | Path, start: date | None = None, end: date | None = None) -> list[Path]:
    files = []
    for p in sorted(Path(directory).glob("*.jsonl.gz")):
        try:
            d = date.fromisoformat(p.name.split(".")[0])
        except ValueError:
            continue
        if (start is None or d >= start) and (end is None or d <= end):
            files.append(p)
    return files


def read_events(directory: str | Path, start: date | None = None, end: date | None = None) -> Iterator[Event]:
    """Yield recorded events in time order. Tolerates a truncated tail (crash)."""
    last = float("-inf")
    for path in recording_files(directory, start, end):
        try:
            with gzip.open(path, "rt") as f:
                for n, line in enumerate(f, 1):
                    if not line.strip():
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        log.warning("%s:%d: skipping corrupt line", path.name, n)
                        continue
                    t = max(d["t"], last)  # never step backwards (e.g. clock adjustments)
                    last = t
                    yield Event(t, d["v"], d.get("s"), d.get("b", []), d.get("a", []))
        except (EOFError, gzip.BadGzipFile) as e:
            log.warning("%s: truncated after a crash? read what was intact (%s)", path.name, e)
