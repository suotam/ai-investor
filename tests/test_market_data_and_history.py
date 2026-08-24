from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select

from src.analytics.performance import benchmark_series, build_value_history, time_weighted_return
from src.connectors.ibkr.flex_parser import parse_flex_statement
from src.db.models import Benchmark, FxRate, Instrument, Price
from src.market_data.base import FxBar, MarketDataProvider, PriceBar
from src.market_data.service import PriceStore, ensure_benchmarks, update_prices
from src.portfolio.importer import import_statement
from src.portfolio.positions import rebuild_positions
from tests.conftest import add_fx, add_price

D = Decimal


class FakeProvider(MarketDataProvider):
    """Deterministic provider that records every call (to verify caching)."""

    name = "fake"

    def __init__(self) -> None:
        self.price_calls: list[tuple[str, date, date]] = []
        self.fx_calls: list[tuple[str, str, date, date]] = []

    def fetch_prices(self, symbol: str, start: date, end: date) -> list[PriceBar]:
        self.price_calls.append((symbol, start, end))
        if symbol == "UNKNOWN":
            return []
        out = []
        d = start
        while d <= end:
            if d.weekday() < 5:
                out.append(PriceBar(price_date=d, close=D(100 + d.toordinal() % 7)))
            d += timedelta(days=1)
        return out

    def fetch_fx(self, base: str, quote: str, start: date, end: date) -> list[FxBar]:
        self.fx_calls.append((base, quote, start, end))
        out = []
        d = start
        while d <= end:
            if d.weekday() < 5:
                out.append(FxBar(rate_date=d, rate=D("23.5") if base == "USD" else D("25")))
            d += timedelta(days=1)
        return out


def test_update_prices_caches_and_is_incremental(session, settings, flex_xml: str) -> None:
    import_statement(session, parse_flex_statement(flex_xml), source="ibkr")
    rebuild_positions(session)
    session.commit()
    provider = FakeProvider()
    end = date(2024, 3, 15)
    summary = update_prices(session, provider, settings, end=end)
    session.commit()
    n_prices = session.scalar(select(func.count(Price.id)))
    n_fx = session.scalar(select(func.count(FxRate.id)))
    assert n_prices > 0 and n_fx > 0
    assert session.scalar(select(func.count(Benchmark.id))) == 1
    symbols = {c[0] for c in provider.price_calls}
    assert {"AAPL", "VOO", "SAP.DE", "SPY"} <= symbols
    assert "EUR.USD" not in symbols  # cash instruments never fetched
    assert {("USD", "CZK"), ("EUR", "CZK")} == {(c[0], c[1]) for c in provider.fx_calls}
    assert summary["errors"] == []

    # second run same end date: nothing fetched, nothing inserted
    provider2 = FakeProvider()
    update_prices(session, provider2, settings, end=end)
    session.commit()
    assert provider2.price_calls == [] and provider2.fx_calls == []
    assert session.scalar(select(func.count(Price.id))) == n_prices

    # extend by a week: only the tail (with overlap) is requested, no duplicates
    provider3 = FakeProvider()
    update_prices(session, provider3, settings, end=end + timedelta(days=7))
    session.commit()
    assert all(c[1] >= end - timedelta(days=3) for c in provider3.price_calls)
    assert session.scalar(select(func.count(Price.id))) > n_prices
    dup = session.execute(
        select(Price.instrument_id, Price.price_date, func.count()).group_by(Price.instrument_id, Price.price_date).having(func.count() > 1)
    ).all()
    assert dup == []


def test_update_prices_reports_missing_symbol(session, settings) -> None:
    inst = Instrument(symbol="UNKNOWN", asset_type="stock", currency="USD", price_symbol="UNKNOWN")
    session.add(inst)
    session.flush()
    from src.db.models import Account, Transaction

    acc = Account(name="x", provider="manual", account_external_id="m1", base_currency="USD")
    session.add(acc)
    session.flush()
    session.add(
        Transaction(account_id=acc.id, instrument_id=inst.id, transaction_type="buy", trade_date=date(2024, 1, 2), quantity=1, price=1, currency="USD", net_amount=-1, source="manual", source_hash="h1")
    )
    session.commit()
    summary = update_prices(session, FakeProvider(), settings, end=date(2024, 1, 10))
    assert any("UNKNOWN" in e for e in summary["errors"])


def test_value_history_and_benchmark(session, settings, flex_xml: str) -> None:
    import_statement(session, parse_flex_statement(flex_xml), source="ibkr")
    rebuild_positions(session)
    session.commit()
    inst = {i.symbol: i for i in session.scalars(select(Instrument))}
    d0, d1 = date(2024, 1, 2), date(2024, 1, 3)
    for d in (d0, d1):
        add_price(session, inst["AAPL"], d, "185" if d == d0 else "190")
        add_fx(session, "USD", "CZK", d, "23")
    session.commit()
    pts = build_value_history(session, "CZK", start=d0, end=d1)
    assert [x.day for x in pts] == [d0, d1]
    # day0: deposit 10000 USD, bought 15 AAPL for 2777.5 -> cash 7222.5, holdings 15*185=2775
    assert pts[0].external_flow == D("10000") * 23
    assert pts[0].value == (D("7222.5") + D("2775")) * 23
    assert pts[1].value == (D("7222.5") + 15 * D("190")) * 23
    twr = time_weighted_return(pts)
    assert twr == pts[1].value / pts[0].value - 1
    # a day beyond prices for VOO (bought 2024-01-10) -> value unavailable
    pts2 = build_value_history(session, "CZK", start=date(2024, 1, 10), end=date(2024, 1, 10))
    assert pts2[0].value is None and any("VOO" in i for i in pts2[0].issues)

    # benchmark series converted to base currency
    ensure_benchmarks(session, settings)
    bm = session.scalars(select(Benchmark)).one()
    add_price(session, bm.instrument, d0, "470")
    add_price(session, bm.instrument, d1, "480")
    session.commit()
    series, label = benchmark_series(session, bm.instrument_id, d0, d1, "CZK")
    assert label == "CZK" and series[0][1] == D("470") * 23 and series[1][1] == D("480") * 23
    store = PriceStore(session)
    assert store.close_on(bm.instrument_id, d1 + timedelta(days=2)) == (D("480"), d1, "USD")
    assert store.latest(bm.instrument_id) == (D("480"), d1, "USD")
