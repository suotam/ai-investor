"""NU (Nu Holdings) earnings-release extractor.

Targets the recurring language of Nu's quarterly earnings press releases (6-K exhibits /
IR releases). Patterns are deliberately conservative:
  * a KPI is extracted only when exactly ONE consistent match is found;
  * multiple conflicting matches -> mode='ambiguous' (human review, never stored);
  * the matched sentence is preserved as the provenance excerpt.

KPI names mirror the user's real NU KPI definitions (matched dash/case-insensitively):
Customers, Active customers, ARPAC, Loan portfolio, Loan growth YoY, 15-90 day NPL,
NPL 90+, Risk-adjusted NIM, ROE, Net income, Mexico customers.
"""
from __future__ import annotations

import re

from src.intelligence.issuer_extractors import ExtractedKpi, IssuerExtractor, register

NUM = r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"


def _f(raw: str) -> float:
    return float(raw.replace(",", ""))


def _sentence(text: str, start: int, end: int) -> str:
    left = max(text.rfind(". ", 0, start), 0)
    right = text.find(". ", end)
    if right == -1:
        right = min(end + 200, len(text))
    return text[left:right + 1].strip()[:400]


class NuExtractor(IssuerExtractor):
    name = "nu"
    version = "1.1"
    ticker = "NU"

    # (kpi_name, [patterns], unit, value transform, exclude-if-sentence-matches)
    RULES: list[tuple[str, list[str], str | None, str, str | None]] = [
        ("Customers",
         [rf"(?:reach(?:ed|ing)|totaling|to|of)\s+{NUM}\s+million\s+(?:total\s+)?customers",
          rf"customer\s+base\s+(?:grew|reached|of)[^.]*?{NUM}\s+million"],
         "m", "millions", r"(?i)mexico|colombia|brazil\s+reached"),
        ("Active customers",
         [rf"{NUM}\s+million\s+(?:monthly\s+)?active\s+customers",
          rf"active\s+customers\s+(?:reached|of|totaling)\s+{NUM}\s+million"],
         "m", "millions", r"(?i)mexico|colombia"),
        ("Mexico customers",
         [rf"(?:in\s+)?Mexico[^.]*?(?:reach(?:ed|ing)|surpassed|to|of|totaling)\s+{NUM}\s+million\s+customers",
          rf"{NUM}\s+million\s+customers\s+in\s+Mexico"],
         "m", "millions", None),
        ("ARPAC",
         [rf"(?:monthly\s+)?(?:average\s+revenue\s+per\s+active\s+customer|ARPAC)[^.]*?(?:US)?\$\s*{NUM}"],
         "USD", "plain", r"(?i)mexico|colombia"),
        ("Loan portfolio",
         [rf"(?:interest-earning|total)\s+(?:loan\s+)?portfolio[^.]*?(?:US)?\$\s*{NUM}\s+billion",
          rf"(?:loan|credit)\s+portfolio[^.]*?(?:reached|of|totaling|to)[^.]*?(?:US)?\$\s*{NUM}\s+billion"],
         "USD bn", "plain", None),
        ("Loan growth YoY",
         [rf"(?:loan|credit)\s+portfolio[^.]*?(?:grew|up|increased|expanded)[^.]*?{NUM}\s*%[^.]*?(?:YoY|year[- ]over[- ]year)"],
         "%", "plain", None),
        ("15-90 day NPL",
         [rf"15[- ]90\s*(?:day)?\s*NPL[^.]*?(?:at|to|of|was|were|ratio)?\s*{NUM}\s*%",
          rf"NPL\s*15[- ]90[^.]*?{NUM}\s*%"],
         "%", "plain", None),
        ("NPL 90+",
         [rf"(?<![\d-])90\+\s*(?:day)?\s*NPL[^.]*?(?:at|to|of|was|were|ratio)?\s*{NUM}\s*%",
          rf"NPL\s*90\+[^.]*?{NUM}\s*%",
          rf"90\s+days?\s+NPL\s+ratio[^.]*?{NUM}\s*%"],
         "%", "plain", None),
        ("Risk-adjusted NIM",
         [rf"risk[- ]adjusted\s+(?:net\s+interest\s+margin|NIM)[^.]*?{NUM}\s*%"],
         "%", "plain", None),
        ("ROE",
         [rf"(?:annualized\s+)?(?:return\s+on\s+equity|ROE)[^.]*?(?:at|of|to|was|reached)?\s*{NUM}\s*%"],
         "%", "plain", None),
        ("Net income",
         [rf"(?<!adjusted\s)net\s+income\s+(?:of|reached|was|totaling|totaled)[^.]*?(?:US)?\$\s*{NUM}\s+(million|billion)"],
         "USD m", "money_millions", r"(?i)adjusted\s+net\s+income\s+(?:of|reached|was)"),
    ]

    def extract(self, text: str) -> list[ExtractedKpi]:
        out: list[ExtractedKpi] = []
        for kpi_name, patterns, unit, transform, exclude in self.RULES:
            matches: list[tuple[float, str]] = []
            for pat in patterns:
                for m in re.finditer(pat, text, flags=re.IGNORECASE):
                    sentence = _sentence(text, m.start(), m.end())
                    if exclude and re.search(exclude, sentence):
                        continue  # e.g. country-level customer counts must not feed the global KPI
                    value = _f(m.group(1))
                    if transform == "money_millions" and len(m.groups()) >= 2 and m.group(2):
                        if m.group(2).lower() == "billion":
                            value *= 1000.0
                    matches.append((value, sentence))
                if matches:
                    break  # first matching pattern wins; later patterns are fallbacks
            if not matches:
                continue
            values = {v for v, _ in matches}
            if len(values) == 1:
                out.append(ExtractedKpi(kpi_name=kpi_name, value=matches[0][0], unit=unit,
                                        excerpt=matches[0][1], mode="deterministic"))
            else:
                out.append(ExtractedKpi(
                    kpi_name=kpi_name, value=matches[0][0], unit=unit,
                    excerpt=" | ".join(e for _, e in matches[:3]),
                    mode="ambiguous",
                    notes=f"conflicting matches: {sorted(values)} - review required",
                ))
        return out


register(NuExtractor())
