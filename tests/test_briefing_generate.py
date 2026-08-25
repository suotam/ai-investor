"""Phase C/E tests: brief generation, AI fallback, audio, weekly review, calibration, feedback."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import func, select

from src.db.briefing import BriefFeedback, BriefItem, BriefRun
from src.intelligence.briefing.assemble import NO_CHANGE_SENTENCE
from src.intelligence.briefing.calibration import calibration_report
from src.intelligence.briefing.generate import generate_brief
from src.intelligence.briefing.regime import macro_regime
from src.research.investments import create_investment
from src.research.theses import create_thesis

T1 = datetime(2026, 8, 21, 6, 0)
T2 = datetime(2026, 8, 22, 6, 0)


@pytest.fixture
def quiet_nu(session):
    inv = create_investment(session, "NU", name="Nu Holdings", status="OWNED")
    inv.created_at = T1 - timedelta(days=60)
    inv.next_review_date = date(2026, 12, 1)
    thesis, v1 = create_thesis(session, inv, "NU thesis", core_thesis="Long-term compounder")
    v1.created_at = thesis.created_at = T1 - timedelta(days=60)
    session.commit()
    return inv


# ---------------------------------------------------------------- CRITICAL 50 end-to-end


def test_no_change_day_produces_no_change_brief(session, settings, quiet_nu) -> None:
    doc, run, md_path, audio_path = generate_brief(
        session, settings, "daily", use_ai=False, audio=True, now=T1
    )
    session.commit()
    assert doc.no_change and doc.executive_summary == [NO_CHANGE_SENTENCE]
    md = md_path.read_text(encoding="utf-8")
    assert NO_CHANGE_SENTENCE in md
    # NU still gets its calm one-liner section, not a wall of static risks
    assert "NU — thesis: UNCHANGED" in md
    assert "New information: none material." in md
    audio = audio_path.read_text(encoding="utf-8")
    assert NO_CHANGE_SENTENCE in audio
    assert "thesis unchanged, no new fundamental information" in audio
    assert "#" not in audio and "*" not in audio  # audio has no markdown noise
    assert run is not None and run.status == "completed" and run.items_count == 0


def test_ai_unavailable_fallback_never_fails_brief(session, settings, quiet_nu) -> None:
    settings.ai_enabled = False  # provider raises AIUnavailable
    doc, run, md_path, _ = generate_brief(session, settings, "daily", use_ai=True, now=T1)
    session.commit()
    assert doc.ai_synthesis is None
    assert "AI mentor synthesis unavailable" in (doc.ai_note or "")
    assert run is not None  # deterministic brief still completed
    assert "AI mentor synthesis unavailable" in md_path.read_text(encoding="utf-8")


def test_mentor_synthesis_included_when_ai_works(session, settings, quiet_nu) -> None:
    from tests.test_intelligence_ai import fake_provider

    reply = json.dumps({
        "today_in_one_minute": "Nothing material happened. NU thesis is unchanged. No action required.",
        "portfolio": None, "thesis_changes": None, "macro": None,
        "new_opportunities": None, "needs_your_judgment": None,
        "can_be_ignored": "All quiet; the suppressed items are unchanged background risks.",
    })
    doc, run, md_path, audio_path = generate_brief(
        session, settings, "daily", use_ai=True, now=T1, ai_provider=fake_provider(reply)
    )
    session.commit()
    assert doc.ai_synthesis["today_in_one_minute"].startswith("Nothing material")
    assert run.ai_used and run.ai_model == "test-model" and run.ai_prompt_version == "mentor-daily-1.0"
    assert run.ai_context_hash
    assert "No action required" in audio_path.read_text(encoding="utf-8")


def test_brief_rerun_semantics_no_false_news(session, settings, quiet_nu) -> None:
    from src.research.items import add_risk

    r = add_risk(session, quiet_nu, "Credit deterioration", severity="HIGH", category="financial")
    r.created_at = r.updated_at = T1 - timedelta(hours=2)
    session.commit()
    doc1, run1, _, _ = generate_brief(session, settings, "daily", use_ai=False, now=T1)
    session.commit()
    assert any("Credit deterioration" in s for s in doc1.executive_summary)

    # same-day re-run WITHOUT force -> preview, no new run, same content basis
    doc2, run2, _, _ = generate_brief(session, settings, "daily", use_ai=False, now=T1 + timedelta(hours=2))
    assert run2 is None and doc2.mode == "preview"
    assert session.scalar(select(func.count(BriefRun.id))) == 1

    # same-day --force -> supersedes, re-runs same window, still shows the item (not a false suppression)
    doc3, run3, _, _ = generate_brief(session, settings, "daily", use_ai=False, now=T1 + timedelta(hours=3), force=True)
    session.commit()
    assert run3 is not None and doc3.mode == "force"
    assert session.get(BriefRun, run1.id).status == "superseded"
    # force reproduces the same items - the superseded run must NOT suppress them
    assert any("Credit deterioration" in x for x in doc3.executive_summary)

    # next day -> risk does NOT reappear (critical suppression), no-change brief
    doc4, run4, _, _ = generate_brief(session, settings, "daily", use_ai=False, now=T2)
    session.commit()
    assert doc4.no_change
    assert not any("Credit deterioration" in s for s in doc4.executive_summary)
    # background line may mention it calmly - but never as an attention item
    md_sections = [s for s in doc4.investment_sections if s.ticker == "NU"]
    assert md_sections[0].background.startswith("Credit deterioration remains the most important")


def test_items_persisted_with_reason_and_sources(session, settings, quiet_nu) -> None:
    from src.research.items import add_risk

    r = add_risk(session, quiet_nu, "New risk", severity="HIGH", category="macro")
    r.created_at = r.updated_at = T1 - timedelta(hours=1)
    session.commit()
    _doc, run, _, _ = generate_brief(session, settings, "daily", use_ai=False, now=T1)
    session.commit()
    item = session.scalars(select(BriefItem).where(BriefItem.brief_run_id == run.id)).one()
    assert item.reason and "Shown once" in item.reason  # why am I seeing this
    assert json.loads(item.source_refs) == [f"risks:{r.id}"]  # traceable


def test_weekly_review_sections(session, settings, quiet_nu) -> None:
    from src.research import kpis
    from src.research.predictions import create_prediction, resolve_prediction

    kpi = kpis.add_kpi(session, quiet_nu, "ROE", unit="%", direction_good="up")
    kpis.add_observation(session, kpi, "2026Q1", 29.0, period_date=date(2026, 3, 31), source="c")
    kpis.add_observation(session, kpi, "2026Q2", 28.0, period_date=date(2026, 6, 30), source="c")
    p = create_prediction(session, "test", 70, investment=quiet_nu)
    resolve_prediction(session, p, "RESOLVED_TRUE")
    session.commit()
    doc, run, md_path, _ = generate_brief(session, settings, "weekly", use_ai=False, now=T1)
    session.commit()
    md = md_path.read_text(encoding="utf-8")
    for section in ("Performance vs benchmark", "Thesis health", "Contradicting evidence this week",
                    "KPI comparison", "Macro regime", "Decisions this week", "Prediction calibration",
                    "Questions for next week"):
        assert section in md, section
    assert "Insufficient sample" in md  # 1 resolved prediction < 10
    assert "ROE: 28" in md
    assert "inaction is a valid decision" in md


def test_calibration_insufficient_and_sufficient(session, quiet_nu) -> None:
    from src.research.predictions import create_prediction, resolve_prediction

    rep = calibration_report(session)
    assert not rep.sufficient and "Insufficient sample" in rep.note
    for i in range(10):
        p = create_prediction(session, f"p{i}", 70, investment=quiet_nu)
        resolve_prediction(session, p, "RESOLVED_TRUE" if i < 7 else "RESOLVED_FALSE")
    session.commit()
    rep2 = calibration_report(session)
    assert rep2.sufficient and rep2.resolved == 10
    assert rep2.hit_rate == 0.7
    # all predictions at 70%, 70% true -> perfectly calibrated bucket; Brier = 0.7*0.09+0.3*0.49
    assert abs(rep2.brier_score - (0.7 * 0.09 + 0.3 * 0.49)) < 1e-6
    bucket = rep2.buckets[0]
    assert bucket["avg_stated_probability"] == 70.0 and bucket["observed_frequency_pct"] == 70.0


def test_macro_regime_unknown_without_data(session) -> None:
    dims = {d.name: d for d in macro_regime(session)}
    assert set(dims) == {"Growth", "Inflation", "Rates", "Liquidity", "Credit", "Labor", "USD"}
    assert all(d.state == "UNKNOWN" for d in dims.values())  # no data -> UNKNOWN, never guessed
    assert all(d.rule for d in dims.values())  # documented rule always present


def test_feedback_stored(session, settings, quiet_nu) -> None:
    session.add(BriefFeedback(item_key="risk:1:new", rating="TOO_NOISY", note="known risk"))
    session.commit()
    fb = session.scalars(select(BriefFeedback)).one()
    assert fb.rating == "TOO_NOISY" and fb.created_at is not None


def test_management_claims_require_source(session, quiet_nu) -> None:
    from src.intelligence.claims import add_claim, list_claims, set_claim_status
    from src.research.investments import ResearchError

    with pytest.raises(ResearchError, match="requires a source"):
        add_claim(session, quiet_nu, "We expect NPLs to normalize")
    c = add_claim(
        session, quiet_nu, "We expect Mexico deposits to double by 2027", speaker="CEO",
        claim_date=date(2026, 8, 14), topic="Mexico", time_horizon="2027",
        source_reference="Q2 2026 earnings call, prepared remarks",
    )
    assert c.status == "OPEN"
    set_claim_status(session, c, "FULFILLED", outcome_note="Deposits doubled in Q3 2027")
    assert list_claims(session, quiet_nu)[0].status == "FULFILLED"
