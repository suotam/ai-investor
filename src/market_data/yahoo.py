"""Yahoo Finance provider via yfinance - isolated here; nothing else imports yfinance."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from src.core import D
from src.logging_setup import get_logger
from src.market_data.base import FxBar, MarketDataProvider, PriceBar, ProviderError

log = get_logger("market_data.yahoo")


class YahooProvider(MarketDataProvider):
    name = "yahoo"

    def _download(self, symbol: str, start: date, end: date):
        try:
            import yfinance as yf  # local import keeps dependency optional for tests
        except ImportError as exc:  # pragma: no cover
            raise ProviderError("yfinance is not installed") from exc
        try:
            df = yf.download(
                symbol,
                start=start.isoformat(),
                end=(end + timedelta(days=1)).isoformat(),
                progress=False,
                auto_adjust=False,
                actions=False,
                threads=False,
            )
        except Exception as exc:
            log.warning("yahoo download failed for %s: %s", symbol, exc)
            return None
        if df is None or df.empty:
            return None
        # yfinance >= 0.2.40 returns MultiIndex columns (field, ticker) even for one ticker
        if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
            df.columns = [c[0] for c in df.columns]
        return df

    def fetch_prices(self, symbol: str, start: date, end: date) -> list[PriceBar]:
        df = self._download(symbol, start, end)
        if df is None:
            return []
        bars: list[PriceBar] = []
        for idx, row in df.iterrows():
            close = row.get("Close")
            if close is None or close != close:  # NaN
                continue
            bars.append(
                PriceBar(
                    price_date=idx.date(),
                    close=D(round(float(close), 6)),
                    open=_opt(row.get("Open")),
                    high=_opt(row.get("High")),
                    low=_opt(row.get("Low")),
                    adjusted_close=_opt(row.get("Adj Close")),
                    volume=_opt(row.get("Volume")),
                )
            )
        return bars

    def fetch_fx(self, base: str, quote: str, start: date, end: date) -> list[FxBar]:
        symbol = f"{base.upper()}{quote.upper()}=X"
        df = self._download(symbol, start, end)
        if df is None:
            return []
        out: list[FxBar] = []
        for idx, row in df.iterrows():
            close = row.get("Close")
            if close is None or close != close:
                continue
            out.append(FxBar(rate_date=idx.date(), rate=D(round(float(close), 8))))
        return out


def _opt(v) -> Decimal | None:
    if v is None or v != v:
        return None
    return D(round(float(v), 6))
