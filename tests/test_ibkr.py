from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from src.connectors.ibkr.flex_parser import FlexParseError, parse_flex_statement
from src.connectors.ibkr.sync import sync_ibkr
from src.db.models import Account, CashFlow, ImportRun, Instrument, Transaction
from src.portfolio.cash import cash_balances
from src.portfolio.importer import import_statement
from tests.conftest import FIXTURES


def test_parse_flex_sample(flex_xml: str) -> None:
    stmt = parse_flex_statement(flex_xml)
    assert stmt.account_external_id == "U0000001"
    assert stmt.account_base_currency == "USD"
    assert stmt.period_from == date(2024, 1, 2) and stmt.period_to == date(2024, 3, 15)
    # 6 executions, the <Lot> row is skipped
    assert len(stmt.transactions) == 6
    # 4 DETAIL cash transactions, SUMMARY row skipped
    assert len(stmt.cash_flows) == 4

    aapl_buy = stmt.transactions[0]
    assert aapl_buy.external_id == "50001"
    assert aapl_buy.transaction_type == "buy"
    assert aapl_buy.quantity == Decimal("10")
    assert aapl_buy.price == Decimal("185.00")
    assert aapl_buy.gross_amount == Decimal("-1850")
    assert aapl_buy.commission == Decimal("-1")
    assert aapl_buy.net_amount == Decimal("-1851")
    assert aapl_buy.trade_date == date(2024, 1, 2)
    assert aapl_buy.settlement_date == date(2024, 1, 4)
    assert aapl_buy.instrument.provider_ids["ibkr_conid"] == "265598"
    assert aapl_buy.instrument.isin == "US0378331005"

    voo = stmt.transactions[2]
    assert voo.instrument.asset_type == "etf"

    sap = stmt.transactions[3]
    assert sap.currency == "EUR" and sap.fx_rate == Decimal("1.09")

    sell = stmt.transactions[4]
    assert sell.transaction_type == "sell" and sell.quantity == Decimal("-5") and sell.net_amount == Decimal("899")

    fx = stmt.transactions[5]
    assert fx.instrument.asset_type == "cash" and fx.instrument.symbol == "EUR.USD"

    types = {cf.flow_type for cf in stmt.cash_flows}
    assert types == {"deposit", "dividend", "tax", "fee"}
    dep = next(cf for cf in stmt.cash_flows if cf.flow_type == "deposit")
    assert dep.is_external and dep.amount == Decimal("10000")
    div = next(cf for cf in stmt.cash_flows if cf.flow_type == "dividend")
    assert not div.is_external and div.instrument.symbol == "AAPL"


def test_parse_error_response() -> None:
    xml = "<FlexStatementResponse><Status>Fail</Status><ErrorCode>1020</ErrorCode><ErrorMessage>Invalid</ErrorMessage></FlexStatementResponse>"
    with pytest.raises(FlexParseError):
        parse_flex_statement(xml)


def test_import_is_idempotent(session, flex_xml: str) -> None:
    stmt = parse_flex_statement(flex_xml)
    r1 = import_statement(session, stmt, source="ibkr")
    assert r1.transactions_inserted == 6 and r1.cash_flows_inserted == 4
    session.commit()

    r2 = import_statement(session, parse_flex_statement(flex_xml), source="ibkr")
    assert r2.transactions_inserted == 0 and r2.transactions_duplicate == 6
    assert r2.cash_flows_inserted == 0 and r2.cash_flows_duplicate == 4
    session.commit()

    assert session.scalar(select(func.count(Transaction.id))) == 6
    assert session.scalar(select(func.count(CashFlow.id))) == 4
    assert session.scalar(select(func.count(Account.id))) == 1


def test_import_without_external_ids_uses_hash(session, flex_xml: str) -> None:
    stripped = flex_xml.replace('transactionID="50001"', "").replace('tradeID="1001"', "").replace('ibExecID="0000e1a7.1"', "")
    stmt = parse_flex_statement(stripped)
    assert stmt.transactions[0].external_id is None
    import_statement(session, stmt, source="ibkr")
    session.commit()
    r2 = import_statement(session, parse_flex_statement(stripped), source="ibkr")
    assert r2.transactions_inserted == 0 and r2.transactions_duplicate == 6


def test_sync_from_file_archives_raw_and_records_run(session, settings) -> None:
    result = sync_ibkr(session, settings, xml_file=FIXTURES / "ibkr_flex_sample.xml")
    session.commit()
    assert result.inserted == 10
    run = session.scalars(select(ImportRun)).one()
    assert run.status == "success" and run.records_inserted == 10 and run.raw_sha256
    raw_files = list((settings.raw_dir / "ibkr").rglob("*.xml"))
    assert len(raw_files) == 1
    # repeated sync: zero inserts, second run recorded
    result2 = sync_ibkr(session, settings, xml_file=FIXTURES / "ibkr_flex_sample.xml")
    assert result2.inserted == 0 and result2.duplicates == 10


def test_instrument_resolution(session, flex_xml: str) -> None:
    import_statement(session, parse_flex_statement(flex_xml), source="ibkr")
    session.commit()
    instruments = {i.symbol: i for i in session.scalars(select(Instrument))}
    # AAPL traded 3 times -> one instrument
    assert session.scalar(select(func.count(Instrument.id)).where(Instrument.symbol == "AAPL")) == 1
    assert instruments["AAPL"].isin == "US0378331005"
    assert instruments["VOO"].asset_type == "etf"
    assert instruments["SAP"].currency == "EUR" and instruments["SAP"].price_symbol == "SAP.DE"
    assert instruments["EUR.USD"].asset_type == "cash"


def test_cash_balances_after_import(session, flex_xml: str) -> None:
    import_statement(session, parse_flex_statement(flex_xml), source="ibkr")
    session.commit()
    bal = cash_balances(session)
    acc = session.scalars(select(Account)).one().id
    # USD: 10000 - 1851 - 926.5 - 1721 - 1092 + 899 + 3.6 - 0.54 - 10
    assert bal[(acc, "USD")] == Decimal("5301.56")
    # EUR: +1000 (fx conversion) - 843 (SAP)
    assert bal[(acc, "EUR")] == Decimal("157")
