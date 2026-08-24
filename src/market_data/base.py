"""MarketDataProvider interface. The rest of the app depends ONLY on this module."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True)
class PriceBar:
    price_date: date
    close: Decimal
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    adjusted_close: Decimal | None = None
    volume: Decimal | None = None


@dataclass(frozen=True)
class FxBar:
    rate_date: date
    rate: Decimal  # quote per 1 base


class MarketDataProvider(ABC):
    name: str = "abstract"

    @abstractmethod
    def fetch_prices(self, symbol: str, start: date, end: date) -> list[PriceBar]:
        """Daily bars for `symbol` in [start, end]. Empty list if unknown. Must not raise for 404s."""

    @abstractmethod
    def fetch_fx(self, base: str, quote: str, start: date, end: date) -> list[FxBar]:
        """Daily FX rates: how many `quote` per 1 `base`."""

    def price_currency(self, symbol: str) -> str | None:  # pragma: no cover - optional
        return None


class ProviderError(RuntimeError):
    pass
