"""Decision journal: IMMUTABLE records with a frozen deterministic context snapshot.

Snapshot rules:
  * values come from the v1 portfolio engine at decision time (price cache, valuation),
  * anything unavailable is stored as None - never fabricated, never backfilled later,
  * once written, a decision row is never updated (no update function exists here);
    corrections are new rows referencing the original via amends_decision_id.
"""
from __future__ import annotations

import json
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.core import D, f, q, utcnow
from src.db.research import DECISION_TYPES, Decision, Investment
from src.market_data.service import PriceStore
from src.portfolio.valuation import value_portfolio
from src.research.investments import ResearchError

REASONING_FIELDS = (
    "intended_position_change", "actual_position_change", "reasoning", "confidence",
    "time_horizon", "expected_outcome", "alternative_considered", "key_risks",
    "what_would_make_this_wrong", "information_available_summary",
)


def build_decision_snapshot(
    session: Session, investment: Investment, base_currency: str, as_of: date | None = None
) -> dict:
    """Deterministic context at decision time. Every unavailable component is None."""
    as_of = as_of or date.today()
    snapshot: dict = {
        "as_of": as_of.isoformat(),
        "base_currency": base_currency,
        "instrument_price": None,
        "price_currency": None,
        "price_date": None,
        "portfolio_value": None,
        "cash_value": None,
        "invested_value": None,
        "position_quantity": None,
        "position_weight": None,
        "position_market_value": None,
        "position_cost_basis": None,
    }
    if investment.instrument_id is not None:
        found = PriceStore(session).close_on(investment.instrument_id, as_of)
        if found:
            snapshot["instrument_price"] = float(found.close)
            snapshot["price_currency"] = found.currency
            snapshot["price_date"] = found.price_date.isoformat()
    val = value_portfolio(session, base_currency, as_of)
    snapshot["portfolio_value"] = f(q(val.total_value_base))
    snapshot["cash_value"] = f(q(val.cash_base))
    snapshot["invested_value"] = f(q(val.invested_value_base))
    for r in val.positions:
        if r.instrument_id == investment.instrument_id:
            snapshot["position_quantity"] = f(q(r.quantity))
            snapshot["position_weight"] = f(q(r.weight))
            snapshot["position_market_value"] = f(q(r.market_value_base))
            snapshot["position_cost_basis"] = f(q(r.cost_basis_local))
            if snapshot["instrument_price"] is None and r.price is not None:
                snapshot["instrument_price"] = f(q(r.price))
                snapshot["price_currency"] = r.price_currency
    return snapshot


def _research_context(session: Session, investment: Investment) -> dict:
    """Frozen names/ids of the research state (assumptions, risks, breakers, valuation)."""
    from src.db.research import Risk, ThesisAssumption, ThesisBreaker
    from src.research.theses import active_thesis, current_version
    from src.research.valuation import models_for, scenarios_for

    ctx: dict = {"thesis_version": None, "assumptions": [], "risks": [], "breakers": [], "valuation": []}
    thesis = active_thesis(session, investment)
    if thesis:
        version = current_version(session, thesis)
        if version:
            ctx["thesis_version"] = {"id": version.id, "number": version.version_number}
        for a in session.scalars(
            select(ThesisAssumption).where(
                ThesisAssumption.thesis_id == thesis.id, ThesisAssumption.active.is_(True)
            )
        ):
            ctx["assumptions"].append({"id": a.id, "name": a.name, "status": a.status})
    for r in session.scalars(
        select(Risk).where(Risk.investment_id == investment.id, Risk.status == "OPEN")
    ):
        ctx["risks"].append({"id": r.id, "name": r.name, "severity": r.severity})
    for b in session.scalars(select(ThesisBreaker).where(ThesisBreaker.investment_id == investment.id)):
        ctx["breakers"].append({"id": b.id, "name": b.name, "status": b.status})
    for m in models_for(session, investment):
        ctx["valuation"].append(
            {
                "model": m.name,
                "reference_price": m.reference_price,
                "scenarios": [
                    {"name": s.scenario_name, "probability": s.probability, "target": s.target_price}
                    for s in scenarios_for(session, m)
                ],
            }
        )
    return ctx


def record_decision(
    session: Session,
    investment: Investment,
    decision_type: str,
    base_currency: str,
    thesis_version_id: int | None = None,
    decided_at: datetime | None = None,
    amends_decision_id: int | None = None,
    created_by: str = "USER",
    **reasoning_fields,
) -> Decision:
    if decision_type not in DECISION_TYPES:
        raise ResearchError(f"invalid decision_type {decision_type!r}; allowed: {DECISION_TYPES}")
    unknown = set(reasoning_fields) - set(REASONING_FIELDS)
    if unknown:
        raise ResearchError(f"unknown decision fields: {sorted(unknown)}")
    conf = reasoning_fields.get("confidence")
    if conf is not None and not (0 <= int(conf) <= 100):
        raise ResearchError("confidence must be 0-100")
    if thesis_version_id is None:
        from src.research.theses import active_thesis, current_version

        thesis = active_thesis(session, investment)
        if thesis:
            version = current_version(session, thesis)
            thesis_version_id = version.id if version else None

    decided_at = decided_at or utcnow()
    snap = build_decision_snapshot(session, investment, base_currency, decided_at.date())
    snap["research"] = _research_context(session, investment)

    d = Decision(
        investment_id=investment.id,
        decision_type=decision_type,
        decided_at=decided_at,
        thesis_version_id=thesis_version_id,
        amends_decision_id=amends_decision_id,
        instrument_price=snap["instrument_price"],
        price_currency=snap["price_currency"],
        portfolio_value=snap["portfolio_value"],
        cash_value=snap["cash_value"],
        position_quantity_before=snap["position_quantity"],
        position_weight_before=snap["position_weight"],
        snapshot_json=json.dumps(snap),
        created_by=created_by,
        **reasoning_fields,
    )
    session.add(d)
    session.flush()
    return d


def list_decisions(session: Session, investment: Investment | None = None) -> list[Decision]:
    stmt = select(Decision).order_by(Decision.decided_at.desc(), Decision.id.desc())
    if investment is not None:
        stmt = stmt.where(Decision.investment_id == investment.id)
    return list(session.scalars(stmt))


def decision_snapshot(decision: Decision) -> dict:
    if not decision.snapshot_json:
        return {}
    try:
        return json.loads(decision.snapshot_json)
    except json.JSONDecodeError:
        return {}
