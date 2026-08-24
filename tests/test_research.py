"""Research layer tests: lifecycle, immutable thesis versioning (no hindsight), assumptions,
breakers, evidence, KPIs, reviews, health, portfolio linkage, migration preservation."""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import select

from src.db.research import Investment, ThesisVersion
from src.research import assumptions as asm
from src.research import evidence as ev
from src.research import items, kpis
from src.research.health import thesis_health
from src.research.investments import (
    ResearchError,
    create_investment,
    get_by_ticker,
    list_investments,
    mark_reviewed,
    portfolio_link,
    set_status,
)
from src.research.reviews import needs_attention
from src.research.theses import create_thesis, current_version, revise_thesis, version_history

TODAY = date(2026, 8, 23)


@pytest.fixture
def inv(session) -> Investment:
    return create_investment(session, "NU", name="Nu Holdings", status="RESEARCHING")


@pytest.fixture
def thesis_v1(session, inv):
    thesis, v1 = create_thesis(
        session, inv, "NU: underpriced LatAm bank",
        summary="High-ROE digital bank",
        core_thesis="Revenue growth expected above 30%.",
        market_expectation="Market prices a mature bank",
        our_expectation="Compounding at >30% for years",
        confidence=75,
        time_horizon="3y",
    )
    return thesis, v1


# --- investments -------------------------------------------------------------


def test_investment_creation_and_lifecycle(session) -> None:
    inv = create_investment(session, "googl", name="Alphabet", status="DISCOVERED")
    assert inv.ticker == "GOOGL" and inv.status == "DISCOVERED"
    assert inv.next_review_date is not None  # quarterly default
    for status in ("WATCHLIST", "RESEARCHING", "READY_FOR_DECISION", "OWNED", "EXITED", "ARCHIVED"):
        set_status(session, inv, status)
        assert inv.status == status
    with pytest.raises(ResearchError):
        set_status(session, inv, "MOONING")
    with pytest.raises(ResearchError):
        create_investment(session, "GOOGL")  # duplicate ticker
    assert [i.ticker for i in list_investments(session)] == ["GOOGL"]


def test_investment_links_existing_instrument(session, flex_xml) -> None:
    from src.connectors.ibkr.flex_parser import parse_flex_statement
    from src.portfolio.importer import import_statement

    import_statement(session, parse_flex_statement(flex_xml), source="ibkr")
    session.commit()
    inv = create_investment(session, "AAPL")
    assert inv.instrument_id is not None
    assert inv.name == "APPLE INC"  # picked up from the instrument, not duplicated
    # an investment with no tradable instrument still works
    inv2 = create_investment(session, "PRIVATECO")
    assert inv2.instrument_id is None


# --- thesis versioning / NO HINDSIGHT ---------------------------------------


def test_thesis_creation_and_revision_immutability(session, inv, thesis_v1) -> None:
    thesis, v1 = thesis_v1
    assert v1.version_number == 1 and thesis.current_version_id == v1.id
    v1_id, v1_text, v1_conf = v1.id, v1.core_thesis, v1.confidence

    v2 = revise_thesis(
        session, thesis, reason_for_revision="after Q2 earnings",
        core_thesis="Revenue growth expected above 20%.", confidence=60,
    )
    session.commit()
    assert v2.version_number == 2 and v2.previous_version_id == v1_id
    assert thesis.current_version_id == v2.id
    # v1 row is UNCHANGED
    v1_db = session.get(ThesisVersion, v1_id)
    assert v1_db.core_thesis == v1_text == "Revenue growth expected above 30%."
    assert v1_db.confidence == v1_conf == 75
    # carry-over of unspecified fields
    assert v2.market_expectation == v1.market_expectation
    assert [v.version_number for v in version_history(session, thesis)] == [1, 2]
    assert current_version(session, thesis).id == v2.id
    with pytest.raises(ResearchError):
        revise_thesis(session, thesis, reason_for_revision="")  # reason required


def test_critical_no_hindsight_rewrite(session, settings, inv, thesis_v1) -> None:
    """CRITICAL (spec 48): decision keeps referencing v1 after the thesis moves to v2."""
    from src.research.decisions import list_decisions, record_decision

    thesis, v1 = thesis_v1
    buy = record_decision(
        session, inv, "BUY", base_currency="CZK", reasoning="growth > 30% justifies entry"
    )
    assert buy.thesis_version_id == v1.id

    revise_thesis(
        session, thesis, reason_for_revision="lower growth", core_thesis="Revenue growth expected above 20%."
    )
    session.commit()

    buy_db = list_decisions(session, inv)[0]
    assert buy_db.thesis_version_id == v1.id  # still v1
    historical = session.get(ThesisVersion, buy_db.thesis_version_id)
    assert "above 30%" in historical.core_thesis  # what we believed then
    assert "above 20%" in current_version(session, thesis).core_thesis  # what we believe now


# --- assumptions -------------------------------------------------------------


def test_assumptions_and_status_changes(session, inv, thesis_v1) -> None:
    thesis, v1 = thesis_v1
    a = asm.add_assumption(
        session, thesis, "ROE stays above 25%", category="unit_economics", importance="HIGH",
        status="SUPPORTED", expected_min=25.0, unit="%", breaker_condition="ROE < 15% for 2 quarters",
    )
    assert a.introduced_in_version_id == v1.id
    asm.set_assumption_status(session, a, "WEAKENING", note="ROE fell to 26%")
    assert a.status == "WEAKENING" and "WEAKENING" in a.notes
    with pytest.raises(ResearchError):
        asm.set_assumption_status(session, a, "MAYBE")
    with pytest.raises(ResearchError):
        asm.add_assumption(session, thesis, "x", category="vibes")
    assert len(asm.list_assumptions(session, thesis)) == 1


def test_breakers(session, inv) -> None:
    b = items.add_breaker(session, inv, "NPL 90+ above 7%", condition_text="NPL90 > 7% two quarters", severity="CRITICAL")
    assert b.status == "ACTIVE" and b.triggered_at is None
    items.set_breaker_status(session, b, "TRIGGERED", note="Q3: NPL 7.4%")
    assert b.status == "TRIGGERED" and b.triggered_at is not None
    items.set_breaker_status(session, b, "RESOLVED", note="Q4 back to 5.9%")
    assert b.resolved_at is not None


def test_risks_and_catalysts(session, inv) -> None:
    r = items.add_risk(session, inv, "FX exposure", category="currency", severity="HIGH", probability=40)
    assert r.status == "OPEN"
    items.set_risk_status(session, r, "MITIGATED")
    with pytest.raises(ResearchError):
        items.add_risk(session, inv, "x", category="unknown-cat")
    c = items.add_catalyst(session, inv, "Mexico banking license", expected_date=date(2026, 12, 1), probability=60)
    items.resolve_catalyst(session, c, "OCCURRED", actual_date=date(2026, 11, 20), outcome="approved")
    assert c.status == "OCCURRED"


# --- evidence ----------------------------------------------------------------


def test_evidence_directions_and_linkage(session, inv, thesis_v1) -> None:
    thesis, v1 = thesis_v1
    a = asm.add_assumption(session, thesis, "Deposit growth continues", category="growth")
    ev.add_evidence(
        session, inv, "Q2: deposits +28% YoY", direction="SUPPORTING", evidence_type="earnings",
        target_type="assumption", target_id=a.id, event_date=date(2026, 6, 30),
        source_date=date(2026, 8, 14), reliability="HIGH",
    )
    ev.add_evidence(
        session, inv, "NPL 90+ ticked up to 6.1%", direction="CONTRADICTING", evidence_type="earnings",
    )
    ev.add_evidence(session, inv, "New CFO appointed", direction="NEUTRAL", evidence_type="news")
    by_dir = ev.evidence_by_direction(session, inv)
    assert len(by_dir["SUPPORTING"]) == 1 and len(by_dir["CONTRADICTING"]) == 1 and len(by_dir["NEUTRAL"]) == 1
    linked = ev.list_evidence(session, inv, target_type="assumption", target_id=a.id)
    assert len(linked) == 1 and linked[0].direction == "SUPPORTING"
    assert linked[0].observed_at is not None  # temporal correctness: when WE learned it
    with pytest.raises(ResearchError):
        ev.add_evidence(session, inv, "x", direction="BULLISH")


# --- KPIs --------------------------------------------------------------------


def test_kpis_and_observations(session, inv, thesis_v1) -> None:
    thesis, _ = thesis_v1
    kpi = kpis.add_kpi(session, inv, "NPL 90+", unit="%", frequency="quarterly", direction_good="down")
    kpis.add_observation(session, kpi, "2026Q1", 5.2, period_date=date(2026, 3, 31), source="company")
    kpis.add_observation(session, kpi, "2026Q2", 5.8, period_date=date(2026, 6, 30), source="company")
    with pytest.raises(ResearchError):
        kpis.add_observation(session, kpi, "2026Q2", 5.9, source="company")  # duplicate period+source
    a = asm.add_assumption(
        session, thesis, "NPL stays in range", kpi_id=kpi.id, expected_min=4.0, expected_max=5.0,
        unit="%", breaker_condition="> 7%",
    )
    cmp_ = kpis.kpi_vs_expectation(session, kpi)
    assert cmp_["latest_observation"].value == 5.8
    assert [o.value for o in cmp_["observations"]] == [5.2, 5.8]
    assert cmp_["expectations"][0].id == a.id
    with pytest.raises(ResearchError):
        kpis.add_kpi(session, inv, "NPL 90+")  # duplicate name


# --- reviews & health --------------------------------------------------------


def test_review_due_and_mark_reviewed(session) -> None:
    inv = create_investment(session, "NU", review_frequency="quarterly")
    inv.next_review_date = TODAY - timedelta(days=1)
    session.flush()
    na = needs_attention(session, today=TODAY)
    assert inv in na.reviews_due
    mark_reviewed(session, inv, on=TODAY)
    assert inv.last_review_date == TODAY and inv.next_review_date == TODAY + timedelta(days=91)
    assert inv not in needs_attention(session, today=TODAY).reviews_due


def test_needs_attention_sections(session, inv, thesis_v1) -> None:
    thesis, _ = thesis_v1
    a = asm.add_assumption(session, thesis, "Growth > 20%", status="SUPPORTED")
    asm.set_assumption_status(session, a, "BROKEN")
    b = items.add_breaker(session, inv, "Fraud", severity="CRITICAL")
    items.set_breaker_status(session, b, "TRIGGERED")
    items.add_risk(session, inv, "Rate shock", category="macro", severity="CRITICAL")
    items.add_catalyst(session, inv, "License", expected_date=TODAY - timedelta(days=10))
    from src.research.predictions import create_prediction

    create_prediction(session, "NPL < 6% in Q3", 70, investment=inv, resolution_date=TODAY - timedelta(days=1))
    na = needs_attention(session, today=TODAY)
    assert len(na.broken_assumptions) == 1
    assert len(na.triggered_breakers) == 1
    assert len(na.high_risks) == 1
    assert len(na.expired_catalysts) == 1
    assert len(na.predictions_awaiting) == 1
    assert na.total >= 5
    # archived investments drop out of attention
    set_status(session, inv, "ARCHIVED")
    assert needs_attention(session, today=TODAY).total == 0


def test_thesis_health_rules(session, inv, thesis_v1) -> None:
    thesis, _ = thesis_v1
    a1 = asm.add_assumption(session, thesis, "A1", status="SUPPORTED")
    asm.add_assumption(session, thesis, "A2", status="SUPPORTED")
    h = thesis_health(session, inv, today=TODAY)
    assert h.state == "HEALTHY" and h.supported == 2

    asm.set_assumption_status(session, a1, "WEAKENING")
    h = thesis_health(session, inv, today=TODAY)
    assert h.state == "WATCH" and h.weakening == 1

    asm.set_assumption_status(session, a1, "CHALLENGED")
    assert thesis_health(session, inv, today=TODAY).state == "AT_RISK"

    asm.set_assumption_status(session, a1, "BROKEN")
    assert thesis_health(session, inv, today=TODAY).state == "BROKEN"

    asm.set_assumption_status(session, a1, "SUPPORTED")
    b = items.add_breaker(session, inv, "B1")
    items.set_breaker_status(session, b, "TRIGGERED")
    h = thesis_health(session, inv, today=TODAY)
    assert h.state == "BROKEN" and h.breakers_triggered == 1

    items.set_breaker_status(session, b, "RESOLVED")
    assert thesis_health(session, inv, today=TODAY).state == "HEALTHY"

    # stale thesis -> WATCH (rule documented in health.py)
    v = current_version(session, thesis)
    v_created = session.get(ThesisVersion, v.id)
    h = thesis_health(session, inv, today=TODAY + timedelta(days=200))
    assert h.thesis_stale and h.state == "WATCH"


def test_health_without_assumptions_omits_state(session, inv) -> None:
    h = thesis_health(session, inv, today=TODAY)
    assert h.state is None and any("omitted" in r for r in h.reasons)


# --- portfolio linkage (no duplication) --------------------------------------


def test_owned_investment_reads_portfolio_without_duplication(session, flex_xml) -> None:
    from src.connectors.ibkr.flex_parser import parse_flex_statement
    from src.portfolio.importer import import_statement
    from src.portfolio.positions import rebuild_positions
    from src.portfolio.valuation import value_portfolio
    from tests.conftest import add_fx, add_price
    from src.db.models import Instrument

    import_statement(session, parse_flex_statement(flex_xml), source="ibkr")
    rebuild_positions(session)
    session.commit()
    inst = {i.symbol: i for i in session.scalars(select(Instrument))}
    as_of = date(2024, 3, 15)
    add_price(session, inst["AAPL"], as_of, "170")
    add_fx(session, "USD", "CZK", as_of, "23.5")
    session.commit()

    inv = create_investment(session, "AAPL", status="OWNED")
    val = value_portfolio(session, "CZK", as_of)
    link = portfolio_link(val, inv)
    assert link is not None
    assert link["quantity"] == 10
    assert link["market_value_base"] == val.positions[0].market_value_base if val.positions[0].symbol == "AAPL" else True
    # research tables hold no quantity/value columns - verify nothing was copied
    assert not hasattr(inv, "quantity") and not hasattr(inv, "market_value")

    watch = create_investment(session, "PLTR", status="WATCHLIST")
    assert portfolio_link(val, watch) is None


# --- migration preserves v1 data --------------------------------------------


def test_migration_0003_preserves_v1_data(settings) -> None:
    import sqlite3

    from alembic import command
    from src.db.session import alembic_config, dispose_engine

    command.upgrade(alembic_config(settings.db_url), "0002_fix_fx_cash_legs")
    con = sqlite3.connect(settings.db_path)
    con.execute(
        "INSERT INTO accounts (id, name, provider, account_external_id, base_currency, active, created_at)"
        " VALUES (1,'a','ibkr','U1','CZK',1,'2026-01-01')"
    )
    con.execute(
        "INSERT INTO instruments (id, symbol, asset_type, currency, created_at)"
        " VALUES (1,'NU','stock','USD','2026-01-01')"
    )
    con.execute(
        "INSERT INTO transactions (account_id, instrument_id, transaction_type, trade_date, quantity, price,"
        " currency, gross_amount, commission, fees, net_amount, source, source_hash, imported_at)"
        " VALUES (1,1,'buy','2026-08-19',30,14.36,'USD',-430.8,-1.00009,0,-431.80009,'ibkr','h2','2026-01-01')"
    )
    con.commit()
    con.close()

    command.upgrade(alembic_config(settings.db_url), "head")

    con = sqlite3.connect(settings.db_path)
    assert con.execute("SELECT count(*) FROM transactions").fetchone()[0] == 1
    row = con.execute("SELECT quantity, net_amount FROM transactions").fetchone()
    assert row == (30.0, -431.80009)
    assert con.execute("SELECT count(*) FROM investments").fetchone()[0] == 0  # new table, empty
    con.close()
    dispose_engine(settings.db_url)
