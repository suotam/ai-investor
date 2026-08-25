"""Management consistency tracker (foundation): sourced statements, promise vs outcome.

Every claim must point to a stored source (document id or explicit reference) - never
unsourced LLM memory. Status transitions are human-controlled in v4.
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.core import utcnow
from src.db.briefing import CLAIM_STATUSES, ManagementClaim
from src.db.research import Investment
from src.research.investments import ResearchError


def add_claim(
    session: Session,
    investment: Investment,
    statement: str,
    source_document_id: int | None = None,
    source_reference: str | None = None,
    speaker: str | None = None,
    claim_date: date | None = None,
    topic: str | None = None,
    time_horizon: str | None = None,
    created_by: str = "USER",
) -> ManagementClaim:
    if not statement.strip():
        raise ResearchError("statement is required")
    if source_document_id is None and not (source_reference or "").strip():
        raise ResearchError(
            "a management claim requires a source (source_document_id or source_reference); "
            "unsourced statements are not stored"
        )
    c = ManagementClaim(
        investment_id=investment.id, statement=statement.strip(), speaker=speaker,
        claim_date=claim_date, topic=topic, time_horizon=time_horizon,
        source_document_id=source_document_id, source_reference=source_reference,
        created_by=created_by,
    )
    session.add(c)
    session.flush()
    return c


def set_claim_status(session: Session, claim: ManagementClaim, status: str, outcome_note: str | None = None) -> ManagementClaim:
    if status not in CLAIM_STATUSES:
        raise ResearchError(f"invalid claim status {status!r}; allowed: {CLAIM_STATUSES}")
    claim.status = status
    claim.outcome_note = outcome_note
    claim.updated_at = utcnow()
    session.flush()
    return claim


def list_claims(session: Session, investment: Investment, status: str | None = None) -> list[ManagementClaim]:
    stmt = select(ManagementClaim).where(ManagementClaim.investment_id == investment.id)
    if status:
        stmt = stmt.where(ManagementClaim.status == status)
    return list(session.scalars(stmt.order_by(ManagementClaim.claim_date.desc().nullslast(), ManagementClaim.id.desc())))
