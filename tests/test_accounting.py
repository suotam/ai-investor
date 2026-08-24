"""Reconciliation regression tests (real-account bug: FX conversion cash legs).

These tests encode the accounting invariants:
  * a BUY consumes cash, a SELL creates cash (commissions/fees included),
  * a deposit does not count twice once invested,
  * FX conversions move cash between currency ledgers (IBKR reports netCash="0" for them!),
  * portfolio equity = cash + market value of positions.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select

from src.connectors.ibkr.flex_parser import parse_flex_statement
from src.db.models import Instrument, Price
from src.portfolio.cash import cash_balances
from src.portfolio.importer import import_statement
from src.portfolio.positions import rebuild_positions
from src.portfolio.reconcile import format_report, reconcile
from src.portfolio.valuation import value_portfolio
from tests.conftest import add_fx, add_price

D = Decimal


def flex(trades: str = "", cash: str = "", currency: str = "CZK") -> str:
    return f"""<FlexQueryResponse queryName="t" type="AF">
<FlexStatements count="1">
<FlexStatement accountId="U1" fromDate="20260817" toDate="20260822">
<AccountInformation accountId="U1" currency="{currency}" name="Test" />
<Trades>{trades}</Trades>
<CashTransactions>{cash}</CashTransactions>
</FlexStatement>
</FlexStatements>
</FlexQueryResponse>"""


DEPOSIT_11000 = (
    '<CashTransaction currency="CZK" type="Deposits/Withdrawals" amount="11000" dateTime="20260817" '
    'transactionID="90001" description="EFT" levelOfDetail="DETAIL" />'
)


def stock_trade(tid: str, day: str, qty: str, price: str, comm: str, ccy: str = "CZK", symbol: str = "CEZ") -> str:
    q_, p_, c_ = D(qty), D(price), D(comm)
    proceeds = -(q_ * p_)
    net = proceeds + c_
    side = "BUY" if q_ > 0 else "SELL"
    return (
        f'<Trade assetCategory="STK" symbol="{symbol}" conid="111" currency="{ccy}" quantity="{qty}" '
        f'tradePrice="{price}" proceeds="{proceeds}" netCash="{net}" ibCommission="{comm}" '
        f'ibCommissionCurrency="{ccy}" buySell="{side}" tradeDate="{day}" dateTime="{day};120000" '
        f'transactionID="{tid}" levelOfDetail="EXECUTION" multiplier="1" />'
    )


def fx_trade(tid: str, day: str, pair: str, qty: str, rate: str, quote_ccy: str, comm: str = "0") -> str:
    """Real-world IBKR forex conversion row: netCash is ALWAYS 0, cash moves via proceeds+quantity."""
    proceeds = -(D(qty) * D(rate))
    side = "BUY" if D(qty) > 0 else "SELL"
    return (
        f'<Trade assetCategory="CASH" symbol="{pair}" currency="{quote_ccy}" quantity="{qty}" '
        f'tradePrice="{rate}" proceeds="{proceeds}" netCash="0" cost="0" ibCommission="{comm}" '
        f'ibCommissionCurrency="{quote_ccy}" buySell="{side}" tradeDate="{day}" dateTime="{day};110000" '
        f'transactionID="{tid}" levelOfDetail="EXECUTION" multiplier="1" />'
    )


def _import(session, xml: str) -> None:
    import_statement(session, parse_flex_statement(xml), source="ibkr")
    rebuild_positions(session)
    session.commit()


def _acc(session) -> int:
    from src.db.models import Account

    return session.scalars(select(Account)).one().id


def test_a_deposit_buy_cash_equity(session) -> None:
    """Deposit 11,000; buy 100 x 89 CZK, commission 10 -> cash 2,090; cost 8,910; equity 11,090."""
    xml = flex(trades=stock_trade("1001", "20260818", "100", "89", "-10"), cash=DEPOSIT_11000)
    _import(session, xml)
    acc = _acc(session)
    bal = cash_balances(session)
    assert bal == {(acc, "CZK"): D("2090")}

    inst = session.scalars(select(Instrument).where(Instrument.symbol == "CEZ")).one()
    as_of = date(2026, 8, 20)
    session.add(Price(instrument_id=inst.id, price_date=as_of, close=90.0, currency="CZK", source="test"))
    session.commit()

    val = value_portfolio(session, "CZK", as_of)
    row = val.positions[0]
    assert row.cost_basis_local == D("8910")
    assert row.market_value_base == D("9000")
    assert row.unrealized_pnl_base == D("90")
    assert val.cash_base == D("2090")
    assert val.invested_value_base == D("9000")
    assert val.total_value_base == D("11090")
    assert val.net_external_flows_base == D("11000")
    # critical invariant: equity == deposits + P/L, deposit NOT counted twice
    assert val.total_value_base == val.net_external_flows_base + D("90")


def test_b_deposit_buy_partial_sell(session) -> None:
    """Deposit 11,000; buy 100@89 (comm 10); sell 40@95 (comm 10)."""
    xml = flex(
        trades=stock_trade("1001", "20260818", "100", "89", "-10")
        + stock_trade("1002", "20260819", "-40", "95", "-10"),
        cash=DEPOSIT_11000,
    )
    _import(session, xml)
    acc = _acc(session)
    # cash: 11000 - 8910 + (40*95 - 10) = 11000 - 8910 + 3790 = 5880
    assert cash_balances(session) == {(acc, "CZK"): D("5880")}

    inst = session.scalars(select(Instrument).where(Instrument.symbol == "CEZ")).one()
    as_of = date(2026, 8, 20)
    session.add(Price(instrument_id=inst.id, price_date=as_of, close=90.0, currency="CZK", source="test"))
    session.commit()
    val = value_portfolio(session, "CZK", as_of)
    row = val.positions[0]
    avg = D("8910") / 100  # 89.10 incl. commission
    assert row.quantity == D("60")
    assert row.cost_basis_local == avg * 60  # 5346
    assert val.realized_pnl_base == D("3790") - avg * 40  # 226
    assert row.market_value_base == D("5400")
    assert row.unrealized_pnl_base == D("5400") - avg * 60  # 54
    assert val.total_value_base == D("5880") + D("5400")  # 11280
    # equity == deposit + realized + unrealized
    assert val.total_value_base == D("11000") + val.realized_pnl_base + val.unrealized_pnl_base


def test_c_multicurrency_fx_then_usd_stock(session) -> None:
    """Real-account regression: CZK deposit -> USD.CZK conversion (netCash=0!) -> USD stock buy."""
    xml = flex(
        trades=fx_trade("2001", "20260818", "USD.CZK", "430.8", "20.7339", "CZK")
        + stock_trade("2002", "20260819", "30", "14.36", "-1.00009", ccy="USD", symbol="NU"),
        cash=DEPOSIT_11000,
    )
    _import(session, xml)
    acc = _acc(session)
    bal = cash_balances(session)
    # CZK: 11000 - 430.8*20.7339 = 11000 - 8932.16412 = 2067.83588
    assert bal[(acc, "CZK")] == D("11000") - D("430.8") * D("20.7339")
    # USD: +430.8 (conversion) - (30*14.36 + 1.00009) = 430.8 - 431.80009 = -1.00009
    assert bal[(acc, "USD")] == D("430.8") - D("431.80009")

    # positions: only NU (the FX pair is cash, not a position)
    from src.db.models import Position

    pos = list(session.scalars(select(Position).where(Position.quantity != 0)))
    assert len(pos) == 1

    nu = session.scalars(select(Instrument).where(Instrument.symbol == "NU")).one()
    as_of = date(2026, 8, 20)
    session.add(Price(instrument_id=nu.id, price_date=as_of, close=15.0, currency="USD", source="test"))
    add_fx(session, "USD", "CZK", as_of, "20.70")
    session.commit()
    val = value_portfolio(session, "CZK", as_of)
    expected_cash = (D("11000") - D("430.8") * D("20.7339")) + (D("430.8") - D("431.80009")) * D("20.70")
    assert val.cash_base == expected_cash
    assert val.invested_value_base == 30 * D("15.0") * D("20.70")  # 9315 CZK
    assert val.total_value_base == expected_cash + D("9315.0")
    # equity stays near the deposit: ~11,362 CZK, NOT ~20,000 (no double counting)
    assert D("11000") < val.total_value_base < D("11700")


def test_d_commission_signs(session) -> None:
    """Commission must reduce cash exactly once, on both buys and sells."""
    xml = flex(
        trades=stock_trade("3001", "20260818", "10", "100", "-5")
        + stock_trade("3002", "20260819", "-10", "100", "-5"),
        cash=DEPOSIT_11000,
    )
    _import(session, xml)
    acc = _acc(session)
    # buy: -(1000) - 5 ; sell: +1000 - 5 => net effect on cash = -10 (2x commission), price unchanged
    assert cash_balances(session) == {(acc, "CZK"): D("10990")}
    val = value_portfolio(session, "CZK", date(2026, 8, 20))
    assert val.realized_pnl_base == D("-10")  # the two commissions
    assert val.positions == []  # closed
    assert val.total_value_base == D("10990")


def test_e_reimport_leaves_ledger_unchanged(session, settings) -> None:
    xml = flex(
        trades=fx_trade("2001", "20260818", "USD.CZK", "430.8", "20.7339", "CZK")
        + stock_trade("2002", "20260819", "30", "14.36", "-1.00009", ccy="USD", symbol="NU"),
        cash=DEPOSIT_11000,
    )
    _import(session, xml)
    bal1 = cash_balances(session)
    r2 = import_statement(session, parse_flex_statement(xml), source="ibkr")
    rebuild_positions(session)
    session.commit()
    assert r2.inserted == 0 and r2.duplicates == 3
    assert cash_balances(session) == bal1
    val = value_portfolio(session, "CZK", date(2026, 8, 20))
    assert val.positions_count == 1  # still exactly one open position


def test_fx_sell_direction_and_history_consistency(session) -> None:
    """SELL EUR.CZK moves EUR->CZK; daily history ledger equals point-in-time ledger."""
    xml = flex(
        trades=fx_trade("4001", "20260818", "EUR.CZK", "-100", "24.5", "CZK"),
        cash=DEPOSIT_11000,
    )
    _import(session, xml)
    acc = _acc(session)
    bal = cash_balances(session)
    assert bal[(acc, "CZK")] == D("11000") + D("2450")
    assert bal[(acc, "EUR")] == D("-100")

    from src.analytics.performance import build_value_history

    pts = build_value_history(session, "CZK", start=date(2026, 8, 19), end=date(2026, 8, 19))
    # cash in history must match the ledger (needs FX for EUR)
    add_fx(session, "EUR", "CZK", date(2026, 8, 19), "24.5")
    session.commit()
    pts = build_value_history(session, "CZK", start=date(2026, 8, 19), end=date(2026, 8, 19))
    assert pts[0].cash == D("13450") - D("100") * D("24.5")
    assert pts[0].value == pts[0].cash  # no securities


def test_migration_0002_fixes_old_fx_rows(settings) -> None:
    """Rows imported before the fix (net_amount=0 from netCash) are repaired by the data migration."""
    import sqlite3

    from src.db.session import dispose_engine, get_session_factory
    from alembic import command
    from src.db.session import alembic_config

    # build schema at revision 0001 and insert a broken pre-fix FX row
    command.upgrade(alembic_config(settings.db_url), "0001_v1_core")
    con = sqlite3.connect(settings.db_path)
    con.execute(
        "INSERT INTO accounts (id, name, provider, account_external_id, base_currency, active, created_at)"
        " VALUES (1,'a','ibkr','U1','CZK',1,'2026-01-01')"
    )
    con.execute(
        "INSERT INTO instruments (id, symbol, asset_type, currency, created_at)"
        " VALUES (1,'USD.CZK','cash','CZK','2026-01-01'), (2,'NU','stock','USD','2026-01-01')"
    )
    con.execute(
        "INSERT INTO transactions (account_id, instrument_id, transaction_type, trade_date, quantity, price,"
        " currency, gross_amount, commission, fees, net_amount, source, source_hash, imported_at)"
        " VALUES (1,1,'buy','2026-08-19',430.8,20.7339,'CZK',-8932.16412,0,0,0.0,'ibkr','h1','2026-01-01'),"
        "        (1,2,'buy','2026-08-19',30,14.36,'USD',-430.8,-1.00009,0,-431.80009,'ibkr','h2','2026-01-01')"
    )
    con.commit()
    con.close()

    command.upgrade(alembic_config(settings.db_url), "head")

    factory = get_session_factory(settings.db_url)
    s = factory()
    try:
        bal = cash_balances(s)
        assert bal[(1, "CZK")] == D("-8932.16412")  # FX leg now applied
        assert bal[(1, "USD")] == D("430.8") - D("431.80009")
        recs, val = reconcile(s, "CZK", date(2026, 8, 20))
        report = format_report(recs, val, "CZK")
        assert "FX conversions" in report and "Security trade cash legs" in report
    finally:
        s.close()
        dispose_engine(settings.db_url)
