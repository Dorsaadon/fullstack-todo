from decimal import Decimal

import pytest

from tradebot.models import OrderBook


def D(x) -> Decimal:
    return Decimal(str(x))


def book(venue, symbol, bids, asks, ts=None):
    return OrderBook.from_raw(venue, symbol, bids, asks, ts)


@pytest.fixture
def no_kill_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
