"""News provider abstraction. No commercial provider is bundled in v3 (documented limitation).

The architecture is provider-agnostic: implement NewsProvider, normalize into NewsItem, and
`ingest_news` handles dedup (URL/title hash), syndication clustering (same normalized title
within a day), entity linking and event creation. Copyright: only metadata + short excerpts
are stored, never full article bodies.
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from src.config import Settings
from src.core import stable_hash
from src.intelligence.entities import resolve_investment
from src.intelligence.events import record_event
from src.intelligence.provenance import register_source
from src.logging_setup import get_logger

log = get_logger("intelligence.news")

MAX_EXCERPT = 500  # store short extracts only, never full articles


@dataclass
class NewsItem:
    title: str
    publisher: str
    published_at: datetime
    url: str
    symbols: list[str] = field(default_factory=list)
    summary: str | None = None  # short excerpt/derived summary only
    source_tier: int = 3
    raw_metadata: dict | None = None


class NewsProvider(ABC):
    name: str = "abstract"

    @abstractmethod
    def fetch(self, symbols: list[str]) -> list[NewsItem]:  # pragma: no cover - interface
        raise NotImplementedError


class NullNewsProvider(NewsProvider):
    """Default: no news source configured. The app works without news (documented limitation)."""

    name = "null"

    def fetch(self, symbols: list[str]) -> list[NewsItem]:
        return []


def _norm_title(title: str) -> str:
    return re.sub(r"\W+", " ", title.lower()).strip()


def ingest_news(session: Session, settings: Settings, provider: NewsProvider, symbols: list[str]) -> dict:
    items = provider.fetch(symbols)
    inserted = duplicates = 0
    seen_clusters: set[str] = set()
    for item in items:
        cluster_key = stable_hash("news-cluster", _norm_title(item.title), item.published_at.date())[:32]
        external_id = stable_hash("news", item.url)[:32]
        summary = (item.summary or "")[:MAX_EXCERPT] or None
        doc, created = register_source(
            session, settings, provider=f"news:{provider.name}", source_type="news",
            external_id=external_id, raw=None, category="news", url=item.url, title=item.title,
            published_at=item.published_at, source_tier=item.source_tier,
            metadata={"publisher": item.publisher, "symbols": item.symbols, "excerpt": summary,
                      "cluster": cluster_key} | (item.raw_metadata or {}),
        )
        if not created:
            duplicates += 1
            continue
        inserted += 1
        if cluster_key in seen_clusters:
            continue  # syndicated duplicate of a story already evented in this batch
        seen_clusters.add(cluster_key)
        for symbol in item.symbols:
            inv = resolve_investment(session, ticker=symbol)
            record_event(
                session, "NEWS_EVENT",
                dedup_key=f"news:{cluster_key}:{symbol}",
                title=f"{symbol}: {item.title[:120]}",
                occurred_at=item.published_at,
                investment_id=inv.id if inv else None,
                summary=summary,
                source_document_id=doc.id,
                payload={"publisher": item.publisher, "url": item.url, "tier": item.source_tier},
            )
    return {"provider": provider.name, "items": len(items), "inserted": inserted, "duplicates": duplicates}
