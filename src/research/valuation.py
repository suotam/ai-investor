"""Valuation framework: multiple models per investment, scenario storage and DETERMINISTIC
calculations (Decimal). The LLM never computes these numbers.

Definitions:
  * reference price  - the current price the scenarios are compared against
  * target price     - scenario end-of-horizon price
  * expected return  - (target + explicit dividends) / reference - 1
  * annualized       - (1+r)^(12/months) - 1 (only when a horizon is given)
  * weighted target  - sum(p_i * target_i) with probabilities summing to ~100
  * margin of safety - (fair - reference) / fair (vs base scenario and vs weighted fair)
Margin of safety is information, never an automatic BUY signal.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.core import D, ZERO, utcnow
from src.db.research import (
    VALUATION_MODEL_TYPES,
    Investment,
    ValuationModel,
    ValuationScenario,
)
from src.research.investments import ResearchError

PROBABILITY_TOLERANCE = D("0.5")  # scenario probabilities must sum to 100 +/- this


def create_model(
    session: Session, investment: Investment, name: str, model_type: str = "custom",
    reference_price: Decimal | float | None = None, reference_currency: str | None = None,
    reference_date: date | None = None, notes: str | None = None, created_by: str = "USER",
) -> ValuationModel:
    if model_type not in VALUATION_MODEL_TYPES:
        raise ResearchError(f"invalid model_type {model_type!r}; allowed: {VALUATION_MODEL_TYPES}")
    m = ValuationModel(
        investment_id=investment.id, name=name, model_type=model_type,
        reference_price=float(reference_price) if reference_price is not None else None,
        reference_currency=reference_currency, reference_date=reference_date,
        notes=notes, created_by=created_by,
    )
    session.add(m)
    session.flush()
    return m


def add_scenario(
    session: Session, model: ValuationModel, scenario_name: str, target_price: Decimal | float,
    probability: Decimal | float | None = None, currency: str | None = None,
    time_horizon_months: int | None = None, expected_dividends: Decimal | float | None = None,
    assumptions_json: str | None = None, notes: str | None = None,
) -> ValuationScenario:
    if probability is not None and not (0 <= float(probability) <= 100):
        raise ResearchError("scenario probability must be 0-100")
    if float(target_price) <= 0:
        raise ResearchError("target_price must be positive")
    s = ValuationScenario(
        model_id=model.id, scenario_name=scenario_name.strip().lower(),
        probability=float(probability) if probability is not None else None,
        target_price=float(target_price), currency=currency or model.reference_currency,
        time_horizon_months=time_horizon_months,
        expected_dividends=float(expected_dividends) if expected_dividends is not None else None,
        assumptions_json=assumptions_json, notes=notes,
    )
    session.add(s)
    session.flush()
    model.updated_at = utcnow()
    return s


def validate_probabilities(scenarios: list[ValuationScenario]) -> None:
    """Reject probability sets that do not sum to ~100 (only when all scenarios carry one)."""
    probs = [s.probability for s in scenarios]
    if not probs or any(p is None for p in probs):
        return  # weighting simply unavailable
    total = sum(D(p) for p in probs)
    if abs(total - 100) > PROBABILITY_TOLERANCE:
        raise ResearchError(f"scenario probabilities sum to {total}, must be 100 +/- {PROBABILITY_TOLERANCE}")


# --- pure calculations -------------------------------------------------------


@dataclass
class ScenarioResult:
    scenario_name: str
    probability: Decimal | None  # 0-100
    target_price: Decimal
    expected_dividends: Decimal
    expected_return: Decimal | None  # vs reference
    annualized_return: Decimal | None
    time_horizon_months: int | None


@dataclass
class ValuationSummary:
    reference_price: Decimal | None
    currency: str | None
    scenarios: list[ScenarioResult]
    weighted_target: Decimal | None
    weighted_return: Decimal | None
    weighted_annualized: Decimal | None
    base_fair_value: Decimal | None
    margin_of_safety_base: Decimal | None
    margin_of_safety_weighted: Decimal | None


def scenario_return(reference: Decimal, target: Decimal, dividends: Decimal = ZERO) -> Decimal:
    if reference <= 0:
        raise ResearchError("reference price must be positive")
    return (target + dividends) / reference - 1


def annualize(total_return: Decimal, months: int) -> Decimal | None:
    if months is None or months <= 0:
        return None
    base = 1 + total_return
    if base <= 0:
        return None  # total loss; annualization not meaningful
    return D(float(base) ** (12.0 / months)) - 1


def summarize_model(model: ValuationModel, scenarios: list[ValuationScenario]) -> ValuationSummary:
    """Deterministic summary. Anything not computable is None - never guessed."""
    validate_probabilities(scenarios)
    ref = D(model.reference_price) if model.reference_price is not None else None
    results: list[ScenarioResult] = []
    for s in scenarios:
        target = D(s.target_price)
        divs = D(s.expected_dividends) if s.expected_dividends is not None else ZERO
        ret = scenario_return(ref, target, divs) if ref is not None else None
        ann = annualize(ret, s.time_horizon_months) if (ret is not None and s.time_horizon_months) else None
        results.append(
            ScenarioResult(
                scenario_name=s.scenario_name,
                probability=D(s.probability) if s.probability is not None else None,
                target_price=target,
                expected_dividends=divs,
                expected_return=ret,
                annualized_return=ann,
                time_horizon_months=s.time_horizon_months,
            )
        )

    weighted_target = weighted_return = weighted_ann = None
    if results and all(r.probability is not None for r in results):
        weighted_target = sum((r.probability / 100) * (r.target_price + r.expected_dividends) for r in results)
        if ref is not None:
            weighted_return = weighted_target / ref - 1
            horizons = {r.time_horizon_months for r in results}
            if len(horizons) == 1 and (h := horizons.pop()):
                weighted_ann = annualize(weighted_return, h)

    base = next((r for r in results if r.scenario_name == "base"), None)
    base_fair = (base.target_price + base.expected_dividends) if base else None
    mos_base = mos_weighted = None
    if ref is not None and base_fair is not None and base_fair > 0:
        mos_base = (base_fair - ref) / base_fair
    if ref is not None and weighted_target is not None and weighted_target > 0:
        mos_weighted = (weighted_target - ref) / weighted_target

    return ValuationSummary(
        reference_price=ref,
        currency=model.reference_currency,
        scenarios=results,
        weighted_target=weighted_target,
        weighted_return=weighted_return,
        weighted_annualized=weighted_ann,
        base_fair_value=base_fair,
        margin_of_safety_base=mos_base,
        margin_of_safety_weighted=mos_weighted,
    )


def models_for(session: Session, investment: Investment, active_only: bool = True) -> list[ValuationModel]:
    stmt = select(ValuationModel).where(ValuationModel.investment_id == investment.id)
    if active_only:
        stmt = stmt.where(ValuationModel.active.is_(True))
    return list(session.scalars(stmt.order_by(ValuationModel.id)))


def scenarios_for(session: Session, model: ValuationModel) -> list[ValuationScenario]:
    return list(
        session.scalars(
            select(ValuationScenario).where(ValuationScenario.model_id == model.id).order_by(ValuationScenario.id)
        )
    )


def set_reference_price(
    session: Session, model: ValuationModel, price: Decimal | float, currency: str | None = None,
    on: date | None = None,
) -> ValuationModel:
    if float(price) <= 0:
        raise ResearchError("reference price must be positive")
    model.reference_price = float(price)
    if currency:
        model.reference_currency = currency
    model.reference_date = on or date.today()
    model.updated_at = utcnow()
    session.flush()
    return model
