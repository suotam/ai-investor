"""Risks, catalysts, thesis breakers, pre-mortems and red-team entries.

Simple validated CRUD; each concept is a separate table (see src/db/research.py).
Risk = something that could hurt the investment. Breaker = something that invalidates
the original reasoning. They are deliberately not merged.
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.core import utcnow
from src.db.research import (
    BREAKER_STATUSES,
    CATALYST_STATUSES,
    IMPORTANCES,
    REDTEAM_STATUSES,
    RISK_CATEGORIES,
    RISK_STATUSES,
    SEVERITIES,
    Catalyst,
    Investment,
    PreMortem,
    RedTeamEntry,
    Risk,
    ThesisBreaker,
)
from src.research.investments import ResearchError


def _check(value, allowed, label: str) -> None:
    if value not in allowed:
        raise ResearchError(f"invalid {label} {value!r}; allowed: {allowed}")


def _check_prob(p) -> None:
    if p is not None and not (0 <= int(p) <= 100):
        raise ResearchError("probability must be 0-100")


# --- risks -------------------------------------------------------------------


def add_risk(
    session: Session, investment: Investment, name: str, description: str | None = None,
    category: str = "other", probability: int | None = None, impact: str | None = None,
    severity: str = "MEDIUM", mitigation: str | None = None, created_by: str = "USER",
) -> Risk:
    _check(category, RISK_CATEGORIES, "category")
    _check(severity, SEVERITIES, "severity")
    if impact is not None:
        _check(impact, IMPORTANCES, "impact")
    _check_prob(probability)
    r = Risk(
        investment_id=investment.id, name=name, description=description, category=category,
        probability=probability, impact=impact, severity=severity, mitigation=mitigation,
        created_by=created_by,
    )
    session.add(r)
    session.flush()
    return r


def set_risk_status(session: Session, risk: Risk, status: str) -> Risk:
    _check(status, RISK_STATUSES, "status")
    risk.status = status
    risk.updated_at = utcnow()
    session.flush()
    return risk


# --- catalysts ---------------------------------------------------------------


def add_catalyst(
    session: Session, investment: Investment, name: str, description: str | None = None,
    expected_date: date | None = None, probability: int | None = None,
    potential_impact: str | None = None, created_by: str = "USER",
) -> Catalyst:
    _check_prob(probability)
    c = Catalyst(
        investment_id=investment.id, name=name, description=description,
        expected_date=expected_date, probability=probability, potential_impact=potential_impact,
        created_by=created_by,
    )
    session.add(c)
    session.flush()
    return c


def resolve_catalyst(
    session: Session, catalyst: Catalyst, status: str, actual_date: date | None = None,
    outcome: str | None = None,
) -> Catalyst:
    _check(status, CATALYST_STATUSES, "status")
    catalyst.status = status
    catalyst.actual_date = actual_date
    catalyst.outcome = outcome
    catalyst.updated_at = utcnow()
    session.flush()
    return catalyst


# --- thesis breakers ---------------------------------------------------------


def add_breaker(
    session: Session, investment: Investment, name: str, condition_text: str | None = None,
    description: str | None = None, severity: str = "HIGH", thesis_id: int | None = None,
    created_by: str = "USER",
) -> ThesisBreaker:
    _check(severity, SEVERITIES, "severity")
    b = ThesisBreaker(
        investment_id=investment.id, thesis_id=thesis_id, name=name, description=description,
        severity=severity, condition_text=condition_text, created_by=created_by,
    )
    session.add(b)
    session.flush()
    return b


def set_breaker_status(
    session: Session, breaker: ThesisBreaker, status: str, note: str | None = None
) -> ThesisBreaker:
    _check(status, BREAKER_STATUSES, "status")
    now = utcnow()
    if status == "TRIGGERED" and breaker.status != "TRIGGERED":
        breaker.triggered_at = now
    if status == "RESOLVED":
        breaker.resolved_at = now
    if note:
        stamp = f"[{now:%Y-%m-%d}] {breaker.status} -> {status}: {note}"
        breaker.notes = f"{breaker.notes}\n{stamp}" if breaker.notes else stamp
    breaker.status = status
    breaker.updated_at = now
    session.flush()
    return breaker


# --- pre-mortem --------------------------------------------------------------


def add_premortem(
    session: Session, investment: Investment, scenario: str, probability: int | None = None,
    impact: str | None = None, early_warning_signs: str | None = None,
    possible_mitigation: str | None = None, thesis_version_id: int | None = None,
    decision_id: int | None = None, created_by: str = "USER",
) -> PreMortem:
    _check_prob(probability)
    if impact is not None:
        _check(impact, IMPORTANCES, "impact")
    p = PreMortem(
        investment_id=investment.id, thesis_version_id=thesis_version_id, decision_id=decision_id,
        scenario=scenario, probability=probability, impact=impact,
        early_warning_signs=early_warning_signs, possible_mitigation=possible_mitigation,
        created_by=created_by,
    )
    session.add(p)
    session.flush()
    return p


# --- red team ----------------------------------------------------------------


def add_red_team_entry(
    session: Session, investment: Investment, argument: str, severity: str = "MEDIUM",
    evidence_reference: str | None = None, thesis_version_id: int | None = None,
    created_by: str = "USER",
) -> RedTeamEntry:
    _check(severity, SEVERITIES, "severity")
    e = RedTeamEntry(
        investment_id=investment.id, thesis_version_id=thesis_version_id, argument=argument,
        severity=severity, evidence_reference=evidence_reference, created_by=created_by,
    )
    session.add(e)
    session.flush()
    return e


def set_red_team_status(
    session: Session, entry: RedTeamEntry, status: str, resolution: str | None = None
) -> RedTeamEntry:
    _check(status, REDTEAM_STATUSES, "status")
    entry.status = status
    entry.resolution = resolution
    entry.updated_at = utcnow()
    session.flush()
    return entry


def list_for_investment(session: Session, model, investment: Investment, **filters) -> list:
    stmt = select(model).where(model.investment_id == investment.id)
    for field, value in filters.items():
        stmt = stmt.where(getattr(model, field) == value)
    return list(session.scalars(stmt.order_by(model.id)))
