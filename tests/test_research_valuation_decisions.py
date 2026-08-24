"""Critical v2 tests: deterministic valuation (spec 49), immutable decision snapshots (spec 50),
predictions and simple calibration storage."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from src.db.research import Decision
from src.research.decisions import decision_snapshot, list_decisions, record_decision
from src.research.investments import ResearchError, create_investment
from src.research.predictions import (
    create_prediction,
    overdue_predictions,
    resolve_prediction,
    simple_stats,
)
from src.research.valuation import (
    add_scenario,
    annualize,
    create_model,
    scenario_return,
    scenarios_for,
    set_reference_price,
    summarize_model,
    validate_probabilities,
)

D = Decimal


# --- valuation (spec 49) ----------------------------------------------------


def test_critical_probability_weighted_valuation(session) -> None:
    inv = create_investment(session, "TEST")
    m = create_model(session, inv, "Scenario PT", model_type="custom", reference_price=100, reference_currency="USD")
    add_scenario(session, m, "bear", target_price=70, probability=20)
    add_scenario(session, m, "base", target_price=140, probability=50)
    add_scenario(session, m, "bull", target_price=200, probability=30)
    summary = summarize_model(m, scenarios_for(session, m))
    # 0.2*70 + 0.5*140 + 0.3*200 = 144
    assert summary.weighted_target == D("144")
    assert summary.weighted_return == D("0.44")  # +44%
    assert summary.base_fair_value == D("140")
    # margin of safety = (fair - ref) / fair
    assert summary.margin_of_safety_base == (D("140") - 100) / D("140")
    assert summary.margin_of_safety_weighted == (D("144") - 100) / D("144")
    by_name = {r.scenario_name: r for r in summary.scenarios}
    assert by_name["bear"].expected_return == D("-0.3")
    assert by_name["bull"].expected_return == D("1.0")


def test_critical_invalid_probabilities_rejected(session) -> None:
    inv = create_investment(session, "TEST")
    m = create_model(session, inv, "PT", reference_price=100)
    add_scenario(session, m, "bear", target_price=70, probability=20)
    add_scenario(session, m, "base", target_price=140, probability=50)
    add_scenario(session, m, "bull", target_price=200, probability=40)  # sums to 110
    with pytest.raises(ResearchError, match="probabilities sum"):
        validate_probabilities(scenarios_for(session, m))
    with pytest.raises(ResearchError):
        summarize_model(m, scenarios_for(session, m))


def test_valuation_edge_cases(session) -> None:
    inv = create_investment(session, "TEST")
    m = create_model(session, inv, "PT", reference_price=100, reference_currency="USD")
    # missing probabilities -> weighting unavailable, scenarios still computed
    add_scenario(session, m, "base", target_price=120, time_horizon_months=24, expected_dividends=6)
    summary = summarize_model(m, scenarios_for(session, m))
    assert summary.weighted_target is None
    base = summary.scenarios[0]
    assert base.expected_return == D("0.26")  # (120+6)/100 - 1
    # annualized: 1.26^(12/24)-1
    assert abs(base.annualized_return - (D(str(1.26**0.5)) - 1)) < D("0.000001")
    assert summary.margin_of_safety_base == (D("126") - 100) / D("126")

    with pytest.raises(ResearchError):
        add_scenario(session, m, "bad", target_price=0)
    with pytest.raises(ResearchError):
        add_scenario(session, m, "bad2", target_price=100, probability=120)
    with pytest.raises(ResearchError):
        scenario_return(D("0"), D("100"))
    assert annualize(D("-1.5"), 12) is None  # total loss: not annualizable
    assert annualize(D("0.1"), 0) is None

    set_reference_price(session, m, 110, on=date(2026, 8, 1))
    assert m.reference_price == 110 and m.reference_date == date(2026, 8, 1)


def test_valuation_without_reference_price(session) -> None:
    inv = create_investment(session, "TEST")
    m = create_model(session, inv, "PT")  # no reference price
    add_scenario(session, m, "base", target_price=120, probability=100)
    summary = summarize_model(m, scenarios_for(session, m))
    assert summary.reference_price is None
    assert summary.weighted_target == D("120")  # target itself is computable
    assert summary.weighted_return is None  # but returns/MoS are Unavailable, not guessed
    assert summary.margin_of_safety_base is None


# --- decision snapshots (spec 50) -------------------------------------------


def _build_portfolio(session, flex_xml: str, as_of: date) -> None:
    from src.connectors.ibkr.flex_parser import parse_flex_statement
    from src.db.models import Instrument
    from src.portfolio.importer import import_statement
    from src.portfolio.positions import rebuild_positions
    from tests.conftest import add_fx, add_price

    import_statement(session, parse_flex_statement(flex_xml), source="ibkr")
    rebuild_positions(session)
    inst = {i.symbol: i for i in session.scalars(select(Instrument))}
    add_price(session, inst["AAPL"], as_of, "170")
    add_price(session, inst["VOO"], as_of, "470")
    add_price(session, inst["SAP"], as_of, "150")
    add_fx(session, "USD", "CZK", as_of, "23.5")
    add_fx(session, "USD", "CZK", date(2024, 1, 2), "23.5")
    add_fx(session, "EUR", "CZK", as_of, "25.0")
    session.commit()


def test_critical_decision_snapshot_is_frozen(session, flex_xml) -> None:
    from datetime import datetime

    from src.db.models import Price
    from src.db.models import Instrument
    from src.portfolio.valuation import value_portfolio

    as_of = date(2024, 3, 15)
    _build_portfolio(session, flex_xml, as_of)
    inv = create_investment(session, "AAPL", status="OWNED")

    val_before = value_portfolio(session, "CZK", as_of)
    decision = record_decision(
        session, inv, "BUY", base_currency="CZK",
        decided_at=datetime(2024, 3, 15, 10, 0), reasoning="test",
    )
    session.commit()
    frozen_portfolio = decision.portfolio_value
    frozen_weight = decision.position_weight_before
    frozen_price = decision.instrument_price
    assert frozen_portfolio == float(round(val_before.total_value_base, 8))
    assert frozen_price == 170.0
    assert decision.position_quantity_before == 10.0

    # portfolio changes later: price doubles
    aapl = session.scalars(select(Instrument).where(Instrument.symbol == "AAPL")).one()
    session.add(Price(instrument_id=aapl.id, price_date=date(2024, 6, 1), close=340.0, currency="USD", source="test"))
    session.commit()
    val_after = value_portfolio(session, "CZK", date(2024, 6, 1))
    assert val_after.total_value_base != val_before.total_value_base

    # historical decision is untouched
    d_db = session.get(Decision, decision.id)
    assert d_db.portfolio_value == frozen_portfolio
    assert d_db.position_weight_before == frozen_weight
    assert d_db.instrument_price == 170.0
    snap = decision_snapshot(d_db)
    assert snap["portfolio_value"] == frozen_portfolio
    assert snap["as_of"] == "2024-03-15"


def test_decision_snapshot_unavailable_values_are_none(session) -> None:
    """No prices, no portfolio -> snapshot must contain None, never fabricated numbers."""
    inv = create_investment(session, "PRIVATECO")
    d = record_decision(session, inv, "WATCH", base_currency="CZK", reasoning="no data yet")
    assert d.instrument_price is None
    assert d.position_quantity_before is None
    snap = decision_snapshot(d)
    assert snap["position_weight"] is None


def test_decision_validation_and_research_context(session, inv_with_thesis=None) -> None:
    from src.research.theses import create_thesis

    inv = create_investment(session, "TEST")
    thesis, v1 = create_thesis(session, inv, "T")
    with pytest.raises(ResearchError):
        record_decision(session, inv, "YOLO", base_currency="CZK")
    with pytest.raises(ResearchError):
        record_decision(session, inv, "BUY", base_currency="CZK", confidence=150)
    d = record_decision(session, inv, "NO_ACTION", base_currency="CZK", reasoning="valuation too high")
    assert d.thesis_version_id == v1.id  # auto-linked to current version
    snap = decision_snapshot(d)
    assert snap["research"]["thesis_version"] == {"id": v1.id, "number": 1}
    # decisions NOT to act are recorded equally
    assert list_decisions(session, inv)[0].decision_type == "NO_ACTION"


# --- predictions -------------------------------------------------------------


def test_prediction_lifecycle_and_stats(session) -> None:
    inv = create_investment(session, "TEST")
    p1 = create_prediction(
        session, "Revenue growth > 25% in FY2027", 70, investment=inv,
        resolution_date=date(2027, 3, 1), category="growth",
    )
    p2 = create_prediction(session, "NPL < 6% next quarter", 80, investment=inv,
                           resolution_date=date(2026, 8, 1))
    assert p1.status == "OPEN"
    with pytest.raises(ResearchError):
        create_prediction(session, "x", 120)
    with pytest.raises(ResearchError):
        create_prediction(session, "  ", 50)

    overdue = overdue_predictions(session, as_of=date(2026, 8, 23))
    assert [p.id for p in overdue] == [p2.id]

    resolve_prediction(session, p2, "RESOLVED_TRUE", outcome="NPL was 5.8%")
    assert p2.resolved_at is not None
    with pytest.raises(ResearchError):
        resolve_prediction(session, p2, "RESOLVED_FALSE")  # already resolved
    with pytest.raises(ResearchError):
        resolve_prediction(session, p1, "OPEN")

    resolve_prediction(session, p1, "RESOLVED_FALSE", outcome="growth 18%")
    stats = simple_stats(session)
    assert stats["total"] == 2 and stats["resolved"] == 2
    assert stats["resolved_true"] == 1 and stats["hit_rate"] == 0.5
    assert stats["avg_probability_when_true"] == 80
    assert stats["avg_probability_when_false"] == 70
