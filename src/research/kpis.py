"""Investment KPIs (definition) + observations (FACT layer). Manual entry in v2.

Assumptions may link to a KPI (thesis_assumptions.kpi_id), which enables
ACTUAL (kpi_observations) vs OUR EXPECTATION (assumption expected_min/max) comparison.
Consensus ingestion is a future source: kpi_observations.source distinguishes origins.
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.core import utcnow
from src.db.research import IMPORTANCES, Investment, InvestmentKpi, KpiObservation, ThesisAssumption
from src.research.investments import ResearchError

KPI_FREQUENCIES = ("monthly", "quarterly", "semiannual", "annual", "irregular")


def add_kpi(
    session: Session, investment: Investment, name: str, description: str | None = None,
    unit: str | None = None, frequency: str = "quarterly", importance: str = "MEDIUM",
    direction_good: str | None = None, thesis_relevance: str | None = None,
    source_preference: str | None = None, created_by: str = "USER",
) -> InvestmentKpi:
    if frequency not in KPI_FREQUENCIES:
        raise ResearchError(f"invalid frequency {frequency!r}; allowed: {KPI_FREQUENCIES}")
    if importance not in IMPORTANCES:
        raise ResearchError(f"invalid importance {importance!r}; allowed: {IMPORTANCES}")
    if direction_good is not None and direction_good not in ("up", "down", "range"):
        raise ResearchError("direction_good must be up | down | range")
    existing = session.scalars(
        select(InvestmentKpi).where(
            InvestmentKpi.investment_id == investment.id, InvestmentKpi.name == name
        )
    ).first()
    if existing:
        raise ResearchError(f"KPI {name!r} already exists for {investment.ticker}")
    kpi = InvestmentKpi(
        investment_id=investment.id, name=name, description=description, unit=unit,
        frequency=frequency, importance=importance, direction_good=direction_good,
        thesis_relevance=thesis_relevance, source_preference=source_preference,
        created_by=created_by,
    )
    session.add(kpi)
    session.flush()
    return kpi


def add_observation(
    session: Session, kpi: InvestmentKpi, period: str, value: float,
    period_date: date | None = None, reported_at: date | None = None,
    source: str | None = None, source_reference: str | None = None,
    notes: str | None = None, created_by: str = "USER",
) -> KpiObservation:
    dup = session.scalars(
        select(KpiObservation).where(
            KpiObservation.kpi_id == kpi.id,
            KpiObservation.period == period,
            KpiObservation.source.is_(source) if source is None else KpiObservation.source == source,
        )
    ).first()
    if dup:
        raise ResearchError(f"observation for {kpi.name} period {period} source {source!r} already exists")
    obs = KpiObservation(
        kpi_id=kpi.id, period=period, period_date=period_date, value=value,
        reported_at=reported_at, observed_at=utcnow(), source=source,
        source_reference=source_reference, notes=notes, created_by=created_by,
    )
    session.add(obs)
    session.flush()
    return obs


def list_kpis(session: Session, investment: Investment, active_only: bool = True) -> list[InvestmentKpi]:
    stmt = select(InvestmentKpi).where(InvestmentKpi.investment_id == investment.id)
    if active_only:
        stmt = stmt.where(InvestmentKpi.active.is_(True))
    return list(session.scalars(stmt.order_by(InvestmentKpi.id)))


def observations(session: Session, kpi: InvestmentKpi) -> list[KpiObservation]:
    return list(
        session.scalars(
            select(KpiObservation)
            .where(KpiObservation.kpi_id == kpi.id)
            .order_by(KpiObservation.period_date.is_(None), KpiObservation.period_date, KpiObservation.period)
        )
    )


def kpi_vs_expectation(session: Session, kpi: InvestmentKpi) -> dict:
    """ACTUAL (latest observation) vs OUR EXPECTATION (linked assumptions). No consensus yet."""
    obs = observations(session, kpi)
    latest = obs[-1] if obs else None
    expectations = list(
        session.scalars(
            select(ThesisAssumption).where(
                ThesisAssumption.kpi_id == kpi.id, ThesisAssumption.active.is_(True)
            )
        )
    )
    return {
        "kpi": kpi,
        "latest_observation": latest,
        "observations": obs,
        "expectations": expectations,  # each carries expected_value/min/max + breaker_condition
    }
