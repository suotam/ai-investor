"""Investment lifecycle: research entities independent of (but linkable to) holdings."""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.core import utcnow
from src.db.models import Instrument
from src.db.research import INVESTMENT_STATUSES, REVIEW_FREQUENCIES, Investment

REVIEW_INTERVALS = {
    "monthly": 30,
    "quarterly": 91,
    "semiannual": 182,
    "annual": 365,
    # after_earnings / manual: no automatic interval; next_review_date is set explicitly
}


class ResearchError(ValueError):
    pass


def create_investment(
    session: Session,
    ticker: str,
    name: str | None = None,
    status: str = "DISCOVERED",
    instrument_id: int | None = None,
    review_frequency: str = "quarterly",
    next_review_date: date | None = None,
    notes: str | None = None,
    created_by: str = "USER",
) -> Investment:
    ticker = ticker.strip().upper()
    if not ticker:
        raise ResearchError("ticker is required")
    if status not in INVESTMENT_STATUSES:
        raise ResearchError(f"invalid status {status!r}; allowed: {INVESTMENT_STATUSES}")
    if review_frequency not in REVIEW_FREQUENCIES:
        raise ResearchError(f"invalid review_frequency {review_frequency!r}")
    if session.scalars(select(Investment).where(Investment.ticker == ticker)).first():
        raise ResearchError(f"investment {ticker} already exists")
    if instrument_id is None:
        # best-effort link to an existing portfolio instrument by symbol (never required)
        inst = session.scalars(
            select(Instrument).where(Instrument.symbol == ticker, Instrument.asset_type != "cash")
        ).first()
        instrument_id = inst.id if inst else None
        if name is None and inst is not None:
            name = inst.name
    inv = Investment(
        ticker=ticker,
        name=name,
        status=status,
        instrument_id=instrument_id,
        review_frequency=review_frequency,
        next_review_date=next_review_date or _default_next_review(review_frequency),
        notes=notes,
        created_by=created_by,
    )
    session.add(inv)
    session.flush()
    return inv


def _default_next_review(frequency: str, from_date: date | None = None) -> date | None:
    days = REVIEW_INTERVALS.get(frequency)
    if days is None:
        return None
    return (from_date or date.today()) + timedelta(days=days)


def set_status(session: Session, investment: Investment, status: str) -> Investment:
    if status not in INVESTMENT_STATUSES:
        raise ResearchError(f"invalid status {status!r}; allowed: {INVESTMENT_STATUSES}")
    investment.status = status
    investment.updated_at = utcnow()
    session.flush()
    return investment


def mark_reviewed(session: Session, investment: Investment, on: date | None = None) -> Investment:
    on = on or date.today()
    investment.last_review_date = on
    investment.next_review_date = _default_next_review(investment.review_frequency, on)
    investment.updated_at = utcnow()
    session.flush()
    return investment


def get_by_ticker(session: Session, ticker: str) -> Investment | None:
    return session.scalars(select(Investment).where(Investment.ticker == ticker.strip().upper())).first()


def list_investments(session: Session, status: str | None = None) -> list[Investment]:
    stmt = select(Investment).order_by(Investment.ticker)
    if status:
        stmt = stmt.where(Investment.status == status)
    return list(session.scalars(stmt))


def portfolio_link(val, investment: Investment) -> dict | None:
    """Read-only view of the position for a linked instrument, taken from an existing
    PortfolioValuation (v1 engine output). Returns None when not currently held.
    Never copies accounting data into research tables."""
    if investment.instrument_id is None:
        return None
    for r in val.positions:
        if r.instrument_id == investment.instrument_id:
            return {
                "quantity": r.quantity,
                "currency": r.currency,
                "price": r.price,
                "price_currency": r.price_currency,
                "price_date": r.price_date,
                "market_value_base": r.market_value_base,
                "weight": r.weight,
                "cost_basis_local": r.cost_basis_local,
                "average_cost": r.average_cost,
                "unrealized_pnl_base": r.unrealized_pnl_base,
                "realized_pnl_local": r.realized_pnl_local,
            }
    return None
