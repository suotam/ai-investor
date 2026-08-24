"""Deterministic technical/market context from the existing v1 price cache.

Context, not signals: outputs are factual statements ("price is 18% below the 52-week high",
"below the 200d SMA"), computed in Decimal-safe Python. No BUY/SELL language anywhere.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from src.core import D
from src.market_data.service import PriceStore


@dataclass
class TechnicalContext:
    as_of: date
    price: Decimal | None = None
    sma20: Decimal | None = None
    sma50: Decimal | None = None
    sma200: Decimal | None = None
    high_52w: Decimal | None = None
    low_52w: Decimal | None = None
    distance_from_52w_high: Decimal | None = None  # negative fraction below high
    realized_vol_20d: Decimal | None = None  # annualized, fraction
    atr14: Decimal | None = None
    rsi14: Decimal | None = None
    drawdown_from_ath: Decimal | None = None
    observations: int = 0

    def statements(self) -> list[str]:
        """Human-readable factual context lines. No recommendations."""
        out: list[str] = []
        if self.price is None:
            return ["No cached price data available."]
        if self.distance_from_52w_high is not None:
            out.append(f"Price is {abs(self.distance_from_52w_high) * 100:.1f}% below the 52-week high.")
        for sma, label in ((self.sma50, "50d"), (self.sma200, "200d")):
            if sma is not None:
                rel = "above" if self.price >= sma else "below"
                out.append(f"Price is {rel} the {label} moving average.")
        if self.realized_vol_20d is not None:
            out.append(f"20d realized volatility: {self.realized_vol_20d * 100:.0f}% annualized.")
        if self.rsi14 is not None:
            out.append(f"RSI(14): {self.rsi14:.0f}.")
        if self.drawdown_from_ath is not None and self.drawdown_from_ath < 0:
            out.append(f"Drawdown from cached all-time high: {self.drawdown_from_ath * 100:.1f}%.")
        return out


def _sma(closes: list[Decimal], n: int) -> Decimal | None:
    if len(closes) < n:
        return None
    return sum(closes[-n:]) / n


def compute_technical_context(closes_by_date: dict[date, Decimal], as_of: date) -> TechnicalContext:
    """Pure function over a {date: close} series (highs/lows approximated by closes when OHLC
    is unavailable - stated in README limitations)."""
    dates = sorted(d for d in closes_by_date if d <= as_of)
    ctx = TechnicalContext(as_of=as_of, observations=len(dates))
    if not dates:
        return ctx
    closes = [closes_by_date[d] for d in dates]
    ctx.price = closes[-1]
    ctx.sma20 = _sma(closes, 20)
    ctx.sma50 = _sma(closes, 50)
    ctx.sma200 = _sma(closes, 200)
    year_ago = as_of - timedelta(days=365)
    window = [closes_by_date[d] for d in dates if d >= year_ago]
    if window:
        ctx.high_52w, ctx.low_52w = max(window), min(window)
        if ctx.high_52w > 0:
            ctx.distance_from_52w_high = ctx.price / ctx.high_52w - 1
    ath = max(closes)
    if ath > 0:
        ctx.drawdown_from_ath = ctx.price / ath - 1
    # daily returns for vol/ATR/RSI
    rets: list[Decimal] = []
    trs: list[Decimal] = []
    gains: list[Decimal] = []
    losses: list[Decimal] = []
    for prev, cur in zip(closes[:-1], closes[1:]):
        if prev > 0:
            rets.append(cur / prev - 1)
        trs.append(abs(cur - prev))
        change = cur - prev
        gains.append(max(change, Decimal(0)))
        losses.append(max(-change, Decimal(0)))
    if len(rets) >= 20:
        tail = rets[-20:]
        mean = sum(tail) / len(tail)
        var = sum((r - mean) ** 2 for r in tail) / (len(tail) - 1)
        ctx.realized_vol_20d = D(float(var) ** 0.5) * D(float(252) ** 0.5)
    if len(trs) >= 14:
        ctx.atr14 = sum(trs[-14:]) / 14
    if len(gains) >= 14:
        avg_gain = sum(gains[-14:]) / 14
        avg_loss = sum(losses[-14:]) / 14
        if avg_loss == 0:
            ctx.rsi14 = Decimal(100)
        else:
            rs = avg_gain / avg_loss
            ctx.rsi14 = Decimal(100) - Decimal(100) / (1 + rs)
    return ctx


def technical_context_for_instrument(session: Session, instrument_id: int, as_of: date | None = None) -> TechnicalContext:
    as_of = as_of or date.today()
    series = PriceStore(session).series(instrument_id)
    closes = {d: close for d, (close, _ccy) in series.items()}
    return compute_technical_context(closes, as_of)
