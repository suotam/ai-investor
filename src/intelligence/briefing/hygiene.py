"""Alert hygiene: suppression rules so the brief shows change, not static state.

Rules (deterministic, documented):
  1. An item_key already surfaced in a completed brief is suppressed - each delta key is
     built to identify one real change, so a repeat means "nothing new".
  2. Standing-condition items (REVIEW_DUE, PREDICTION_DUE) whose condition persists are
     re-surfaced after RESURFACE_DAYS as a gentle reminder, not daily.
  3. Human triage via attention_states: DEFERRED suppresses until defer_until;
     RESOLVED suppresses permanently (a genuinely new change gets a new key anyway).
Suppressed items are counted and reported ("n items suppressed"), never silently dropped.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.core import utcnow
from src.db.briefing import AttentionState, BriefItem, BriefRun
from src.intelligence.briefing.deltas import Delta

RESURFACE_DAYS = 7
RESURFACEABLE = {"REVIEW_DUE", "PREDICTION_DUE"}


def _last_surfaced(session: Session, brief_type: str | None = None) -> dict[str, datetime]:
    stmt = (
        select(BriefItem.item_key, func.max(BriefItem.created_at))
        .join(BriefRun, BriefRun.id == BriefItem.brief_run_id)
        .where(BriefRun.status == "completed")
        .group_by(BriefItem.item_key)
    )
    if brief_type:
        stmt = stmt.where(BriefRun.brief_type == brief_type)
    rows = session.execute(stmt).all()
    return {k: ts for k, ts in rows}


def apply_hygiene(
    session: Session, deltas: list[Delta], now: datetime | None = None, brief_type: str | None = None
) -> tuple[list[Delta], list[Delta]]:
    """Returns (surfaced, suppressed). Suppression memory is per brief type: the weekly
    review re-summarizes the week even when the daily briefs already showed the items."""
    now = now or utcnow()
    seen = _last_surfaced(session, brief_type)
    states = {a.item_key: a for a in session.scalars(select(AttentionState))}
    surfaced: list[Delta] = []
    suppressed: list[Delta] = []
    for d in deltas:
        st = states.get(d.item_key)
        if st is not None:
            if st.status == "RESOLVED":
                suppressed.append(d)
                continue
            if st.status == "DEFERRED" and st.defer_until and st.defer_until > now.date():
                suppressed.append(d)
                continue
        last = seen.get(d.item_key)
        if last is not None:
            if d.delta_type in RESURFACEABLE and (now - last) >= timedelta(days=RESURFACE_DAYS):
                d.reason = (d.reason or "") + f" (reminder: unresolved for {(now - last).days} days)"
                surfaced.append(d)
            else:
                suppressed.append(d)
            continue
        surfaced.append(d)
    return surfaced, suppressed


def set_attention(
    session: Session, item_key: str, status: str, defer_until: date | None = None,
    note: str | None = None, investment_id: int | None = None,
) -> AttentionState:
    from src.db.briefing import ATTENTION_STATUSES
    from src.research.investments import ResearchError

    if status not in ATTENTION_STATUSES:
        raise ResearchError(f"invalid attention status {status!r}; allowed: {ATTENTION_STATUSES}")
    st = session.scalars(select(AttentionState).where(AttentionState.item_key == item_key)).first()
    if st is None:
        st = AttentionState(item_key=item_key, investment_id=investment_id)
        session.add(st)
    st.status = status
    st.defer_until = defer_until
    st.note = note
    st.updated_at = utcnow()
    session.flush()
    return st
