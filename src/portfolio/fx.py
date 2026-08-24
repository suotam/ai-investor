"""FX conversion using the fx_rates table. Never guesses: returns None when no rate exists.

fx_rates.rate means: 1 base_currency = rate quote_currency. We store provider pairs as fetched
(e.g. USD->CZK) and derive the inverse on the fly. Cross rates are derived via the portfolio
base currency only when both legs exist. Rates are looked up as of a date with a limited
backward fill (weekends/holidays), max `max_staleness_days`.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.core import D
from src.db.models import FxRate

MAX_STALENESS_DAYS = 7


class FxConverter:
    def __init__(self, session: Session, base_currency: str, max_staleness_days: int = MAX_STALENESS_DAYS):
        self.session = session
        self.base = base_currency.upper()
        self.max_staleness = max_staleness_days
        self._cache: dict[tuple[str, str], dict[date, Decimal]] = {}

    def _series(self, base: str, quote: str) -> dict[date, Decimal]:
        key = (base, quote)
        if key not in self._cache:
            rows = self.session.execute(
                select(FxRate.rate_date, FxRate.rate).where(
                    FxRate.base_currency == base, FxRate.quote_currency == quote
                )
            ).all()
            self._cache[key] = {d: D(r) for d, r in rows}
        return self._cache[key]

    def _lookup(self, base: str, quote: str, on: date) -> Decimal | None:
        direct = self._series(base, quote)
        inverse = self._series(quote, base)
        for back in range(self.max_staleness + 1):
            d = on - timedelta(days=back)
            if d in direct:
                return direct[d]
            if d in inverse and inverse[d] != 0:
                return Decimal(1) / inverse[d]
        return None

    def rate(self, from_ccy: str, to_ccy: str, on: date) -> Decimal | None:
        """How many `to_ccy` per 1 `from_ccy` on date `on` (None if unavailable)."""
        from_ccy, to_ccy = from_ccy.upper(), to_ccy.upper()
        if from_ccy == to_ccy:
            return Decimal(1)
        r = self._lookup(from_ccy, to_ccy, on)
        if r is not None:
            return r
        # cross via base currency
        if self.base not in (from_ccy, to_ccy):
            a = self._lookup(from_ccy, self.base, on)
            b = self._lookup(to_ccy, self.base, on)
            if a is not None and b not in (None, Decimal(0)):
                return a / b
        return None

    def convert(self, amount: Decimal | None, from_ccy: str, on: date, to_ccy: str | None = None) -> Decimal | None:
        if amount is None:
            return None
        r = self.rate(from_ccy, to_ccy or self.base, on)
        return None if r is None else amount * r

    def required_pairs(self, currencies: set[str]) -> list[tuple[str, str]]:
        """Pairs (ccy -> base) the market data layer should maintain."""
        return [(c.upper(), self.base) for c in sorted(currencies) if c.upper() != self.base]
