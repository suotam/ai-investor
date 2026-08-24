"""Read-only data access for the dashboard. All numbers come from the Python engine, never UI math."""
from __future__ import annotations

import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pandas as pd
from sqlalchemy import func, select

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.analytics.attribution import attribution  # noqa: E402
from src.analytics.performance import (  # noqa: E402
    benchmark_series,
    build_value_history,
    drawdown_series,
    max_drawdown,
    money_weighted_return,
    normalize_to_100,
    simple_return,
    time_weighted_return,
    total_pnl,
    twr_index,
)
from src.analytics.trading_stats import compute_trading_stats  # noqa: E402
from src.config import Settings, load_settings  # noqa: E402
from src.db.models import Account, Benchmark, CashFlow, ImportRun, Instrument, Position, Price, Transaction  # noqa: E402
from src.db.session import current_revision, session_scope  # noqa: E402
from src.portfolio.valuation import value_portfolio  # noqa: E402


def fnum(v) -> float | None:
    return None if v is None else float(v)


def db_signature(settings: Settings) -> float:
    """Cache key: DB file mtime (changes on every write)."""
    try:
        return settings.db_path.stat().st_mtime
    except FileNotFoundError:
        return 0.0


def get_settings() -> Settings:
    return load_settings()


def load_overview(settings: Settings, as_of: date | None = None) -> dict:
    with session_scope(settings.db_url) as s:
        val = value_portfolio(s, settings.base_currency, as_of)
        points = build_value_history(s, settings.base_currency, end=as_of)
        idx = twr_index(points)
        bm_code = settings.default_benchmark
        bm = s.scalars(select(Benchmark).where(Benchmark.code == bm_code)).first()
        bm_norm: list[tuple[date, Decimal]] = []
        bm_label = ""
        if bm and points:
            series, bm_label = benchmark_series(
                s, bm.instrument_id, points[0].day, points[-1].day, settings.base_currency
            )
            bm_norm = normalize_to_100(series)
        positions = [
            {
                "Account": r.account_name,
                "Ticker": r.symbol,
                "Company": r.name or "",
                "Asset": r.asset_type,
                "Sector": r.sector or "Unknown",
                "Currency": r.currency,
                "Quantity": fnum(r.quantity),
                "Average Cost": fnum(r.average_cost),
                "Current Price": fnum(r.price),
                "Price Currency": r.price_currency,
                "Price Date": r.price_date.isoformat() if r.price_date else None,
                "Market Value (local)": fnum(r.market_value_local),
                f"Market Value ({settings.base_currency})": fnum(r.market_value_base),
                "Weight": fnum(r.weight),
                "Unrealized P/L (local)": fnum(r.unrealized_pnl_local),
                f"Unrealized P/L ({settings.base_currency})": fnum(r.unrealized_pnl_base),
                "Return %": fnum(r.return_pct),
                "Realized P/L (local)": fnum(r.realized_pnl_local),
                "Issues": "; ".join(r.issues),
            }
            for r in val.positions
        ]
        capital_base = None
        if points and points[0].value is not None:
            capital_base = points[0].value + sum((p.external_flow for p in points[1:]), Decimal(0))
        attr = attribution(val, capital_base)
        today_pnl = None
        if len(points) >= 2 and points[-1].value is not None and points[-2].value is not None:
            today_pnl = points[-1].value - points[-2].value - points[-1].external_flow
        bm_return = None
        if bm_norm:
            bm_return = bm_norm[-1][1] / Decimal(100) - 1
        return {
            "as_of": val.as_of,
            "base_currency": settings.base_currency,
            "total_value": fnum(val.total_value_base),
            "cash": fnum(val.cash_base),
            "cash_by_currency": {k: fnum(v) for k, v in val.cash_by_currency.items()},
            "invested": fnum(val.invested_value_base),
            "cost_basis": fnum(val.cost_basis_base),
            "unrealized_pnl": fnum(val.unrealized_pnl_base),
            "realized_pnl": fnum(val.realized_pnl_base),
            "net_external_flows": fnum(val.net_external_flows_base),
            "positions_count": val.positions_count,
            "incomplete": val.incomplete,
            "issues": val.issues,
            "positions": positions,
            "history": pd.DataFrame(
                {
                    "date": [p.day for p in points],
                    "value": [fnum(p.value) for p in points],
                    "invested": [fnum(p.invested) for p in points],
                    "cash": [fnum(p.cash) for p in points],
                    "flow": [fnum(p.external_flow) for p in points],
                }
            ),
            "history_issues": sorted({i for p in points for i in p.issues})[:20],
            "twr_index": pd.DataFrame({"date": [d for d, _ in idx], "level": [fnum(v) for _, v in idx]}),
            "benchmark": pd.DataFrame({"date": [d for d, _ in bm_norm], "level": [fnum(v) for _, v in bm_norm]}),
            "benchmark_label": f"{bm_code} ({bm_label})" if bm_label else bm_code,
            "twr": fnum(time_weighted_return(points)),
            "simple_return": fnum(simple_return(points)),
            "mwr": fnum(money_weighted_return(points)),
            "total_pnl_history": fnum(total_pnl(points)),
            "today_pnl": fnum(today_pnl),
            "benchmark_return": fnum(bm_return),
            "max_drawdown": fnum(max_drawdown(idx)),
            "drawdown": pd.DataFrame(
                {"date": [d for d, _ in drawdown_series(idx)], "dd": [fnum(v) for _, v in drawdown_series(idx)]}
            ),
            "attribution": [
                {
                    "Ticker": a.symbol,
                    "Company": a.name or "",
                    "Weight": fnum(a.weight),
                    "Unrealized P/L": fnum(a.unrealized_pnl_base),
                    "Realized P/L": fnum(a.realized_pnl_base),
                    "Total P/L": fnum(a.total_pnl_base),
                    "Contribution (simple)": fnum(a.contribution_to_simple_return),
                }
                for a in attr
            ],
        }


def load_trading_stats(settings: Settings) -> dict:
    with session_scope(settings.db_url) as s:
        ts = compute_trading_stats(s, settings.base_currency)
        return {
            "closed_trades": ts.closed_trades,
            "win_rate": fnum(ts.win_rate),
            "average_winner": fnum(ts.average_winner),
            "average_loser": fnum(ts.average_loser),
            "profit_factor": fnum(ts.profit_factor),
            "average_holding_days": fnum(ts.average_holding_days),
            "largest_winner": fnum(ts.largest_winner),
            "largest_loser": fnum(ts.largest_loser),
            "incomplete": ts.incomplete,
            "rows": [
                {
                    "Ticker": r.symbol,
                    "Opened": r.open_date,
                    "Closed": r.close_date,
                    "Quantity": fnum(r.quantity),
                    "P/L (local)": fnum(r.pnl_local),
                    "Currency": r.currency,
                    f"P/L ({settings.base_currency})": fnum(r.pnl_base),
                    "Holding days": r.holding_days,
                }
                for r in ts.rows
            ],
        }


def load_status(settings: Settings) -> dict:
    if not settings.db_path.exists():
        return {"db_exists": False}
    with session_scope(settings.db_url) as s:
        runs = []
        for r in s.scalars(select(ImportRun).order_by(ImportRun.started_at.desc()).limit(25)):
            runs.append(
                {
                    "job": r.job,
                    "source": r.source,
                    "status": r.status,
                    "started": r.started_at,
                    "finished": r.finished_at,
                    "seen": r.records_seen,
                    "inserted": r.records_inserted,
                    "duplicates": r.records_duplicate,
                    "error": r.error or "",
                    "raw": r.raw_path or "",
                }
            )
        last = {}
        for job in ("sync-ibkr", "import-crypto", "update-prices"):
            r = s.scalars(
                select(ImportRun).where(ImportRun.job == job, ImportRun.status == "success").order_by(ImportRun.started_at.desc())
            ).first()
            last[job] = r.started_at if r else None
        accounts = [
            {"id": a.id, "name": a.name, "provider": a.provider, "external_id": a.account_external_id, "currency": a.base_currency}
            for a in s.scalars(select(Account))
        ]
        return {
            "db_exists": True,
            "db_path": str(settings.db_path),
            "revision": current_revision(settings.db_url),
            "counts": {
                "accounts": s.scalar(select(func.count(Account.id))),
                "instruments": s.scalar(select(func.count(Instrument.id))),
                "transactions": s.scalar(select(func.count(Transaction.id))),
                "cash_flows": s.scalar(select(func.count(CashFlow.id))),
                "open_positions": s.scalar(select(func.count(Position.id)).where(Position.quantity != 0)),
                "prices": s.scalar(select(func.count(Price.id))),
            },
            "last_price_date": s.scalar(select(func.max(Price.price_date))),
            "last": last,
            "runs": runs,
            "accounts": accounts,
        }
