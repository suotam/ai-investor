from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from src.config import BenchmarkConfig, Settings
from src.db.models import FxRate, Instrument, Price
from src.db.session import dispose_engine, get_session_factory, run_migrations

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings(
        base_currency="CZK",
        default_benchmark="SPY",
        benchmarks=[BenchmarkConfig("SPY", "S&P 500", "USD")],
        db_path=tmp_path / f"test_{uuid.uuid4().hex}.db",
        raw_dir=tmp_path / "raw",
        log_path=tmp_path / "test.log",
    )
    return s


@pytest.fixture
def db_url(settings: Settings) -> str:
    run_migrations(settings.db_url)
    yield settings.db_url
    dispose_engine(settings.db_url)


@pytest.fixture
def session(db_url: str):
    factory = get_session_factory(db_url)
    s = factory()
    try:
        yield s
        s.commit()
    finally:
        s.close()


@pytest.fixture
def flex_xml() -> str:
    return (FIXTURES / "ibkr_flex_sample.xml").read_text(encoding="utf-8")


def add_price(session, instrument: Instrument, on: date, close: str, source: str = "test") -> None:
    session.add(
        Price(instrument_id=instrument.id, price_date=on, close=float(Decimal(close)), currency=instrument.currency, source=source)
    )
    session.flush()


def add_fx(session, base: str, quote: str, on: date, rate: str, source: str = "test") -> None:
    session.add(FxRate(rate_date=on, base_currency=base, quote_currency=quote, rate=float(Decimal(rate)), source=source))
    session.flush()
