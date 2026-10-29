"""Polymarket prediction markets via the official CLOB client.

Each outcome (YES / NO) is its own token id; symbols in this bot are token ids,
quoted in USDC. Prices are probabilities in [0, 1] and a winning share pays $1.

Order books are public, so paper trading needs no keys. Live trading needs a
Polygon wallet private key; Polymarket's international venue is not available
to US persons, so check eligibility for your jurisdiction first.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

from ..models import Order, OrderBook, OrderRequest, OrderStatus, Side, TimeInForce
from .base import Exchange

HOST = "https://clob.polymarket.com"
POLYGON = 137
USDC_DECIMALS = Decimal(10) ** 6


class PolymarketExchange(Exchange):
    def __init__(
        self,
        name: str,
        private_key: str | None = None,
        funder: str | None = None,
        signature_type: int | None = None,
        taker_fee: Decimal = Decimal("0"),
        host: str = HOST,
    ):
        super().__init__(name, taker_fee, "USDC")
        from py_clob_client.client import ClobClient

        self.client = ClobClient(host, chain_id=POLYGON, key=private_key, signature_type=signature_type, funder=funder)
        self._authed = private_key is not None

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
