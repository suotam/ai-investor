"""Extractor assistant: show candidate KPI contexts in a quarterly source document so a
human (optionally helped by AI) can write deterministic extraction rules.

Nothing here creates automatic rules - generated suggestions must be reviewed and tested
before they become part of an extractor.
"""
from __future__ import annotations

import re

from src.intelligence.issuer_extractors import html_to_text

GENERIC_KPI_HINTS = [
    "customers", "active customers", "ARPAC", "revenue", "net income", "gross margin",
    "NPL", "NIM", "ROE", "ROIC", "loan portfolio", "deposits", "guidance", "EPS",
    "free cash flow", "operating margin",
]

_NUMBER_NEAR = re.compile(r"\d")


def inspect_candidates(
    raw: str, kpi_names: list[str] | None = None, is_html: bool = True, max_per_kpi: int = 4
) -> dict[str, list[str]]:
    """Return {kpi_hint: [sentences containing the hint and at least one number]}."""
    text = html_to_text(raw) if is_html else raw
    sentences = re.split(r"(?<=[.!?])\s+", text)
    hints = list(dict.fromkeys((kpi_names or []) + GENERIC_KPI_HINTS))
    out: dict[str, list[str]] = {}
    for hint in hints:
        pattern = re.compile(re.escape(hint.replace("–", "-")), re.IGNORECASE)
        matches = [
            s.strip()[:300] for s in sentences
            if pattern.search(s.replace("–", "-")) and _NUMBER_NEAR.search(s)
        ]
        if matches:
            out[hint] = matches[:max_per_kpi]
    return out


def format_inspection(candidates: dict[str, list[str]]) -> str:
    if not candidates:
        return "No numeric KPI candidates found in this document."
    lines = []
    for hint, sentences in candidates.items():
        lines.append(f"## {hint}")
        for s in sentences:
            lines.append(f"  - {s}")
        lines.append("")
    lines.append("Review these contexts, then encode deterministic regex rules in an issuer "
                 "extractor (see src/intelligence/issuer_extractors/nu.py). Ambiguity must "
                 "stay ambiguous.")
    return "\n".join(lines)
