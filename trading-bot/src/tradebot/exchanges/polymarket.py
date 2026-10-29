"""Polymarket prediction markets via the official CLOB client.

Each outcome (YES / NO) is its own token id; symbols in this bot are token ids,
quoted in USDC. Prices are probabilities in [0, 1] and a winning share pays $1.

Books stream from the public market WebSocket channel: a full "book" snapshot
per token on subscribe, then "price_change" deltas applied to a local copy.

Order books are public, so paper trading needs no keys. Live trading needs a
Polygon wallet private key; Polymarket's international venue is not available
to US persons, so check eligibility for your jurisdiction first.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from decimal import Decimal

from ..models import Order, OrderBook, OrderRequest, OrderStatus, Side, TimeInForce
from .base import Exchange

HOST = "https://clob.polymarket.com"
POLYGON = 137
USDC_DECIMALS = Decimal(10) ** 6
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
PING_INTERVAL_S = 10.0  # the server expects a text "PING" about this often

_Side = dict[Decimal, Decimal]  # price -> size


def apply_market_message(books: dict[str, tuple[_Side, _Side]], msg: dict) -> list[str]:
    """Update local books (token -> (bids, asks)) from one channel message.

    Returns the token ids whose book changed. Deltas for a token are ignored
    until its snapshot has arrived.
    """
    kind = msg.get("event_type")
    if kind == "book":
        def side(levels) -> _Side:
            return {Decimal(l["price"]): Decimal(l["size"]) for l in levels or []}

        asset = msg["asset_id"]
        books[asset] = (side(msg.get("bids", msg.get("buys"))), side(msg.get("asks", msg.get("sells"))))
        return [asset]
    if kind == "price_change":
        # Current format: "price_changes" each carry asset_id. Older: "changes".
        changes = msg.get("price_changes") or [{**c, "asset_id": msg.get("asset_id")} for c in msg.get("changes", [])]
        changed = []
        for c in changes:
            asset = c.get("asset_id")
            if asset not in books:
                continue
            levels = books[asset][0 if c["side"].upper() == "BUY" else 1]
            price, size = Decimal(c["price"]), Decimal(c["size"])
            if size == 0:
                levels.pop(price, None)
            else:
                levels[price] = size
            if asset not in changed:
                changed.append(asset)
        return changed
    return []


class PolymarketExchange(Exchange):
    def __init__(
        self,
        name: str,
        private_key: str | None = None,
        funder: str | None = None,
        signature_type: int | None = None,
        taker_fee: Decimal = Decimal("0"),
        host: str = HOST,
        ws_url: str = WS_URL,
    ):
        super().__init__(name, taker_fee, "USDC")
        from py_clob_client.client import ClobClient

        self.client = ClobClient(host, chain_id=POLYGON, key=private_key, signature_type=signature_type, funder=funder)
        self._authed = private_key is not None
        self.ws_url = ws_url

    async def connect(self) -> None:
        if self._authed:
            creds = await asyncio.to_thread(self.client.create_or_derive_api_creds)
            self.client.set_api_creds(creds)

    async def fetch_order_book(self, symbol: str, depth: int = 10) -> OrderBook:
        ob = await asyncio.to_thread(self.client.get_order_book, symbol)
        ts = int(ob.timestamp) / 1000 if ob.timestamp else None
        book = OrderBook.from_raw(
            self.name, symbol, [(l.price, l.size) for l in ob.bids or []], [(l.price, l.size) for l in ob.asks or []], ts
        )
        return OrderBook(book.venue, symbol, book.bids[:depth], book.asks[:depth], book.ts)

    async def watch_order_books(self, symbols: list[str], depth: int = 10) -> AsyncIterator[OrderBook | None]:
        import websockets

        books: dict[str, tuple[_Side, _Side]] = {}
        wanted = set(symbols)
        async with websockets.connect(self.ws_url, ping_interval=None) as ws:
            await ws.send(json.dumps({"assets_ids": symbols, "type": "market"}))
            pinger = asyncio.create_task(self._ping(ws))
            try:
                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), PING_INTERVAL_S * 3)
                    except asyncio.TimeoutError:
                        raise ConnectionError("polymarket: no data or PONG for 30s") from None
                    if raw == "PONG":
                        yield None
                        continue
                    data = json.loads(raw)
                    for msg in data if isinstance(data, list) else [data]:
                        for asset in apply_market_message(books, msg):
                            if asset in wanted:
                                bids, asks = books[asset]
                                book = OrderBook.from_raw(self.name, asset, bids.items(), asks.items(), time.time())
                                yield OrderBook(self.name, asset, book.bids[:depth], book.asks[:depth], book.ts)
            finally:
                pinger.cancel()

    @staticmethod
    async def _ping(ws) -> None:
        while True:
            await asyncio.sleep(PING_INTERVAL_S)
            await ws.send("PING")

    async def fetch_balances(self) -> dict[str, Decimal]:
        if not self._authed:
            return {}
        from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        raw = await asyncio.to_thread(self.client.get_balance_allowance, params)
        return {"USDC": Decimal(str(raw["balance"])) / USDC_DECIMALS}

    async def cancel_order(self, order_id: str, symbol: str) -> None:
        await asyncio.to_thread(self.client.cancel, order_id)

    async def place_order(self, req: OrderRequest) -> Order:
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY, SELL

        if req.price is None:
            return Order(request=req, id="", status=OrderStatus.REJECTED, error="Polymarket needs a limit price")
        args = OrderArgs(token_id=req.symbol, price=float(req.price), size=float(req.size), side=BUY if req.side is Side.BUY else SELL)
        tif = {TimeInForce.FOK: OrderType.FOK, TimeInForce.IOC: OrderType.FAK, TimeInForce.GTC: OrderType.GTC}[req.tif]
        try:
            signed = await asyncio.to_thread(self.client.create_order, args)
            resp = await asyncio.to_thread(self.client.post_order, signed, tif)
            if not resp.get("success", False):
                return Order(request=req, id="", status=OrderStatus.REJECTED, error=resp.get("errorMsg") or str(resp))
            order_id = resp["orderID"]
            state = await asyncio.to_thread(self.client.get_order, order_id)
        except Exception as e:  # the client raises PolyApiException and plain request errors
            return Order(request=req, id="", status=OrderStatus.REJECTED, error=str(e))

        filled = Decimal(str(state.get("size_matched") or 0))
        status = OrderStatus.FILLED if filled >= req.size else (
            OrderStatus.NEW if req.tif is TimeInForce.GTC else OrderStatus.CANCELED
        )
        # Fills happen at the limit or better; report the limit as a conservative average.
        return Order(request=req, id=order_id, status=status, filled=filled, avg_price=req.price if filled else None)
