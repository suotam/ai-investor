from src.market_data.base import FxBar, MarketDataProvider, PriceBar, ProviderError


def get_provider(name: str) -> MarketDataProvider:
    if name == "yahoo":
        from src.market_data.yahoo import YahooProvider

        return YahooProvider()
    raise ValueError(f"Unknown market data provider: {name}")


__all__ = ["FxBar", "MarketDataProvider", "PriceBar", "ProviderError", "get_provider"]
