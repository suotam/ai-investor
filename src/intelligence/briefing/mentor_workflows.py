"""v5 mentor workflows: opportunity cost, add-review, exit-review, feedback preferences,
"teach me" concepts, research questions.

Deterministic facts always remain visible; AI (optional) synthesizes considerations - never
an order, never an exact size.
"""
from __future__ import annotations

import json
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.config import Settings
from src.core import utcnow
from src.db.briefing import BriefFeedback, BriefItem
from src.db.operations import ConceptHistory
from src.db.research import Investment
from src.intelligence.ai.provider import AIInvalidOutput, AIProvider
from src.intelligence.technical import technical_context_for_instrument
from src.research.health import thesis_health
from src.research.theses import active_thesis, current_version
from src.research.valuation import models_for, scenarios_for, summarize_model


# --- opportunity cost --------------------------------------------------------


def opportunity_table(session: Session, settings: Settings) -> list[dict]:
    """Owned positions + research candidates across visible dimensions. No composite score."""
    from src.db.intelligence import ResearchCandidate
    from src.db.research import Risk
    from src.portfolio.valuation import value_portfolio

    val = value_portfolio(session, settings.base_currency)
    weights = {r.symbol: float(r.weight) if r.weight is not None else None for r in val.positions}
    rows = []
    for inv in session.scalars(select(Investment).where(Investment.status.notin_(("ARCHIVED", "REJECTED", "EXITED")))):
        h = thesis_health(session, inv)
        thesis = active_thesis(session, inv)
        v = current_version(session, thesis) if thesis else None
        expected = None
        for m in models_for(session, inv):
            s = summarize_model(m, scenarios_for(session, m))
            if s.weighted_return is not None:
                expected = round(float(s.weighted_return) * 100, 1)
        open_risks = len(list(session.scalars(
            select(Risk).where(Risk.investment_id == inv.id, Risk.status == "OPEN")
        )))
        rows.append({
            "ticker": inv.ticker, "status": inv.status,
            "weight_pct": round(weights.get(inv.ticker, 0) * 100, 1) if weights.get(inv.ticker) else 0.0,
            "expected_return_pct": expected,
            "thesis_confidence": v.confidence if v else None,
            "thesis_health": h.state or "n/a",
            "open_risks": open_risks,
            "time_horizon": v.time_horizon if v else None,
        })
    for c in session.scalars(select(ResearchCandidate).where(ResearchCandidate.status == "NEW")):
        rows.append({
            "ticker": c.ticker, "status": f"CANDIDATE ({c.source})", "weight_pct": 0.0,
            "expected_return_pct": None, "thesis_confidence": None, "thesis_health": "no thesis yet",
            "open_risks": None, "time_horizon": None,
        })
    return rows


# --- add / exit review agents -----------------------------------------------


class PositionReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    arguments_for: list[str] = Field(default_factory=list)
    arguments_against: list[str] = Field(default_factory=list)
    what_would_improve_entry: Optional[str] = None
    what_would_make_waiting_better: Optional[str] = None
    unknowns: list[str] = Field(default_factory=list)
    summary: str = ""


ADD_PROMPT_VERSION = "add-review-1.0"
EXIT_PROMPT_VERSION = "exit-review-1.0"


def _position_facts(session: Session, settings: Settings, inv: Investment) -> dict:
    from src.portfolio.valuation import value_portfolio

    val = value_portfolio(session, settings.base_currency)
    pos = next((r for r in val.positions if r.instrument_id == inv.instrument_id), None)
    facts: dict = {
        "ticker": inv.ticker,
        "current_weight_pct": round(float(pos.weight) * 100, 1) if pos and pos.weight is not None else 0.0,
        "thesis_health": thesis_health(session, inv).state,
        "valuation": [],
        "technical_context": [],
        "alternatives": [r["ticker"] for r in opportunity_table(session, settings) if r["ticker"] != inv.ticker][:5],
    }
    thesis = active_thesis(session, inv)
    v = current_version(session, thesis) if thesis else None
    facts["thesis_one_line"] = (v.core_thesis or "")[:250] if v else None
    facts["thesis_confidence"] = v.confidence if v else None
    for m in models_for(session, inv):
        s = summarize_model(m, scenarios_for(session, m))
        facts["valuation"].append({
            "model": m.name, "reference": m.reference_price,
            "weighted_target": float(s.weighted_target) if s.weighted_target is not None else None,
        })
    if inv.instrument_id:
        facts["technical_context"] = technical_context_for_instrument(session, inv.instrument_id).statements()
    from src.research.evidence import evidence_by_direction

    by_dir = evidence_by_direction(session, inv)
    facts["recent_contradicting_evidence"] = [e.title for e in by_dir["CONTRADICTING"][:5]]
    return facts


def _review(session, settings, provider: AIProvider, inv: Investment, mode: str) -> tuple[dict, PositionReview]:
    facts = _position_facts(session, settings, inv)
    focus = (
        "The user considers ADDING to this position. Weigh thesis strength, valuation vs "
        "targets, concentration, technical entry context and alternatives."
        if mode == "add"
        else "The user considers SELLING/TRIMMING. Focus strictly on: is the thesis broken? "
        "is valuation extreme vs the user's own targets? has risk or opportunity cost "
        "changed? did the original thesis play out? or is this reaction to price noise?"
    )
    system = (
        "You are a calm, skeptical position-review mentor. " + focus + "\n"
        "Rules: use ONLY the provided facts; UNKNOWN where data is missing; NEVER give an "
        "order, a target size, or a buy/sell instruction - list considerations. Respond with "
        "JSON keys: arguments_for, arguments_against, what_would_improve_entry, "
        "what_would_make_waiting_better, unknowns, summary."
    )
    raw = provider.complete_json(system, json.dumps(facts, indent=1, default=str))
    try:
        review = PositionReview.model_validate(raw)
    except ValidationError as exc:
        raise AIInvalidOutput(f"position review failed schema validation: {exc.errors()[:3]}") from exc
    return facts, review


def add_review(session, settings, provider, inv):
    return _review(session, settings, provider, inv, "add")


def exit_review(session, settings, provider, inv):
    return _review(session, settings, provider, inv, "exit")


# --- feedback preferences (deterministic, inspectable) -----------------------


def feedback_preferences(session: Session) -> dict:
    """Aggregate stored feedback per delta type. Used only as an ORDERING hint (documented),
    never as silent filtering; fully inspectable via `mentor prefs`."""
    items = {i.id: i for i in session.scalars(select(BriefItem))}
    by_type: dict[str, dict[str, int]] = {}
    for fb in session.scalars(select(BriefFeedback)):
        dt = None
        if fb.brief_item_id and fb.brief_item_id in items:
            dt = items[fb.brief_item_id].delta_type
        else:
            prefix = fb.item_key.split(":", 1)[0]
            dt = {"risk": "NEW_RISK", "evidence": "NEW_EVIDENCE", "event": "NEW_EVENT",
                  "insider": "NEW_INSIDER_ACTIVITY", "macro": "NEW_MACRO_OBSERVATION"}.get(prefix, prefix)
        bucket = by_type.setdefault(dt, {r: 0 for r in ("USEFUL", "NOT_USEFUL", "TOO_NOISY", "MORE_LIKE_THIS")})
        if fb.rating in bucket:
            bucket[fb.rating] += 1
    prefs = {}
    for dt, counts in by_type.items():
        positive = counts["USEFUL"] + counts["MORE_LIKE_THIS"]
        negative = counts["NOT_USEFUL"] + counts["TOO_NOISY"]
        prefs[dt] = {"counts": counts,
                     "hint": "prefer" if positive > negative else ("deprioritize" if negative > positive else "neutral")}
    return prefs


# --- teach me ----------------------------------------------------------------

CONCEPTS: dict[str, dict] = {
    "NPL 90+": {"tags": ["NEW_KPI", "credit"], "text": (
        "Loans more than 90 days past due as a share of the portfolio. It is a LAGGING credit "
        "indicator: today's 90+ bucket reflects loans that stopped paying a quarter ago, so "
        "early-stage buckets (15-90) and vintage cohorts turn first.")},
    "Operating leverage": {"tags": ["NEW_KPI", "margin"], "text": (
        "Revenue growing faster than costs: each new customer adds revenue at low marginal "
        "cost, so margins expand as the platform scales - and compress fast if growth stalls.")},
    "ROE": {"tags": ["NEW_KPI"], "text": (
        "Net income over shareholders' equity - how much profit each unit of capital produces. "
        "Durably high ROE with a long reinvestment runway is the engine of compounding; watch "
        "whether it relies on rising leverage.")},
    "FX translation": {"tags": ["NEW_MACRO_OBSERVATION", "PRICE_MOVE"], "text": (
        "A company earning in BRL but reported in USD shows lower dollar results when BRL "
        "weakens even if the local business is unchanged. Separate currency translation from "
        "operating performance.")},
    "Yield curve": {"tags": ["NEW_MACRO_OBSERVATION"], "text": (
        "The spread between long and short government yields. Inversion (short above long) has "
        "historically preceded slowdowns; steepening often accompanies easing cycles.")},
    "Margin of safety": {"tags": ["VALUATION_REVIEW"], "text": (
        "The gap between your estimate of fair value and the price paid. It exists to absorb "
        "estimation error - not to maximize upside.")},
    "Base rates": {"tags": ["NEW_DECISION", "RED_TEAM_ARGUMENT"], "text": (
        "Before trusting a specific story, ask how often similar situations worked out in "
        "general. Few banks sustain 25%+ ROE for a decade - your thesis should explain why "
        "this one is the exception.")},
}


def pick_concept(session: Session, recent_delta_types: list[str]) -> tuple[str, str] | None:
    shown = {c.concept for c in session.scalars(select(ConceptHistory))}
    candidates = [
        (name, c["text"]) for name, c in CONCEPTS.items()
        if name not in shown and (not recent_delta_types or any(t in recent_delta_types for t in c["tags"]))
    ]
    if not candidates:
        candidates = [(n, c["text"]) for n, c in CONCEPTS.items() if n not in shown]
    if not candidates:
        return None  # all concepts shown; history keeps them from repeating endlessly
    name, text = candidates[0]
    session.add(ConceptHistory(concept=name))
    session.flush()
    return name, text


# --- research questions ------------------------------------------------------


def open_research_questions(session: Session) -> list[dict]:
    from src.db.intelligence import AiProposal

    rows = list(session.scalars(
        select(AiProposal).where(
            AiProposal.proposal_type.in_(("RESEARCH_QUESTION", "VALUATION_QUESTION")),
            AiProposal.status.in_(("PENDING", "DEFERRED")),
        ).order_by(AiProposal.confidence.desc().nullslast())
    ))
    return [{"id": p.id, "question": p.title, "why": p.why_it_matters, "confidence": p.confidence} for p in rows]


def search_local_sources(session: Session, keywords: list[str], limit: int = 8) -> list[dict]:
    """Deterministic local search of archived source documents for a research question.
    No web browsing - only what Investor OS already stored."""
    from pathlib import Path

    from src.db.intelligence import SourceDocument

    hits = []
    for doc in session.scalars(select(SourceDocument).where(SourceDocument.raw_path.isnot(None))):
        path = Path(doc.raw_path)
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        low = text.lower()
        if all(k.lower() in low for k in keywords):
            idx = low.find(keywords[0].lower())
            excerpt = " ".join(text[max(0, idx - 150): idx + 250].split())
            hits.append({"source_document_id": doc.id, "title": doc.title or doc.external_id,
                         "excerpt": excerpt})
            if len(hits) >= limit:
                break
    return hits
