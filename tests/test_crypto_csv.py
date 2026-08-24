from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select

from src.connectors.crypto.csv_importer import SOURCE, CsvCryptoImporter
from src.db.models import Instrument
from src.portfolio.cash import cash_balances
from src.portfolio.importer import import_statement
from src.portfolio.positions import rebuild_positions
from tests.conftest import FIXTURES

D = Decimal


def test_csv_parse() -> None:
    stmt = CsvCryptoImporter(FIXTURES / "crypto_sample.csv").load()
    assert stmt.account_external_id == "crypto-csv"
    assert stmt.period_from == date(2024, 1, 5) and stmt.period_to == date(2024, 4, 1)
    assert len(stmt.transactions) == 4  # 2 buys, 1 sell, 1 staking reward
    assert len(stmt.cash_flows) == 2  # deposit + withdrawal
    buy = stmt.transactions[0]
    assert buy.instrument.symbol == "BTC" and buy.instrument.asset_type == "crypto"
    assert buy.quantity == D("0.5") and buy.price == D("1000000")
    assert buy.gross_amount == D("-500000") and buy.commission == D("-250") and buy.net_amount == D("-500250")
    assert buy.trade_datetime is not None
    sell = stmt.transactions[2]
    assert sell.quantity == D("-0.2") and sell.net_amount == D("240000") - 120
    staking = stmt.transactions[3]
    assert staking.quantity == D("0.01") and staking.price == 0 and staking.net_amount == 0
    dep, wd = stmt.cash_flows
    assert dep.flow_type == "deposit" and dep.amount == D("700000") and dep.is_external
    assert wd.flow_type == "withdrawal" and wd.amount == D("-20000")
    assert stmt.warnings == []


def test_csv_import_idempotent_and_positions(session) -> None:
    stmt = CsvCryptoImporter(FIXTURES / "crypto_sample.csv", base_currency="CZK").load()
    r1 = import_statement(session, stmt, source=SOURCE, source_file="crypto_sample.csv")
    session.commit()
    assert r1.inserted == 6
    r2 = import_statement(session, CsvCryptoImporter(FIXTURES / "crypto_sample.csv").load(), source=SOURCE)
    assert r2.inserted == 0 and r2.duplicates == 6

    states = rebuild_positions(session)
    session.commit()
    by = {session.get(Instrument, i).symbol: st for (_a, i), st in states.items()}
    assert by["BTC"].quantity == D("0.3")
    assert by["BTC"].cost_basis == D("500250") * D("0.3") / D("0.5")
    assert by["BTC"].realized_pnl == (D("240000") - 120) - D("500250") * D("0.2") / D("0.5")
    assert by["ETH"].quantity == D("2.01")
    assert by["ETH"].cost_basis == D("110100")  # staking adds quantity at zero cost
    bal = cash_balances(session)
    acc = r1.account_id
    assert bal[(acc, "CZK")] == D("700000") - D("500250") - D("110100") + (D("240000") - 120) - D("20000")


def test_csv_missing_columns(tmp_path) -> None:
    import pytest

    from src.connectors.crypto.csv_importer import CsvFormatError

    f = tmp_path / "bad.csv"
    f.write_text("date,type\n2024-01-01,buy\n", encoding="utf-8")
    with pytest.raises(CsvFormatError):
        CsvCryptoImporter(f).load()
