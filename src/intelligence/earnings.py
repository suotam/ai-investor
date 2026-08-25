"""Earnings pipeline: issuer-specific extraction -> KPI observations (with provenance) ->
deterministic comparison (QoQ / YoY / vs thesis expectation) -> review proposals.

Rules:
  * deterministic extractions are stored as kpi_observations (source=issuer_extractor:...,
    source_reference carries the document id and the exact excerpt); duplicates are skipped,
    existing observations are never overwritten;
  * ambiguous extractions become KPI_OBSERVATION proposals - values are never guessed;
  * a KPI outside an assumption's expected range creates an ASSUMPTION_STATUS_CHANGE
    proposal (flag), it NEVER changes the assumption automatically.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.intelligence import AiProposal, SourceDocument
from src.db.research import Investment, InvestmentKpi, KpiObservation, ThesisAssumption
from src.intelligence.issuer_extractors import (
    ExtractedKpi,
    get_extractor,
    html_to_text,
    normalize_kpi_name,
)
from src.logging_setup import get_logger
from src.research import kpis as kpi_service
from src.research.investments import ResearchError

log = get_logger("intelligence.earnings")


@dataclass
class ExtractionResult:
    extractor: str
    period: str
    stored: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    ambiguous: list[str] = field(default_factory=list)
    unmatched_kpi_names: list[str] = field(default_factory=list)
    extracted: list[ExtractedKpi] = field(default_factory=list)


def run_extraction(
    session: Session,
    investment: Investment,
    raw_document: str,
    period: str,
    period_date: date | None = None,
    source_document: SourceDocument | None = None,
    reported_at: date | None = None,
    is_html: bool = True,
) -> ExtractionResult:
    extractor = get_extractor(investment.ticker)
    if extractor is None:
        raise ResearchError(f"no issuer extractor registered for {investment.ticker}")
    text = html_to_text(raw_document) if is_html else raw_document
    extracted = extractor.extract(text)
    result = ExtractionResult(extractor=extractor.source_tag, period=period, extracted=extracted)

    kpis_by_norm = {normalize_kpi_name(k.name): k for k in kpi_service.list_kpis(session, investment)}
    doc_ref = f"source_documents:{source_document.id}" if source_document else "manual-file"

    for ex in extracted:
        kpi = kpis_by_norm.get(normalize_kpi_name(ex.kpi_name))
        if kpi is None:
            result.unmatched_kpi_names.append(ex.kpi_name)
            continue
        source_reference = f"{doc_ref} | excerpt: {ex.excerpt[:250]}"
        value = _reconcile_money_unit(ex.value, ex.unit, kpi.unit)
        if ex.mode == "ambiguous":
            _kpi_review_proposal(session, investment, kpi, ex, period, source_reference)
            result.ambiguous.append(f"{ex.kpi_name} ({ex.notes})")
            continue
        try:
            kpi_service.add_observation(
                session, kpi, period, value, period_date=period_date, reported_at=reported_at,
                source=extractor.source_tag, source_reference=source_reference, created_by="SYSTEM",
            )
            result.stored.append(f"{ex.kpi_name}={value:g} {kpi.unit or ex.unit or ''}".rstrip())
        except ResearchError:
            result.duplicates.append(ex.kpi_name)

    log.info(
        "extraction %s %s: stored=%d dup=%d ambiguous=%d unmatched=%d",
        investment.ticker, period, len(result.stored), len(result.duplicates),
        len(result.ambiguous), len(result.unmatched_kpi_names),
    )
    return result


def _kpi_review_proposal(
    session, investment, kpi: InvestmentKpi, ex: ExtractedKpi, period: str, source_reference: str
) -> None:
    title = f"Review extracted KPI: {kpi.name} = {ex.value:g} [{period}] (ambiguous)"
    exists = session.scalars(
        select(AiProposal).where(
            AiProposal.investment_id == investment.id,
            AiProposal.proposal_type == "KPI_OBSERVATION",
            AiProposal.title == title,
            AiProposal.status.in_(("PENDING", "DEFERRED")),
        )
    ).first()
    if exists:
        return
    from src.intelligence.ai.proposals import create_proposal

    create_proposal(
        session, "KPI_OBSERVATION", title, investment=investment,
        what_happened=f"Extractor found conflicting values: {ex.notes}",
        why_it_matters="Ambiguous extraction is never stored automatically - confirm the correct value.",
        proposed_change={"kpi_id": kpi.id, "period": period, "value": ex.value,
                         "source": "issuer_extractor:ambiguous", "source_reference": source_reference[:300]},
        reasoning=ex.excerpt[:500],
        provider="deterministic", prompt_version="issuer-extract-1.0", created_by="SYSTEM",
    )


_MONEY_SCALE = {"usd": 1.0, "usd m": 1e6, "usd million": 1e6, "usd millions": 1e6,
                "usd bn": 1e9, "usd billion": 1e9, "usd billions": 1e9}


def _reconcile_money_unit(value: float, extracted_unit: str | None, kpi_unit: str | None) -> float:
    """Deterministic million/billion reconciliation so the stored value matches the KPI's own
    unit (e.g. extractor emits USD millions, the KPI is defined in USD billions)."""
    src = _MONEY_SCALE.get((extracted_unit or "").strip().lower())
    dst = _MONEY_SCALE.get((kpi_unit or "").strip().lower())
    if src is None or dst is None or src == dst:
        return value
    return value * src / dst


# --- comparison engine -------------------------------------------------------


@dataclass
class KpiComparison:
    kpi: str
    unit: str | None
    direction_good: str | None
    current_period: str | None
    current: float | None
    prev_quarter: float | None
    prev_quarter_period: str | None
    yoy: float | None
    yoy_period: str | None
    qoq_change: float | None  # pp for %, relative % otherwise
    yoy_change: float | None
    expectation: str | None  # linked assumption expected range, if any
    flag: str | None  # None | "outside_expected_range" | "moving_against_good_direction"
    assumption: str | None


def compare_kpis(session: Session, investment: Investment) -> list[KpiComparison]:
    out: list[KpiComparison] = []
    for kpi in kpi_service.list_kpis(session, investment):
        obs = [o for o in kpi_service.observations(session, kpi) if o.period_date is not None]
        obs.sort(key=lambda o: o.period_date)
        if not obs:
            continue
        cur = obs[-1]
        prev = obs[-2] if len(obs) > 1 else None
        yoy = None
        for o in reversed(obs[:-1]):
            if abs((cur.period_date - o.period_date).days - 365) <= 45:
                yoy = o
                break
        is_pct = (kpi.unit or "").strip() == "%"

        def change(new: float, old: float | None) -> float | None:
            if old is None:
                return None
            return round(new - old, 4) if is_pct else (round(100 * (new - old) / old, 2) if old else None)

        linked = session.scalars(
            select(ThesisAssumption).where(ThesisAssumption.kpi_id == kpi.id, ThesisAssumption.active.is_(True))
        ).first()
        expectation = None
        flag = None
        if linked and (linked.expected_min is not None or linked.expected_max is not None):
            expectation = f"{linked.expected_min if linked.expected_min is not None else '-inf'}"
            expectation += f" to {linked.expected_max if linked.expected_max is not None else '+inf'} {linked.unit or ''}".rstrip()
            below = linked.expected_min is not None and cur.value < linked.expected_min
            above = linked.expected_max is not None and cur.value > linked.expected_max
            if below or above:
                flag = "outside_expected_range"
        if flag is None and prev is not None and kpi.direction_good in ("up", "down"):
            qoq = change(cur.value, prev.value)
            if qoq is not None and ((kpi.direction_good == "up" and qoq < 0) or (kpi.direction_good == "down" and qoq > 0)):
                flag = "moving_against_good_direction"
        out.append(
            KpiComparison(
                kpi=kpi.name, unit=kpi.unit, direction_good=kpi.direction_good,
                current_period=cur.period, current=cur.value,
                prev_quarter=prev.value if prev else None,
                prev_quarter_period=prev.period if prev else None,
                yoy=yoy.value if yoy else None, yoy_period=yoy.period if yoy else None,
                qoq_change=change(cur.value, prev.value if prev else None),
                yoy_change=change(cur.value, yoy.value if yoy else None),
                expectation=expectation, flag=flag,
                assumption=linked.name if linked else None,
            )
        )
    return out


def flag_contradictions(session: Session, investment: Investment, comparisons: list[KpiComparison]) -> list[int]:
    """Create ASSUMPTION_STATUS_CHANGE review proposals for out-of-range KPIs. Never mutates.
    Returns created proposal ids."""
    from src.intelligence.ai.proposals import create_proposal

    created = []
    for c in comparisons:
        if c.flag != "outside_expected_range" or not c.assumption:
            continue
        a = session.scalars(
            select(ThesisAssumption).where(
                ThesisAssumption.name == c.assumption,
                ThesisAssumption.active.is_(True),
            )
        ).first()
        if a is None or a.status in ("CHALLENGED", "BROKEN"):
            continue
        title = f"{investment.ticker}: {c.kpi} = {c.current:g}{c.unit or ''} [{c.current_period}] is outside expected {c.expectation}"
        exists = session.scalars(
            select(AiProposal).where(
                AiProposal.proposal_type == "ASSUMPTION_STATUS_CHANGE",
                AiProposal.title == title,
                AiProposal.status.in_(("PENDING", "DEFERRED")),
            )
        ).first()
        if exists:
            continue
        p = create_proposal(
            session, "ASSUMPTION_STATUS_CHANGE", title, investment=investment,
            what_happened=f"{c.kpi}: {c.prev_quarter} -> {c.current} ({c.qoq_change:+g}{'pp' if (c.unit or '') == '%' else '%'})"
            if c.prev_quarter is not None else f"{c.kpi} = {c.current}",
            why_it_matters=f"Contradicting datapoint for assumption '{c.assumption}'. "
                           "Review suggested; nothing changes without your acceptance.",
            proposed_change={"assumption_id": a.id, "new_status": "WEAKENING",
                             "note": f"{c.kpi} {c.current}{c.unit or ''} outside expected {c.expectation} [{c.current_period}]"},
            provider="deterministic", prompt_version="kpi-compare-1.0", created_by="SYSTEM",
        )
        created.append(p.id)
    return created


def comparison_table_text(comparisons: list[KpiComparison]) -> str:
    lines = []
    for c in comparisons:
        unit = c.unit or ""
        parts = [f"{c.kpi}: {c.current:g}{unit} [{c.current_period}]"]
        if c.prev_quarter is not None:
            parts.append(f"QoQ {c.qoq_change:+g}{'pp' if unit == '%' else '%'} (from {c.prev_quarter:g})")
        if c.yoy is not None:
            parts.append(f"YoY {c.yoy_change:+g}{'pp' if unit == '%' else '%'}")
        if c.expectation:
            parts.append(f"expected {c.expectation}")
        if c.flag:
            parts.append(f"FLAG: {c.flag}")
        lines.append(" | ".join(parts))
    return "\n".join(lines)
