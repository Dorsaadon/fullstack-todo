"""Crypto exchanges via ccxt: Binance, Binance.US, Coinbase, Kraken, OKX, Bybit...

One adapter covers 100+ venues; `exchange_id` picks which. US residents must use
"binanceus" rather than "binance". Venues supported by ccxt.pro (bundled with
ccxt) stream order books over WebSockets; the rest fall back to REST polling.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from decimal import Decimal

from ..models import Order, OrderBook, OrderRequest, OrderStatus, OrderType
from .base import Exchange

_STATUS = {
    "open": OrderStatus.NEW,
    "closed": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELED,
    "expired": OrderStatus.CANCELED,
    "rejected": OrderStatus.REJECTED,
}


class CcxtExchange(Exchange):
    def __init__(
        self,
        name: str,
        exchange_id: str,
        api_key: str | None = None,
        secret: str | None = None,
        password: str | None = None,
        sandbox: bool = False,
        taker_fee: Decimal = Decimal("0.001"),
        quote_currency: str = "USDT",
    ):
        super().__init__(name, taker_fee, quote_currency)
        import ccxt.async_support as ccxt  # optional dependency
        import ccxt.pro as ccxtpro

        self._errors = ccxt.BaseError
        # ccxt.pro classes extend the async REST ones, so one client does both.
        self.streaming = exchange_id in ccxtpro.exchanges
        module = ccxtpro if self.streaming else ccxt
        creds = {k: v for k, v in {"apiKey": api_key, "secret": secret, "password": password}.items() if v}
        self.client = getattr(module, exchange_id)({**creds, "enableRateLimit": True})
        if sandbox:
            self.client.set_sandbox_mode(True)

    async def connect(self) -> None:
        await self.client.load_markets()

    async def close(self) -> None:
        await self.client.close()

    async def fetch_order_book(self, symbol: str, depth: int = 10) -> OrderBook:
        return self._to_book(symbol, await self.client.fetch_order_book(symbol, limit=depth), depth)

    def _to_book(self, symbol: str, ob: dict, depth: int) -> OrderBook:
        ts = ob["timestamp"] / 1000 if ob.get("timestamp") else time.time()
        return OrderBook.from_raw(
            self.name, symbol, [lv[:2] for lv in ob["bids"][:depth]], [lv[:2] for lv in ob["asks"][:depth]], ts
        )

    async def watch_order_books(self, symbols: list[str], depth: int = 10) -> AsyncIterator[OrderBook | None]:
        if not self.streaming:
            async for item in super().watch_order_books(symbols, depth):
                yield item
            return
        # ccxt.pro multiplexes symbols over its own connections; we run one
        # watcher per symbol and merge them. Any watcher's error ends the
        # stream so the hub reconnects the venue.
        queue: asyncio.Queue = asyncio.Queue()

        async def watch(symbol: str) -> None:
            try:
                while True:
                    # No limit: some venues only accept specific WS depths. We trim instead.
                    ob = await self.client.watch_order_book(symbol)
                    await queue.put(self._to_book(symbol, ob, depth))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                await queue.put(e)

        tasks = [asyncio.create_task(watch(s)) for s in symbols]
        try:
            while True:
                item = await queue.get()
                if isinstance(item, Exception):
                    raise item
                yield item
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def fetch_balances(self) -> dict[str, Decimal]:
        bal = await self.client.fetch_balance()
        return {a: Decimal(str(v)) for a, v in bal.get("free", {}).items() if v}

    async def cancel_order(self, order_id: str, symbol: str) -> None:
        await self.client.cancel_order(order_id, symbol)

    async def place_order(self, req: OrderRequest) -> Order:
        amount = float(self.client.amount_to_precision(req.symbol, req.size))
        price = float(self.client.price_to_precision(req.symbol, req.price)) if req.price is not None else None
        params = {"timeInForce": req.tif.value.upper()} if req.order_type is OrderType.LIMIT else {}
        try:
            raw = await self.client.create_order(
                req.symbol, req.order_type.value, req.side.value, amount, price, params
            )
            # Some venues return a bare ack; fetch the final state for IOC/FOK.
            if raw.get("status") not in ("closed", "canceled", "expired", "rejected"):
                raw = await self.client.fetch_order(raw["id"], req.symbol)
        except self._errors as e:
            return Order(request=req, id="", status=OrderStatus.REJECTED, error=str(e))
        return self._to_order(req, raw)

    def _to_order(self, req: OrderRequest, raw: dict) -> Order:
        _, quote = self.assets(req.symbol)
        filled = Decimal(str(raw.get("filled") or 0))
        status = _STATUS.get(raw.get("status"), OrderStatus.NEW)
        if status is OrderStatus.NEW and filled > 0:
            status = OrderStatus.PARTIALLY_FILLED
        fee = raw.get("fee") or {}
        # Fees paid in another asset (e.g. BNB discounts) are ignored here; the
        # risk manager's equity tracking still sees them through balances.
        fee_cost = Decimal(str(fee["cost"])) if fee.get("cost") and fee.get("currency") == quote else Decimal("0")
        avg = raw.get("average")
        return Order(
            request=req,
            id=str(raw["id"]),
            status=status,
            filled=filled,
            avg_price=Decimal(str(avg)) if avg else None,
            fee=fee_cost,
        )
