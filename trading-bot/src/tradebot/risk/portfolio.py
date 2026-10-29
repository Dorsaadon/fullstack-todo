"""Mark-to-market valuation in USD, used for P&L and loss limits."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

from ..models import ONE, STABLE_ASSETS, ZERO, MarketSnapshot

AssetsOf = Callable[[str, str], tuple[str, str]]  # (venue, symbol) -> (base, quote)


def usd_prices(snap: MarketSnapshot, assets_of: AssetsOf) -> dict[str, Decimal]:
    """USD price per asset from the books in view. Cross pairs (ETH/BTC) resolve
    once their quote asset has a price."""
    prices = {a: ONE for a in STABLE_ASSETS}
    pending = [(assets_of(v, s), book.mid) for (v, s), book in snap.books.items() if book.mid is not None]
    for _ in range(3):
        for (base, quote), mid in pending:
            if base not in prices and quote in prices:
                prices[base] = mid * prices[quote]
    return prices


def equity(snap: MarketSnapshot, prices: dict[str, Decimal]) -> tuple[Decimal, set[str]]:
    """Total USD value across venues, plus the assets we could not price."""
    total, unpriced = ZERO, set()
    for balances in snap.balances.values():
        for asset, amount in balances.items():
            if asset in prices:
                total += amount * prices[asset]
            elif amount:
                unpriced.add(asset)
    return total, unpriced
