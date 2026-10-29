"""US equities via Alpaca (commission-free API broker with a free paper account).

Stocks only expose top-of-book (NBBO) through this feed, so books have a single
level per side. The free "iex" feed covers one exchange; "sip" is the full
consolidated tape and needs a paid data plan.

Streaming speaks Alpaca's market-data WebSocket protocol directly (alpaca-py's
StockDataStream runs its own event loop). Alpaca caps concurrent data
connections per account (one on the free plan), so all symbols share one.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from decimal import Decimal

from ..models import Order, OrderBook, OrderRequest, OrderStatus, OrderType, Side, TimeInForce
from .base import Exchange

_TERMINAL = {"filled", "canceled", "expired", "rejected", "done_for_day"}
STREAM_URL = "wss://stream.data.alpaca.markets/v2/{feed}"
IDLE_HEARTBEAT_S = 5.0  # quiet this long on an open socket => report "alive"


class AlpacaExchange(Exchange):
    def __init__(
        self, name: str, api_key: str, secret: str, paper: bool = True, feed: str = "iex", stream_url: str | None = None
    ):
        super().__init__(name, Decimal("0"), "USD")
        self._key, self._secret = api_key, secret
        self.stream_url = stream_url or STREAM_URL.format(feed=feed)
        from alpaca.data.enums import DataFeed
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.trading.client import TradingClient

        self.trading = TradingClient(api_key, secret, paper=paper)
        self.data = StockHistoricalDataClient(api_key, secret)
        self.feed = DataFeed(feed)

    async def fetch_order_book(self, symbol: str, depth: int = 10) -> OrderBook:
        from alpaca.data.requests import StockLatestQuoteRequest

        req = StockLatestQuoteRequest(symbol_or_symbols=symbol, feed=self.feed)
        q = (await asyncio.to_thread(self.data.get_stock_latest_quote, req))[symbol]
        return OrderBook.from_raw(
            self.name, symbol, [(q.bid_price, q.bid_size)], [(q.ask_price, q.ask_size)], q.timestamp.timestamp()
        )

    async def watch_order_books(self, symbols: list[str], depth: int = 10) -> AsyncIterator[OrderBook | None]:
        import websockets

        # websockets pings every 10s and drops the socket if pongs stop, so an
        # idle-but-open connection is genuinely alive.
        async with websockets.connect(self.stream_url, ping_interval=10, ping_timeout=10) as ws:
            await self._expect(ws, "connected")
            await ws.send(json.dumps({"action": "auth", "key": self._key, "secret": self._secret}))
            await self._expect(ws, "authenticated")
            await ws.send(json.dumps({"action": "subscribe", "quotes": symbols}))
            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), IDLE_HEARTBEAT_S)
                except asyncio.TimeoutError:
                    yield None
                    continue
                for msg in json.loads(raw):
                    kind = msg.get("T")
                    if kind == "q":
                        yield OrderBook.from_raw(
                            self.name, msg["S"], [(msg["bp"], msg["bs"])], [(msg["ap"], msg["as"])]
                        )
                    elif kind == "subscription":
                        yield None
                    elif kind == "error":
                        raise ConnectionError(f"alpaca stream error {msg.get('code')}: {msg.get('msg')}")

    @staticmethod
    async def _expect(ws, what: str) -> None:
        msg = json.loads(await asyncio.wait_for(ws.recv(), 10))[0]
        if msg.get("T") == "error":
            raise ConnectionError(f"alpaca stream error {msg.get('code')}: {msg.get('msg')}")
        if msg.get("T") != "success" or msg.get("msg") != what:
            raise ConnectionError(f"alpaca stream: expected {what!r}, got {msg}")

    async def fetch_balances(self) -> dict[str, Decimal]:
        account = await asyncio.to_thread(self.trading.get_account)
        positions = await asyncio.to_thread(self.trading.get_all_positions)
        balances = {"USD": Decimal(str(account.cash))}
        for p in positions:
            qty = Decimal(str(p.qty))
            side = getattr(getattr(p, "side", None), "value", getattr(p, "side", None))
            balances[p.symbol] = -abs(qty) if side == "short" else qty  # shorts as negative quantities
        return balances

    async def cancel_order(self, order_id: str, symbol: str) -> None:
        await asyncio.to_thread(self.trading.cancel_order_by_id, order_id)

    async def place_order(self, req: OrderRequest) -> Order:
        from alpaca.common.exceptions import APIError
        from alpaca.trading.enums import OrderSide
        from alpaca.trading.enums import TimeInForce as ATif
        from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

        common = dict(
            symbol=req.symbol,
            qty=float(req.size),
            side=OrderSide.BUY if req.side is Side.BUY else OrderSide.SELL,
            # Alpaca market orders don't accept FOK; use DAY so they execute.
            time_in_force=ATif(req.tif.value) if req.order_type is OrderType.LIMIT else ATif.DAY,
            client_order_id=req.client_id,
        )
        payload = (
            LimitOrderRequest(limit_price=float(req.price), **common)
            if req.order_type is OrderType.LIMIT
            else MarketOrderRequest(**common)
        )
        try:
            o = await asyncio.to_thread(self.trading.submit_order, payload)
            # Submission is async on Alpaca's side; poll until IOC/FOK settle.
            deadline = time.monotonic() + 5
            while o.status.value not in _TERMINAL and time.monotonic() < deadline and req.tif is not TimeInForce.GTC:
                await asyncio.sleep(0.25)
                o = await asyncio.to_thread(self.trading.get_order_by_id, o.id)
        except APIError as e:
            return Order(request=req, id="", status=OrderStatus.REJECTED, error=str(e))

        filled = Decimal(str(o.filled_qty or 0))
        status = {
            "filled": OrderStatus.FILLED,
            "rejected": OrderStatus.REJECTED,
            "canceled": OrderStatus.CANCELED,
            "expired": OrderStatus.CANCELED,
            "done_for_day": OrderStatus.CANCELED,
        }.get(o.status.value, OrderStatus.PARTIALLY_FILLED if filled else OrderStatus.NEW)
        return Order(
            request=req,
            id=str(o.id),
            status=status,
            filled=filled,
            avg_price=Decimal(str(o.filled_avg_price)) if o.filled_avg_price else None,
        )
