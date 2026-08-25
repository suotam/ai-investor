"""Portfolio risk decomposition (deterministic, honest about missing metadata).

Dimensions: company concentration, currency, sector (Unknown when metadata missing),
asset class, and SHARED MACRO THESIS exposure - positions linked to the same macro series
(investment_macro_links) are flagged as one correlated bet, without pretending correlation
equals causal factor exposure.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.config import Settings
from src.portfolio.valuation import value_portfolio


def portfolio_risk_report(session: Session, settings: Settings) -> dict:
    val = value_portfolio(session, settings.base_currency)
    rows = [r for r in val.positions if r.weight is not None]
    report: dict = {
        "positions": len(val.positions),
        "by_currency": {}, "by_sector": {}, "by_asset_type": {},
        "max_weight_pct": None, "top_position": None,
        "shared_macro_exposure": [], "notes": [],
    }
    for r in rows:
        w = float(r.weight) * 100
        report["by_currency"][r.currency] = round(report["by_currency"].get(r.currency, 0) + w, 1)
        report["by_sector"][r.sector or "Unknown"] = round(report["by_sector"].get(r.sector or "Unknown", 0) + w, 1)
        report["by_asset_type"][r.asset_type] = round(report["by_asset_type"].get(r.asset_type, 0) + w, 1)
    if rows:
        top = max(rows, key=lambda r: r.weight)
        report["max_weight_pct"] = round(float(top.weight) * 100, 1)
        report["top_position"] = top.symbol
        if float(top.weight) > 0.25:
            report["notes"].append(
                f"Company concentration: {top.symbol} is {float(top.weight) * 100:.0f}% of invested value."
            )
    unknown_sector = report["by_sector"].get("Unknown", 0)
    if unknown_sector > 50:
        report["notes"].append(
            f"Sector metadata missing for {unknown_sector:.0f}% of the portfolio (shown as Unknown, not guessed)."
        )

    # shared macro thesis: several investments linked to the same macro series
    from src.db.intelligence import InvestmentMacroLink, MacroSeries
    from src.db.research import Investment

    by_series: dict[int, list[str]] = {}
    investments = {i.id: i for i in session.scalars(select(Investment))}
    for link in session.scalars(select(InvestmentMacroLink)):
        inv = investments.get(link.investment_id)
        if inv:
            by_series.setdefault(link.series_id, []).append(inv.ticker)
    for series_id, tickers in by_series.items():
        if len(set(tickers)) > 1:
            series = session.get(MacroSeries, series_id)
            report["shared_macro_exposure"].append(
                {"series": series.name if series else str(series_id), "investments": sorted(set(tickers)),
                 "note": "These positions share one macro driver - closer to one bet than independent bets."}
            )
    return report
