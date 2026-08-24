"""Trading statistics from realization events (average-cost method, instrument currency
converted to base at the close date when FX is available).

NOTE: outcome metrics (win rate etc.) do not measure decision quality - shown for reference only.
A "closed trade" here is one realization event (partial closes count separately).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.core import ZERO
from src.db.models import Instrument
from src.portfolio.fx import FxConverter
from src.portfolio.positions import ClosedTrade, load_trades, compute_position


@dataclass
class ClosedTradeRow:
    symbol: str
    open_date: object
    close_date: object
    quantity: Decimal
    pnl_base: Decimal | None
    pnl_local: Decimal
    currency: str
    holding_days: int | None


@dataclass
class TradingStats:
    closed_trades: int
    win_rate: Decimal | None
    average_winner: Decimal | None
    average_loser: Decimal | None
    profit_factor: Decimal | None
    average_holding_days: Decimal | None
    largest_winner: Decimal | None
    largest_loser: Decimal | None
    rows: list[ClosedTradeRow]
    incomplete: bool


def closed_trade_rows(session: Session, base_currency: str) -> tuple[list[ClosedTradeRow], bool]:
    instruments = {i.id: i for i in session.scalars(select(Instrument))}
    fx = FxConverter(session, base_currency)
    rows: list[ClosedTradeRow] = []
    incomplete = False
    for (_acc, inst_id), trades in load_trades(session).items():
        inst = instruments[inst_id]
        st = compute_position(trades)
        ct: ClosedTrade
        for ct in st.closed_trades:
            rate = fx.rate(inst.currency, base_currency, ct.close_date)
            pnl_base = ct.realized_pnl * rate if rate is not None else None
            if pnl_base is None:
                incomplete = True
            rows.append(
                ClosedTradeRow(
                    symbol=inst.symbol,
                    open_date=ct.open_date,
                    close_date=ct.close_date,
                    quantity=ct.quantity,
                    pnl_base=pnl_base,
                    pnl_local=ct.realized_pnl,
                    currency=inst.currency,
                    holding_days=(ct.close_date - ct.open_date).days if ct.open_date else None,
                )
            )
    rows.sort(key=lambda r: r.close_date)
    return rows, incomplete


def compute_trading_stats(session: Session, base_currency: str) -> TradingStats:
    rows, incomplete = closed_trade_rows(session, base_currency)
    pnls = [r.pnl_base for r in rows if r.pnl_base is not None]
    n = len(rows)
    if n == 0 or incomplete:
        return TradingStats(n, None, None, None, None, None, None, None, rows, incomplete)
    winners = [p for p in pnls if p > 0]
    losers = [p for p in pnls if p < 0]
    gross_win = sum(winners, ZERO)
    gross_loss = -sum(losers, ZERO)
    hold = [Decimal(r.holding_days) for r in rows if r.holding_days is not None]
    return TradingStats(
        closed_trades=n,
        win_rate=Decimal(len(winners)) / Decimal(n),
        average_winner=(gross_win / len(winners)) if winners else None,
        average_loser=(-gross_loss / len(losers)) if losers else None,
        profit_factor=(gross_win / gross_loss) if gross_loss > 0 else None,
        average_holding_days=(sum(hold, ZERO) / len(hold)) if hold else None,
        largest_winner=max(winners) if winners else None,
        largest_loser=min(losers) if losers else None,
        rows=rows,
        incomplete=incomplete,
    )
