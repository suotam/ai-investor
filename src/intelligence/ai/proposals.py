"""AI proposal persistence and the human review workflow.

The ONLY mutation path from AI to research data:
    proposal (PENDING) -> human ACCEPT -> typed payload validated -> existing v2 service call.
Reject/defer/expire never touch research tables. Thesis revisions additionally REQUIRE an
explicit human reason_for_revision at acceptance time.
"""
from __future__ import annotations

import json
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.core import utcnow
from src.db.intelligence import PROPOSAL_TYPES, AiProposal
from src.db.research import Investment
from src.logging_setup import get_logger
from src.research.investments import ResearchError

log = get_logger("intelligence.ai.proposals")


# --- typed payloads (validated at creation AND at acceptance) -----------------


class _P(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvidencePayload(_P):
    title: str
    direction: str = "NEUTRAL"
    evidence_type: str = "manual"
    summary: Optional[str] = None
    source_name: Optional[str] = None
    source_url: Optional[str] = None
    reliability: Optional[str] = None
    importance: str = "MEDIUM"


class AssumptionStatusPayload(_P):
    assumption_id: int
    new_status: str
    note: Optional[str] = None


class RiskPayload(_P):
    name: str
    description: Optional[str] = None
    category: str = "other"
    severity: str = "MEDIUM"
    probability: Optional[int] = Field(None, ge=0, le=100)
    mitigation: Optional[str] = None


class CatalystPayload(_P):
    name: str
    description: Optional[str] = None
    expected_date: Optional[str] = None
    probability: Optional[int] = Field(None, ge=0, le=100)


class ThesisRevisionPayload(_P):
    core_thesis: Optional[str] = None
    market_expectation: Optional[str] = None
    our_expectation: Optional[str] = None
    why_market_may_be_wrong: Optional[str] = None
    summary: Optional[str] = None
    confidence: Optional[int] = Field(None, ge=0, le=100)
    time_horizon: Optional[str] = None


class RedTeamPayload(_P):
    argument: str
    severity: str = "MEDIUM"
    evidence_reference: Optional[str] = None


class PredictionPayload(_P):
    statement: str
    probability: int = Field(ge=0, le=100)
    resolution_date: Optional[str] = None
    resolution_condition: Optional[str] = None


class KpiObservationPayload(_P):
    kpi_id: int
    period: str
    value: float
    source: str
    source_reference: Optional[str] = None


PAYLOAD_SCHEMAS: dict[str, type[_P] | None] = {
    "NEW_EVIDENCE": EvidencePayload,
    "ASSUMPTION_STATUS_CHANGE": AssumptionStatusPayload,
    "NEW_RISK": RiskPayload,
    "RISK_UPDATE": RiskPayload,
    "NEW_CATALYST": CatalystPayload,
    "THESIS_REVISION": ThesisRevisionPayload,
    "RED_TEAM_ARGUMENT": RedTeamPayload,
    "NEW_PREDICTION": PredictionPayload,
    "KPI_OBSERVATION": KpiObservationPayload,
    "KPI_MAPPING": None,  # informational; acceptance handled by kpi bridge overrides (manual)
    "BREAKER_WARNING": None,  # informational: human checks the breaker in the dashboard
    "VALUATION_QUESTION": None,
    "RESEARCH_QUESTION": None,
}

INFORMATIONAL_TYPES = {k for k, v in PAYLOAD_SCHEMAS.items() if v is None}


def create_proposal(
    session: Session,
    proposal_type: str,
    title: str,
    investment: Investment | None = None,
    event_id: int | None = None,
    what_happened: str | None = None,
    why_it_matters: str | None = None,
    proposed_change: dict | None = None,
    reasoning: str | None = None,
    supporting_refs: list | None = None,
    contradicting_refs: list | None = None,
    confidence: int | None = None,
    provider: str | None = None,
    model: str | None = None,
    prompt_version: str | None = None,
    context_hash: str | None = None,
    created_by: str = "AI",
) -> AiProposal:
    if proposal_type not in PROPOSAL_TYPES:
        raise ResearchError(f"invalid proposal_type {proposal_type!r}; allowed: {PROPOSAL_TYPES}")
    if confidence is not None and not (0 <= int(confidence) <= 100):
        raise ResearchError("confidence must be 0-100")
    schema = PAYLOAD_SCHEMAS.get(proposal_type)
    if schema is not None:
        if proposed_change is None:
            raise ResearchError(f"{proposal_type} requires a proposed_change payload")
        try:
            proposed_change = schema.model_validate(proposed_change).model_dump()
        except ValidationError as exc:
            raise ResearchError(f"invalid {proposal_type} payload: {exc.errors()[0]['msg']}") from exc
    p = AiProposal(
        proposal_type=proposal_type,
        investment_id=investment.id if investment else None,
        event_id=event_id,
        title=title[:300],
        what_happened=what_happened,
        why_it_matters=why_it_matters,
        proposed_change_json=json.dumps(proposed_change) if proposed_change is not None else None,
        reasoning=reasoning,
        supporting_refs=json.dumps(supporting_refs) if supporting_refs else None,
        contradicting_refs=json.dumps(contradicting_refs) if contradicting_refs else None,
        confidence=confidence,
        provider=provider,
        model=model,
        prompt_version=prompt_version,
        context_hash=context_hash,
        created_by=created_by,
    )
    session.add(p)
    session.flush()
    return p


def list_proposals(
    session: Session, status: str | None = "PENDING", investment_id: int | None = None
) -> list[AiProposal]:
    stmt = select(AiProposal).order_by(AiProposal.created_at.desc(), AiProposal.id.desc())
    if status:
        stmt = stmt.where(AiProposal.status == status)
    if investment_id is not None:
        stmt = stmt.where(AiProposal.investment_id == investment_id)
    return list(session.scalars(stmt))


def reject_proposal(session: Session, proposal: AiProposal, note: str | None = None) -> AiProposal:
    return _resolve(session, proposal, "REJECTED", note)


def defer_proposal(session: Session, proposal: AiProposal, note: str | None = None) -> AiProposal:
    if proposal.status != "PENDING":
        raise ResearchError(f"proposal {proposal.id} is {proposal.status}, not PENDING")
    proposal.status = "DEFERRED"
    proposal.resolution_note = note
    session.flush()
    return proposal


def _resolve(session: Session, proposal: AiProposal, status: str, note: str | None) -> AiProposal:
    if proposal.status not in ("PENDING", "DEFERRED"):
        raise ResearchError(f"proposal {proposal.id} already resolved ({proposal.status})")
    proposal.status = status
    proposal.resolution_note = note
    proposal.resolved_at = utcnow()
    session.flush()
    return proposal


def accept_proposal(
    session: Session,
    proposal: AiProposal,
    base_currency: str = "CZK",
    edited_payload: dict | None = None,
    reason_for_revision: str | None = None,
    note: str | None = None,
) -> dict:
    """HUMAN acceptance: validate payload, call the existing v2 service, mark resolved.
    Returns a dict describing what was created. Raises without side effects on invalid input."""
    if proposal.status not in ("PENDING", "DEFERRED"):
        raise ResearchError(f"proposal {proposal.id} already resolved ({proposal.status})")
    ptype = proposal.proposal_type
    payload_raw = edited_payload if edited_payload is not None else (
        json.loads(proposal.proposed_change_json) if proposal.proposed_change_json else None
    )
    schema = PAYLOAD_SCHEMAS.get(ptype)
    payload = None
    if schema is not None:
        if payload_raw is None:
            raise ResearchError(f"{ptype} proposal has no payload to accept")
        try:
            payload = schema.model_validate(payload_raw)
        except ValidationError as exc:
            raise ResearchError(f"invalid {ptype} payload: {exc.errors()[0]['msg']}") from exc

    inv = session.get(Investment, proposal.investment_id) if proposal.investment_id else None
    result: dict = {"proposal_id": proposal.id, "type": ptype}

    if ptype in INFORMATIONAL_TYPES:
        result["action"] = "acknowledged (informational proposal; no data change)"
    elif ptype == "NEW_EVIDENCE":
        from src.research.evidence import add_evidence

        e = add_evidence(
            session, inv, payload.title, direction=payload.direction, evidence_type=payload.evidence_type,
            summary=payload.summary, source_name=payload.source_name, source_url=payload.source_url,
            reliability=payload.reliability, importance=payload.importance, created_by="AI",
        )
        result["action"] = f"evidence #{e.id} created"
    elif ptype == "ASSUMPTION_STATUS_CHANGE":
        from src.db.research import ThesisAssumption
        from src.research.assumptions import set_assumption_status

        a = session.get(ThesisAssumption, payload.assumption_id)
        if a is None:
            raise ResearchError(f"assumption {payload.assumption_id} not found")
        set_assumption_status(session, a, payload.new_status, note=payload.note or f"accepted AI proposal #{proposal.id}")
        result["action"] = f"assumption #{a.id} -> {payload.new_status}"
    elif ptype in ("NEW_RISK", "RISK_UPDATE"):
        from src.research.items import add_risk

        r = add_risk(
            session, inv, payload.name, description=payload.description, category=payload.category,
            severity=payload.severity, probability=payload.probability, mitigation=payload.mitigation,
            created_by="AI",
        )
        result["action"] = f"risk #{r.id} created"
    elif ptype == "NEW_CATALYST":
        from datetime import date as _date

        from src.research.items import add_catalyst

        c = add_catalyst(
            session, inv, payload.name, description=payload.description,
            expected_date=_date.fromisoformat(payload.expected_date) if payload.expected_date else None,
            probability=payload.probability, created_by="AI",
        )
        result["action"] = f"catalyst #{c.id} created"
    elif ptype == "THESIS_REVISION":
        from src.research.theses import active_thesis, revise_thesis

        if not reason_for_revision or not reason_for_revision.strip():
            raise ResearchError("accepting a THESIS_REVISION requires an explicit reason_for_revision")
        thesis = active_thesis(session, inv)
        if thesis is None:
            raise ResearchError(f"{inv.ticker} has no thesis to revise")
        fields = {k: v for k, v in payload.model_dump().items() if v is not None}
        version = revise_thesis(session, thesis, reason_for_revision=reason_for_revision, created_by="AI", **fields)
        result["action"] = f"thesis revised to v{version.version_number} (new immutable version)"
    elif ptype == "RED_TEAM_ARGUMENT":
        from src.research.items import add_red_team_entry

        e = add_red_team_entry(
            session, inv, payload.argument, severity=payload.severity,
            evidence_reference=payload.evidence_reference, created_by="AI",
        )
        result["action"] = f"red team entry #{e.id} created"
    elif ptype == "NEW_PREDICTION":
        from datetime import date as _date

        from src.research.predictions import create_prediction

        p = create_prediction(
            session, payload.statement, payload.probability, investment=inv,
            resolution_date=_date.fromisoformat(payload.resolution_date) if payload.resolution_date else None,
            resolution_condition=payload.resolution_condition, created_by="AI",
        )
        result["action"] = f"prediction #{p.id} created"
    elif ptype == "KPI_OBSERVATION":
        from src.db.research import InvestmentKpi
        from src.research.kpis import add_observation

        kpi = session.get(InvestmentKpi, payload.kpi_id)
        if kpi is None:
            raise ResearchError(f"KPI {payload.kpi_id} not found")
        o = add_observation(
            session, kpi, payload.period, payload.value, source=payload.source,
            source_reference=payload.source_reference, created_by="AI",
        )
        result["action"] = f"KPI observation #{o.id} created"
    else:  # pragma: no cover - PROPOSAL_TYPES is exhaustive above
        raise ResearchError(f"acceptance not implemented for {ptype}")

    status = "EDITED" if edited_payload is not None else "ACCEPTED"
    _resolve(session, proposal, status, note or result.get("action"))
    log.info("proposal #%s %s: %s", proposal.id, status, result.get("action"))
    return result
