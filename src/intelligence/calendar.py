"""Investment calendar: deterministic aggregation of KNOWN dates only - nothing fabricated.

Sources: catalysts (expected_date), predictions (resolution_date), investment reviews
(next_review_date), and expected filing cadence is NOT guessed (earnings dates appear only
when entered as catalysts or once a filing event exists)."""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.research import Catalyst, Investment, Prediction


def upcoming_events(session: Session, days: int = 30, today: date | None = None) -> list[dict]:
    today = today or date.today()
    horizon = today + timedelta(days=days)
    out: list[dict] = []
    investments = {i.id: i for i in session.scalars(select(Investment))}

    for c in session.scalars(
        select(Catalyst).where(
            Catalyst.status == "PENDING",
            Catalyst.expected_date.isnot(None),
            Catalyst.expected_date >= today,
            Catalyst.expected_date <= horizon,
        )
    ):
        inv = investments.get(c.investment_id)
        out.append({"date": c.expected_date, "kind": "catalyst",
                    "title": f"{inv.ticker if inv else '?'}: {c.name}"})

    for p in session.scalars(
        select(Prediction).where(
            Prediction.status == "OPEN",
            Prediction.resolution_date.isnot(None),
            Prediction.resolution_date >= today,
            Prediction.resolution_date <= horizon,
        )
    ):
        inv = investments.get(p.investment_id)
        out.append({"date": p.resolution_date, "kind": "prediction due",
                    "title": f"{inv.ticker + ': ' if inv else ''}{p.statement[:70]}"})

    for inv in investments.values():
        if inv.status in ("ARCHIVED", "REJECTED"):
            continue
        if inv.next_review_date and today <= inv.next_review_date <= horizon:
            out.append({"date": inv.next_review_date, "kind": "thesis review",
                        "title": f"{inv.ticker}: scheduled review"})

    return sorted(out, key=lambda e: e["date"])
