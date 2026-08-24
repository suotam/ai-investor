from __future__ import annotations

from datetime import date
from decimal import Decimal

from src.analytics.performance import (
    DailyPoint,
    drawdown_series,
    max_drawdown,
    money_weighted_return,
    normalize_to_100,
    simple_return,
    time_weighted_return,
    total_pnl,
    twr_index,
    xirr,
)

D = Decimal


def p(day: str, value: str | None, flow: str = "0") -> DailyPoint:
    return DailyPoint(day=date.fromisoformat(day), value=D(value) if value is not None else None, external_flow=D(flow))


def test_twr_ignores_deposit_timing() -> None:
    # Day0: deposit 1000 -> value 1000. Day1: +10% -> 1100. Day2: deposit 1000 at start, then -5%:
    # (1100 + 1000) * 0.95 = 1995. TWR = 1.10 * 0.95 - 1 = 4.5%
    pts = [p("2024-01-01", "1000", "1000"), p("2024-01-02", "1100"), p("2024-01-03", "1995", "1000")]
    assert time_weighted_return(pts) == D("0.045")
    idx = twr_index(pts)
    assert [round(v, 6) for _, v in idx] == [D("100"), D("110"), D("104.5")]


def test_simple_return_and_total_pnl() -> None:
    pts = [p("2024-01-01", "1000", "1000"), p("2024-01-02", "1100"), p("2024-01-03", "1995", "1000")]
    # P/L = 1995 - 1000 - 1000 = -5 ; capital = 2000 -> -0.25%
    assert total_pnl(pts) == D("-5")
    assert simple_return(pts) == D("-5") / D("2000")


def test_twr_unavailable_when_any_day_missing() -> None:
    pts = [p("2024-01-01", "1000", "1000"), p("2024-01-02", None), p("2024-01-03", "1200")]
    assert time_weighted_return(pts) is None
    assert twr_index(pts) == []
    assert simple_return(pts) == D("0.2")  # endpoints available


def test_twr_with_full_withdrawal_then_empty() -> None:
    pts = [p("2024-01-01", "1000", "1000"), p("2024-01-02", "1200"), p("2024-01-03", "0", "-1200"), p("2024-01-04", "0")]
    assert time_weighted_return(pts) == D("0.2")


def test_xirr_known_values() -> None:
    # -1000 today, +1100 in exactly one year -> 10%
    r = xirr([(date(2024, 1, 1), D("-1000")), (date(2024, 12, 31), D("1100"))])
    assert abs(r - D("0.10")) < D("0.0005")
    # two deposits: -1000 at t0, -1000 at 6 months, +2200 at 1 year
    r = xirr([(date(2024, 1, 1), D("-1000")), (date(2024, 7, 1), D("-1000")), (date(2024, 12, 31), D("2200"))])
    # check by NPV ~ 0
    t0 = date(2024, 1, 1)
    npv = sum(a / (1 + r) ** (D((d - t0).days) / 365) for d, a in [(date(2024, 1, 1), D("-1000")), (date(2024, 7, 1), D("-1000")), (date(2024, 12, 31), D("2200"))])
    assert abs(npv) < D("0.001")
    assert xirr([(date(2024, 1, 1), D("-1000"))]) is None
    assert xirr([(date(2024, 1, 1), D("-1000")), (date(2024, 6, 1), D("-5"))]) is None


def test_money_weighted_return_from_points() -> None:
    pts = [p("2024-01-01", "1000", "1000"), p("2024-12-31", "1100")]
    r = money_weighted_return(pts)
    assert abs(r - D("0.10")) < D("0.0005")
    assert money_weighted_return([p("2024-01-01", "1000", "1000")]) is None


def test_drawdown() -> None:
    idx = [(date(2024, 1, i), D(v)) for i, v in enumerate(["100", "110", "99", "105", "120"], start=1)]
    dd = drawdown_series(idx)
    assert dd[2][1] == D("99") / D("110") - 1
    assert dd[4][1] == 0
    assert max_drawdown(idx) == D("99") / D("110") - 1
    assert max_drawdown([]) is None


def test_normalize_to_100() -> None:
    s = [(date(2024, 1, 1), D("50")), (date(2024, 1, 2), D("55"))]
    assert normalize_to_100(s) == [(date(2024, 1, 1), D("100")), (date(2024, 1, 2), D("110"))]
    assert normalize_to_100([]) == []
