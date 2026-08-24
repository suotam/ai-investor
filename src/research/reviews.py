"""Review system: what needs attention. Deterministic queries only - no scoring.

Sections mirror the dashboard "Needs Attention" page. Staleness thresholds come from
src/research/health.py (documented there).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.research import (
    Catalyst,
    Investment,
    Prediction,
    Risk,
    Thesis,
    ThesisAssumption,
    ThesisBreaker,
    ThesisVersion,
    ValuationModel,
)
from src.research.health import STALE_THESIS_DAYS, STALE_VALUATION_DAYS


@dataclass
class NeedsAttention:
    reviews_due: list = field(default_factory=list)  # Investment
    broken_assumptions: list = field(default_factory=list)  # (Investment, ThesisAssumption)
    challenged_assumptions: list = field(default_factory=list)
    triggered_breakers: list = field(default_factory=list)  # (Investment, ThesisBreaker)
    high_risks: list = field(default_factory=list)  # (Investment, Risk)
    expired_catalysts: list = field(default_factory=list)  # (Investment, Catalyst)
    predictions_awaiting: list = field(default_factory=list)  # Prediction
    stale_valuations: list = field(default_factory=list)  # (Investment, ValuationModel, age_days)
    stale_theses: list = field(default_factory=list)  # (Investment, Thesis, age_days)

    @property
    def total(self) -> int:
        return sum(
            len(x)
            for x in (
                self.reviews_due, self.broken_assumptions, self.challenged_assumptions,
                self.triggered_breakers, self.high_risks, self.expired_catalysts,
                self.predictions_awaiting, self.stale_valuations, self.stale_theses,
            )
        )


def needs_attention(session: Session, today: date | None = None) -> NeedsAttention:
    today = today or date.today()
    out = NeedsAttention()
    investments = {i.id: i for i in session.scalars(select(Investment))}
    active = {i.id: i for i in investments.values() if i.status not in ("ARCHIVED", "REJECTED")}

    out.reviews_due = [
        i for i in active.values() if i.next_review_date is not None and i.next_review_date <= today
    ]

    thesis_owner = {
        t.id: t for t in session.scalars(select(Thesis)) if t.investment_id in active
    }
    for a in session.scalars(
        select(ThesisAssumption).where(
            ThesisAssumption.active.is_(True), ThesisAssumption.status.in_(("BROKEN", "CHALLENGED"))
        )
    ):
        thesis = thesis_owner.get(a.thesis_id)
        if thesis is None:
            continue
        inv = active[thesis.investment_id]
        (out.broken_assumptions if a.status == "BROKEN" else out.challenged_assumptions).append((inv, a))

    for b in session.scalars(select(ThesisBreaker).where(ThesisBreaker.status == "TRIGGERED")):
        if b.investment_id in active:
            out.triggered_breakers.append((active[b.investment_id], b))

    for r in session.scalars(
        select(Risk).where(Risk.status == "OPEN", Risk.severity.in_(("HIGH", "CRITICAL")))
    ):
        if r.investment_id in active:
            out.high_risks.append((active[r.investment_id], r))

    for c in session.scalars(
        select(Catalyst).where(
            Catalyst.status == "PENDING",
            Catalyst.expected_date.isnot(None),
            Catalyst.expected_date < today,
        )
    ):
        if c.investment_id in active:
            out.expired_catalysts.append((active[c.investment_id], c))

    out.predictions_awaiting = [
        p
        for p in session.scalars(
            select(Prediction).where(
                Prediction.status == "OPEN",
                Prediction.resolution_date.isnot(None),
                Prediction.resolution_date <= today,
            )
        )
        if p.investment_id is None or p.investment_id in active
    ]

    val_cutoff = today - timedelta(days=STALE_VALUATION_DAYS)
    for m in session.scalars(select(ValuationModel).where(ValuationModel.active.is_(True))):
        if m.investment_id in active and m.updated_at.date() <= val_cutoff:
            out.stale_valuations.append((active[m.investment_id], m, (today - m.updated_at.date()).days))

    thesis_cutoff = today - timedelta(days=STALE_THESIS_DAYS)
    for t in thesis_owner.values():
        if not t.active:
            continue
        version = session.get(ThesisVersion, t.current_version_id) if t.current_version_id else None
        if version is not None and version.created_at.date() <= thesis_cutoff:
            out.stale_theses.append(
                (active[t.investment_id], t, (today - version.created_at.date()).days)
            )
    return out
