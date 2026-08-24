"""Deterministic performance engine.

Definitions (see README for the longer explanation):
  * Simple return     = (V_end - V_start - net external flows) / (V_start + net external flows)
                        -> P/L over net contributed capital, ignores timing of contributions.
  * Time-weighted     = chain of daily returns r_t = V_t / (V_{t-1} + F_t) - 1, flows at start of
    return (TWR)        day. Independent of deposit timing -> correct for comparing to a benchmark.
  * Money-weighted    = XIRR of investor cash flows (-deposits, +withdrawals, +V_end). Measures the
    return (MWR/XIRR)   investor's actual experience including timing of contributions.

All functions are pure (lists in, numbers out) and return None when the answer cannot be
computed correctly from the data ("Unavailable").
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.core import D, ZERO
from src.db.models import CashFlow, Instrument, Transaction
from src.market_data.service import PriceStore
from src.portfolio.cash import external_flows, transaction_cash_effects
from src.portfolio.fx import FxConverter


@dataclass
class DailyPoint:
    day: date
    value: Decimal | None  # total portfolio value in base currency (None = unavailable)
    external_flow: Decimal  # net investor flow that day in base currency (None-safe: 0 if none)
    invested: Decimal | None = None
    cash: Decimal | None = None
    issues: list[str] = field(default_factory=list)


# --- pure calculations ------------------------------------------------------------


def time_weighted_return(points: list[DailyPoint]) -> Decimal | None:
    """Cumulative TWR over the points (first point is the base). None if data incomplete."""
    idx = twr_index(points)
    if not idx:
        return None
    return idx[-1][1] / Decimal(100) - 1


def twr_index(points: list[DailyPoint], start_level: Decimal = Decimal(100)) -> list[tuple[date, Decimal]]:
    """Growth-of-100 index of daily chained returns. Empty list when data is incomplete."""
    if not points:
        return []
    if any(p.value is None for p in points):
        return []
    out: list[tuple[date, Decimal]] = []
    level = start_level
    prev_value: Decimal | None = None
    for p in points:
        if prev_value is None:
            # first day: a flow on this day is treated as initial capital
            out.append((p.day, level))
            prev_value = p.value
            continue
        denom = prev_value + p.external_flow
        if denom > 0:
            r = p.value / denom - 1
            level = level * (1 + r)
        elif denom == 0 and p.value == 0:
            pass  # empty portfolio; level unchanged
        else:
            return []  # negative capital base: return is not meaningful
        out.append((p.day, level))
        prev_value = p.value
    return out


def simple_return(points: list[DailyPoint]) -> Decimal | None:
    """P/L over contributed capital. V_0 (end of first day) is the starting capital; flows on
    later days are added to the capital base without time weighting."""
    if not points or points[0].value is None or points[-1].value is None:
        return None
    v_start = points[0].value
    flows = sum((p.external_flow for p in points[1:]), ZERO)
    base = v_start + flows
    if base <= 0:
        return None
    return (points[-1].value - v_start - flows) / base


def total_pnl(points: list[DailyPoint]) -> Decimal | None:
    """V_end - V_0 - net external flows after day 0."""
    if not points or points[0].value is None or points[-1].value is None:
        return None
    flows = sum((p.external_flow for p in points[1:]), ZERO)
    return points[-1].value - points[0].value - flows


def xirr(cashflows: list[tuple[date, Decimal]], tol: Decimal = Decimal("0.0000001")) -> Decimal | None:
    """Annualized money-weighted return by bisection. cashflows from the investor's view:
    deposits negative, withdrawals and final value positive. None if no solution/sign change."""
    flows = [(d, D(a)) for d, a in cashflows if a != 0]
    if len(flows) < 2:
        return None
    if not (any(a > 0 for _, a in flows) and any(a < 0 for _, a in flows)):
        return None
    t0 = min(d for d, _ in flows)

    def npv(rate: Decimal) -> Decimal:
        total = ZERO
        for d, a in flows:
            years = Decimal((d - t0).days) / Decimal(365)
            total += a / ((1 + rate) ** years)
        return total

    lo, hi = Decimal("-0.9999"), Decimal("10")
    f_lo, f_hi = npv(lo), npv(hi)
    if f_lo * f_hi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        f_mid = npv(mid)
        if abs(f_mid) < tol or (hi - lo) < tol:
            return mid
        if f_lo * f_mid < 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2


def money_weighted_return(points: list[DailyPoint]) -> Decimal | None:
    """Annualized XIRR from the investor's view: -V_0, -flows, +V_end. Needs >= 1 day span."""
    if not points or points[0].value is None or points[-1].value is None:
        return None
    if (points[-1].day - points[0].day).days < 1:
        return None
    flows: list[tuple[date, Decimal]] = [(points[0].day, -points[0].value)]
    for p in points[1:]:
        if p.external_flow != 0:
            flows.append((p.day, -p.external_flow))
    flows.append((points[-1].day, points[-1].value))
    return xirr(flows)


def drawdown_series(index: list[tuple[date, Decimal]]) -> list[tuple[date, Decimal]]:
    out: list[tuple[date, Decimal]] = []
    peak: Decimal | None = None
    for d, level in index:
        peak = level if peak is None or level > peak else peak
        out.append((d, (level / peak - 1) if peak else ZERO))
    return out


def max_drawdown(index: list[tuple[date, Decimal]]) -> Decimal | None:
    dd = drawdown_series(index)
    if not dd:
        return None
    return min(v for _, v in dd)


def normalize_to_100(series: list[tuple[date, Decimal]]) -> list[tuple[date, Decimal]]:
    if not series:
        return []
    base = series[0][1]
    if base == 0:
        return []
    return [(d, v / base * 100) for d, v in series]


# --- DB-backed history -------------------------------------------------------------


def build_value_history(
    session: Session, base_currency: str, start: date | None = None, end: date | None = None
) -> list[DailyPoint]:
    """Replay holdings and cash day by day and value them with cached prices + FX."""
    end = end or date.today()
    fx = FxConverter(session, base_currency)
    prices = PriceStore(session)
    instruments = {i.id: i for i in session.scalars(select(Instrument))}

    txs = list(session.scalars(select(Transaction).order_by(Transaction.trade_date, Transaction.id)))
    cfs = list(session.scalars(select(CashFlow).order_by(CashFlow.flow_date, CashFlow.id)))
    if not txs and not cfs:
        return []
    first = min([t.trade_date for t in txs] + [c.flow_date for c in cfs])
    ext = external_flows(session)

    # state
    holdings: dict[tuple[int, int], Decimal] = {}
    cash: dict[str, Decimal] = {}
    ti = ci = 0
    points: list[DailyPoint] = []
    day = first
    while day <= end:
        while ti < len(txs) and txs[ti].trade_date <= day:
            t = txs[ti]
            ti += 1
            inst = instruments.get(t.instrument_id) if t.instrument_id is not None else None
            is_cash_pair = inst is not None and inst.asset_type == "cash"
            if inst is not None and not is_cash_pair and t.transaction_type in ("buy", "sell"):
                key = (t.account_id, t.instrument_id)
                holdings[key] = holdings.get(key, ZERO) + D(t.quantity)
            for ccy, amount in transaction_cash_effects(t, inst.symbol if is_cash_pair else None):
                cash[ccy] = cash.get(ccy, ZERO) + amount
        while ci < len(cfs) and cfs[ci].flow_date <= day:
            c = cfs[ci]
            ci += 1
            cash[c.currency] = cash.get(c.currency, ZERO) + D(c.amount)

        if start is None or day >= start:
            issues: list[str] = []
            invested: Decimal | None = ZERO
            for (acc, inst_id), qty in holdings.items():
                if abs(qty) < Decimal("0.000000005"):
                    continue
                inst = instruments[inst_id]
                if inst.asset_type in ("option", "other"):
                    issues.append(f"{inst.symbol}: unsupported asset type")
                    invested = None
                    continue
                found = prices.close_on(inst_id, day)
                rate = fx.rate(found.currency, base_currency, day) if found else None
                if found is None or rate is None:
                    issues.append(f"{inst.symbol}: missing {'price' if found is None else 'FX'} on {day}")
                    invested = None
                    continue
                if invested is not None:
                    invested += qty * found.close * rate
            cash_base: Decimal | None = ZERO
            for ccy, amt in cash.items():
                if amt == 0:
                    continue
                conv = fx.convert(amt, ccy, day, base_currency)
                if conv is None:
                    issues.append(f"cash {ccy}: missing FX on {day}")
                    cash_base = None
                    break
                cash_base += conv
            flow = ZERO
            flow_ok = True
            for d, _acc, ccy, amt in ext:
                if d == day:
                    conv = fx.convert(amt, ccy, day, base_currency)
                    if conv is None:
                        flow_ok = False
                        issues.append(f"external flow {ccy}: missing FX on {day}")
                    else:
                        flow += conv
            value = invested + cash_base if (invested is not None and cash_base is not None and flow_ok) else None
            points.append(
                DailyPoint(day=day, value=value, external_flow=flow, invested=invested, cash=cash_base, issues=issues)
            )
        day += timedelta(days=1)
    return points


def benchmark_series(
    session: Session, instrument_id: int, start: date, end: date, base_currency: str | None = None
) -> tuple[list[tuple[date, Decimal]], str]:
    """Daily close series of a benchmark, forward-filled on calendar days.
    If base_currency is given and FX exists for EVERY day, the whole series is converted to base;
    otherwise the whole series stays in the benchmark's local currency (never mixed)."""
    prices = PriceStore(session)
    inst = session.get(Instrument, instrument_id)
    if inst is None:
        return [], ""
    local: list[tuple[date, Decimal]] = []
    day = start
    while day <= end:
        found = prices.close_on(instrument_id, day)
        if found is not None:
            local.append((day, found.close))
        day += timedelta(days=1)
    if not base_currency or base_currency == inst.currency or not local:
        return local, inst.currency
    fx = FxConverter(session, base_currency)
    converted: list[tuple[date, Decimal]] = []
    for d, v in local:
        r = fx.rate(inst.currency, base_currency, d)
        if r is None:
            return local, inst.currency
        converted.append((d, v * r))
    return converted, base_currency
