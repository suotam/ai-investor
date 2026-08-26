"""Phase D tests: AI provider, structured outputs, proposal workflow, safety boundaries."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import func, select

from src.config import Settings
from src.db.intelligence import AiProposal
from src.db.research import ThesisVersion
from src.intelligence.ai.analysis import analyze_event, run_earnings_review, run_red_team
from src.intelligence.ai.context import build_context_packet
from src.intelligence.ai.provider import (
    AIInvalidOutput,
    AIUnavailable,
    OpenAICompatProvider,
    extract_json,
    get_ai_provider,
)
from src.intelligence.ai.proposals import (
    accept_proposal,
    create_proposal,
    defer_proposal,
    list_proposals,
    reject_proposal,
)
from src.intelligence.events import record_event
from src.research.assumptions import add_assumption
from src.research.investments import ResearchError, create_investment
from src.research.theses import create_thesis, current_version, version_history


def ai_settings(**kw) -> Settings:
    s = Settings(ai_enabled=True, ai_provider="llama_cpp", ai_model="test-model")
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def fake_provider(reply: str | Exception) -> OpenAICompatProvider:
    def post_fn(url, payload, timeout):
        if isinstance(reply, Exception):
            raise reply
        return {"choices": [{"message": {"content": reply}}], "model": "test-model"}

    return OpenAICompatProvider(ai_settings(), post_fn=post_fn)


# ---------------------------------------------------------------- provider


def test_ai_disabled_and_unavailable() -> None:
    with pytest.raises(AIUnavailable, match="disabled"):
        get_ai_provider(Settings(ai_enabled=False))
    p = fake_provider(AIUnavailable("server down"))
    with pytest.raises(AIUnavailable):
        p.complete("s", "u")
    assert p.health()["available"] is False


def test_extract_json_variants() -> None:
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('Sure! ```json\n{"a": 1}\n``` hope that helps') == {"a": 1}
    assert extract_json('preamble {"a": {"b": 2}} trailing') == {"a": {"b": 2}}
    with pytest.raises(AIInvalidOutput):
        extract_json("no json here")
    with pytest.raises(AIInvalidOutput):
        extract_json("{broken json]")
    with pytest.raises(AIInvalidOutput):
        extract_json("[1, 2]")


# ---------------------------------------------------------------- proposals


def test_proposal_lifecycle_reject_defer(session) -> None:
    inv = create_investment(session, "NU")
    p = create_proposal(
        session, "NEW_RISK", "FX risk emerging", investment=inv,
        proposed_change={"name": "BRL depreciation", "category": "currency", "severity": "HIGH"},
        provider="llama_cpp", model="m", prompt_version="v1",
    )
    assert p.status == "PENDING" and p.created_by == "AI"
    defer_proposal(session, p)
    assert p.status == "DEFERRED"
    reject_proposal(session, p, note="not new")
    assert p.status == "REJECTED" and p.resolved_at is not None
    with pytest.raises(ResearchError, match="already resolved"):
        accept_proposal(session, p)
    # rejection created NO research rows
    from src.db.research import Risk

    assert session.scalar(select(func.count(Risk.id))) == 0


def test_proposal_payload_validated_at_creation(session) -> None:
    inv = create_investment(session, "NU")
    with pytest.raises(ResearchError, match="invalid NEW_RISK payload"):
        create_proposal(session, "NEW_RISK", "bad", investment=inv, proposed_change={"nonsense": 1})
    with pytest.raises(ResearchError, match="requires a proposed_change"):
        create_proposal(session, "NEW_PREDICTION", "no payload", investment=inv)
    with pytest.raises(ResearchError, match="invalid proposal_type"):
        create_proposal(session, "AUTO_TRADE", "x", investment=inv)


def test_accept_new_risk_and_prediction(session) -> None:
    inv = create_investment(session, "NU")
    p = create_proposal(
        session, "NEW_RISK", "FX risk", investment=inv,
        proposed_change={"name": "BRL depreciation", "category": "currency", "severity": "HIGH"},
    )
    res = accept_proposal(session, p)
    assert p.status == "ACCEPTED" and "risk #" in res["action"]
    from src.db.research import Risk

    r = session.scalars(select(Risk)).one()
    assert r.created_by == "AI" and r.severity == "HIGH"

    p2 = create_proposal(
        session, "NEW_PREDICTION", "NPL prediction", investment=inv,
        proposed_change={"statement": "NPL < 6% next quarter", "probability": 70,
                         "resolution_date": "2026-12-31"},
    )
    accept_proposal(session, p2, edited_payload={"statement": "NPL < 6.5% next quarter", "probability": 75})
    assert p2.status == "EDITED"
    from src.db.research import Prediction

    pred = session.scalars(select(Prediction)).one()
    assert pred.probability == 75 and "6.5%" in pred.statement


def test_accept_thesis_revision_creates_new_immutable_version(session) -> None:
    """CRITICAL: AI thesis change goes PROPOSAL -> human accept (with reason) -> NEW version."""
    inv = create_investment(session, "NU")
    thesis, v1 = create_thesis(session, inv, "NU thesis", core_thesis="Growth > 30%", confidence=75)
    p = create_proposal(
        session, "THESIS_REVISION", "Lower growth expectation", investment=inv,
        proposed_change={"core_thesis": "Growth > 20%", "confidence": 60},
    )
    # creating the proposal did NOT touch the thesis
    assert current_version(session, thesis).id == v1.id
    assert session.scalar(select(func.count(ThesisVersion.id))) == 1
    # acceptance requires an explicit human reason
    with pytest.raises(ResearchError, match="reason_for_revision"):
        accept_proposal(session, p)
    assert p.status == "PENDING"  # failed acceptance left everything untouched
    res = accept_proposal(session, p, reason_for_revision="Q2 growth decelerated; accepting AI proposal")
    assert "v2" in res["action"]
    v2 = current_version(session, thesis)
    assert v2.version_number == 2 and v2.created_by == "AI" and v2.confidence == 60
    # v1 unchanged (immutability preserved through the AI path)
    v1_db = session.get(ThesisVersion, v1.id)
    assert v1_db.core_thesis == "Growth > 30%" and v1_db.confidence == 75
    assert [v.version_number for v in version_history(session, thesis)] == [1, 2]


def test_accept_assumption_status_change(session) -> None:
    inv = create_investment(session, "NU")
    thesis, _ = create_thesis(session, inv, "T")
    a = add_assumption(session, thesis, "ROE > 25%", status="SUPPORTED")
    p = create_proposal(
        session, "ASSUMPTION_STATUS_CHANGE", "ROE weakening", investment=inv,
        proposed_change={"assumption_id": a.id, "new_status": "WEAKENING", "note": "Q2 ROE 24%"},
    )
    accept_proposal(session, p)
    assert a.status == "WEAKENING" and "proposal" in a.notes or "Q2 ROE" in a.notes


def test_informational_proposal_accept_no_mutation(session) -> None:
    inv = create_investment(session, "NU")
    p = create_proposal(session, "BREAKER_WARNING", "NPL nearing breaker", investment=inv)
    res = accept_proposal(session, p)
    assert "informational" in res["action"]
    from src.db.research import Risk, ThesisAssumption

    assert session.scalar(select(func.count(Risk.id))) == 0
    assert session.scalar(select(func.count(ThesisAssumption.id))) == 0


# ---------------------------------------------------------------- context packet


def test_context_packet_time_awareness_no_hindsight(session) -> None:
    inv = create_investment(session, "NU")
    thesis, v1 = create_thesis(session, inv, "T", core_thesis="Growth > 30%")
    add_assumption(session, thesis, "Old assumption")
    session.commit()
    t_between = v1.created_at + timedelta(hours=1)
    # later: revision + new assumption (timestamps set explicitly for determinism)
    from src.research.theses import revise_thesis

    v2 = revise_thesis(session, thesis, "update", core_thesis="Growth > 20%")
    v2.created_at = v1.created_at + timedelta(hours=2)
    a2 = add_assumption(session, thesis, "New assumption")
    a2.created_at = v2.created_at
    session.commit()

    now_packet = build_context_packet(session, inv)
    assert now_packet.data["thesis"]["version"] == 2
    assert len(now_packet.data["assumptions"]) == 2

    historical = build_context_packet(session, inv, as_of=t_between)
    assert historical.data["thesis"]["version"] == 1  # what we believed THEN
    assert historical.data["thesis"]["core_thesis"] == "Growth > 30%"
    assert [a["name"] for a in historical.data["assumptions"]] == ["Old assumption"]
    assert historical.context_hash != now_packet.context_hash


def test_context_packet_unknown_not_fabricated(session) -> None:
    inv = create_investment(session, "NU")
    from src.research.kpis import add_kpi

    add_kpi(session, inv, "NPL 90+", unit="%")
    packet = build_context_packet(session, inv)
    assert packet.data["kpis"][0]["recent_observations"] == "UNKNOWN (no stored observations)"
    assert "UNKNOWN" in packet.data["note"]


# ---------------------------------------------------------------- analysis agents


VALID_ANALYSIS = json.dumps(
    {
        "what_supports_thesis": "Revenue grew (SOURCE FACT from context).",
        "what_contradicts_thesis": "NPL ticked up.",
        "assumptions_strengthened": ["Growth"],
        "assumptions_weakened": ["Credit quality"],
        "breakers_closer": True,
        "breakers_closer_explanation": "NPL breaker slightly closer.",
        "skeptical_view": "Credit cycle turning.",
        "missing_information": "Cohort-level NPL data: UNKNOWN.",
        "valuation_vs_quality": "Multiple unchanged.",
        "genuinely_new": True,
        "genuinely_new_explanation": "New quarterly data.",
        "monitor_next": "Q3 NPL.",
        "proposals": [
            {"proposal_type": "NEW_RISK", "title": "Credit deterioration risk",
             "proposed_change": {"name": "Credit deterioration", "category": "financial", "severity": "HIGH"},
             "reasoning": "NPL up 2 quarters", "confidence": 60},
            {"proposal_type": "AUTO_TRADE", "title": "invalid type gets dropped",
             "proposed_change": {}, "confidence": 90},
        ],
    }
)


def test_analyze_event_structured_and_safe(session) -> None:
    inv = create_investment(session, "NU")
    create_thesis(session, inv, "T", core_thesis="Growth > 30%")
    event, _ = record_event(session, "EARNINGS_RELEASE", "e1", "Q2 results", datetime(2026, 8, 14),
                            investment_id=inv.id)
    provider = fake_provider(VALID_ANALYSIS)
    analysis, proposals = analyze_event(session, provider, event, inv)
    session.commit()
    assert "NPL" in analysis.what_contradicts_thesis  # contradiction answered explicitly
    # valid draft persisted; invalid type dropped, not stored
    assert len(proposals) == 1
    assert proposals[0].proposal_type == "NEW_RISK" and proposals[0].status == "PENDING"
    assert proposals[0].prompt_version == "event-analysis-1.1"
    assert proposals[0].context_hash and proposals[0].model == "test-model"
    assert event.ai_state == "DONE"
    # analysis created NO research rows - proposals only
    from src.db.research import Risk

    assert session.scalar(select(func.count(Risk.id))) == 0
    assert session.scalar(select(func.count(AiProposal.id))) == 1


def test_analyze_event_invalid_json_fails_cleanly(session) -> None:
    inv = create_investment(session, "NU")
    event, _ = record_event(session, "NEWS_EVENT", "e2", "story", datetime(2026, 8, 14), investment_id=inv.id)
    with pytest.raises(AIInvalidOutput):
        analyze_event(session, fake_provider("I think the stock looks great!"), event, inv)
    with pytest.raises(AIInvalidOutput, match="schema validation"):
        analyze_event(session, fake_provider('{"wrong": "shape"}'), event, inv)
    assert session.scalar(select(func.count(AiProposal.id))) == 0  # nothing persisted


def test_red_team_agent(session) -> None:
    inv = create_investment(session, "NU")
    create_thesis(session, inv, "T")
    reply = json.dumps(
        {
            "strongest_bear_case": "Credit cycle + competition.",
            "fragile_assumptions": ["ROE persistence"],
            "hidden_dependencies": "Funding costs.",
            "base_rate_concerns": "EM banks rarely sustain 25% ROE.",
            "management_incentives": "SBC heavy.",
            "accounting_concerns": "UNKNOWN - no filings in context.",
            "competitive_threats": "Incumbents copying.",
            "regulatory_threats": "Brazil caps.",
            "macro_sensitivity": "Selic and BRL.",
            "valuation_risk": "High multiple.",
            "underweighted_evidence": "NPL uptick.",
            "arguments": [
                {"proposal_type": "RED_TEAM_ARGUMENT", "title": "Base-rate argument",
                 "proposed_change": {"argument": "EM banks rarely sustain >25% ROE for a decade",
                                     "severity": "MEDIUM"}, "confidence": 55}
            ],
        }
    )
    output, proposals = run_red_team(session, fake_provider(reply), inv)
    assert "UNKNOWN" in output.accounting_concerns  # missing data stays unknown
    assert len(proposals) == 1 and proposals[0].prompt_version == "red-team-1.0"
    # red team output is a proposal - nothing written to red_team_entries until accepted
    from src.db.research import RedTeamEntry

    assert session.scalar(select(func.count(RedTeamEntry.id))) == 0
    accept_proposal(session, proposals[0])
    assert session.scalar(select(func.count(RedTeamEntry.id))) == 1


def test_earnings_review_agent(session) -> None:
    inv = create_investment(session, "NU")
    create_thesis(session, inv, "T")
    event, _ = record_event(session, "EARNINGS_RELEASE", "e3", "Q2 6-K", datetime(2026, 8, 14),
                            investment_id=inv.id)
    reply = json.dumps(
        {
            "executive_summary": "Solid quarter; credit worth watching.",
            "most_important_changes": ["Revenue +32%", "NPL 5.4%"],
            "kpi_table": [{"kpi": "NPL 90+", "previous": "5.1", "current": "5.4", "change": "+0.3pp",
                           "thesis_relevance": "credit quality assumption"}],
            "thesis_verdict": "unchanged",
            "assumption_analysis": "Credit assumption slightly weaker.",
            "breaker_analysis": "NPL breaker at 7% not close.",
            "risks": "Credit cycle.",
            "catalysts": "Mexico license.",
            "management_commentary": "UNKNOWN - transcript not stored.",
            "valuation_implications": "None material.",
            "questions_for_next_quarter": ["NPL trajectory?"],
            "citations": ["source: Q2 6-K event"],
            "proposals": [],
        }
    )
    review, proposals = run_earnings_review(session, fake_provider(reply), inv, event)
    assert review.thesis_verdict == "unchanged" and review.citations
    assert proposals == [] and event.ai_state == "DONE"


# ---------------------------------------------------------------- boolean flags regression


def _analysis_payload(**overrides) -> dict:
    base = {
        "what_supports_thesis": "s",
        "what_contradicts_thesis": "c",
        "breakers_closer": False,
        "skeptical_view": "sk",
        "missing_information": "m",
        "valuation_vs_quality": "v",
        "genuinely_new": False,
        "monitor_next": "n",
    }
    base.update(overrides)
    return base


def test_event_analysis_accepts_boolean_flags(session) -> None:
    """Regression: local model returns JSON booleans (the real Glimmer output that failed)."""
    inv = create_investment(session, "NU")
    create_thesis(session, inv, "T")
    event, _ = record_event(session, "NEWS_EVENT", "eb1", "story", datetime(2026, 8, 24), investment_id=inv.id)
    reply = json.dumps(_analysis_payload(breakers_closer=False, genuinely_new=False))
    analysis, proposals = analyze_event(session, fake_provider(reply), event, inv)
    assert analysis.breakers_closer is False and analysis.genuinely_new is False
    assert analysis.breakers_closer_explanation is None  # optional explanation may be missing
    assert proposals == []


def test_event_analysis_true_flags_with_explanations(session) -> None:
    inv = create_investment(session, "NU")
    create_thesis(session, inv, "T")
    event, _ = record_event(session, "NEWS_EVENT", "eb2", "story", datetime(2026, 8, 24), investment_id=inv.id)
    reply = json.dumps(_analysis_payload(
        breakers_closer=True, breakers_closer_explanation="NPL breaker closer",
        genuinely_new=True, genuinely_new_explanation="first disclosure of Q2 NPL",
    ))
    analysis, _ = analyze_event(session, fake_provider(reply), event, inv)
    assert analysis.breakers_closer is True and "NPL" in analysis.breakers_closer_explanation
    assert analysis.genuinely_new is True


def test_event_analysis_legacy_yes_no_strings_coerced(session) -> None:
    """Backwards compatibility: unambiguous legacy strings still validate."""
    inv = create_investment(session, "NU")
    create_thesis(session, inv, "T")
    event, _ = record_event(session, "NEWS_EVENT", "eb3", "story", datetime(2026, 8, 24), investment_id=inv.id)
    reply = json.dumps(_analysis_payload(breakers_closer="No", genuinely_new="YES"))
    analysis, _ = analyze_event(session, fake_provider(reply), event, inv)
    assert analysis.breakers_closer is False and analysis.genuinely_new is True


def test_event_analysis_malformed_flag_rejected_no_db_writes(session) -> None:
    """'maybe' (or a sentence) is NOT a boolean -> clean AIInvalidOutput, nothing persisted."""
    inv = create_investment(session, "NU")
    create_thesis(session, inv, "T")
    event, _ = record_event(session, "NEWS_EVENT", "eb4", "story", datetime(2026, 8, 24), investment_id=inv.id)
    for bad in ("maybe", "NPL breaker slightly closer.", 1, None):
        reply = json.dumps(_analysis_payload(breakers_closer=bad))
        with pytest.raises(AIInvalidOutput, match="schema validation"):
            analyze_event(session, fake_provider(reply), event, inv)
    assert session.scalar(select(func.count(AiProposal.id))) == 0
    assert event.ai_state == "NONE"  # failed analysis never marks the event analyzed


# ------------------------------------------------- reasoning-model response handling (v5 fix)


def reasoning_provider(content, reasoning="thinking...", finish_reason="stop",
                       prompt_tokens=340, completion_tokens=1313) -> OpenAICompatProvider:
    def post_fn(url, payload, timeout):
        return {
            "choices": [{
                "finish_reason": finish_reason,
                "message": {"role": "assistant", "content": content, "reasoning_content": reasoning},
            }],
            "model": "glimmer-test",
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
        }

    return OpenAICompatProvider(ai_settings(), post_fn=post_fn)


def test_reasoning_response_content_parsed_not_reasoning() -> None:
    """A: reasoning_content non-empty + valid JSON content -> succeeds from CONTENT only."""
    p = reasoning_provider('{"a": 1}', reasoning='{"a": 999} <- must never be used')
    resp = p.complete("s", "u")
    assert resp.text == '{"a": 1}' and resp.reasoning_chars > 0
    assert resp.finish_reason == "stop" and resp.completion_tokens == 1313
    assert p.complete_json("s", "u") == {"a": 1}  # reasoning JSON never substituted


def test_reasoning_exhaustion_clean_diagnostic() -> None:
    """B: budget consumed by reasoning -> empty content, finish_reason=length -> clear error."""
    p = reasoning_provider("", reasoning="x" * 3476, finish_reason="length", completion_tokens=900)
    with pytest.raises(AIInvalidOutput) as exc:
        p.complete_json("s", "u", max_tokens=900)
    msg = str(exc.value)
    assert "no final content" in msg
    assert "finish_reason=length" in msg and "reasoning_chars=3476" in msg
    assert "exhausted by reasoning" in msg  # actionable diagnosis
    assert "900/900" in msg


def test_empty_content_finish_stop_clean_failure() -> None:
    """C: empty content even with finish_reason=stop -> clean AIInvalidOutput (no crash)."""
    p = reasoning_provider("", reasoning="some thoughts", finish_reason="stop")
    with pytest.raises(AIInvalidOutput, match="no final content"):
        p.complete_json("s", "u")


def test_invalid_final_json_falls_back_without_db_writes(session, settings) -> None:
    """D: garbage content -> mentor falls back deterministically, nothing persisted."""
    from src.intelligence.briefing.generate import generate_brief
    from src.research.items import add_risk

    inv = create_investment(session, "NU", status="OWNED")
    create_thesis(session, inv, "T")
    r = add_risk(session, inv, "R", severity="HIGH", category="macro")
    r.created_at = r.updated_at = datetime(2026, 8, 21, 5, 0)
    session.commit()
    bad = reasoning_provider("here are my thoughts in prose, no json", reasoning="...")
    doc, run, md_path, _ = generate_brief(
        session, settings, "daily", use_ai=True, now=datetime(2026, 8, 21, 6, 0), ai_provider=bad
    )
    session.commit()
    assert doc.ai_synthesis is None
    assert "unavailable" in doc.ai_note.lower()
    assert run is not None and run.ai_used is False  # brief completed deterministically
    assert session.scalar(select(func.count(AiProposal.id))) == 0


def test_event_analysis_handles_reasoning_payload(session) -> None:
    """G: the event-analysis path parses content correctly with reasoning_content present."""
    inv = create_investment(session, "NU")
    create_thesis(session, inv, "T")
    event, _ = record_event(session, "EARNINGS_RELEASE", "rg1", "Q2", datetime(2026, 8, 14),
                            investment_id=inv.id)
    p = reasoning_provider(VALID_ANALYSIS, reasoning="long hidden reasoning " * 50)
    analysis, proposals = analyze_event(session, p, event, inv)
    assert analysis.genuinely_new is True and len(proposals) == 1
