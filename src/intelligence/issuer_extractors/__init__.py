"""Issuer-specific KPI extraction framework (plugin registry).

Many of the KPIs that drive a thesis (customers, ARPAC, NPL buckets, risk-adjusted NIM)
do not exist in standard XBRL. Extractors are per-issuer plugins that deterministically
parse the issuer's own primary documents (earnings releases, 6-K exhibits).

Contract:
  * deterministic regex/label extraction only - an ambiguous match is returned with
    mode='ambiguous' and becomes a human-review proposal, never a stored fact;
  * every extracted value carries the exact source excerpt for provenance;
  * extractors are registered here and NEVER hardcoded into generic core modules.
"""
from __future__ import annotations

import html as html_lib
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class ExtractedKpi:
    kpi_name: str  # must match the investment's v2 KPI name (dash/case-insensitively)
    value: float
    unit: str | None
    excerpt: str  # exact source text the value came from
    mode: str = "deterministic"  # deterministic | ambiguous
    period_hint: str | None = None
    notes: str | None = None


class IssuerExtractor(ABC):
    name: str = "abstract"
    version: str = "0"
    ticker: str = ""

    @abstractmethod
    def extract(self, text: str) -> list[ExtractedKpi]:
        """Extract KPIs from plain text (already HTML-stripped)."""

    @property
    def source_tag(self) -> str:
        return f"issuer_extractor:{self.name}-{self.version}"


def html_to_text(raw: str) -> str:
    """Deterministic HTML -> text: drop scripts/styles/tags, unescape, normalize whitespace."""
    txt = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    txt = re.sub(r"(?s)<[^>]+>", " ", txt)
    txt = html_lib.unescape(txt)
    txt = txt.replace("–", "-").replace("—", "-").replace("\xa0", " ")
    return re.sub(r"\s+", " ", txt).strip()


def normalize_kpi_name(name: str) -> str:
    return re.sub(r"\s+", " ", name.replace("–", "-").replace("—", "-")).strip().lower()


_REGISTRY: dict[str, IssuerExtractor] = {}


def register(extractor: IssuerExtractor) -> None:
    _REGISTRY[extractor.ticker.upper()] = extractor


def get_extractor(ticker: str) -> IssuerExtractor | None:
    _ensure_loaded()
    return _REGISTRY.get(ticker.upper())


def _ensure_loaded() -> None:
    if not _REGISTRY:
        from src.intelligence.issuer_extractors import nu  # noqa: F401  (registers itself)
