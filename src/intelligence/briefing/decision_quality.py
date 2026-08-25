"""Decision-quality mentor: process over outcome.

Deterministic part: completeness/discipline stats over the decision journal (were risks,
falsifiers and alternatives recorded? was confidence stated?). AI part (optional): the
mentor evaluates reasoning consistency, luck vs skill, ignored risks, story drift and
sizing - explicitly instructed NOT to judge by P/L alone, not to criticize losses or praise
wins automatically. Output is text + optional proposals; never a mutation.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

from pydantic import BaseModel, ConfigDict, ValidationError

from src.db.research import Investment
from src.intelligence.ai.provider import AIInvalidOutput, AIProvider
from src.research.decisions import decision_snapshot, list_decisions

DECISION_PROMPT_VERSION = "decision-quality-1.0"


@dataclass
class JournalDiscipline:
    decisions: int
    with_reasoning: int = 0
    with_falsifier: int = 0  # what_would_make_this_wrong filled
    with_alternatives: int = 0
    with_confidence: int = 0
    with_key_risks: int = 0
    notes: list[str] = field(default_factory=list)


def journal_discipline(session, investment: Investment) -> JournalDiscipline:
    decisions = list_decisions(session, investment)
    d = JournalDiscipline(decisions=len(decisions))
    for dec in decisions:
        d.with_reasoning += bool(dec.reasoning)
        d.with_falsifier += bool(dec.what_would_make_this_wrong)
        d.with_alternatives += bool(dec.alternative_considered)
        d.with_confidence += dec.confidence is not None
        d.with_key_risks += bool(dec.key_risks)
    if d.decisions and d.with_falsifier < d.decisions:
        d.notes.append(
            f"{d.decisions - d.with_falsifier} decision(s) lack 'what would prove us wrong' - "
            "the strongest guard against story drift."
        )
    if d.decisions and d.with_alternatives < d.decisions:
        d.notes.append(f"{d.decisions - d.with_alternatives} decision(s) did not record alternatives considered.")
    return d


class DecisionQualityReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    overall_observations: str
    reasoning_consistency: str
    luck_vs_skill: str
    ignored_risks: str
    confidence_justified: str
    followed_original_thesis: str
    story_drift: str
    sizing_vs_uncertainty: str
    questions_for_the_investor: list[str] = []


def run_decision_review(session, provider: AIProvider, investment: Investment) -> DecisionQualityReview:
    decisions = list_decisions(session, investment)
    if not decisions:
        raise AIInvalidOutput(f"{investment.ticker} has no decisions to review")
    payload = []
    for d in decisions:
        snap = decision_snapshot(d)
        payload.append({
            "type": d.decision_type, "date": d.decided_at.date().isoformat(),
            "price_at_decision": d.instrument_price, "confidence": d.confidence,
            "reasoning": d.reasoning, "expected_outcome": d.expected_outcome,
            "what_would_make_this_wrong": d.what_would_make_this_wrong,
            "alternatives_considered": d.alternative_considered, "key_risks": d.key_risks,
            "thesis_version_at_decision": (snap.get("research") or {}).get("thesis_version"),
        })
    from src.research.theses import active_thesis, current_version

    t = active_thesis(session, investment)
    v = current_version(session, t) if t else None
    system = (
        "You are a decision-quality mentor for a long-term investor. Evaluate the PROCESS of "
        "these journal entries, not the outcome.\nRules:\n"
        "- NEVER judge a decision solely by profit or loss; do not criticize losing decisions "
        "or praise profitable ones automatically.\n"
        "- Assess: internal consistency of reasoning; whether outcomes would owe to luck; "
        "whether known risks were ignored; whether stated confidence was justified by "
        "evidence; whether decisions followed the original thesis; signs of changing the "
        "story after the fact; whether sizing matched uncertainty.\n"
        "- Use ONLY the provided journal and thesis; unknown outcomes are UNKNOWN.\n"
        "- Calm, precise, no trading advice.\n"
        "Respond with JSON keys: overall_observations, reasoning_consistency, luck_vs_skill, "
        "ignored_risks, confidence_justified, followed_original_thesis, story_drift, "
        "sizing_vs_uncertainty, questions_for_the_investor (list of strings)."
    )
    user = json.dumps({
        "ticker": investment.ticker,
        "current_thesis_one_line": (v.core_thesis or "")[:300] if v else None,
        "decisions": payload,
    }, indent=1)
    raw = provider.complete_json(system, user, max_tokens=1200)
    try:
        return DecisionQualityReview.model_validate(raw)
    except ValidationError as exc:
        raise AIInvalidOutput(f"decision review failed schema validation: {exc.errors()[:3]}") from exc
