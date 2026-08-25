"""Phase B tests: NU issuer extractor, extraction pipeline, earnings comparison (crit. test 49)."""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import func, select

from src.db.intelligence import AiProposal
from src.db.research import KpiObservation
from src.intelligence.earnings import (
    compare_kpis,
    comparison_table_text,
    flag_contradictions,
    run_extraction,
)
from src.intelligence.issuer_extractors import get_extractor, html_to_text, normalize_kpi_name
from src.research import kpis
from src.research.assumptions import add_assumption
from src.research.investments import ResearchError, create_investment
from src.research.theses import create_thesis

# Fixture modeled on the recurring language of Nu Holdings quarterly earnings releases
NU_RELEASE_HTML = """
<html><body>
<h1>Nu Holdings Ltd. Reports Second Quarter 2026 Financial Results</h1>
<p>Nu added 4.1 million customers in the quarter, reaching 105.5 million customers globally.</p>
<p>Monthly active customers reached 87.2 million, an activity rate of 83%.</p>
<p>In Mexico, Nu continued to scale, reaching 12.3 million customers.</p>
<p>Monthly average revenue per active customer (ARPAC) expanded to US$ 12.9.</p>
<p>The interest-earning portfolio reached US$ 13.8 billion, while the total credit portfolio grew 41% YoY.</p>
<p>Asset quality remained resilient: the 15-90 day NPL ratio was 4.4%, and the 90+ NPL ratio reached 6.9%.</p>
<p>Risk-adjusted NIM was 9.8% in the quarter.</p>
<p>Net income of US$ 553.2 million, with annualized ROE of 28%.</p>
</body></html>
"""


def _nu_with_kpis(session):
    inv = create_investment(session, "NU", name="Nu Holdings", status="OWNED")
    for name, unit, direction in [
        ("Customers", "m", "up"), ("Active customers", "m", "up"), ("ARPAC", "USD", "up"),
        ("Loan portfolio", "USD bn", "up"), ("Loan growth YoY", "%", "up"),
        ("15–90 day NPL", "%", "down"), ("NPL 90+", "%", "down"),
        ("Risk-adjusted NIM", "%", "up"), ("ROE", "%", "up"), ("Net income", "USD m", "up"),
        ("Mexico customers", "m", "up"),
    ]:
        kpis.add_kpi(session, inv, name, unit=unit, direction_good=direction)
    return inv


def test_nu_extractor_finds_kpis_with_excerpts() -> None:
    ex = get_extractor("nu")
    assert ex is not None and ex.version == "1.1"
    text = html_to_text(NU_RELEASE_HTML)
    results = {r.kpi_name: r for r in ex.extract(text)}
    expected = {
        "Customers": 105.5, "Active customers": 87.2, "Mexico customers": 12.3,
        "ARPAC": 12.9, "Loan portfolio": 13.8, "Loan growth YoY": 41.0,
        "15-90 day NPL": 4.4, "NPL 90+": 6.9, "Risk-adjusted NIM": 9.8,
        "ROE": 28.0, "Net income": 553.2,
    }
    for name, value in expected.items():
        assert name in results, f"missing {name}"
        assert results[name].value == value, (name, results[name].value)
        assert results[name].mode == "deterministic"
        assert results[name].excerpt  # provenance excerpt always present
    # excerpts actually contain the numbers (traceability)
    assert "105.5 million customers" in results["Customers"].excerpt
    assert "6.9%" in results["NPL 90+"].excerpt


def test_nu_extractor_missing_is_ok_wrong_is_not() -> None:
    ex = get_extractor("NU")
    sparse = html_to_text("<p>Nu reported net income of US$ 100 million. Nothing else disclosed.</p>")
    results = {r.kpi_name: r for r in ex.extract(sparse)}
    assert results["Net income"].value == 100.0  # billions->millions transform not applied
    assert "Customers" not in results and "NPL 90+" not in results  # missing, not guessed
    billions = html_to_text("<p>net income of US$ 1.2 billion for the year.</p>")
    assert {r.kpi_name: r.value for r in ex.extract(billions)}["Net income"] == 1200.0


def test_nu_extractor_ambiguous_conflicting_values() -> None:
    ex = get_extractor("NU")
    text = html_to_text(
        "<p>The 90+ NPL ratio reached 6.9%.</p><p>Excluding one-offs, 90+ NPL was 5.1%.</p>"
    )
    results = {r.kpi_name: r for r in ex.extract(text)}
    assert results["NPL 90+"].mode == "ambiguous"
    assert "conflicting" in results["NPL 90+"].notes


def test_extraction_pipeline_provenance_and_idempotency(session) -> None:
    inv = _nu_with_kpis(session)
    res = run_extraction(session, inv, NU_RELEASE_HTML, period="2026Q2", period_date=date(2026, 6, 30))
    session.commit()
    assert len(res.stored) == 11 and res.ambiguous == [] and res.unmatched_kpi_names == []
    obs = list(session.scalars(select(KpiObservation)))
    assert len(obs) == 11
    for o in obs:
        assert o.source == "issuer_extractor:nu-1.1"
        assert "excerpt:" in o.source_reference  # every value points to its source text
        assert o.created_by == "SYSTEM"
    npl = next(o for o in obs if "6.9" == f"{o.value:g}")
    assert "90+ NPL ratio reached 6.9%" in npl.source_reference
    # re-run: everything duplicate, nothing overwritten
    res2 = run_extraction(session, inv, NU_RELEASE_HTML, period="2026Q2", period_date=date(2026, 6, 30))
    assert len(res2.duplicates) == 11 and res2.stored == []
    assert session.scalar(select(func.count(KpiObservation.id))) == 11


def test_ambiguous_extraction_creates_proposal_not_fact(session) -> None:
    inv = _nu_with_kpis(session)
    ambiguous_html = "<p>The 90+ NPL ratio reached 6.9%.</p><p>Adjusted 90+ NPL was 5.1%.</p>"
    res = run_extraction(session, inv, ambiguous_html, period="2026Q2")
    session.commit()
    assert len(res.ambiguous) == 1 and res.stored == []
    assert session.scalar(select(func.count(KpiObservation.id))) == 0  # no fact stored
    p = session.scalars(select(AiProposal).where(AiProposal.proposal_type == "KPI_OBSERVATION")).one()
    assert p.status == "PENDING" and p.created_by == "SYSTEM"
    # re-run does not duplicate the proposal
    run_extraction(session, inv, ambiguous_html, period="2026Q2")
    assert session.scalar(select(func.count(AiProposal.id))) == 1


def test_extractor_missing_for_unknown_issuer(session) -> None:
    inv = create_investment(session, "ZZZZ")
    with pytest.raises(ResearchError, match="no issuer extractor"):
        run_extraction(session, inv, "<p>x</p>", period="2026Q2")


# ---------------------------------------------------------------- CRITICAL 49: KPI comparison


def test_critical_kpi_comparison_and_review_proposal(session) -> None:
    """NPL 90+ 6.5 -> 6.9 (+0.4pp), outside expected range -> proposal, NOT auto status change."""
    inv = create_investment(session, "NU", status="OWNED")
    thesis, _ = create_thesis(session, inv, "T")
    kpi = kpis.add_kpi(session, inv, "NPL 90+", unit="%", direction_good="down")
    a = add_assumption(
        session, thesis, "Credit quality remains controlled", kpi_id=kpi.id,
        status="SUPPORTED", expected_min=4.0, expected_max=6.6, unit="%",
    )
    kpis.add_observation(session, kpi, "2026Q1", 6.5, period_date=date(2026, 3, 31), source="c")
    kpis.add_observation(session, kpi, "2026Q2", 6.9, period_date=date(2026, 6, 30), source="c")
    session.commit()

    comps = compare_kpis(session, inv)
    row = next(c for c in comps if c.kpi == "NPL 90+")
    assert row.current == 6.9 and row.prev_quarter == 6.5
    assert row.qoq_change == 0.4  # +0.4pp deterministic
    assert row.flag == "outside_expected_range"
    assert row.assumption == "Credit quality remains controlled"
    assert "+0.4pp" in comparison_table_text([row]) or "+0.4" in comparison_table_text([row])

    created = flag_contradictions(session, inv, comps)
    session.commit()
    assert len(created) == 1
    p = session.get(AiProposal, created[0])
    assert p.proposal_type == "ASSUMPTION_STATUS_CHANGE" and p.status == "PENDING"
    assert "outside expected" in p.title
    # assumption NOT changed automatically
    assert a.status == "SUPPORTED"
    # idempotent: second run creates nothing new
    assert flag_contradictions(session, inv, compare_kpis(session, inv)) == []


def test_comparison_yoy_and_direction_flag(session) -> None:
    inv = create_investment(session, "NU")
    kpi = kpis.add_kpi(session, inv, "ROE", unit="%", direction_good="up")
    kpis.add_observation(session, kpi, "2025Q2", 30.0, period_date=date(2025, 6, 30), source="c")
    kpis.add_observation(session, kpi, "2026Q1", 29.0, period_date=date(2026, 3, 31), source="c")
    kpis.add_observation(session, kpi, "2026Q2", 28.0, period_date=date(2026, 6, 30), source="c")
    session.commit()
    row = next(c for c in compare_kpis(session, inv) if c.kpi == "ROE")
    assert row.yoy == 30.0 and row.yoy_change == -2.0
    assert row.qoq_change == -1.0
    assert row.flag == "moving_against_good_direction"  # info flag, no proposal without assumption
    assert flag_contradictions(session, inv, [row]) == []
