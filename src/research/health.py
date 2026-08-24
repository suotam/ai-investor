"""Deterministic thesis health: transparent counts + a state derived from DOCUMENTED rules.

This is explicitly NOT an AI score and never a BUY/SELL signal.

State rules (in order; first match wins):
  BROKEN  - any assumption with status BROKEN, or any breaker with status TRIGGERED
  AT_RISK - any CHALLENGED assumption, or >= 2 WEAKENING assumptions,
            or any OPEN risk with severity CRITICAL
  WATCH   - any WEAKENING assumption, or any OPEN risk with severity HIGH,
            or thesis is stale (no revision for STALE_THESIS_DAYS),
            or valuation is stale (no update for STALE_VALUATION_DAYS)
  HEALTHY - otherwise, provided at least one active assumption exists
  None    - no assumptions exist -> aggregate state is omitted; only components are shown
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.research import Investment, Risk, ThesisAssumption, ThesisBreaker
from src.research.theses import active_thesis, current_version

STALE_THESIS_DAYS = 120
STALE_VALUATION_DAYS = 180


@dataclass
class ThesisHealth:
    supported: int = 0
    weakening: int = 0
    unknown: int = 0
    challenged: int = 0
    broken: int = 0
    breakers_active: int = 0
    breakers_triggered: int = 0
    high_risks: int = 0  # OPEN risks with severity HIGH or CRITICAL
    critical_risks: int = 0
    thesis_age_days: int | None = None
    thesis_stale: bool = False
    valuation_age_days: int | None = None
    valuation_stale: bool = False
    state: str | None = None  # HEALTHY | WATCH | AT_RISK | BROKEN | None
    reasons: list[str] = field(default_factory=list)


def _days_since(ts: datetime | date | None, today: date) -> int | None:
    if ts is None:
        return None
    d = ts.date() if isinstance(ts, datetime) else ts
    return (today - d).days


def thesis_health(session: Session, investment: Investment, today: date | None = None) -> ThesisHealth:
    today = today or date.today()
    h = ThesisHealth()

    thesis = active_thesis(session, investment)
    if thesis:
        for a in session.scalars(
            select(ThesisAssumption).where(
                ThesisAssumption.thesis_id == thesis.id, ThesisAssumption.active.is_(True)
            )
        ):
            key = a.status.lower()
            setattr(h, key, getattr(h, key) + 1)
        version = current_version(session, thesis)
        if version:
            h.thesis_age_days = _days_since(version.created_at, today)
            h.thesis_stale = (h.thesis_age_days or 0) > STALE_THESIS_DAYS

    for b in session.scalars(select(ThesisBreaker).where(ThesisBreaker.investment_id == investment.id)):
        if b.status == "ACTIVE":
            h.breakers_active += 1
        elif b.status == "TRIGGERED":
            h.breakers_triggered += 1

    for r in session.scalars(
        select(Risk).where(Risk.investment_id == investment.id, Risk.status == "OPEN")
    ):
        if r.severity == "CRITICAL":
            h.critical_risks += 1
            h.high_risks += 1
        elif r.severity == "HIGH":
            h.high_risks += 1

    from src.research.valuation import models_for

    models = models_for(session, investment)
    if models:
        latest = max(m.updated_at for m in models)
        h.valuation_age_days = _days_since(latest, today)
        h.valuation_stale = (h.valuation_age_days or 0) > STALE_VALUATION_DAYS

    # --- documented deterministic state rules --------------------------------
    total_assumptions = h.supported + h.weakening + h.unknown + h.challenged + h.broken
    if h.broken > 0:
        h.state = "BROKEN"
        h.reasons.append(f"{h.broken} broken assumption(s)")
    if h.breakers_triggered > 0:
        h.state = "BROKEN"
        h.reasons.append(f"{h.breakers_triggered} triggered thesis breaker(s)")
    if h.state is None and (h.challenged > 0 or h.weakening >= 2 or h.critical_risks > 0):
        h.state = "AT_RISK"
        if h.challenged:
            h.reasons.append(f"{h.challenged} challenged assumption(s)")
        if h.weakening >= 2:
            h.reasons.append(f"{h.weakening} weakening assumptions")
        if h.critical_risks:
            h.reasons.append(f"{h.critical_risks} critical open risk(s)")
    if h.state is None and (h.weakening > 0 or h.high_risks > 0 or h.thesis_stale or h.valuation_stale):
        h.state = "WATCH"
        if h.weakening:
            h.reasons.append(f"{h.weakening} weakening assumption(s)")
        if h.high_risks:
            h.reasons.append(f"{h.high_risks} high-severity open risk(s)")
        if h.thesis_stale:
            h.reasons.append(f"thesis not revised for {h.thesis_age_days} days")
        if h.valuation_stale:
            h.reasons.append(f"valuation not updated for {h.valuation_age_days} days")
    if h.state is None:
        h.state = "HEALTHY" if total_assumptions > 0 else None
        if h.state is None:
            h.reasons.append("no assumptions recorded - aggregate state omitted")
    return h
