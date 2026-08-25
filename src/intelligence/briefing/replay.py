"""Decision replay: outcome-bias defense.

First pass shows ONLY what was known at decision time (frozen snapshot + time-aware context
packet, as_of = decision timestamp). The user answers "would you make the same decision?"
BEFORE any outcome is revealed. Ratings are stored outcome-independently.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.operations import DECISION_RATINGS, DecisionReview
from src.db.research import Decision, Investment
from src.intelligence.ai.context import build_context_packet
from src.research.decisions import decision_snapshot
from src.research.investments import ResearchError


def replay_view(session: Session, decision: Decision) -> dict:
    """BLIND view: only information available at decision time. No later data, no outcome."""
    inv = session.get(Investment, decision.investment_id)
    packet = build_context_packet(session, inv, as_of=decision.decided_at)  # no-hindsight
    snap = decision_snapshot(decision)
    return {
        "mode": "DECISION REPLAY (blind: information as of decision time only)",
        "decision_id": decision.id,
        "ticker": inv.ticker,
        "type": decision.decision_type,
        "decided_at": decision.decided_at.isoformat(),
        "price_at_decision": decision.instrument_price,
        "portfolio_value_at_decision": decision.portfolio_value,
        "weight_before": decision.position_weight_before,
        "reasoning": decision.reasoning,
        "confidence": decision.confidence,
        "expected_outcome": decision.expected_outcome,
        "what_would_make_this_wrong": decision.what_would_make_this_wrong,
        "context_as_of_then": packet.data,
        "question": "Would you make the same decision with ONLY this information?",
    }


def reveal_outcome(session: Session, decision: Decision, settings) -> dict:
    """Second pass: what happened afterwards (price now vs then, evidence since)."""
    inv = session.get(Investment, decision.investment_id)
    out: dict = {"decision_id": decision.id, "since": decision.decided_at.date().isoformat()}
    if inv.instrument_id and decision.instrument_price:
        from src.market_data.service import PriceStore

        latest = PriceStore(session).latest(inv.instrument_id)
        if latest:
            out["price_then"] = decision.instrument_price
            out["price_now"] = float(latest.close)
            out["price_change_pct"] = round(100 * (float(latest.close) / decision.instrument_price - 1), 1)
    from src.db.research import Evidence

    later = [
        e for e in session.scalars(select(Evidence).where(Evidence.investment_id == inv.id))
        if e.created_at > decision.decided_at
    ]
    out["evidence_since"] = [
        {"title": e.title, "direction": e.direction} for e in later[:10]
    ]
    out["note"] = ("Outcome revealed AFTER the blind pass. A good outcome does not validate a bad "
                   "process, and a bad outcome does not condemn a good one.")
    return out


def rate_decision(
    session: Session, decision: Decision, rating: str, would_repeat: bool | None = None,
    replay_used: bool = False, notes: str | None = None,
) -> DecisionReview:
    if rating not in DECISION_RATINGS:
        raise ResearchError(f"rating must be one of {DECISION_RATINGS}")
    review = DecisionReview(
        decision_id=decision.id, user_rating=rating, would_repeat=would_repeat,
        replay_used=replay_used, notes=notes,
    )
    session.add(review)
    session.flush()
    return review
