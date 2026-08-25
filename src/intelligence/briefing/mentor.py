"""Mentor synthesis: ONE compact Glimmer call over the deterministic deltas.

Performance discipline (local model ~3-4 tok/s): no per-event calls, no full-database dumps -
the input is the already-filtered delta list plus one-line thesis summaries. The mentor
explains; it never computes numbers and never recommends trades. Failure degrades to the
deterministic brief with a clear note.
"""
from __future__ import annotations

import json
from typing import Optional

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.core import sha256_bytes
from src.db.research import Investment
from src.intelligence.ai.provider import AIInvalidOutput, AIProvider
from src.intelligence.briefing.assemble import BriefDocument
from src.logging_setup import get_logger

log = get_logger("briefing.mentor")

MENTOR_PROMPT_VERSION = "mentor-daily-1.0"

MENTOR_STYLE = """You are a calm, skeptical, source-grounded long-term investment mentor.
Rules:
- Explain today's meaningful changes to a disciplined long-term investor in natural prose
  suitable for listening (no tables, no markdown, no hype, no trading language).
- Use ONLY the deltas and thesis summaries provided. Numbers must come from the input.
  If something is not in the input, do not invent it.
- Always distinguish PRICE changes from BUSINESS changes. A price move with no new company
  evidence means: price changed, thesis did not.
- Explicitly say what is noise and requires no action. "No action required" is a valuable
  conclusion. Never recommend buying or selling.
- Challenge the user when evidence warrants it, without theatrical contrarianism."""


class MentorSynthesis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    today_in_one_minute: str
    portfolio: Optional[str] = None
    thesis_changes: Optional[str] = None
    macro: Optional[str] = None
    new_opportunities: Optional[str] = None
    needs_your_judgment: Optional[str] = None
    can_be_ignored: Optional[str] = None


def _compact_context(session: Session, doc: BriefDocument) -> dict:
    """The ONLY data the mentor sees: surfaced deltas + one-line thesis summaries."""
    from src.research.theses import active_thesis, current_version

    theses = {}
    for inv in session.scalars(select(Investment).where(Investment.status == "OWNED")):
        t = active_thesis(session, inv)
        v = current_version(session, t) if t else None
        if v:
            theses[inv.ticker] = {
                "core_thesis_one_line": (v.core_thesis or "")[:220],
                "confidence": v.confidence,
                "version": v.version_number,
            }
    items = []
    for section in (
        doc.portfolio_items, [d for s in doc.investment_sections for d in s.items],
        doc.macro_items, doc.judgment_items, doc.opportunity_items, doc.other_items,
    ):
        for d in section:
            items.append({
                "type": d.delta_type, "severity": d.severity, "title": d.title,
                "detail": d.detail, "why_shown": d.reason,
            })
    return {
        "date": doc.brief_date.isoformat(),
        "portfolio_headline": doc.portfolio_lines,
        "theses": theses,
        "deltas": items,
        "suppressed_low_signal_items": doc.suppressed_count,
        "note": "Deltas are deterministic and pre-filtered; there is nothing else new today.",
    }


def synthesize(
    session: Session, provider: AIProvider, doc: BriefDocument
) -> tuple[dict | None, str | None, str | None]:
    """Returns (synthesis dict | None, context_hash, error note). Never raises."""
    context = _compact_context(session, doc)
    payload = json.dumps(context, indent=1, default=str)
    context_hash = sha256_bytes(payload.encode("utf-8"))
    system = (
        MENTOR_STYLE
        + "\nProduce a single JSON object with keys: today_in_one_minute (required, ~4 sentences), "
        "portfolio, thesis_changes, macro, new_opportunities, needs_your_judgment, can_be_ignored "
        "(each a short spoken-style paragraph or null when there is nothing to say)."
    )
    try:
        raw = provider.complete_json(system, f"TODAY'S DELTAS AND CONTEXT:\n{payload}", max_tokens=900)
        synthesis = MentorSynthesis.model_validate(raw)
        return synthesis.model_dump(), context_hash, None
    except (AIInvalidOutput, ValidationError) as exc:
        log.warning("mentor synthesis invalid output: %s", exc)
        return None, context_hash, "AI mentor synthesis unavailable (invalid model output)."
    except Exception as exc:  # AIUnavailable and transport errors degrade gracefully
        log.info("mentor synthesis unavailable: %s", exc)
        return None, context_hash, "AI mentor synthesis unavailable."
