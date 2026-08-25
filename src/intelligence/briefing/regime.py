"""Macro regime dimensions: transparent, deterministic, no magic composite label.

Rule (documented, same for every dimension): compare the latest observation with the mean of
the previous `LOOKBACK` observations of the mapped series. A relative change beyond
`threshold_pct` marks the dimension IMPROVING or DETERIORATING (direction-aware: for some
series a fall is an improvement); otherwise STABLE. Missing data -> UNKNOWN, never guessed.
AI may explain a regime; it never defines these states.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.intelligence import MacroSeries
from src.intelligence.connectors.macro import latest_observations

LOOKBACK = 3

# dimension -> (series_code, threshold_pct, falling_is_improving)
DIMENSION_RULES: dict[str, tuple[str, float, bool]] = {
    "Growth": ("GDPC1", 0.4, False),
    "Inflation": ("CPIAUCSL", 0.5, True),
    "Rates": ("DGS10", 3.0, True),
    "Liquidity": ("FEDFUNDS", 3.0, True),
    "Credit": ("T10Y2Y", 15.0, False),  # steepening spread ~ easing credit conditions
    "Labor": ("UNRATE", 3.0, True),
    "USD": ("DTWEXBGS", 1.5, True),  # weaker USD ~ improving for EM-exposed holdings
}


@dataclass
class RegimeDimension:
    name: str
    state: str  # IMPROVING | STABLE | DETERIORATING | UNKNOWN
    series_code: str
    latest: float | None
    baseline: float | None
    change_pct: float | None
    rule: str


def macro_regime(session: Session) -> list[RegimeDimension]:
    out: list[RegimeDimension] = []
    for name, (code, threshold, falling_good) in DIMENSION_RULES.items():
        series = session.scalars(select(MacroSeries).where(MacroSeries.series_code == code)).first()
        rule = (
            f"latest vs mean of previous {LOOKBACK} observations of {code}; "
            f"|change| > {threshold}% -> direction ({'fall=improving' if falling_good else 'rise=improving'})"
        )
        if series is None:
            out.append(RegimeDimension(name, "UNKNOWN", code, None, None, None, rule + " [series not tracked]"))
            continue
        obs = latest_observations(session, series, n=LOOKBACK + 1)
        if len(obs) < LOOKBACK + 1:
            out.append(RegimeDimension(name, "UNKNOWN", code, obs[-1].value if obs else None,
                                       None, None, rule + " [insufficient data]"))
            continue
        latest = obs[-1].value
        baseline = sum(o.value for o in obs[:-1]) / LOOKBACK
        change_pct = 100 * (latest - baseline) / abs(baseline) if baseline else None
        if change_pct is None or abs(change_pct) <= threshold:
            state = "STABLE"
        else:
            rising = change_pct > 0
            improving = (not rising) if falling_good else rising
            state = "IMPROVING" if improving else "DETERIORATING"
        out.append(RegimeDimension(name, state, code, latest, round(baseline, 4),
                                   round(change_pct, 2) if change_pct is not None else None, rule))
    return out
