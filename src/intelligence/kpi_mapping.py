"""KPI bridging: connect normalized financial facts (XBRL) to v2 investment KPIs.

Three explicit classes of mapping (spec 7):
  A) DETERMINISTIC: an explicit (kpi name -> metric, unit, scale) entry in KPI_BRIDGE below
     or a per-investment override. The system creates KPI observations automatically -
     with full provenance (source=sec_xbrl, source_reference=accession) and never
     overwriting existing observations (v2 uniqueness per kpi+period+source).
  B) SUGGESTED: a metric exists whose name loosely matches a KPI -> an AI/system PROPOSAL
     (KPI_MAPPING) is created for human review; nothing is written to kpi_observations.
  C) UNSUPPORTED: issuer-specific KPIs (customers, ARPAC, NPL ratios...) that do not exist
     in XBRL. These remain manual or come from future issuer-specific extractors. The
     system reports them as unsupported instead of inventing values.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.intelligence import AiProposal, FinancialFact
from src.db.research import Investment, InvestmentKpi
from src.intelligence.entities import normalize_cik
from src.logging_setup import get_logger
from src.research import kpis as kpi_service
from src.research.investments import ResearchError

log = get_logger("intelligence.kpi_mapping")

# Deterministic bridge: lowercase KPI name -> (normalized metric, expected units, scale)
# Only metrics whose economic meaning is unambiguous belong here.
KPI_BRIDGE: dict[str, tuple[str, tuple[str, ...], float]] = {
    "net income": ("net_income", ("USD",), 1.0),
    "net income (usd m)": ("net_income", ("USD",), 1e-6),
    "revenue": ("revenue", ("USD",), 1.0),
    "revenue (usd m)": ("revenue", ("USD",), 1e-6),
    "total assets": ("total_assets", ("USD",), 1.0),
    "eps": ("eps_diluted", ("USD/shares",), 1.0),
}

SUGGEST_HINTS: dict[str, str] = {  # loose name fragments -> metric (proposal only, never auto)
    "income": "net_income",
    "revenue": "revenue",
    "eps": "eps_diluted",
    "equity": "total_equity",
    "assets": "total_assets",
}


@dataclass
class KpiMappingResult:
    deterministic: list[str]
    observations_created: int
    observations_duplicate: int
    suggested: list[str]
    unsupported: list[str]


def _period_label(fact: FinancialFact) -> str:
    if fact.fiscal_period and fact.fiscal_year:
        return f"{fact.fiscal_year}{fact.fiscal_period}" if fact.fiscal_period != "FY" else f"FY{fact.fiscal_year}"
    return fact.period_end.isoformat()


def apply_kpi_bridge(
    session: Session, investment: Investment, cik: str,
    overrides: dict[str, tuple[str, tuple[str, ...], float]] | None = None,
) -> KpiMappingResult:
    """Create KPI observations for deterministic mappings; proposals for suggested ones."""
    cik = normalize_cik(cik)
    bridge = dict(KPI_BRIDGE)
    if overrides:
        bridge.update({k.lower(): v for k, v in overrides.items()})
    result = KpiMappingResult([], 0, 0, [], [])

    inv_kpis = kpi_service.list_kpis(session, investment)
    facts_by_metric: dict[str, list[FinancialFact]] = {}
    for f in session.scalars(
        select(FinancialFact).where(FinancialFact.cik == cik, FinancialFact.metric.isnot(None))
    ):
        facts_by_metric.setdefault(f.metric, []).append(f)

    for kpi in inv_kpis:
        entry = bridge.get(kpi.name.lower())
        if entry is not None:
            metric, units, scale = entry
            facts = [f for f in facts_by_metric.get(metric, []) if f.unit in units]
            if not facts:
                result.unsupported.append(f"{kpi.name} (no {metric} facts in accepted units)")
                continue
            result.deterministic.append(kpi.name)
            for f in _dedupe_periods(facts):
                period = _period_label(f)
                try:
                    kpi_service.add_observation(
                        session, kpi, period, f.value * scale,
                        period_date=f.period_end, reported_at=f.filed_at,
                        source="sec_xbrl",
                        source_reference=f"{f.accession or ''} {f.taxonomy}:{f.concept}".strip(),
                        created_by="SYSTEM",
                    )
                    result.observations_created += 1
                except ResearchError:
                    result.observations_duplicate += 1  # existing observation is never overwritten
            continue
        hint = next((m for frag, m in SUGGEST_HINTS.items() if frag in kpi.name.lower()), None)
        if hint and facts_by_metric.get(hint):
            result.suggested.append(kpi.name)
            _suggest_mapping_proposal(session, investment, kpi, hint)
        else:
            result.unsupported.append(kpi.name)

    log.info(
        "kpi bridge %s: deterministic=%s obs+%d dup=%d suggested=%s unsupported=%d",
        investment.ticker, result.deterministic, result.observations_created,
        result.observations_duplicate, result.suggested, len(result.unsupported),
    )
    return result


def _dedupe_periods(facts: list[FinancialFact]) -> list[FinancialFact]:
    """One fact per fiscal period: prefer the latest filing (restatements stay in facts table)."""
    best: dict[str, FinancialFact] = {}
    for f in facts:
        key = _period_label(f)
        cur = best.get(key)
        if cur is None or (f.filed_at or f.retrieved_at.date()) > (cur.filed_at or cur.retrieved_at.date()):
            best[key] = f
    return sorted(best.values(), key=lambda f: f.period_end)


def _suggest_mapping_proposal(session: Session, investment: Investment, kpi: InvestmentKpi, metric: str) -> None:
    dedup_title = f"KPI mapping suggestion: {kpi.name} <- {metric}"
    exists = session.scalars(
        select(AiProposal).where(
            AiProposal.investment_id == investment.id,
            AiProposal.proposal_type == "KPI_MAPPING",
            AiProposal.title == dedup_title,
            AiProposal.status.in_(("PENDING", "DEFERRED")),
        )
    ).first()
    if exists:
        return
    session.add(
        AiProposal(
            proposal_type="KPI_MAPPING",
            investment_id=investment.id,
            title=dedup_title,
            what_happened=f"XBRL facts contain normalized metric '{metric}'.",
            why_it_matters=f"KPI '{kpi.name}' might be fillable automatically, but the mapping is not "
                           "deterministic (name match only). Review before any observation is created.",
            proposed_change_json=json.dumps({"kpi_id": kpi.id, "kpi_name": kpi.name, "metric": metric}),
            confidence=None,
            provider="deterministic",
            model=None,
            prompt_version="kpi-bridge-1",
            created_by="SYSTEM",
        )
    )
    session.flush()
