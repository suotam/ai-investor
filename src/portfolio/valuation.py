"""Portfolio valuation in base currency. Every unavailable component is None - never guessed."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.core import D, ZERO, f, q, utcnow
from src.db.models import Account, Instrument, PortfolioSnapshot, Position
from src.logging_setup import get_logger
from src.market_data.service import PriceStore
from src.portfolio.cash import cash_balances, external_flows
from src.portfolio.fx import FxConverter
from src.portfolio.positions import QTY_EPS

log = get_logger("valuation")


@dataclass
class PositionValuation:
    account_id: int
    account_name: str
    instrument_id: int
    symbol: str
    name: str | None
    asset_type: str
    sector: str | None
    currency: str
    quantity: Decimal
    average_cost: Decimal | None
    cost_basis_local: Decimal | None
    price: Decimal | None
    price_currency: str
    price_date: date | None
    fx_rate: Decimal | None  # local -> base
    market_value_local: Decimal | None
    market_value_base: Decimal | None
    cost_basis_base: Decimal | None
    unrealized_pnl_local: Decimal | None
    unrealized_pnl_base: Decimal | None
    realized_pnl_local: Decimal
    realized_pnl_base: Decimal | None
    return_pct: Decimal | None
    weight: Decimal | None = None
    issues: list[str] = field(default_factory=list)


@dataclass
class PortfolioValuation:
    as_of: date
    base_currency: str
    positions: list[PositionValuation]
    cash_by_currency: dict[str, Decimal]
    cash_base: Decimal | None
    invested_value_base: Decimal | None
    cost_basis_base: Decimal | None
    unrealized_pnl_base: Decimal | None
    realized_pnl_base: Decimal | None
    total_value_base: Decimal | None
    net_external_flows_base: Decimal | None
    incomplete: bool
    issues: list[str]

    @property
    def positions_count(self) -> int:
        return len(self.positions)


def value_portfolio(
    session: Session, base_currency: str, as_of: date | None = None, account_id: int | None = None
) -> PortfolioValuation:
    as_of = as_of or date.today()
    fx = FxConverter(session, base_currency)
    prices = PriceStore(session)
    issues: list[str] = []

    stmt = select(Position).where(Position.quantity != 0)
    if account_id is not None:
        stmt = stmt.where(Position.account_id == account_id)
    positions = list(session.scalars(stmt))
    instruments = {i.id: i for i in session.scalars(select(Instrument))}
    accounts = {a.id: a for a in session.scalars(select(Account))}

    rows: list[PositionValuation] = []
    for p in positions:
        if abs(D(p.quantity)) <= QTY_EPS:
            continue
        inst = instruments[p.instrument_id]
        qty = D(p.quantity)
        avg = D(p.average_cost) if p.average_cost is not None else None
        cost_local = D(p.cost_basis) if p.cost_basis is not None else None
        realized_local = D(p.realized_pnl)
        row_issues: list[str] = []

        px = None
        px_date = None
        px_ccy = inst.currency
        if inst.asset_type in ("option", "other"):
            row_issues.append("valuation unsupported for asset type")
        else:
            found = prices.close_on(inst.id, as_of)
            if found:
                px, px_date, px_ccy = found.close, found.price_date, found.currency
            else:
                row_issues.append("no price")
        rate = fx.rate(inst.currency, base_currency, as_of)  # instrument ccy -> base (cost basis, realized)
        if rate is None:
            row_issues.append(f"no FX {inst.currency}->{base_currency}")
        px_rate = fx.rate(px_ccy, base_currency, as_of) if px is not None else None  # price ccy -> base
        if px is not None and px_rate is None:
            row_issues.append(f"no FX {px_ccy}->{base_currency}")

        mv_base = qty * px * px_rate if (px is not None and px_rate is not None) else None
        # market value expressed in the instrument (cost basis) currency
        if px is not None and px_ccy == inst.currency:
            mv_local = qty * px
        else:
            mv_local = (mv_base / rate) if (mv_base is not None and rate not in (None, ZERO)) else None
        cost_base = cost_local * rate if (cost_local is not None and rate is not None) else None
        upl_local = (mv_local - cost_local) if (mv_local is not None and cost_local is not None) else None
        upl_base = (mv_base - cost_base) if (mv_base is not None and cost_base is not None) else None
        realized_base = realized_local * rate if rate is not None else None
        ret = None
        if upl_local is not None and cost_local not in (None, ZERO):
            ret = upl_local / abs(cost_local)

        rows.append(
            PositionValuation(
                account_id=p.account_id,
                account_name=accounts[p.account_id].name,
                instrument_id=inst.id,
                symbol=inst.symbol,
                name=inst.name,
                asset_type=inst.asset_type,
                sector=inst.sector,
                currency=inst.currency,
                quantity=qty,
                average_cost=avg,
                cost_basis_local=cost_local,
                price=px,
                price_currency=px_ccy,
                price_date=px_date,
                fx_rate=rate,
                market_value_local=mv_local,
                market_value_base=mv_base,
                cost_basis_base=cost_base,
                unrealized_pnl_local=upl_local,
                unrealized_pnl_base=upl_base,
                realized_pnl_local=realized_local,
                realized_pnl_base=realized_base,
                return_pct=ret,
                issues=row_issues,
            )
        )
        for i in row_issues:
            issues.append(f"{inst.symbol}: {i}")

    # Realized P/L also from closed positions (quantity == 0)
    closed_stmt = select(Position).where(Position.quantity == 0)
    if account_id is not None:
        closed_stmt = closed_stmt.where(Position.account_id == account_id)
    realized_closed_base: Decimal | None = ZERO
    for p in session.scalars(closed_stmt):
        inst = instruments[p.instrument_id]
        rate = fx.rate(inst.currency, base_currency, as_of)
        if rate is None:
            realized_closed_base = None
            issues.append(f"{inst.symbol}: no FX for realized P/L")
            break
        realized_closed_base += D(p.realized_pnl) * rate

    # Cash
    balances = cash_balances(session, as_of)
    cash_by_ccy: dict[str, Decimal] = {}
    for (acc, ccy), amt in balances.items():
        if account_id is not None and acc != account_id:
            continue
        cash_by_ccy[ccy] = cash_by_ccy.get(ccy, ZERO) + amt
    cash_base: Decimal | None = ZERO
    for ccy, amt in cash_by_ccy.items():
        conv = fx.convert(amt, ccy, as_of, base_currency)
        if conv is None:
            cash_base = None
            issues.append(f"cash {ccy}: no FX")
            break
        cash_base += conv

    invested = _sum_or_none([r.market_value_base for r in rows])
    cost_total = _sum_or_none([r.cost_basis_base for r in rows])
    upl_total = _sum_or_none([r.unrealized_pnl_base for r in rows])
    realized_open = _sum_or_none([r.realized_pnl_base for r in rows])
    realized_total = (
        realized_open + realized_closed_base
        if (realized_open is not None and realized_closed_base is not None)
        else None
    )
    total = invested + cash_base if (invested is not None and cash_base is not None) else None

    if invested not in (None, ZERO):
        for r in rows:
            r.weight = (r.market_value_base / invested) if r.market_value_base is not None else None

    net_flows: Decimal | None = ZERO
    for d, acc, ccy, amt in external_flows(session, account_id):
        if d > as_of:
            continue
        conv = fx.convert(amt, ccy, d, base_currency)
        if conv is None:
            net_flows = None
            issues.append(f"external flow {d} {ccy}: no FX")
            break
        net_flows += conv

    return PortfolioValuation(
        as_of=as_of,
        base_currency=base_currency,
        positions=rows,
        cash_by_currency=cash_by_ccy,
        cash_base=cash_base,
        invested_value_base=invested,
        cost_basis_base=cost_total,
        unrealized_pnl_base=upl_total,
        realized_pnl_base=realized_total,
        total_value_base=total,
        net_external_flows_base=net_flows,
        incomplete=bool(issues),
        issues=issues,
    )


def _sum_or_none(values: list[Decimal | None]) -> Decimal | None:
    if not values:
        return ZERO
    if any(v is None for v in values):
        return None
    return sum(values, ZERO)


def create_snapshot(session: Session, base_currency: str, as_of: date | None = None) -> PortfolioSnapshot:
    """Create or replace the consolidated snapshot for `as_of`. Idempotent per date."""
    val = value_portfolio(session, base_currency, as_of)
    existing = session.scalars(
        select(PortfolioSnapshot).where(
            PortfolioSnapshot.snapshot_date == val.as_of, PortfolioSnapshot.account_id.is_(None)
        )
    ).first()
    snap = existing or PortfolioSnapshot(snapshot_date=val.as_of, account_id=None, base_currency=base_currency)
    snap.base_currency = base_currency
    snap.account_value = f(q(val.total_value_base))
    snap.cash = f(q(val.cash_base))
    snap.invested_value = f(q(val.invested_value_base))
    snap.cost_basis = f(q(val.cost_basis_base))
    snap.unrealized_pnl = f(q(val.unrealized_pnl_base))
    snap.realized_pnl = f(q(val.realized_pnl_base))
    snap.net_external_flows_to_date = f(q(val.net_external_flows_base))
    snap.positions_count = val.positions_count
    snap.incomplete = val.incomplete
    snap.details = json.dumps(
        {
            "issues": val.issues,
            "cash_by_currency": {k: str(q(v)) for k, v in val.cash_by_currency.items()},
            "positions": [
                {
                    "symbol": r.symbol,
                    "account_id": r.account_id,
                    "quantity": str(q(r.quantity)),
                    "price": str(q(r.price)) if r.price is not None else None,
                    "price_date": r.price_date.isoformat() if r.price_date else None,
                    "market_value_base": str(q(r.market_value_base)) if r.market_value_base is not None else None,
                }
                for r in val.positions
            ],
        }
    )
    snap.created_at = utcnow()
    if existing is None:
        session.add(snap)
    session.flush()
    log.info(
        "snapshot %s: value=%s cash=%s invested=%s incomplete=%s",
        val.as_of,
        snap.account_value,
        snap.cash,
        snap.invested_value,
        snap.incomplete,
    )
    return snap


def valuation_to_dicts(val: PortfolioValuation) -> list[dict]:
    out = []
    for r in val.positions:
        d = asdict(r)
        out.append(d)
    return out
