"""AI analysis agents: contradiction-first event analysis, red team, earnings review.

Every agent: builds a context packet (thesis-aware), sends a versioned prompt, validates the
structured JSON answer (pydantic), and persists ONLY ai_proposals rows. Epistemics are
enforced in the prompt: SOURCE FACT / CALCULATION / INTERPRETATION / HYPOTHESIS / UNKNOWN,
and missing data must be answered UNKNOWN - never filled from pretrained memory.
"""
from __future__ import annotations

import json
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy.orm import Session

from src.db.intelligence import AiProposal, IntelligenceEvent
from src.db.research import Investment
from src.intelligence.ai.context import build_context_packet
from src.intelligence.ai.provider import AIInvalidOutput, AIProvider
from src.intelligence.ai.proposals import create_proposal
from src.intelligence.events import payload_of
from src.logging_setup import get_logger
from src.research.investments import ResearchError

log = get_logger("intelligence.ai.analysis")

PROMPT_VERSIONS = {
    "event_analysis": "event-analysis-1.1",
    "red_team": "red-team-1.0",
    "earnings_review": "earnings-review-1.0",
}

EPISTEMIC_RULES = """Epistemic rules (mandatory):
- Label every claim internally as SOURCE FACT (traceable to the provided context/event),
  CALCULATION (arithmetic on provided numbers), INTERPRETATION, HYPOTHESIS, or UNKNOWN.
- If information is not in the provided context or event, answer UNKNOWN. Do NOT use your
  pretrained knowledge about the company as if it were current sourced fact.
- You support decisions; you never make them. No BUY/SELL recommendations.
- Confidence values are 0-100 integers."""

CONTRADICTION_QUESTIONS = """Answer ALL of these explicitly (contradiction-first, fight confirmation bias):
1. what_supports_thesis (string)
2. what_contradicts_thesis (string)
3. assumptions_strengthened (list of assumption ids/names)
4. assumptions_weakened (list of assumption ids/names)
5. breakers_closer (JSON boolean true/false: did any thesis breaker move closer to triggering?)
   + breakers_closer_explanation (string or null: which breaker and why)
6. skeptical_view (string: what would a skeptical investor emphasize?)
7. missing_information (string)
8. valuation_vs_quality (string: has valuation changed independently of business quality?)
9. genuinely_new (JSON boolean true/false: is this new information, not already known?)
   + genuinely_new_explanation (string or null)
10. monitor_next (string)
Type discipline: breakers_closer and genuinely_new MUST be JSON booleans (true/false without
quotes); the *_explanation fields carry any nuance as strings."""


class ProposalDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    proposal_type: str
    title: str
    why_it_matters: Optional[str] = None
    proposed_change: Optional[dict] = None
    reasoning: Optional[str] = None
    confidence: Optional[int] = Field(None, ge=0, le=100)


def _strict_flag(field: str):
    """Narrow coercion for yes/no facts: JSON booleans, plus the unambiguous legacy strings
    'yes'/'no'/'true'/'false' (older prompt versions asked free-form). Anything else - e.g.
    'maybe' or a sentence - fails validation; nuance belongs in the *_explanation field."""

    def _coerce(v):
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            low = v.strip().lower()
            if low in ("yes", "true"):
                return True
            if low in ("no", "false"):
                return False
        raise ValueError(
            f"{field} must be a JSON boolean (true/false); put nuance into {field}_explanation"
        )

    return _coerce


class EventAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    what_supports_thesis: str
    what_contradicts_thesis: str
    assumptions_strengthened: list[str] = Field(default_factory=list)
    assumptions_weakened: list[str] = Field(default_factory=list)
    breakers_closer: bool
    breakers_closer_explanation: Optional[str] = None
    skeptical_view: str
    missing_information: str
    valuation_vs_quality: str
    genuinely_new: bool
    genuinely_new_explanation: Optional[str] = None
    monitor_next: str
    proposals: list[ProposalDraft] = Field(default_factory=list)

    _v_breakers = field_validator("breakers_closer", mode="before")(_strict_flag("breakers_closer"))
    _v_new = field_validator("genuinely_new", mode="before")(_strict_flag("genuinely_new"))


def _persist_drafts(
    session: Session,
    drafts: list[ProposalDraft],
    investment: Investment | None,
    event_id: int | None,
    provider: AIProvider,
    prompt_version: str,
    context_hash: str,
    analysis_summary: dict | None = None,
) -> list[AiProposal]:
    out = []
    for d in drafts:
        try:
            out.append(
                create_proposal(
                    session,
                    d.proposal_type,
                    d.title,
                    investment=investment,
                    event_id=event_id,
                    why_it_matters=d.why_it_matters,
                    what_happened=json.dumps(analysis_summary, default=str)[:2000] if analysis_summary else None,
                    proposed_change=d.proposed_change,
                    reasoning=d.reasoning,
                    confidence=d.confidence,
                    provider=provider.name,
                    model=provider.model,
                    prompt_version=prompt_version,
                    context_hash=context_hash,
                )
            )
        except ResearchError as exc:
            log.warning("dropping invalid AI proposal draft %r: %s", d.title[:60], exc)
    return out


def analyze_event(
    session: Session, provider: AIProvider, event: IntelligenceEvent, investment: Investment
) -> tuple[EventAnalysis, list[AiProposal]]:
    """Contradiction-first analysis of one intelligence event against the current thesis."""
    packet = build_context_packet(session, investment)
    system = (
        "You are the analysis engine of Investor OS, a local decision-support system.\n"
        + EPISTEMIC_RULES
        + "\nYou receive the investor's current research context and ONE new event. "
        "Your question is strictly: what does this NEW information change?\n"
        + CONTRADICTION_QUESTIONS
        + "\nAlso emit 'proposals': a list of {proposal_type, title, why_it_matters, proposed_change, "
        "reasoning, confidence} objects. Allowed proposal_type values: NEW_EVIDENCE, "
        "ASSUMPTION_STATUS_CHANGE, NEW_RISK, NEW_CATALYST, BREAKER_WARNING, THESIS_REVISION, "
        "RED_TEAM_ARGUMENT, NEW_PREDICTION, VALUATION_QUESTION, RESEARCH_QUESTION. "
        "Emit proposals only when genuinely warranted; an empty list is a good answer. "
        "Proposals are suggestions for HUMAN review - nothing is applied automatically."
    )
    user = (
        f"CURRENT RESEARCH CONTEXT:\n{packet.to_prompt()}\n\n"
        f"NEW EVENT:\ntype={event.event_type} severity={event.severity} occurred={event.occurred_at}\n"
        f"title={event.title}\nsummary={event.summary}\npayload={json.dumps(payload_of(event), default=str)}"
    )
    raw = provider.complete_json(system, user)
    try:
        analysis = EventAnalysis.model_validate(raw)
    except ValidationError as exc:
        raise AIInvalidOutput(f"event analysis failed schema validation: {exc.errors()[:3]}") from exc
    proposals = _persist_drafts(
        session, analysis.proposals, investment, event.id, provider,
        PROMPT_VERSIONS["event_analysis"], packet.context_hash,
        analysis_summary={"contradicting": analysis.what_contradicts_thesis[:400],
                          "supporting": analysis.what_supports_thesis[:400]},
    )
    event.ai_state = "DONE"
    session.flush()
    return analysis, proposals


class RedTeamOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strongest_bear_case: str
    fragile_assumptions: list[str] = Field(default_factory=list)
    hidden_dependencies: str
    base_rate_concerns: str
    management_incentives: str
    accounting_concerns: str
    competitive_threats: str
    regulatory_threats: str
    macro_sensitivity: str
    valuation_risk: str
    underweighted_evidence: str
    arguments: list[ProposalDraft] = Field(default_factory=list)


def run_red_team(
    session: Session, provider: AIProvider, investment: Investment
) -> tuple[RedTeamOutput, list[AiProposal]]:
    """Dedicated adversarial mode: attack the thesis, never predict the stock price."""
    packet = build_context_packet(session, investment)
    system = (
        "You are the RED TEAM of Investor OS. Your only job is to attack this investment "
        "thesis as the strongest honest skeptic. You do NOT predict prices and you do NOT "
        "recommend trades.\n" + EPISTEMIC_RULES +
        "\nProduce JSON with keys: strongest_bear_case, fragile_assumptions (list), "
        "hidden_dependencies, base_rate_concerns, management_incentives, accounting_concerns, "
        "competitive_threats, regulatory_threats, macro_sensitivity, valuation_risk, "
        "underweighted_evidence, arguments. 'arguments' is a list of proposal drafts "
        "{proposal_type: RED_TEAM_ARGUMENT, title, why_it_matters, proposed_change: "
        "{argument, severity}, reasoning, confidence} - one per distinct bear argument."
    )
    raw = provider.complete_json(system, f"RESEARCH CONTEXT:\n{packet.to_prompt()}")
    try:
        output = RedTeamOutput.model_validate(raw)
    except ValidationError as exc:
        raise AIInvalidOutput(f"red team output failed schema validation: {exc.errors()[:3]}") from exc
    proposals = _persist_drafts(
        session, output.arguments, investment, None, provider,
        PROMPT_VERSIONS["red_team"], packet.context_hash,
    )
    return output, proposals


class KpiRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kpi: str
    previous: Optional[str] = None
    current: Optional[str] = None
    change: Optional[str] = None
    thesis_relevance: Optional[str] = None


class EarningsReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    executive_summary: str
    most_important_changes: list[str] = Field(default_factory=list)  # top 5
    kpi_table: list[KpiRow] = Field(default_factory=list)
    thesis_verdict: str  # strengthened | unchanged | weakened | potentially_broken
    assumption_analysis: str
    breaker_analysis: str
    risks: str
    catalysts: str
    management_commentary: str
    valuation_implications: str
    questions_for_next_quarter: list[str] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)  # references to stored source ids/titles
    proposals: list[ProposalDraft] = Field(default_factory=list)


def run_earnings_review(
    session: Session, provider: AIProvider, investment: Investment, event: IntelligenceEvent | None = None
) -> tuple[EarningsReview, list[AiProposal]]:
    packet = build_context_packet(session, investment)
    event_block = ""
    if event is not None:
        event_block = (
            f"\nEARNINGS EVENT:\ntitle={event.title}\nsummary={event.summary}\n"
            f"payload={json.dumps(payload_of(event), default=str)}"
        )
    system = (
        "You are the EARNINGS REVIEW agent of Investor OS.\n" + EPISTEMIC_RULES +
        "\nCompare the new results against the previous KPI observations and the thesis "
        "expectations in the context. Numbers must come from the context/event; use UNKNOWN "
        "where data is missing - never from memory. Cite the stored sources you used in "
        "'citations' (source titles or ids from the context/event).\n"
        "Produce JSON with keys: executive_summary, most_important_changes (max 5), kpi_table "
        "(list of {kpi, previous, current, change, thesis_relevance}), thesis_verdict "
        "(strengthened|unchanged|weakened|potentially_broken), assumption_analysis, "
        "breaker_analysis, risks, catalysts, management_commentary, valuation_implications, "
        "questions_for_next_quarter, citations, proposals."
    )
    raw = provider.complete_json(system, f"RESEARCH CONTEXT:\n{packet.to_prompt()}{event_block}")
    try:
        review = EarningsReview.model_validate(raw)
    except ValidationError as exc:
        raise AIInvalidOutput(f"earnings review failed schema validation: {exc.errors()[:3]}") from exc
    proposals = _persist_drafts(
        session, review.proposals, investment, event.id if event else None, provider,
        PROMPT_VERSIONS["earnings_review"], packet.context_hash,
        analysis_summary={"verdict": review.thesis_verdict},
    )
    if event is not None:
        event.ai_state = "DONE"
        session.flush()
    return review, proposals
