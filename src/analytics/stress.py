"""Deterministic scenario stress tests: mechanical market-value transformations only.

A shock spec maps keys to percentage changes:
  * "<TICKER>_price": shock to that instrument's price (e.g. "NU_price": -25)
  * "<CCY>/<BASE>" or "<CCY>": shock to the FX rate of that currency vs base ("USD/CZK": +10)
  * "ALL_EQUITIES": shock applied to every stock/etf position
Result = portfolio value under shocked prices/FX vs current value. This is a MECHANICAL
stress test, never a forecast; fundamental impact is out of scope unless explicit
sensitivities exist (they do not in v5 - documented).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal

import yaml
from sqlalchemy.orm import Session

from src.config import Settings
from src.core import D
from src.db.operations import ScenarioRun
from src.portfolio.valuation import value_portfolio
from src.research.investments import ResearchError

BUILTIN_SCENARIOS = [
    {"name": "USD/CZK -10%", "shocks": {"USD": -10}},
    {"name": "USD/CZK +10%", "shocks": {"USD": +10}},
    {"name": "Rates +200bps equity derating -15%", "shocks": {"ALL_EQUITIES": -15}},
    {"name": "Nasdaq -20%", "shocks": {"ALL_EQUITIES": -20}},
    {"name": "LATAM credit shock", "shocks": {"NU_price": -25, "USD": +5}},
    {"name": "BTC -40%", "shocks": {"BTC_price": -40}},
]


@dataclass
class StressResult:
    name: str
    base_value: float
    stressed_value: float
    impact_abs: float
    impact_pct: float
    per_position: dict
    shocks: dict


def _parse_shock_key(key: str) -> tuple[str, str]:
    """Returns (kind, subject): ('price', TICKER) | ('fx', CCY) | ('all_equities', '')."""
    if key.upper() == "ALL_EQUITIES":
        return "all_equities", ""
    if key.endswith("_price"):
        return "price", key[: -len("_price")].upper()
    ccy = key.split("/")[0].upper()
    if len(ccy) == 3 and ccy.isalpha():
        return "fx", ccy
    raise ResearchError(f"unrecognized shock key {key!r} (use TICKER_price, CCY[/BASE], ALL_EQUITIES)")


def run_stress(session: Session, settings: Settings, name: str, shocks: dict[str, float]) -> StressResult:
    val = value_portfolio(session, settings.base_currency)
    if val.total_value_base is None:
        raise ResearchError("portfolio cannot be valued (missing prices/FX) - stress test unavailable")
    parsed = [( *_parse_shock_key(k), float(v)) for k, v in shocks.items()]

    base_total = float(val.total_value_base)
    stressed_total = float(val.cash_base or 0)
    per_position: dict[str, dict] = {}

    # cash FX shocks
    for ccy, amount in val.cash_by_currency.items():
        for kind, subject, pct in parsed:
            if kind == "fx" and subject == ccy.upper():
                from src.portfolio.fx import FxConverter
                from datetime import date

                conv = FxConverter(session, settings.base_currency).convert(amount, ccy, date.today())
                if conv is not None:
                    stressed_total += float(conv) * (pct / 100)

    for r in val.positions:
        if r.market_value_base is None:
            continue
        mv = float(r.market_value_base)
        factor = 1.0
        applied = []
        for kind, subject, pct in parsed:
            if kind == "price" and subject == r.symbol.upper():
                factor *= 1 + pct / 100
                applied.append(f"price {pct:+g}%")
            elif kind == "all_equities" and r.asset_type in ("stock", "etf"):
                factor *= 1 + pct / 100
                applied.append(f"equities {pct:+g}%")
            elif kind == "fx" and subject == (r.price_currency or r.currency).upper():
                factor *= 1 + pct / 100
                applied.append(f"{subject} {pct:+g}%")
        stressed_mv = mv * factor
        stressed_total += stressed_mv
        per_position[r.symbol] = {
            "base": round(mv), "stressed": round(stressed_mv),
            "impact": round(stressed_mv - mv), "applied": applied or ["unshocked"],
        }

    result = StressResult(
        name=name, base_value=round(base_total, 2), stressed_value=round(stressed_total, 2),
        impact_abs=round(stressed_total - base_total, 2),
        impact_pct=round(100 * (stressed_total - base_total) / base_total, 2) if base_total else 0.0,
        per_position=per_position, shocks=shocks,
    )
    session.add(ScenarioRun(name=name, definition=json.dumps(shocks), result=json.dumps(result.__dict__)))
    session.flush()
    return result


def load_scenarios_yaml(path) -> list[dict]:
    data = yaml.safe_load(open(path, encoding="utf-8"))
    scenarios = data if isinstance(data, list) else data.get("scenarios", [data])
    out = []
    for sc in scenarios:
        if not sc.get("name") or not isinstance(sc.get("shocks"), dict):
            raise ResearchError("each scenario needs: name + shocks mapping")
        out.append({"name": sc["name"], "shocks": sc["shocks"]})
    return out


def format_result(r: StressResult) -> str:
    lines = [
        f"Scenario: {r.name} (mechanical stress test, not a forecast)",
        f"  Portfolio: {r.base_value:,.0f} -> {r.stressed_value:,.0f} "
        f"({r.impact_abs:+,.0f}; {r.impact_pct:+.1f}%)",
    ]
    for sym, p in r.per_position.items():
        lines.append(f"  {sym}: {p['base']:,} -> {p['stressed']:,} ({p['impact']:+,}) [{', '.join(p['applied'])}]")
    return "\n".join(lines)
