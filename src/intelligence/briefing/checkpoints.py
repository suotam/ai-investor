"""Brief checkpoints: what was known at the last brief, and re-run semantics.

Rules:
  * The delta window of a new run is (previous completed run's period_end, now].
  * Only status='completed' runs advance the checkpoint. Previews never do.
  * Re-running the same day: default is a PREVIEW over the same window (no checkpoint
    corruption, no false "new" deltas). --force supersedes today's completed run and
    re-runs from the SAME period_start it used.
  * First run ever: the window starts 24h (daily) / 7d (weekly) back.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.core import utcnow
from src.db.briefing import BriefItem, BriefRun

DEFAULT_LOOKBACK = {"daily": timedelta(days=1), "weekly": timedelta(days=7)}


def last_completed_run(session: Session, brief_type: str) -> BriefRun | None:
    return session.scalars(
        select(BriefRun)
        .where(BriefRun.brief_type == brief_type, BriefRun.status == "completed")
        .order_by(BriefRun.period_end.desc(), BriefRun.id.desc())
    ).first()


def resolve_window(
    session: Session, brief_type: str, now: datetime | None = None, force: bool = False
) -> tuple[datetime, datetime, BriefRun | None, str]:
    """Returns (period_start, period_end, superseded_run, mode).

    mode: 'new'      - normal run, window starts at last checkpoint
          'preview'  - a completed run already covers today; same window re-rendered
          'force'    - today's completed run will be superseded; same window re-run
    """
    now = now or utcnow()
    last = last_completed_run(session, brief_type)
    if last is None:
        return now - DEFAULT_LOOKBACK[brief_type], now, None, "new"
    same_period = last.period_end.date() == now.date()
    if same_period and force:
        return last.period_start, now, last, "force"
    if same_period:
        return last.period_start, now, None, "preview"
    return last.period_end, now, None, "new"


def start_run(
    session: Session, brief_type: str, period_start: datetime, period_end: datetime,
    status: str, superseded: BriefRun | None = None,
) -> BriefRun:
    if superseded is not None:
        superseded.status = "superseded"
    run = BriefRun(
        brief_type=brief_type, period_start=period_start, period_end=period_end, status=status
    )
    session.add(run)
    session.flush()
    return run


def complete_run(
    session: Session,
    run: BriefRun,
    items: list[dict],
    suppressed_count: int,
    portfolio_value: float | None,
    base_currency: str | None,
    portfolio_state: dict | None,
    output_path: str | None = None,
    audio_path: str | None = None,
    ai_used: bool = False,
    ai_model: str | None = None,
    ai_prompt_version: str | None = None,
    ai_context_hash: str | None = None,
    research_state_hash: str | None = None,
) -> BriefRun:
    for it in items:
        session.add(
            BriefItem(
                brief_run_id=run.id,
                delta_type=it["delta_type"],
                item_key=it["item_key"],
                investment_id=it.get("investment_id"),
                severity=it.get("severity", "LOW"),
                title=it["title"][:400],
                detail=it.get("detail"),
                reason=it.get("reason"),
                source_refs=json.dumps(it.get("source_refs") or []),
            )
        )
    run.items_count = len(items)
    run.suppressed_count = suppressed_count
    run.portfolio_value = portfolio_value
    run.base_currency = base_currency
    run.portfolio_state_json = json.dumps(portfolio_state, default=str) if portfolio_state else None
    run.output_path = output_path
    run.audio_path = audio_path
    run.ai_used = ai_used
    run.ai_model = ai_model
    run.ai_prompt_version = ai_prompt_version
    run.ai_context_hash = ai_context_hash
    run.research_state_hash = research_state_hash
    run.completed_at = utcnow()
    session.flush()
    return run


def previously_surfaced_keys(session: Session, brief_type: str | None = None) -> set[str]:
    """item_keys shown in ANY completed run (both brief types share suppression memory)."""
    stmt = (
        select(BriefItem.item_key)
        .join(BriefRun, BriefRun.id == BriefItem.brief_run_id)
        .where(BriefRun.status == "completed")
    )
    if brief_type:
        stmt = stmt.where(BriefRun.brief_type == brief_type)
    return set(session.execute(stmt).scalars())


def previous_portfolio_state(session: Session, brief_type: str) -> dict | None:
    last = last_completed_run(session, brief_type)
    if last is None or not last.portfolio_state_json:
        return None
    try:
        return json.loads(last.portfolio_state_json)
    except json.JSONDecodeError:
        return None
