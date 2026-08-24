"""Tests for the structured research importer (YAML/JSON -> v2 service layer)."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import func, select

from src.db.research import (
    Decision,
    Evidence,
    Investment,
    KpiObservation,
    Prediction,
    Thesis,
    ThesisAssumption,
    ThesisVersion,
    ValuationScenario,
)
from src.research.evidence import list_evidence
from src.research.importer import (
    ImportError_,
    apply_import,
    load_research_file,
    plan_import,
)
from src.research.investments import get_by_ticker
from src.research.theses import active_thesis, current_version

EXAMPLE = Path(__file__).parent.parent / "examples" / "research" / "nu_example.yaml"

MINIMAL = """
schema_version: 1
investment:
  symbol: {symbol}
  name: Test Co
  lifecycle_status: {status}
thesis:
  title: "Test thesis"
  core_thesis: "Revenue growth expected above 30%."
  confidence: 75
{extra}
"""


def write_yaml(tmp_path: Path, content: str, name: str = "test.yaml") -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


def minimal_file(tmp_path: Path, symbol: str = "TESTCO", status: str = "RESEARCHING", extra: str = "") -> Path:
    return write_yaml(tmp_path, MINIMAL.format(symbol=symbol, status=status, extra=extra))


# 1. valid import (full example file)
def test_valid_yaml_import_full_example(session) -> None:
    spec, digest = load_research_file(EXAMPLE)
    assert len(digest) == 64
    report = apply_import(session, spec, base_currency="CZK")
    session.commit()
    assert report.ticker == "DEMO" and report.thesis_note == "v1 created"
    inv = get_by_ticker(session, "DEMO")
    assert inv.status == "RESEARCHING"
    thesis = active_thesis(session, inv)
    v1 = current_version(session, thesis)
    assert v1.version_number == 1 and v1.confidence == 70
    assert report.counts == {
        "assumptions": 2, "risks": 1, "breakers": 1, "catalysts": 1, "kpis": 2,
        "kpi_observations": 3, "evidence": 2, "valuation_models": 1, "scenarios": 3,
        "predictions": 1, "decisions": 1,
    }
    # KPI-linked assumption
    a = session.scalars(
        select(ThesisAssumption).where(ThesisAssumption.name == "Credit quality remains controlled")
    ).one()
    assert a.kpi_id is not None
    # everything is provenance-tagged as IMPORT
    assert inv.created_by == "IMPORT" and v1.created_by == "IMPORT"
    # 11. imported BUY decision references thesis v1
    d = session.scalars(select(Decision)).one()
    assert d.thesis_version_id == v1.id and d.decision_type == "BUY"
    assert d.decided_at.date() == date(2026, 8, 19)
    # historical context: empty portfolio deterministically values to 0; the instrument
    # price is unavailable (no price cache) -> None, never fabricated
    assert d.portfolio_value == 0.0 and d.instrument_price is None


# 2. dry run writes nothing
def test_dry_run_creates_zero_rows(session) -> None:
    spec, _ = load_research_file(EXAMPLE)
    report = plan_import(session, spec)
    session.commit()
    assert report.dry_run and report.counts["assumptions"] == 2
    assert "DRY RUN" in report.format()
    for model in (Investment, Thesis, ThesisVersion, ThesisAssumption, Evidence, Prediction, Decision):
        assert session.scalar(select(func.count(model.id))) == 0, model.__tablename__


# 3. transactional rollback on invalid child
def test_rollback_on_invalid_child(tmp_path, session, db_url) -> None:
    from src.db.session import session_scope

    bad = minimal_file(
        tmp_path,
        extra="""
evidence:
  - target_type: assumption
    target_name: "No such assumption"
    direction: SUPPORTING
    title: "Orphan evidence"
""",
    )
    spec, _ = load_research_file(bad)
    with pytest.raises(ImportError_, match="not found in this file"):
        with session_scope(db_url) as s:
            apply_import(s, spec, base_currency="CZK")
    # NOTHING was written - not even the investment
    assert session.scalar(select(func.count(Investment.id))) == 0
    assert session.scalar(select(func.count(ThesisVersion.id))) == 0


# 4. duplicate import aborts safely / 5. existing thesis never overwritten
def test_duplicate_import_aborts_and_thesis_protected(tmp_path, session) -> None:
    f = minimal_file(tmp_path)
    spec, _ = load_research_file(f)
    apply_import(session, spec, base_currency="CZK")
    session.commit()
    with pytest.raises(ImportError_, match="already exists"):
        apply_import(session, load_research_file(f)[0], base_currency="CZK")
    # even with --allow-existing, a thesis section against an existing thesis is refused
    with pytest.raises(ImportError_, match="already has a thesis"):
        apply_import(session, load_research_file(f)[0], base_currency="CZK", allow_existing=True)
    # still exactly one version, unchanged (10. thesis v1 immutable)
    v = session.scalars(select(ThesisVersion)).one()
    assert v.version_number == 1 and "above 30%" in v.core_thesis
    assert session.scalar(select(func.count(Thesis.id))) == 1


def test_allow_existing_adds_non_thesis_records(tmp_path, session) -> None:
    apply_import(session, load_research_file(minimal_file(tmp_path))[0], base_currency="CZK")
    session.commit()
    addendum = write_yaml(
        tmp_path,
        """
schema_version: 1
investment:
  symbol: TESTCO
risks:
  - name: "New risk"
    category: macro
    severity: HIGH
""",
        "addendum.yaml",
    )
    report = apply_import(session, load_research_file(addendum)[0], base_currency="CZK", allow_existing=True)
    session.commit()
    assert report.counts["risks"] == 1
    assert any("already exists" in w for w in report.warnings)
    assert session.scalar(select(func.count(Investment.id))) == 1  # reused, not duplicated
    assert session.scalar(select(func.count(ThesisVersion.id))) == 1  # untouched


# 6. instrument links to existing instrument (no duplicate)
def test_instrument_links_to_existing(tmp_path, session, flex_xml) -> None:
    from src.connectors.ibkr.flex_parser import parse_flex_statement
    from src.db.models import Instrument
    from src.portfolio.importer import import_statement

    import_statement(session, parse_flex_statement(flex_xml), source="ibkr")
    session.commit()
    n_instruments = session.scalar(select(func.count(Instrument.id)))
    f = write_yaml(
        tmp_path,
        """
schema_version: 1
investment:
  symbol: AAPL
instrument:
  symbol: AAPL
  exchange: NASDAQ
  currency: USD
thesis:
  title: "AAPL thesis"
""",
    )
    spec, _ = load_research_file(f)
    report = apply_import(session, spec, base_currency="CZK")
    session.commit()
    inv = get_by_ticker(session, "AAPL")
    aapl = session.scalars(select(Instrument).where(Instrument.symbol == "AAPL")).one()
    assert inv.instrument_id == aapl.id
    assert f"id={aapl.id}" in report.instrument_note
    assert session.scalar(select(func.count(Instrument.id))) == n_instruments  # no duplicate created


def test_instrument_ambiguity_aborts(tmp_path, session) -> None:
    from src.db.models import Instrument

    session.add(Instrument(symbol="DUP", asset_type="stock", exchange="NYSE", currency="USD"))
    session.add(Instrument(symbol="DUP", asset_type="stock", exchange="LSE", currency="GBP"))
    session.commit()
    f = write_yaml(tmp_path, "schema_version: 1\ninvestment:\n  symbol: DUP\n")
    with pytest.raises(ImportError_, match="ambiguous"):
        plan_import(session, load_research_file(f)[0])
    # disambiguated by exchange+currency -> resolves
    f2 = write_yaml(
        tmp_path,
        "schema_version: 1\ninvestment:\n  symbol: DUP\ninstrument:\n  symbol: DUP\n  exchange: NYSE\n  currency: USD\n",
        "dup2.yaml",
    )
    report = plan_import(session, load_research_file(f2)[0])
    assert "NYSE" in report.instrument_note


# 7+8. assumption name reference resolution, evidence targets correct assumption
def test_evidence_targets_named_assumption(session) -> None:
    spec, _ = load_research_file(EXAMPLE)
    apply_import(session, spec, base_currency="CZK")
    session.commit()
    inv = get_by_ticker(session, "DEMO")
    a = session.scalars(
        select(ThesisAssumption).where(ThesisAssumption.name == "Credit quality remains controlled")
    ).one()
    linked = list_evidence(session, inv, target_type="assumption", target_id=a.id)
    assert len(linked) == 1
    assert linked[0].title == "NPL 90+ ticked up to 5.4%" and linked[0].direction == "CONTRADICTING"


# 9. valuation probability validation still applies
def test_scenario_probability_validation(tmp_path, session) -> None:
    f = minimal_file(
        tmp_path,
        extra="""
valuation:
  models:
    - name: "PT"
      model_type: pe
      reference_price: 100
      scenarios:
        - {scenario_name: bear, probability: 20, target_price: 70}
        - {scenario_name: base, probability: 50, target_price: 140}
        - {scenario_name: bull, probability: 40, target_price: 200}
""",
    )
    with pytest.raises(ImportError_, match="probabilities sum"):
        load_research_file(f)
    assert session.scalar(select(func.count(ValuationScenario.id))) == 0


# 12. malformed YAML clear error
def test_malformed_yaml_clear_error(tmp_path) -> None:
    p = write_yaml(tmp_path, "investment: [unclosed\n  - ][")
    with pytest.raises(ImportError_, match="cannot parse"):
        load_research_file(p)
    p2 = write_yaml(tmp_path, "- just\n- a list\n", "list.yaml")
    with pytest.raises(ImportError_, match="must be a mapping"):
        load_research_file(p2)


# 13. unknown lifecycle/status/unknown fields rejected
def test_invalid_enums_and_unknown_fields_rejected(tmp_path) -> None:
    with pytest.raises(ImportError_, match="lifecycle_status"):
        load_research_file(minimal_file(tmp_path, status="MOONING"))
    unknown = write_yaml(
        tmp_path,
        "schema_version: 1\ninvestment:\n  symbol: X\n  moon_target: 100\n",
        "unknown.yaml",
    )
    with pytest.raises(ImportError_, match="moon_target"):
        load_research_file(unknown)
    badver = write_yaml(tmp_path, "schema_version: 2\ninvestment:\n  symbol: X\n", "ver.yaml")
    with pytest.raises(ImportError_, match="schema_version"):
        load_research_file(badver)
    baddir = minimal_file(
        tmp_path,
        extra="""
evidence:
  - title: "x"
    direction: BULLISH
""",
    )
    with pytest.raises(ImportError_, match="direction"):
        load_research_file(baddir)


def test_assumptions_without_thesis_rejected(tmp_path, session) -> None:
    f = write_yaml(
        tmp_path,
        """
schema_version: 1
investment:
  symbol: NOTHESIS
assumptions:
  - name: "Orphan assumption"
""",
    )
    with pytest.raises(ImportError_, match="require a thesis"):
        apply_import(session, load_research_file(f)[0], base_currency="CZK")


def test_json_import_supported(tmp_path, session) -> None:
    import json

    data = {
        "schema_version": 1,
        "investment": {"symbol": "JSONCO", "lifecycle_status": "watchlist"},
        "thesis": {"title": "JSON thesis", "confidence": 50},
        "predictions": [{"statement": "It works", "probability": 90}],
    }
    p = tmp_path / "inv.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    spec, _ = load_research_file(p)
    report = apply_import(session, spec, base_currency="CZK")
    session.commit()
    assert report.counts["predictions"] == 1
    assert get_by_ticker(session, "JSONCO").status == "WATCHLIST"  # case-insensitive enum


def test_kpi_observations_and_aliases(session) -> None:
    spec, _ = load_research_file(EXAMPLE)
    apply_import(session, spec, base_currency="CZK")
    session.commit()
    assert session.scalar(select(func.count(KpiObservation.id))) == 3
    from src.db.research import InvestmentKpi

    roe = session.scalars(select(InvestmentKpi).where(InvestmentKpi.name == "ROE")).one()
    assert roe.direction_good == "up"
