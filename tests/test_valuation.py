from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import func, select

from src.connectors.ibkr.flex_parser import parse_flex_statement
from src.db.models import Instrument, PortfolioSnapshot
from src.portfolio.fx import FxConverter
from src.portfolio.importer import import_statement
from src.portfolio.positions import rebuild_positions
from src.portfolio.valuation import create_snapshot, value_portfolio
from tests.conftest import add_fx, add_price

D = Decimal
AS_OF = date(2024, 3, 15)


def _setup(session, flex_xml: str, with_eur_fx: bool = True) -> dict[str, Instrument]:
    import_statement(session, parse_flex_statement(flex_xml), source="ibkr")
    rebuild_positions(session)
    session.commit()
    inst = {i.symbol: i for i in session.scalars(select(Instrument))}
    add_price(session, inst["AAPL"], AS_OF, "170")
    add_price(session, inst["VOO"], date(2024, 3, 14), "470")  # stale by 1 day -> backfilled
    add_price(session, inst["SAP"], AS_OF, "150")
    add_fx(session, "USD", "CZK", AS_OF, "23.5")
    add_fx(session, "USD", "CZK", date(2024, 1, 2), "23.5")  # deposit date (external flow conversion)
    if with_eur_fx:
        add_fx(session, "EUR", "CZK", AS_OF, "25.0")
    session.commit()
    return inst


def test_fx_converter_direct_inverse_cross_and_staleness(session) -> None:
    add_fx(session, "USD", "CZK", date(2024, 3, 10), "23.5")
    add_fx(session, "EUR", "CZK", date(2024, 3, 10), "25.0")
    fx = FxConverter(session, "CZK")
    assert fx.rate("USD", "CZK", date(2024, 3, 10)) == D("23.5")
    assert fx.rate("CZK", "USD", date(2024, 3, 10)) == D(1) / D("23.5")
    assert fx.rate("EUR", "USD", date(2024, 3, 10)) == D("25.0") / D("23.5")  # cross via base
    assert fx.rate("USD", "CZK", date(2024, 3, 15)) == D("23.5")  # backfill 5 days
    assert fx.rate("USD", "CZK", date(2024, 3, 30)) is None  # too stale -> Unavailable
    assert fx.rate("GBP", "CZK", date(2024, 3, 10)) is None
    assert fx.convert(D("100"), "USD", date(2024, 3, 10)) == D("2350.0")
    assert fx.convert(D("100"), "CZK", date(2024, 3, 10)) == D("100")


def test_value_portfolio_in_base_currency(session, flex_xml: str) -> None:
    _setup(session, flex_xml)
    val = value_portfolio(session, "CZK", AS_OF)
    by = {r.symbol: r for r in val.positions}
    assert set(by) == {"AAPL", "VOO", "SAP"}  # EUR.USD is cash, not a position
    assert by["AAPL"].market_value_local == D("1700")
    assert by["AAPL"].market_value_base == D("1700") * D("23.5")
    assert by["VOO"].price_date == date(2024, 3, 14)
    assert by["SAP"].market_value_base == D("900") * D("25.0")
    # unrealized = MV - cost basis (avg cost incl. commissions)
    aapl_cost = D("2777.5") / 15 * 10
    assert abs(by["AAPL"].unrealized_pnl_local - (D("1700") - aapl_cost)) < D("1e-6")
    assert abs(by["AAPL"].return_pct - (D("1700") - aapl_cost) / aapl_cost) < D("1e-9")

    invested = sum(r.market_value_base for r in val.positions)
    assert val.invested_value_base == invested
    assert abs(sum(r.weight for r in val.positions) - 1) < D("1e-12")
    assert by["AAPL"].weight == by["AAPL"].market_value_base / invested

    # cash: USD 5301.56 * 23.5 + EUR 157 * 25
    assert val.cash_base == D("5301.56") * D("23.5") + D("157") * D("25.0")
    assert val.total_value_base == val.cash_base + invested
    assert val.net_external_flows_base == D("10000") * D("23.5")
    assert not val.incomplete
    # realized: AAPL partial sell (avg cost) converted to CZK
    expected_realized = (D("899") - D("2777.5") / 15 * 5) * D("23.5")
    assert abs(val.realized_pnl_base - expected_realized) < D("1e-6")


def test_missing_fx_marks_unavailable_not_guessed(session, flex_xml: str) -> None:
    _setup(session, flex_xml, with_eur_fx=False)
    val = value_portfolio(session, "CZK", AS_OF)
    by = {r.symbol: r for r in val.positions}
    assert by["SAP"].market_value_local == D("900")
    assert by["SAP"].market_value_base is None
    assert val.invested_value_base is None
    assert val.total_value_base is None
    assert val.cash_base is None
    assert val.incomplete and any("SAP" in i for i in val.issues)


def test_missing_price_marks_unavailable(session, flex_xml: str) -> None:
    _setup(session, flex_xml)
    val = value_portfolio(session, "CZK", date(2024, 2, 1))  # before any prices
    assert all(r.price is None for r in val.positions)
    assert val.invested_value_base is None and val.incomplete


def test_snapshot_is_idempotent_per_date(session, flex_xml: str) -> None:
    _setup(session, flex_xml)
    s1 = create_snapshot(session, "CZK", AS_OF)
    session.commit()
    s2 = create_snapshot(session, "CZK", AS_OF)
    session.commit()
    assert s1.id == s2.id
    assert session.scalar(select(func.count(PortfolioSnapshot.id))) == 1
    assert s2.positions_count == 3 and not s2.incomplete
    assert abs(D(str(s2.account_value)) - D("1700") * D("23.5") - D("1880") * D("23.5") - D("900") * 25 - D("5301.56") * D("23.5") - D("157") * 25) < D("0.01")


def test_price_currency_differs_from_instrument_currency(session) -> None:
    """Crypto bought in CZK but priced by the provider in USD must be converted via FX."""
    from src.connectors.crypto.csv_importer import SOURCE, CsvCryptoImporter
    from src.db.models import Price
    from tests.conftest import FIXTURES

    import_statement(session, CsvCryptoImporter(FIXTURES / "crypto_sample.csv").load(), source=SOURCE)
    rebuild_positions(session)
    session.commit()
    btc = session.scalars(select(Instrument).where(Instrument.symbol == "BTC")).one()
    assert btc.currency == "CZK" and btc.price_symbol == "BTC-USD"
    session.add(Price(instrument_id=btc.id, price_date=AS_OF, close=70000.0, currency="USD", source="test"))
    add_fx(session, "USD", "CZK", AS_OF, "23.5")
    session.commit()
    val = value_portfolio(session, "CZK", AS_OF)
    row = next(r for r in val.positions if r.symbol == "BTC")
    assert row.price_currency == "USD"
    assert row.market_value_base == D("0.3") * D("70000") * D("23.5")
    assert row.market_value_local == row.market_value_base  # instrument currency is the base currency
    assert row.unrealized_pnl_local == row.market_value_local - D("500250") * D("0.3") / D("0.5")
