"""Intelligence events: the central inbox. Deterministic materiality; idempotent by dedup_key."""
from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.core import utcnow
from src.db.intelligence import EVENT_TYPES, IntelligenceEvent
from src.logging_setup import get_logger
from src.research.investments import ResearchError

log = get_logger("intelligence.events")

# Deterministic materiality rules (spec: transparent, AI may suggest but never override these)
DETERMINISTIC_SEVERITY = {
    "EARNINGS_RELEASE": "HIGH",
    "GUIDANCE_CHANGE": "HIGH",
    "NEW_FILING": "MEDIUM",  # upgraded to HIGH for annual/major forms in the connector
    "KPI_UPDATE": "MEDIUM",
    "INSIDER_TRANSACTION": "MEDIUM",  # open-market only; other codes are LOW
    "CONGRESS_TRANSACTION": "LOW",
    "INSTITUTIONAL_CHANGE": "MEDIUM",
    "MACRO_RELEASE": "LOW",  # MEDIUM when linked to an investment
    "PRICE_EVENT": "LOW",
    "NEWS_EVENT": "LOW",
}

HIGH_MATERIALITY_FORMS = {"10-K", "10-Q", "20-F", "8-K", "6-K"}


def default_severity(event_type: str, **hints) -> str:
    sev = DETERMINISTIC_SEVERITY.get(event_type, "LOW")
    if event_type == "NEW_FILING" and hints.get("form") in ("10-K", "20-F"):
        sev = "HIGH"
    if event_type == "INSIDER_TRANSACTION" and hints.get("transaction_type") not in (
        "open_market_purchase", "open_market_sale"
    ):
        sev = "LOW"
    if event_type == "MACRO_RELEASE" and hints.get("linked_investment"):
        sev = "MEDIUM"
    return sev


def record_event(
    session: Session,
    event_type: str,
    dedup_key: str,
    title: str,
    occurred_at: datetime,
    investment_id: int | None = None,
    instrument_id: int | None = None,
    summary: str | None = None,
    severity: str | None = None,
    source_document_id: int | None = None,
    payload: dict | None = None,
    **severity_hints,
) -> tuple[IntelligenceEvent, bool]:
    """Idempotent event creation. Returns (event, created)."""
    if event_type not in EVENT_TYPES:
        raise ResearchError(f"invalid event_type {event_type!r}; allowed: {EVENT_TYPES}")
    existing = session.scalars(
        select(IntelligenceEvent).where(IntelligenceEvent.dedup_key == dedup_key)
    ).first()
    if existing is not None:
        return existing, False
    ev = IntelligenceEvent(
        event_type=event_type,
        dedup_key=dedup_key,
        title=title[:400],
        occurred_at=occurred_at,
        investment_id=investment_id,
        instrument_id=instrument_id,
        summary=summary,
        severity=severity or default_severity(event_type, **severity_hints),
        materiality_source="deterministic",
        source_document_id=source_document_id,
        payload_json=json.dumps(payload, default=str) if payload else None,
    )
    session.add(ev)
    session.flush()
    log.info("event %s [%s] %s", ev.event_type, ev.severity, ev.title[:80])
    return ev, True


def list_events(
    session: Session,
    investment_id: int | None = None,
    state: str | None = None,
    min_severity: str | None = None,
    limit: int = 200,
) -> list[IntelligenceEvent]:
    stmt = select(IntelligenceEvent).order_by(IntelligenceEvent.occurred_at.desc(), IntelligenceEvent.id.desc())
    if investment_id is not None:
        stmt = stmt.where(IntelligenceEvent.investment_id == investment_id)
    if state:
        stmt = stmt.where(IntelligenceEvent.processing_state == state)
    events = list(session.scalars(stmt.limit(limit)))
    if min_severity:
        order = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
        events = [e for e in events if order[e.severity] >= order[min_severity]]
    return events


def set_event_state(session: Session, event: IntelligenceEvent, state: str) -> None:
    if state not in ("NEW", "PROCESSED", "DISMISSED"):
        raise ResearchError(f"invalid event state {state!r}")
    event.processing_state = state
    session.flush()


def payload_of(event: IntelligenceEvent) -> dict:
    try:
        return json.loads(event.payload_json) if event.payload_json else {}
    except json.JSONDecodeError:
        return {}
