"""Thesis identity + IMMUTABLE versioning.

Invariant (tested): revising a thesis inserts a new thesis_versions row and repoints
theses.current_version_id; existing version rows are NEVER updated. There is deliberately
no function in this module that edits an existing version.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.research import Investment, Thesis, ThesisVersion
from src.research.investments import ResearchError

VERSION_FIELDS = (
    "summary", "core_thesis", "variant_perception", "market_expectation", "our_expectation",
    "why_market_may_be_wrong", "expected_return_summary", "confidence", "time_horizon",
    "status", "review_date",
)


def _check_confidence(value) -> None:
    if value is not None and not (0 <= int(value) <= 100):
        raise ResearchError("confidence must be 0-100")


def create_thesis(
    session: Session, investment: Investment, title: str, created_by: str = "USER", **fields
) -> tuple[Thesis, ThesisVersion]:
    """Create the stable thesis and its version 1."""
    unknown = set(fields) - set(VERSION_FIELDS)
    if unknown:
        raise ResearchError(f"unknown thesis fields: {sorted(unknown)}")
    _check_confidence(fields.get("confidence"))
    thesis = Thesis(investment_id=investment.id, title=title, created_by=created_by)
    session.add(thesis)
    session.flush()
    version = ThesisVersion(
        thesis_id=thesis.id, version_number=1, previous_version_id=None,
        reason_for_revision="original", created_by=created_by, **fields,
    )
    session.add(version)
    session.flush()
    thesis.current_version_id = version.id
    session.flush()
    return thesis, version


def revise_thesis(
    session: Session,
    thesis: Thesis,
    reason_for_revision: str,
    created_by: str = "USER",
    created_from_decision_id: int | None = None,
    **fields,
) -> ThesisVersion:
    """Create a NEW immutable version. Unspecified fields carry over from the current version."""
    if not reason_for_revision or not reason_for_revision.strip():
        raise ResearchError("reason_for_revision is required for a thesis revision")
    unknown = set(fields) - set(VERSION_FIELDS)
    if unknown:
        raise ResearchError(f"unknown thesis fields: {sorted(unknown)}")
    current = current_version(session, thesis)
    if current is None:
        raise ResearchError("thesis has no version; create_thesis first")
    _check_confidence(fields.get("confidence"))
    carried = {f: getattr(current, f) for f in VERSION_FIELDS}
    carried.update(fields)
    version = ThesisVersion(
        thesis_id=thesis.id,
        version_number=current.version_number + 1,
        previous_version_id=current.id,
        reason_for_revision=reason_for_revision,
        created_from_decision_id=created_from_decision_id,
        created_by=created_by,
        **carried,
    )
    session.add(version)
    session.flush()
    thesis.current_version_id = version.id
    session.flush()
    return version


def current_version(session: Session, thesis: Thesis) -> ThesisVersion | None:
    if thesis.current_version_id is not None:
        return session.get(ThesisVersion, thesis.current_version_id)
    return session.scalars(
        select(ThesisVersion)
        .where(ThesisVersion.thesis_id == thesis.id)
        .order_by(ThesisVersion.version_number.desc())
    ).first()


def version_history(session: Session, thesis: Thesis) -> list[ThesisVersion]:
    return list(
        session.scalars(
            select(ThesisVersion)
            .where(ThesisVersion.thesis_id == thesis.id)
            .order_by(ThesisVersion.version_number)
        )
    )


def active_thesis(session: Session, investment: Investment) -> Thesis | None:
    return session.scalars(
        select(Thesis)
        .where(Thesis.investment_id == investment.id, Thesis.active.is_(True))
        .order_by(Thesis.id.desc())
    ).first()
