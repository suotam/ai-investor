"""Explicit thesis assumptions. Status changes are user-controlled in v2 (never automatic)."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.core import utcnow
from src.db.research import (
    ASSUMPTION_CATEGORIES,
    ASSUMPTION_STATUSES,
    IMPORTANCES,
    Thesis,
    ThesisAssumption,
)
from src.research.investments import ResearchError
from src.research.theses import current_version


def add_assumption(
    session: Session,
    thesis: Thesis,
    name: str,
    description: str | None = None,
    category: str = "other",
    importance: str = "MEDIUM",
    status: str = "UNKNOWN",
    confidence: int | None = None,
    expected_value: float | None = None,
    expected_min: float | None = None,
    expected_max: float | None = None,
    unit: str | None = None,
    time_horizon: str | None = None,
    breaker_condition: str | None = None,
    kpi_id: int | None = None,
    source_type: str | None = None,
    notes: str | None = None,
    created_by: str = "USER",
) -> ThesisAssumption:
    if category not in ASSUMPTION_CATEGORIES:
        raise ResearchError(f"invalid category {category!r}; allowed: {ASSUMPTION_CATEGORIES}")
    if importance not in IMPORTANCES:
        raise ResearchError(f"invalid importance {importance!r}; allowed: {IMPORTANCES}")
    if status not in ASSUMPTION_STATUSES:
        raise ResearchError(f"invalid status {status!r}; allowed: {ASSUMPTION_STATUSES}")
    if confidence is not None and not (0 <= int(confidence) <= 100):
        raise ResearchError("confidence must be 0-100")
    version = current_version(session, thesis)
    a = ThesisAssumption(
        thesis_id=thesis.id,
        introduced_in_version_id=version.id if version else None,
        name=name,
        description=description,
        category=category,
        importance=importance,
        status=status,
        confidence=confidence,
        expected_value=expected_value,
        expected_min=expected_min,
        expected_max=expected_max,
        unit=unit,
        time_horizon=time_horizon,
        breaker_condition=breaker_condition,
        kpi_id=kpi_id,
        source_type=source_type,
        notes=notes,
        created_by=created_by,
    )
    session.add(a)
    session.flush()
    return a


def set_assumption_status(
    session: Session, assumption: ThesisAssumption, status: str, note: str | None = None
) -> ThesisAssumption:
    if status not in ASSUMPTION_STATUSES:
        raise ResearchError(f"invalid status {status!r}; allowed: {ASSUMPTION_STATUSES}")
    now = utcnow()
    if note:
        stamp = f"[{now:%Y-%m-%d}] status {assumption.status} -> {status}: {note}"
        assumption.notes = f"{assumption.notes}\n{stamp}" if assumption.notes else stamp
    assumption.status = status
    assumption.status_updated_at = now
    assumption.updated_at = now
    session.flush()
    return assumption


def list_assumptions(session: Session, thesis: Thesis, active_only: bool = True) -> list[ThesisAssumption]:
    stmt = select(ThesisAssumption).where(ThesisAssumption.thesis_id == thesis.id)
    if active_only:
        stmt = stmt.where(ThesisAssumption.active.is_(True))
    return list(session.scalars(stmt.order_by(ThesisAssumption.importance.desc(), ThesisAssumption.id)))
