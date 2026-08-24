"""Investment context packet: compact, structured research state handed to the AI.

The AI never gets a bare news article - it gets "here is our thesis, assumptions, breakers,
risks, KPIs, valuation, decisions, predictions and evidence; what does this NEW information
change?".

Time-awareness (no-hindsight): pass `as_of` to build the packet from data that existed at
that time (rows filtered by created_at/observed_at). Retrospective analysis of a historical
decision must use as_of=decision time, and is labeled as retrospective by the caller.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.core import sha256_bytes
from src.db.research import (
    Catalyst,
    Investment,
    Prediction,
    Risk,
    ThesisBreaker,
)
from src.research import assumptions as asm
from src.research import evidence as ev
from src.research import kpis as kpi_service
from src.research.decisions import list_decisions
from src.research.predictions import list_predictions
from src.research.theses import active_thesis, current_version
from src.research.valuation import models_for, scenarios_for, summarize_model


@dataclass
class ContextPacket:
    investment_ticker: str
    as_of: str | None
    data: dict
    context_hash: str

    def to_prompt(self) -> str:
        return json.dumps(self.data, indent=1, default=str)


def _keep(row_created: datetime | None, as_of: datetime | None) -> bool:
    return as_of is None or row_created is None or row_created <= as_of


def build_context_packet(
    session: Session, investment: Investment, as_of: datetime | None = None
) -> ContextPacket:
    d: dict = {
        "ticker": investment.ticker,
        "name": investment.name,
        "lifecycle_status": investment.status,
        "as_of": as_of.isoformat() if as_of else None,
        "note": "All monetary figures come from stored sources; UNKNOWN means data is missing.",
    }

    thesis = active_thesis(session, investment)
    if thesis:
        version = current_version(session, thesis)
        # time-aware: use the version that existed at as_of
        if as_of is not None and version is not None and version.created_at > as_of:
            from src.db.research import ThesisVersion

            version = session.scalars(
                select(ThesisVersion)
                .where(ThesisVersion.thesis_id == thesis.id, ThesisVersion.created_at <= as_of)
                .order_by(ThesisVersion.version_number.desc())
            ).first()
        if version:
            d["thesis"] = {
                "title": thesis.title,
                "version": version.version_number,
                "created_at": version.created_at.isoformat(),
                "core_thesis": version.core_thesis,
                "market_expectation": version.market_expectation,
                "our_expectation": version.our_expectation,
                "why_market_may_be_wrong": version.why_market_may_be_wrong,
                "confidence_0_100": version.confidence,
                "time_horizon": version.time_horizon,
            }
        d["assumptions"] = [
            {
                "id": a.id, "name": a.name, "category": a.category, "importance": a.importance,
                "status": a.status, "expected_min": a.expected_min, "expected_max": a.expected_max,
                "unit": a.unit, "breaker_condition": a.breaker_condition,
            }
            for a in asm.list_assumptions(session, thesis)
            if _keep(a.created_at, as_of)
        ]

    d["breakers"] = [
        {"id": b.id, "name": b.name, "severity": b.severity, "status": b.status, "condition": b.condition_text}
        for b in session.scalars(select(ThesisBreaker).where(ThesisBreaker.investment_id == investment.id))
        if _keep(b.created_at, as_of)
    ]
    d["risks"] = [
        {"id": r.id, "name": r.name, "category": r.category, "severity": r.severity, "status": r.status}
        for r in session.scalars(select(Risk).where(Risk.investment_id == investment.id))
        if _keep(r.created_at, as_of)
    ]
    d["catalysts"] = [
        {"id": c.id, "name": c.name, "status": c.status, "expected_date": c.expected_date}
        for c in session.scalars(select(Catalyst).where(Catalyst.investment_id == investment.id))
        if _keep(c.created_at, as_of)
    ]

    kpis_out = []
    for kpi in kpi_service.list_kpis(session, investment):
        obs = [
            o for o in kpi_service.observations(session, kpi)
            if _keep(o.observed_at or o.created_at, as_of)
        ][-6:]
        kpis_out.append(
            {
                "name": kpi.name, "unit": kpi.unit, "direction_good": kpi.direction_good,
                "recent_observations": [
                    {"period": o.period, "value": o.value, "source": o.source} for o in obs
                ] or "UNKNOWN (no stored observations)",
            }
        )
    d["kpis"] = kpis_out

    vals = []
    for m in models_for(session, investment):
        if not _keep(m.created_at, as_of):
            continue
        s = summarize_model(m, scenarios_for(session, m))
        vals.append(
            {
                "model": m.name, "reference_price": m.reference_price,
                "currency": m.reference_currency,
                "scenarios": [
                    {"name": r.scenario_name, "probability": float(r.probability) if r.probability is not None else None,
                     "target": float(r.target_price)}
                    for r in s.scenarios
                ],
                "weighted_target": float(s.weighted_target) if s.weighted_target is not None else None,
            }
        )
    d["valuation"] = vals

    d["recent_decisions"] = [
        {"type": dec.decision_type, "date": dec.decided_at.date().isoformat(), "reasoning": dec.reasoning,
         "confidence": dec.confidence}
        for dec in list_decisions(session, investment)[:5]
        if _keep(dec.decided_at, as_of)
    ]
    d["open_predictions"] = [
        {"id": p.id, "statement": p.statement, "probability": p.probability,
         "resolution_date": p.resolution_date}
        for p in list_predictions(session, investment, status="OPEN")
        if _keep(p.created_at, as_of)
    ]
    d["evidence"] = {
        direction.lower(): [
            {"title": e.title, "type": e.evidence_type, "date": (e.source_date or e.created_at.date()).isoformat(),
             "reliability": e.reliability}
            for e in rows[:8]
            if _keep(e.created_at, as_of)
        ]
        for direction, rows in ev.evidence_by_direction(session, investment).items()
    }

    payload = json.dumps(d, sort_keys=True, default=str)
    return ContextPacket(
        investment_ticker=investment.ticker,
        as_of=d["as_of"],
        data=d,
        context_hash=sha256_bytes(payload.encode("utf-8")),
    )
