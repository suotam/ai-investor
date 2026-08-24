"""Evidence: raw FACT/OBSERVATION records, kept independent of interpretation.

The system never converts evidence into a conclusion; supporting and contradicting evidence
are stored and displayed side by side. Manual entry in v2; v3 ingestion (filings, earnings,
news, insider, congress, 13F, macro...) reuses the same table via evidence_type + raw_reference.
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.core import utcnow
from src.db.research import (
    DIRECTIONS,
    EVIDENCE_TARGETS,
    EVIDENCE_TYPES,
    IMPORTANCES,
    Evidence,
    Investment,
)
from src.research.investments import ResearchError


def add_evidence(
    session: Session,
    investment: Investment,
    title: str,
    direction: str = "NEUTRAL",
    evidence_type: str = "manual",
    target_type: str = "investment",
    target_id: int | None = None,
    thesis_version_id: int | None = None,
    summary: str | None = None,
    source_url: str | None = None,
    source_name: str | None = None,
    source_date: date | None = None,
    event_date: date | None = None,
    source_published_at: datetime | None = None,
    observed_at: datetime | None = None,
    reliability: str | None = None,
    importance: str = "MEDIUM",
    raw_reference: str | None = None,
    notes: str | None = None,
    created_by: str = "USER",
) -> Evidence:
    if direction not in DIRECTIONS:
        raise ResearchError(f"invalid direction {direction!r}; allowed: {DIRECTIONS}")
    if evidence_type not in EVIDENCE_TYPES:
        raise ResearchError(f"invalid evidence_type {evidence_type!r}; allowed: {EVIDENCE_TYPES}")
    if target_type not in EVIDENCE_TARGETS:
        raise ResearchError(f"invalid target_type {target_type!r}; allowed: {EVIDENCE_TARGETS}")
    if importance not in IMPORTANCES:
        raise ResearchError(f"invalid importance {importance!r}; allowed: {IMPORTANCES}")
    if reliability is not None and reliability not in IMPORTANCES:
        raise ResearchError(f"invalid reliability {reliability!r}; allowed: {IMPORTANCES}")
    e = Evidence(
        investment_id=investment.id,
        thesis_version_id=thesis_version_id,
        target_type=target_type,
        target_id=target_id,
        direction=direction,
        evidence_type=evidence_type,
        title=title,
        summary=summary,
        source_url=source_url,
        source_name=source_name,
        source_date=source_date,
        event_date=event_date,
        source_published_at=source_published_at,
        observed_at=observed_at or utcnow(),
        reliability=reliability,
        importance=importance,
        raw_reference=raw_reference,
        notes=notes,
        created_by=created_by,
    )
    session.add(e)
    session.flush()
    return e


def list_evidence(
    session: Session,
    investment: Investment,
    direction: str | None = None,
    target_type: str | None = None,
    target_id: int | None = None,
) -> list[Evidence]:
    stmt = select(Evidence).where(Evidence.investment_id == investment.id)
    if direction:
        stmt = stmt.where(Evidence.direction == direction)
    if target_type:
        stmt = stmt.where(Evidence.target_type == target_type)
    if target_id is not None:
        stmt = stmt.where(Evidence.target_id == target_id)
    return list(session.scalars(stmt.order_by(Evidence.created_at.desc(), Evidence.id.desc())))


def evidence_by_direction(session: Session, investment: Investment) -> dict[str, list[Evidence]]:
    """Supporting vs contradicting (vs neutral) side by side - never merged into a verdict."""
    out: dict[str, list[Evidence]] = {d: [] for d in DIRECTIONS}
    for e in list_evidence(session, investment):
        out[e.direction].append(e)
    return out
