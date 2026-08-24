"""XBRL company-facts layer: deterministic structured financials, no LLM number-reading.

Source: https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json (Tier 1).

Mapping architecture (three levels, conservative by design):
  * NORMALIZED_METRICS: taxonomy-aware alias lists (us-gaap AND ifrs-full - foreign private
    issuers like NU report under IFRS) mapping raw concepts to internal metric names.
  * issuer_aliases: optional per-CIK overrides passed by the caller for issuer-specific tags.
  * everything else is stored with metric=NULL (raw concept preserved, nothing invented).

KPI bridging (kpi_mapping.py) then distinguishes deterministic exact mappings (auto KPI
observation with provenance) from suggested mappings (AI/human proposal) and unsupported ones.
"""
from __future__ import annotations

import json
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.config import Settings
from src.core import stable_hash
from src.db.intelligence import FinancialFact, SourceDocument
from src.intelligence.connectors.sec import COMPANYFACTS_URL, SecClient, SecError, normalize_cik
from src.intelligence.provenance import register_source
from src.logging_setup import get_logger

log = get_logger("intelligence.xbrl")

# taxonomy -> concept -> normalized metric. One economic KPI can have several concept aliases;
# one concept never maps to two metrics. Extend deliberately, never guess.
NORMALIZED_METRICS: dict[str, dict[str, str]] = {
    "us-gaap": {
        "Revenues": "revenue",
        "RevenueFromContractWithCustomerExcludingAssessedTax": "revenue",
        "NetIncomeLoss": "net_income",
        "Assets": "total_assets",
        "StockholdersEquity": "total_equity",
        "OperatingIncomeLoss": "operating_income",
        "EarningsPerShareBasic": "eps_basic",
        "EarningsPerShareDiluted": "eps_diluted",
        "CashAndCashEquivalentsAtCarryingValue": "cash",
        "LongTermDebt": "debt_long_term",
    },
    "ifrs-full": {
        "Revenue": "revenue",
        "RevenueFromContractsWithCustomers": "revenue",
        "ProfitLoss": "net_income",
        "ProfitLossAttributableToOwnersOfParent": "net_income_attributable",
        "Assets": "total_assets",
        "Equity": "total_equity",
        "EquityAttributableToOwnersOfParent": "equity_attributable",
        "ProfitLossFromOperatingActivities": "operating_income",
        "BasicEarningsLossPerShare": "eps_basic",
        "DilutedEarningsLossPerShare": "eps_diluted",
        "CashAndCashEquivalents": "cash",
    },
}


def normalize_concept(taxonomy: str, concept: str, issuer_aliases: dict[str, str] | None = None) -> str | None:
    if issuer_aliases and concept in issuer_aliases:
        return issuer_aliases[concept]
    return NORMALIZED_METRICS.get(taxonomy, {}).get(concept)


def parse_companyfacts(
    payload: dict, issuer_aliases: dict[str, str] | None = None, taxonomies: tuple[str, ...] = ("us-gaap", "ifrs-full")
) -> list[dict]:
    """Flatten companyfacts JSON into fact dicts (mapped and unmapped concepts alike)."""
    cik = normalize_cik(payload.get("cik", "0"))
    out: list[dict] = []
    facts = payload.get("facts") or {}
    for taxonomy in taxonomies:
        for concept, body in (facts.get(taxonomy) or {}).items():
            metric = normalize_concept(taxonomy, concept, issuer_aliases)
            for unit, rows in (body.get("units") or {}).items():
                for row in rows:
                    end = _d(row.get("end"))
                    start = _d(row.get("start"))
                    if row.get("val") is None or end is None:
                        continue
                    out.append(
                        {
                            "cik": cik,
                            "taxonomy": taxonomy,
                            "concept": concept,
                            "metric": metric,
                            "value": float(row["val"]),
                            "unit": unit,
                            "period_start": start,
                            "period_end": end,
                            "is_instant": start is None,
                            "fiscal_year": row.get("fy"),
                            "fiscal_period": row.get("fp"),
                            "form": row.get("form"),
                            "accession": row.get("accn"),
                            "filed_at": _d(row.get("filed")),
                        }
                    )
    return out


def _d(v) -> date | None:
    if not v:
        return None
    try:
        return date.fromisoformat(str(v))
    except (ValueError, TypeError):
        return None


def fact_identity_hash(f: dict) -> str:
    """Idempotency: same concept+unit+period+value+accession == same fact. A restated value
    (different value or accession for the same period) is a NEW row - history is preserved."""
    return stable_hash(
        f["cik"], f["taxonomy"], f["concept"], f["unit"], f["period_start"], f["period_end"],
        f["value"], f["accession"],
    )


def store_facts(session: Session, facts: list[dict], source_document_id: int | None) -> dict:
    existing = set(session.execute(select(FinancialFact.fact_hash)).scalars())
    inserted = duplicates = 0
    for f in facts:
        h = fact_identity_hash(f)
        if h in existing:
            duplicates += 1
            continue
        session.add(
            FinancialFact(
                source_document_id=source_document_id, fact_hash=h,
                cik=f["cik"], taxonomy=f["taxonomy"], concept=f["concept"], metric=f["metric"],
                value=f["value"], unit=f["unit"], period_start=f["period_start"],
                period_end=f["period_end"], is_instant=f["is_instant"], fiscal_year=f["fiscal_year"],
                fiscal_period=f["fiscal_period"], form=f["form"], accession=f["accession"],
                filed_at=f["filed_at"],
            )
        )
        existing.add(h)
        inserted += 1
    session.flush()
    return {"facts_seen": len(facts), "facts_inserted": inserted, "facts_duplicate": duplicates}


def sync_companyfacts(
    session: Session, settings: Settings, cik: str, client: SecClient | None = None,
    issuer_aliases: dict[str, str] | None = None,
) -> dict:
    client = client or SecClient(settings)
    cik = normalize_cik(cik)
    url = COMPANYFACTS_URL.format(cik10=cik.zfill(10))
    try:
        raw = client.get(url)
    except SecError as exc:
        log.warning("companyfacts unavailable for CIK %s: %s", cik, exc)
        return {"cik": cik, "error": str(exc), "facts_inserted": 0}
    payload = json.loads(raw.decode("utf-8"))
    doc, _ = register_source(
        session, settings, provider="sec_edgar", source_type="companyfacts", external_id=f"CIK{cik}",
        raw=raw, category="xbrl", url=url, title=f"XBRL companyfacts {payload.get('entityName')}",
        issuer=payload.get("entityName"), entity_key=cik, source_tier=1,
    )
    facts = parse_companyfacts(payload, issuer_aliases)
    result = store_facts(session, facts, doc.id)
    mapped = sum(1 for f in facts if f["metric"])
    result |= {"cik": cik, "issuer": payload.get("entityName"), "facts_mapped_to_metrics": mapped}
    log.info("xbrl sync CIK %s: %s", cik, result)
    return result


def latest_metrics(session: Session, cik: str, metrics: list[str] | None = None) -> list[FinancialFact]:
    """Latest fact per (metric, fiscal period type). Read helper for dashboards/context."""
    stmt = select(FinancialFact).where(
        FinancialFact.cik == normalize_cik(cik), FinancialFact.metric.isnot(None)
    )
    if metrics:
        stmt = stmt.where(FinancialFact.metric.in_(metrics))
    rows = list(session.scalars(stmt))
    best: dict[tuple, FinancialFact] = {}
    for r in rows:
        key = (r.metric, r.unit, r.is_instant)
        cur = best.get(key)
        if cur is None or (r.period_end, r.filed_at or date.min) > (cur.period_end, cur.filed_at or date.min):
            best[key] = r
    return sorted(best.values(), key=lambda r: (r.metric or "", r.unit))
