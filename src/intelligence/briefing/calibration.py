"""Prediction calibration: deterministic, honest about small samples.

With fewer than MIN_SAMPLE resolved TRUE/FALSE predictions the answer is
"Insufficient sample" - no statistically meaningless precision.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.research import Prediction

MIN_SAMPLE = 10
BUCKETS = [(0, 20), (20, 40), (40, 60), (60, 80), (80, 101)]


@dataclass
class CalibrationReport:
    resolved: int
    sufficient: bool
    brier_score: float | None = None
    hit_rate: float | None = None
    buckets: list[dict] = field(default_factory=list)
    note: str = ""


def calibration_report(session: Session) -> CalibrationReport:
    resolved = [
        p for p in session.scalars(select(Prediction))
        if p.status in ("RESOLVED_TRUE", "RESOLVED_FALSE")
    ]
    n = len(resolved)
    if n < MIN_SAMPLE:
        return CalibrationReport(
            resolved=n, sufficient=False,
            note=f"Insufficient sample: {n} resolved prediction(s); calibration needs >= {MIN_SAMPLE}.",
        )
    brier = sum(
        ((p.probability / 100) - (1.0 if p.status == "RESOLVED_TRUE" else 0.0)) ** 2 for p in resolved
    ) / n
    hits = sum(1 for p in resolved if p.status == "RESOLVED_TRUE")
    buckets = []
    for lo, hi in BUCKETS:
        rows = [p for p in resolved if lo <= p.probability < hi]
        if not rows:
            continue
        true_rate = sum(1 for p in rows if p.status == "RESOLVED_TRUE") / len(rows)
        buckets.append({
            "bucket": f"{lo}-{hi - 1}%", "n": len(rows),
            "avg_stated_probability": round(sum(p.probability for p in rows) / len(rows), 1),
            "observed_frequency_pct": round(100 * true_rate, 1),
        })
    return CalibrationReport(
        resolved=n, sufficient=True, brier_score=round(brier, 4),
        hit_rate=round(hits / n, 3), buckets=buckets,
        note="Brier score: 0 = perfect, 0.25 = uninformed coin flip.",
    )
