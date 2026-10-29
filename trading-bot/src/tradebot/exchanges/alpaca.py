"""US equities via Alpaca (commission-free API broker with a free paper account).

Stocks only expose top-of-book (NBBO) through this feed, so books have a single
level per side. The free "iex" feed covers one exchange; "sip" is the full
consolidated tape and needs a paid data plan.
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal

from ..models import Order, OrderBook, OrderRequest, OrderStatus, OrderType, Side, TimeInForce
from .base import Exchange

_TERMINAL = {"filled", "canceled", "expired", "rejected", "done_for_day"}


class AlpacaExchange(Exchange):
    def __init__(self, name: str, api_key: str, secret: str, paper: bool = True, feed: str = "iex"):
        super().__init__(name, Decimal("0"), "USD")
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

    async def fetch_balances(self) -> dict[str, Decimal]:
        account = await asyncio.to_thread(self.trading.get_account)
        positions = await asyncio.to_thread(self.trading.get_all_positions)
        balances = {"USD": Decimal(str(account.cash))}
        balances.update({p.symbol: Decimal(str(p.qty)) for p in positions})
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
