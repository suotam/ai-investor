"""Management consistency tracker (foundation): sourced statements, promise vs outcome.

Every claim must point to a stored source (document id or explicit reference) - never
unsourced LLM memory. Status transitions are human-controlled in v4.
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.core import utcnow
from src.db.briefing import CLAIM_STATUSES, ManagementClaim
from src.db.research import Investment
from src.research.investments import ResearchError


def add_claim(
    session: Session,
    investment: Investment,
    statement: str,
    source_document_id: int | None = None,
    source_reference: str | None = None,
    speaker: str | None = None,
    claim_date: date | None = None,
    topic: str | None = None,
    time_horizon: str | None = None,
    created_by: str = "USER",
) -> ManagementClaim:
    if not statement.strip():
        raise ResearchError("statement is required")
    if source_document_id is None and not (source_reference or "").strip():
        raise ResearchError(
            "a management claim requires a source (source_document_id or source_reference); "
            "unsourced statements are not stored"
        )
    c = ManagementClaim(
        investment_id=investment.id, statement=statement.strip(), speaker=speaker,
        claim_date=claim_date, topic=topic, time_horizon=time_horizon,
        source_document_id=source_document_id, source_reference=source_reference,
        created_by=created_by,
    )
    session.add(c)
    session.flush()
    return c


def set_claim_status(session: Session, claim: ManagementClaim, status: str, outcome_note: str | None = None) -> ManagementClaim:
    if status not in CLAIM_STATUSES:
        raise ResearchError(f"invalid claim status {status!r}; allowed: {CLAIM_STATUSES}")
    claim.status = status
    claim.outcome_note = outcome_note
    claim.updated_at = utcnow()
    session.flush()
    return claim


def list_claims(session: Session, investment: Investment, status: str | None = None) -> list[ManagementClaim]:
    stmt = select(ManagementClaim).where(ManagementClaim.investment_id == investment.id)
    if status:
        stmt = stmt.where(ManagementClaim.status == status)
    return list(session.scalars(stmt.order_by(ManagementClaim.claim_date.desc().nullslast(), ManagementClaim.id.desc())))

# --- v5: deterministic claim extraction, outcome linking, track record --------

import re

from src.db.operations import CLAIM_LINK_TARGETS, CLAIM_TYPES, ClaimLink

# Forward-looking statement openers (verbatim capture; classification by keyword)
_CLAIM_PATTERN = re.compile(
    r"(?:^|(?<=[.!?])\s+)((?:We|The company|Management|Nu|Our team)\s+"
    r"(?:expect|anticipate|target|aim|plan|intend|remain confident|are confident|"
    r"guide|project|forecast|believe we will|will continue|reiterate)"
    r"[^.!?]{10,400}[.!?])",
    re.IGNORECASE,
)

_TYPE_KEYWORDS = [
    ("GUIDANCE", ("guide", "guidance", "outlook", "project", "forecast")),
    ("TARGET", ("target", "aim", "goal")),
    ("CAPITAL_ALLOCATION", ("buyback", "repurchase", "dividend", "capital allocation", "invest", "capex")),
    ("RISK_COMMENTARY", ("risk", "credit", "npl", "provision", "deterioration", "headwind")),
    ("STRATEGIC_CLAIM", ("strategy", "expand", "launch", "market", "growth", "customers")),
    ("EXPECTATION", ("expect", "anticipate", "believe", "confident")),
]

_HORIZON_PATTERN = re.compile(
    r"(?i)\b(in|by|for|during|over)\s+((?:H[12]|Q[1-4])\s*'?\s*\d{2,4}|20\d\d|"
    r"the (?:next|coming) (?:quarter|year|two years|few quarters)|(?:next|this) (?:quarter|year)|"
    r"(?:the )?(?:second|first) half(?: of 20\d\d)?|"
    r"(?:the )?(?:short|medium|long)[- ]term)\b"
)


def classify_claim(statement: str) -> str:
    low = statement.lower()
    for claim_type, keywords in _TYPE_KEYWORDS:
        if any(k in low for k in keywords):
            return claim_type
    return "OTHER"


def extract_horizon(statement: str) -> str | None:
    m = _HORIZON_PATTERN.search(statement)
    return f"{m.group(1)} {m.group(2)}" if m else None


def extract_claims_from_text(text: str) -> list[dict]:
    """Deterministic extraction of forward-looking management statements (verbatim quotes).
    Interpretation is NOT performed here - only capture + keyword classification."""
    out = []
    seen = set()
    for m in _CLAIM_PATTERN.finditer(text):
        statement = re.sub(r"\s+", " ", m.group(1)).strip()
        if statement.lower() in seen:
            continue
        seen.add(statement.lower())
        out.append({
            "statement": statement,
            "claim_type": classify_claim(statement),
            "time_horizon": extract_horizon(statement),
        })
    return out


def ingest_claims_from_source(
    session: Session,
    investment: Investment,
    text: str,
    source_document_id: int | None = None,
    source_reference: str | None = None,
    claim_date: date | None = None,
    speaker: str | None = None,
    speaker_role: str | None = None,
) -> list[ManagementClaim]:
    """Store extracted claims (created_by=SYSTEM; each is a verbatim sourced quote).
    Duplicate statements for the same investment are skipped."""
    existing = {
        c.statement.lower()
        for c in session.scalars(
            select(ManagementClaim).where(ManagementClaim.investment_id == investment.id)
        )
    }
    created = []
    for item in extract_claims_from_text(text):
        if item["statement"].lower() in existing:
            continue
        c = add_claim(
            session, investment, item["statement"],
            source_document_id=source_document_id,
            source_reference=source_reference or (f"source_documents:{source_document_id}" if source_document_id else None),
            claim_date=claim_date, speaker=speaker, topic=None,
            time_horizon=item["time_horizon"], created_by="SYSTEM",
        )
        c.claim_type = item["claim_type"]
        c.speaker_role = speaker_role
        created.append(c)
        existing.add(item["statement"].lower())
    session.flush()
    return created


def link_claim_outcome(
    session: Session, claim: ManagementClaim, target_type: str, target_id: int,
    note: str | None = None, new_status: str | None = None, outcome_note: str | None = None,
) -> ClaimLink:
    """Link a claim to later evidence/KPI/event; optionally set the human-judged status."""
    if target_type not in CLAIM_LINK_TARGETS:
        raise ResearchError(f"invalid target_type {target_type!r}; allowed: {CLAIM_LINK_TARGETS}")
    link = ClaimLink(claim_id=claim.id, target_type=target_type, target_id=target_id, note=note)
    session.add(link)
    if new_status:
        set_claim_status(session, claim, new_status, outcome_note=outcome_note or note)
    session.flush()
    return link


def track_record(session: Session, investment: Investment) -> dict:
    """Transparent management track record: counts by status + raw examples. No trust score."""
    claims = list_claims(session, investment)
    by_status: dict[str, int] = {}
    for c in claims:
        by_status[c.status] = by_status.get(c.status, 0) + 1
    resolved = [c for c in claims if c.status in ("CONFIRMED", "PARTIALLY_CONFIRMED", "MISSED", "FULFILLED", "BROKEN")]
    hits = [c for c in resolved if c.status in ("CONFIRMED", "FULFILLED")]
    examples = [
        {"statement": c.statement[:160], "type": c.claim_type, "status": c.status,
         "date": c.claim_date.isoformat() if c.claim_date else None,
         "outcome": (c.outcome_note or "")[:160]}
        for c in claims[:10]
    ]
    return {
        "total": len(claims),
        "open": by_status.get("OPEN", 0),
        "by_status": by_status,
        "resolved": len(resolved),
        "hit_rate": round(len(hits) / len(resolved), 2) if resolved else None,
        "examples": examples,
        "note": "Track record, not rhetoric: statuses are human-judged, examples stay visible.",
    }
