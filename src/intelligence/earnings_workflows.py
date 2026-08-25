"""Earnings preview & post-mortem workflows (deterministic documents; AI optional on top).

Preview (before results): thesis, top KPIs with previous values and expected ranges (only
when structured expectations exist - nothing fabricated), key questions from breakers and
open management claims, valuation and technical context.

Post-mortem (after results): ACTUAL vs PREVIOUS vs OUR EXPECTATION vs CONSENSUS (only if a
'consensus'-sourced observation was stored), flags, open claims to verify. AI synthesis
reuses the existing earnings-review agent and produces proposals only.
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.config import Settings
from src.db.briefing import ManagementClaim
from src.db.research import Investment, ThesisBreaker
from src.intelligence.claims import list_claims
from src.intelligence.earnings import compare_kpis
from src.intelligence.technical import technical_context_for_instrument
from src.research import kpis as kpi_service
from src.research.theses import active_thesis, current_version
from src.research.valuation import models_for, scenarios_for, summarize_model


def _valuation_lines(session: Session, inv: Investment) -> list[str]:
    out = []
    for m in models_for(session, inv):
        s = summarize_model(m, scenarios_for(session, m))
        line = f"{m.name}: reference {m.reference_price or '?'} {m.reference_currency or ''}"
        if s.weighted_target is not None:
            line += f", probability-weighted target {float(s.weighted_target):g}"
        out.append(line)
    return out or ["No valuation model stored."]


def earnings_preview(session: Session, settings: Settings, inv: Investment) -> str:
    thesis = active_thesis(session, inv)
    version = current_version(session, thesis) if thesis else None
    lines = [f"# Pre-Earnings Checklist — {inv.ticker}", ""]
    lines.append("## Current thesis")
    lines.append((version.core_thesis or "No thesis recorded.") if version else "No thesis recorded.")
    lines.append("")

    lines.append("## Top thesis-driving KPIs (previous values + our expected range)")
    from src.db.research import ThesisAssumption

    expectations = {
        a.kpi_id: a
        for a in (session.scalars(
            select(ThesisAssumption).where(ThesisAssumption.thesis_id == thesis.id, ThesisAssumption.active.is_(True))
        ) if thesis else [])
        if a.kpi_id is not None
    }
    ranked = sorted(
        kpi_service.list_kpis(session, inv),
        key=lambda k: {"HIGH": 0, "MEDIUM": 1, "LOW": 2}.get(k.importance, 3),
    )[:5]
    for kpi in ranked:
        obs = kpi_service.observations(session, kpi)
        actuals = [o for o in obs if (o.source or "").lower() != "consensus"]
        last = actuals[-1] if actuals else None
        prev_txt = f"{last.value:g}{kpi.unit or ''} [{last.period}]" if last else "no stored value"
        line = f"* {kpi.name}: previous {prev_txt}"
        a = expectations.get(kpi.id)
        if a and (a.expected_min is not None or a.expected_max is not None):
            line += f" | our expected range {a.expected_min if a.expected_min is not None else '-inf'}–{a.expected_max if a.expected_max is not None else '+inf'} {a.unit or ''}".rstrip()
        else:
            line += " | no structured expectation stored (not fabricated)"
        consensus = [o for o in obs if (o.source or "").lower() == "consensus"]
        if consensus:
            line += f" | consensus {consensus[-1].value:g} [{consensus[-1].period}]"
        lines.append(line)
    lines.append("")

    lines.append("## Key questions")
    breakers = list(session.scalars(
        select(ThesisBreaker).where(ThesisBreaker.investment_id == inv.id, ThesisBreaker.status == "ACTIVE")
    ))
    for b in breakers[:5]:
        lines.append(f"* Could this quarter move breaker '{b.name}' closer? ({b.condition_text or 'no condition text'})")
    open_claims = [c for c in list_claims(session, inv) if c.status == "OPEN"]
    lines.append("")
    lines.append("## What strengthens the thesis / what weakens it")
    lines.append("* Strengthens: KPIs inside expected ranges; assumptions confirmed by reported figures.")
    lines.append("* Weakens: KPIs outside expected ranges (see above); breaker conditions approached.")
    lines.append("")
    lines.append("## Open management claims to verify this quarter")
    for c in open_claims[:8]:
        lines.append(f"* [{c.claim_type}] {c.statement[:160]} ({c.time_horizon or 'no horizon'})")
    if not open_claims:
        lines.append("* None recorded.")
    lines.append("")
    lines.append("## Valuation")
    lines.extend(f"* {v}" for v in _valuation_lines(session, inv))
    lines.append("")
    if inv.instrument_id:
        lines.append("## Price / technical context")
        ctx = technical_context_for_instrument(session, inv.instrument_id)
        lines.extend(f"* {t}" for t in ctx.statements())
    return "\n".join(lines)


def earnings_postmortem(
    session: Session, settings: Settings, inv: Investment, period: str | None = None
) -> str:
    comps = compare_kpis(session, inv)
    lines = [f"# Earnings Post-Mortem — {inv.ticker}" + (f" [{period}]" if period else ""), ""]
    lines.append("## ACTUAL vs PREVIOUS vs OUR EXPECTATION vs CONSENSUS")
    surprises = []
    for c in comps:
        if period and c.current_period != period:
            continue
        unit = c.unit or ""
        row = f"* {c.kpi}: actual {c.current:g}{unit} [{c.current_period}]"
        if c.prev_quarter is not None:
            row += f" | prev {c.prev_quarter:g} ({c.qoq_change:+g}{'pp' if unit == '%' else '%'})"
        if c.yoy is not None:
            row += f" | YoY {c.yoy:g} ({c.yoy_change:+g}{'pp' if unit == '%' else '%'})"
        row += f" | expected {c.expectation}" if c.expectation else " | no structured expectation"
        # consensus only when explicitly stored
        kpi = next((k for k in kpi_service.list_kpis(session, inv) if k.name == c.kpi), None)
        consensus = [
            o for o in (kpi_service.observations(session, kpi) if kpi else [])
            if (o.source or "").lower() == "consensus" and (not period or o.period == period)
        ]
        row += f" | consensus {consensus[-1].value:g}" if consensus else ""
        if c.flag:
            row += f"  <-- {c.flag}"
            surprises.append(f"{c.kpi}: {c.flag}")
        lines.append(row)
    lines.append("")
    lines.append("## What surprised us (deterministic flags)")
    lines.extend(f"* {x}" for x in surprises) if surprises else lines.append("* Nothing outside stored expectations.")
    lines.append("")
    lines.append("## Management claims touching this quarter")
    for c in [c for c in list_claims(session, inv) if c.status == "OPEN"][:8]:
        lines.append(f"* OPEN: [{c.claim_type}] {c.statement[:150]}")
        lines.append(f"  -> verify against reported figures; link the outcome with `claims link`.")
    lines.append("")
    lines.append("## What should the thesis review focus on")
    for x in surprises or ["No deterministic surprises - review qualitative commentary and guidance."]:
        lines.append(f"* {x}")
    lines.append("")
    lines.append("*Deterministic document; run `ai earnings " + inv.ticker + "` for the mentor's structured review (proposals only).*")
    return "\n".join(lines)
