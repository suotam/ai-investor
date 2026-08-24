"""Price/FX cache on top of the prices & fx_rates tables + incremental update job.

Caching rule: for each symbol we only request bars after the last cached date (minus a small
overlap to pick up provider corrections). Same-day re-runs fetch nothing new.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import NamedTuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.config import Settings
from src.core import D, f, utcnow
from src.db.models import Benchmark, FxRate, Instrument, Position, Price, Transaction
from src.logging_setup import get_logger
from src.market_data.base import MarketDataProvider
from src.portfolio.importer import finish_import_run, start_import_run

log = get_logger("market_data.service")

OVERLAP_DAYS = 3
MAX_STALENESS_DAYS = 10


# --- read side ---------------------------------------------------------------


class PriceQuote(NamedTuple):
    close: Decimal
    price_date: date
    currency: str  # currency the price is quoted in (may differ from instrument currency)


def price_currency_for(inst: Instrument, symbol: str) -> str:
    """Currency a provider quotes `symbol` in. Crypto pairs carry it in the symbol (BTC-USD)."""
    if inst.asset_type == "crypto" and "-" in symbol:
        return symbol.rsplit("-", 1)[1].upper()
    return inst.currency


class PriceStore:
    """In-memory view of cached closes for fast valuation; backward-fills up to max_staleness."""

    def __init__(self, session: Session, max_staleness_days: int = MAX_STALENESS_DAYS):
        self.session = session
        self.max_staleness = max_staleness_days
        self._cache: dict[int, dict[date, tuple[Decimal, str]]] = {}

    def series(self, instrument_id: int) -> dict[date, tuple[Decimal, str]]:
        """{date: (close, price_currency)}"""
        if instrument_id not in self._cache:
            rows = self.session.execute(
                select(Price.price_date, Price.close, Price.currency).where(Price.instrument_id == instrument_id)
            ).all()
            self._cache[instrument_id] = {d: (D(c), ccy) for d, c, ccy in rows}
        return self._cache[instrument_id]

    def close_on(self, instrument_id: int, on: date) -> PriceQuote | None:
        """Close on `on` or the latest earlier close within max_staleness days."""
        s = self.series(instrument_id)
        if not s:
            return None
        for back in range(self.max_staleness + 1):
            d = on - timedelta(days=back)
            if d in s:
                return PriceQuote(s[d][0], d, s[d][1])
        return None

    def latest(self, instrument_id: int) -> PriceQuote | None:
        s = self.series(instrument_id)
        if not s:
            return None
        d = max(s)
        return PriceQuote(s[d][0], d, s[d][1])


# --- write side --------------------------------------------------------------


def _last_price_date(session: Session, instrument_id: int, source: str) -> date | None:
    return session.execute(
        select(func.max(Price.price_date)).where(Price.instrument_id == instrument_id, Price.source == source)
    ).scalar()


def _last_fx_date(session: Session, base: str, quote: str, source: str) -> date | None:
    return session.execute(
        select(func.max(FxRate.rate_date)).where(
            FxRate.base_currency == base, FxRate.quote_currency == quote, FxRate.source == source
        )
    ).scalar()


def ensure_benchmarks(session: Session, settings: Settings) -> list[Benchmark]:
    """Create benchmark instruments/definitions from settings (idempotent)."""
    out = []
    for b in settings.benchmarks:
        bm = session.scalars(select(Benchmark).where(Benchmark.code == b.symbol)).first()
        if bm is None:
            inst = session.scalars(
                select(Instrument).where(
                    Instrument.symbol == b.symbol, Instrument.asset_type == "index", Instrument.currency == b.currency
                )
            ).first()
            if inst is None:
                inst = Instrument(
                    symbol=b.symbol,
                    name=b.name,
                    asset_type="index",
                    exchange=None,
                    currency=b.currency,
                    price_symbol=b.symbol,
                    provider_ids=json.dumps({"yahoo": b.symbol}),
                )
                session.add(inst)
                session.flush()
            bm = Benchmark(
                code=b.symbol, name=b.name, instrument_id=inst.id, is_default=(b.symbol == settings.default_benchmark)
            )
            session.add(bm)
        else:
            bm.is_default = b.symbol == settings.default_benchmark
        out.append(bm)
    session.flush()
    return out


def instruments_needing_prices(session: Session) -> list[Instrument]:
    """Every instrument ever traded (needed for value history) + benchmarks. Cash rows excluded."""
    traded_ids = set(session.execute(select(Transaction.instrument_id).distinct()).scalars())
    pos_ids = set(session.execute(select(Position.instrument_id).distinct()).scalars())
    bm_ids = set(session.execute(select(Benchmark.instrument_id)).scalars())
    ids = {i for i in traded_ids | pos_ids | bm_ids if i is not None}
    if not ids:
        return []
    return [
        i
        for i in session.scalars(select(Instrument).where(Instrument.id.in_(ids)))
        if i.asset_type not in ("cash",)
    ]


def first_trade_date(session: Session, instrument_id: int) -> date | None:
    return session.execute(
        select(func.min(Transaction.trade_date)).where(Transaction.instrument_id == instrument_id)
    ).scalar()


def update_prices(
    session: Session, provider: MarketDataProvider, settings: Settings, end: date | None = None
) -> dict:
    """Incrementally refresh prices for traded instruments, benchmarks and FX pairs."""
    end = end or date.today()
    run = start_import_run(session, job="update-prices", source=provider.name)
    log.info("update-prices start (run id=%s, provider=%s)", run.id, provider.name)
    summary: dict = {"instruments": {}, "fx": {}, "errors": []}
    inserted_total = 0
    try:
        ensure_benchmarks(session, settings)
        default_start = date.fromisoformat(settings.default_history_start)
        instruments = instruments_needing_prices(session)
        currencies: set[str] = {settings.base_currency}

        for inst in instruments:
            currencies.add(inst.currency)
            symbol = inst.price_symbol or inst.symbol
            currencies.add(price_currency_for(inst, symbol))
            last = _last_price_date(session, inst.id, provider.name)
            if last is None:
                ftd = first_trade_date(session, inst.id)
                start = min(ftd, default_start) if ftd else default_start
                start -= timedelta(days=OVERLAP_DAYS)
            else:
                if last >= end:
                    summary["instruments"][symbol] = {"inserted": 0, "status": "up-to-date"}
                    continue
                start = last - timedelta(days=OVERLAP_DAYS)
            bars = provider.fetch_prices(symbol, start, end)
            if not bars:
                summary["instruments"][symbol] = {"inserted": 0, "status": "no-data"}
                summary["errors"].append(f"no price data for {symbol} (instrument id={inst.id})")
                continue
            existing = set(
                session.execute(
                    select(Price.price_date).where(
                        Price.instrument_id == inst.id, Price.source == provider.name, Price.price_date >= start
                    )
                ).scalars()
            )
            n = 0
            for bar in bars:
                if bar.price_date in existing:
                    continue
                session.add(
                    Price(
                        instrument_id=inst.id,
                        price_date=bar.price_date,
                        open=f(bar.open),
                        high=f(bar.high),
                        low=f(bar.low),
                        close=float(bar.close),
                        adjusted_close=f(bar.adjusted_close),
                        volume=f(bar.volume),
                        currency=price_currency_for(inst, symbol),
                        source=provider.name,
                        fetched_at=utcnow(),
                    )
                )
                n += 1
            session.flush()
            inserted_total += n
            summary["instruments"][symbol] = {"inserted": n, "status": "ok"}

        # FX pairs: every non-base currency -> base
        # also include currencies of cash flows (deposits in CZK etc.)
        from src.db.models import CashFlow

        currencies |= set(session.execute(select(CashFlow.currency).distinct()).scalars())
        currencies |= set(session.execute(select(Transaction.currency).distinct()).scalars())
        for ccy in sorted(currencies):
            if ccy == settings.base_currency:
                continue
            last = _last_fx_date(session, ccy, settings.base_currency, provider.name)
            start = (last - timedelta(days=OVERLAP_DAYS)) if last else (default_start - timedelta(days=OVERLAP_DAYS))
            if last and last >= end:
                summary["fx"][f"{ccy}{settings.base_currency}"] = {"inserted": 0, "status": "up-to-date"}
                continue
            bars = provider.fetch_fx(ccy, settings.base_currency, start, end)
            if not bars:
                summary["fx"][f"{ccy}{settings.base_currency}"] = {"inserted": 0, "status": "no-data"}
                summary["errors"].append(f"no FX data for {ccy}->{settings.base_currency}")
                continue
            existing = set(
                session.execute(
                    select(FxRate.rate_date).where(
                        FxRate.base_currency == ccy,
                        FxRate.quote_currency == settings.base_currency,
                        FxRate.source == provider.name,
                        FxRate.rate_date >= start,
                    )
                ).scalars()
            )
            n = 0
            for bar in bars:
                if bar.rate_date in existing:
                    continue
                session.add(
                    FxRate(
                        rate_date=bar.rate_date,
                        base_currency=ccy,
                        quote_currency=settings.base_currency,
                        rate=float(bar.rate),
                        source=provider.name,
                    )
                )
                n += 1
            session.flush()
            inserted_total += n
            summary["fx"][f"{ccy}{settings.base_currency}"] = {"inserted": n, "status": "ok"}

        run.records_inserted = inserted_total
        finish_import_run(session, run, details=summary)
        log.info("update-prices end: inserted=%d errors=%d", inserted_total, len(summary["errors"]))
        for e in summary["errors"]:
            log.warning("update-prices: %s", e)
        return summary
    except Exception as exc:
        finish_import_run(session, run, error=f"{type(exc).__name__}: {exc}", details=summary)
        log.error("update-prices failed: %s", exc)
        raise
