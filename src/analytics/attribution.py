"""Simple performance attribution per instrument (base currency).

Provides P/L per instrument (realized + unrealized), allocation weights and a contribution
to total return defined as  pnl_i / (V_0 + net external flows)  - i.e. contribution to the
*simple* return over the whole history. A time-weighted contribution is NOT computed in v1
(it would require daily per-position returns and weights; see README TODO).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from src.core import ZERO
from src.portfolio.valuation import PortfolioValuation


@dataclass
class InstrumentAttribution:
    symbol: str
    name: str | None
    currency: str
    weight: Decimal | None
    unrealized_pnl_base: Decimal | None
    realized_pnl_base: Decimal | None
    total_pnl_base: Decimal | None
    contribution_to_simple_return: Decimal | None


def attribution(val: PortfolioValuation, capital_base: Decimal | None) -> list[InstrumentAttribution]:
    """capital_base = V_0 + net external flows after day 0 (from the performance engine), or None."""
    out: list[InstrumentAttribution] = []
    for r in val.positions:
        total = (
            r.unrealized_pnl_base + r.realized_pnl_base
            if (r.unrealized_pnl_base is not None and r.realized_pnl_base is not None)
            else None
        )
        contrib = None
        if total is not None and capital_base not in (None, ZERO) and capital_base > 0:
            contrib = total / capital_base
        out.append(
            InstrumentAttribution(
                symbol=r.symbol,
                name=r.name,
                currency=r.currency,
                weight=r.weight,
                unrealized_pnl_base=r.unrealized_pnl_base,
                realized_pnl_base=r.realized_pnl_base,
                total_pnl_base=total,
                contribution_to_simple_return=contrib,
            )
        )
    out.sort(key=lambda a: (a.total_pnl_base is None, -(a.total_pnl_base or ZERO)))
    return out
