"""FORECAST layer: explicit probabilistic predictions + resolution (calibration-ready).

probability is 0-100 and refers to the stated event. Resolution is manual in v2.
The stored fields (probability, status, resolved_at, category) are exactly what future
calibration (Brier score, buckets, curves) needs - only simple stats are computed now.
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.core import utcnow
from src.db.research import PREDICTION_STATUSES, Investment, Prediction
from src.research.investments import ResearchError

RESOLVED = ("RESOLVED_TRUE", "RESOLVED_FALSE", "AMBIGUOUS", "CANCELLED")


def create_prediction(
    session: Session,
    statement: str,
    probability: int,
    investment: Investment | None = None,
    thesis_id: int | None = None,
    decision_id: int | None = None,
    category: str | None = None,
    resolution_date: date | None = None,
    resolution_condition: str | None = None,
    notes: str | None = None,
    created_by: str = "USER",
) -> Prediction:
    if not statement.strip():
        raise ResearchError("statement is required")
    if not (0 <= int(probability) <= 100):
        raise ResearchError("probability must be 0-100")
    p = Prediction(
        investment_id=investment.id if investment else None,
        thesis_id=thesis_id,
        decision_id=decision_id,
        statement=statement.strip(),
        category=category,
        probability=int(probability),
        resolution_date=resolution_date,
        resolution_condition=resolution_condition,
        notes=notes,
        created_by=created_by,
    )
    session.add(p)
    session.flush()
    return p


def resolve_prediction(
    session: Session, prediction: Prediction, status: str, outcome: str | None = None
) -> Prediction:
    if status not in RESOLVED:
        raise ResearchError(f"resolution status must be one of {RESOLVED}")
    if prediction.status != "OPEN":
        raise ResearchError(f"prediction {prediction.id} is already {prediction.status}")
    prediction.status = status
    prediction.outcome = outcome
    prediction.resolved_at = utcnow()
    session.flush()
    return prediction


def list_predictions(
    session: Session, investment: Investment | None = None, status: str | None = None
) -> list[Prediction]:
    stmt = select(Prediction).order_by(Prediction.created_at.desc(), Prediction.id.desc())
    if investment is not None:
        stmt = stmt.where(Prediction.investment_id == investment.id)
    if status:
        if status not in PREDICTION_STATUSES:
            raise ResearchError(f"invalid status {status!r}")
        stmt = stmt.where(Prediction.status == status)
    return list(session.scalars(stmt))


def overdue_predictions(session: Session, as_of: date | None = None) -> list[Prediction]:
    as_of = as_of or date.today()
    return list(
        session.scalars(
            select(Prediction).where(
                Prediction.status == "OPEN",
                Prediction.resolution_date.isnot(None),
                Prediction.resolution_date <= as_of,
            )
        )
    )


def simple_stats(session: Session) -> dict:
    """Basic counts on resolved predictions; full calibration curves belong to a later version."""
    all_ = list(session.scalars(select(Prediction)))
    resolved = [p for p in all_ if p.status in ("RESOLVED_TRUE", "RESOLVED_FALSE")]
    true_ = [p for p in resolved if p.status == "RESOLVED_TRUE"]
    stats = {
        "total": len(all_),
        "open": sum(1 for p in all_ if p.status == "OPEN"),
        "resolved": len(resolved),
        "resolved_true": len(true_),
        "resolved_false": len(resolved) - len(true_),
        "hit_rate": (len(true_) / len(resolved)) if resolved else None,
        "avg_probability_when_true": (sum(p.probability for p in true_) / len(true_)) if true_ else None,
        "avg_probability_when_false": (
            sum(p.probability for p in resolved if p.status == "RESOLVED_FALSE")
            / (len(resolved) - len(true_))
            if len(resolved) > len(true_)
            else None
        ),
    }
    return stats
