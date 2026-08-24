from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import func, select

from src.connectors.ibkr.flex_parser import parse_flex_statement
from src.db.models import Instrument, Position, TaxLot
from src.portfolio.importer import import_statement
from src.portfolio.positions import Trade, compute_position, rebuild_positions

D = Decimal


def t(day: str, qty: str, price: str, comm: str = "0") -> Trade:
    return Trade(trade_date=date.fromisoformat(day), quantity=D(qty), price=D(price), commission=D(comm))


def test_average_cost_includes_commission() -> None:
    st = compute_position([t("2024-01-02", "10", "185", "-1"), t("2024-01-02", "5", "185.2", "-0.5")])
    assert st.quantity == D("15")
    # (1850 + 1) + (926 + 0.5) = 2777.5
    assert st.cost_basis == D("2777.5")
    assert st.average_cost == D("2777.5") / D("15")
    assert st.realized_pnl == D("0")
    assert len(st.lots) == 2


def test_partial_sell_realizes_average_cost_pnl() -> None:
    st = compute_position(
        [t("2024-01-02", "10", "185", "-1"), t("2024-01-02", "5", "185.2", "-0.5"), t("2024-03-01", "-5", "180", "-1")]
    )
    avg = D("2777.5") / D("15")
    assert st.quantity == D("10")
    # proceeds 900 - 1 commission = 899 ; cost released 5*avg
    expected_pnl = D("899") - avg * 5
    assert abs(st.realized_pnl - expected_pnl) < D("1e-20")
    assert abs(st.cost_basis - avg * 10) < D("1e-20")
    # FIFO lots: first lot reduced to 5
    assert st.lots[0].quantity_remaining == D("5") and st.lots[1].quantity_remaining == D("5")
    # FIFO realized differs from average-cost realized (first lot cost 185.1/unit)
    assert abs(st.lots[0].realized_pnl - (D("899") - D("185.1") * 5)) < D("1e-20")
    assert len(st.closed_trades) == 1 and st.closed_trades[0].open_date == date(2024, 1, 2)


def test_full_close_resets_position() -> None:
    st = compute_position([t("2024-01-01", "10", "100", "-1"), t("2024-02-01", "-10", "110", "-1")])
    assert st.quantity == 0 and st.cost_basis == 0 and st.average_cost is None
    assert st.realized_pnl == D("98")  # 1100 - 1 - (1000 + 1)
    assert st.lots[0].closed_date == date(2024, 2, 1)


def test_short_position_symmetric() -> None:
    st = compute_position([t("2024-01-01", "-10", "100", "-1")])
    assert st.quantity == D("-10")
    assert st.cost_basis == D("-999")  # received 1000 - 1
    st = compute_position([t("2024-01-01", "-10", "100", "-1"), t("2024-02-01", "10", "90", "-1")])
    assert st.quantity == 0
    assert st.realized_pnl == D("98")  # 999 received - 901 paid


def test_cross_zero_splits_trade() -> None:
    st = compute_position([t("2024-01-01", "10", "100"), t("2024-02-01", "-15", "120")])
    assert st.quantity == D("-5")
    assert st.realized_pnl == D("200")  # closed 10 @ +20
    assert st.cost_basis == D("-600")  # short 5 @ 120
    assert len(st.lots) == 2 and st.lots[1].quantity_open == D("-5")


def test_chronological_order_independent_of_input_order() -> None:
    a = [t("2024-01-01", "10", "100"), t("2024-02-01", "-5", "120"), t("2024-03-01", "5", "90")]
    st1 = compute_position(a)
    st2 = compute_position(list(reversed(a)))
    assert st1.quantity == st2.quantity == D("10")
    assert st1.cost_basis == st2.cost_basis == D("500") + D("450")
    assert st1.realized_pnl == st2.realized_pnl == D("100")


def test_rebuild_positions_from_db(session, flex_xml: str) -> None:
    import_statement(session, parse_flex_statement(flex_xml), source="ibkr")
    session.commit()
    states = rebuild_positions(session)
    session.commit()
    by_symbol = {session.get(Instrument, inst_id).symbol: st for (_acc, inst_id), st in states.items()}
    assert by_symbol["AAPL"].quantity == D("10")
    assert by_symbol["VOO"].quantity == D("4")
    assert by_symbol["SAP"].quantity == D("6")
    assert by_symbol["SAP"].cost_basis == D("843")
    # FX conversion row is asset_type cash, it is still replayed but must not be a "security" position
    pos = {session.get(Instrument, p.instrument_id).symbol: p for p in session.scalars(select(Position))}
    assert abs(pos["AAPL"].average_cost - float(D("2777.5") / 15)) < 1e-6
    assert session.scalar(select(func.count(TaxLot.id))) >= 4
    # rebuilding again is idempotent
    rebuild_positions(session)
    session.commit()
    assert session.scalar(select(func.count(Position.id))) == len(states)
