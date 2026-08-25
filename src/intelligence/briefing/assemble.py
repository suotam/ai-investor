"""Deterministic brief assembly: deltas -> structured document -> markdown + audio text.

Design targets (v4):
  * DELTA, NOT STATE - static risks/theses never repeat; background context is one line;
  * a no-change day produces exactly: "No material thesis-relevant developments since the
    previous brief." and that is a GOOD output;
  * every item keeps its "why am I seeing this" reason and source references;
  * audio text is natural prose (no tables, no markdown), ~5-10 minutes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.config import Settings
from src.db.research import Investment, Risk
from src.intelligence.briefing.deltas import Delta, SEV_ORDER
from src.intelligence.calendar import upcoming_events
from src.portfolio.valuation import value_portfolio
from src.research.health import thesis_health

NO_CHANGE_SENTENCE = "No material thesis-relevant developments since the previous brief."

PORTFOLIO_TYPES = {"PORTFOLIO_MOVE", "PRICE_MOVE", "WEIGHT_CHANGE"}
JUDGMENT_TYPES = {"NEW_PROPOSAL", "REVIEW_DUE", "PREDICTION_DUE", "VALUATION_REVIEW", "BREAKER_TRIGGER"}
MACRO_TYPES = {"NEW_MACRO_OBSERVATION"}
OPPORTUNITY_TYPES = {"NEW_DISCOVERY_CANDIDATE"}


@dataclass
class InvestmentSection:
    ticker: str
    thesis_status: str  # UNCHANGED | NEEDS REVIEW | AT RISK
    status_reason: str
    items: list[Delta] = field(default_factory=list)
    background: str | None = None
    next_trigger: str | None = None


@dataclass
class BriefDocument:
    brief_type: str
    brief_date: date
    mode: str  # new | preview | force
    executive_summary: list[str] = field(default_factory=list)
    portfolio_lines: list[str] = field(default_factory=list)
    portfolio_items: list[Delta] = field(default_factory=list)
    investment_sections: list[InvestmentSection] = field(default_factory=list)
    macro_items: list[Delta] = field(default_factory=list)
    opportunity_items: list[Delta] = field(default_factory=list)
    judgment_items: list[Delta] = field(default_factory=list)
    other_items: list[Delta] = field(default_factory=list)
    upcoming: list[dict] = field(default_factory=list)
    suppressed_count: int = 0
    no_change: bool = False
    ai_synthesis: dict | None = None  # mentor output sections, if AI ran
    ai_note: str | None = None
    weekly_extras: dict = field(default_factory=dict)


def build_document(
    session: Session,
    settings: Settings,
    brief_type: str,
    surfaced: list[Delta],
    suppressed: list[Delta],
    mode: str,
    now: datetime,
    max_items: int | None = None,
) -> BriefDocument:
    max_items = max_items or (settings.brief_daily_max_items if brief_type == "daily" else settings.brief_weekly_max_items)
    doc = BriefDocument(brief_type=brief_type, brief_date=now.date(), mode=mode,
                        suppressed_count=len(suppressed))
    investments = {
        i.id: i for i in session.scalars(
            select(Investment).where(Investment.status.notin_(("ARCHIVED", "REJECTED")))
        )
    }

    ranked = sorted(surfaced, key=lambda d: -SEV_ORDER.get(d.severity, 0))
    kept = ranked[:max_items]
    overflow = ranked[max_items:]
    doc.suppressed_count += len(overflow)

    for d in kept:
        if d.delta_type in PORTFOLIO_TYPES:
            doc.portfolio_items.append(d)
        elif d.delta_type in MACRO_TYPES:
            doc.macro_items.append(d)
        elif d.delta_type in OPPORTUNITY_TYPES:
            doc.opportunity_items.append(d)
        elif d.delta_type in JUDGMENT_TYPES:
            doc.judgment_items.append(d)
        elif d.investment_id is None:
            doc.other_items.append(d)
    doc.no_change = not kept

    # portfolio headline (always shown - one line of state is allowed as orientation)
    val = value_portfolio(session, settings.base_currency, now.date())
    if val.total_value_base is not None:
        doc.portfolio_lines.append(
            f"Portfolio value {float(val.total_value_base):,.0f} {settings.base_currency}"
            + (f" | cash {float(val.cash_base):,.0f}" if val.cash_base is not None else "")
        )

    # per-investment sections
    for inv in investments.values():
        inv_items = [d for d in kept if d.investment_id == inv.id and d.delta_type not in PORTFOLIO_TYPES | MACRO_TYPES]
        status, reason = _thesis_status(session, inv, inv_items)
        section = InvestmentSection(ticker=inv.ticker, thesis_status=status, status_reason=reason, items=inv_items)
        section.background = _background_line(session, inv)
        section.next_trigger = _next_trigger(session, inv, now)
        # include a section either when something happened or the investment is OWNED (one-liner)
        if inv_items or inv.status == "OWNED" or status != "UNCHANGED":
            doc.investment_sections.append(section)

    doc.upcoming = upcoming_events(session, days=7 if brief_type == "daily" else 30, today=now.date())
    doc.executive_summary = _executive_summary(doc, settings)
    return doc


def _thesis_status(session, inv, inv_items) -> tuple[str, str]:
    h = thesis_health(session, inv)
    if h.state in ("AT_RISK", "BROKEN"):
        return "AT RISK", "; ".join(h.reasons) or "thesis health deteriorated"
    needs = [d for d in inv_items if d.delta_type in ("REVIEW_DUE", "VALUATION_REVIEW", "BREAKER_TRIGGER")]
    contradicting = [d for d in inv_items if d.delta_type == "NEW_EVIDENCE" and d.payload.get("direction") == "CONTRADICTING"]
    earnings = [d for d in inv_items if "EARNINGS" in d.title.upper() or d.delta_type == "NEW_KPI"]
    if needs or contradicting or earnings:
        why = (needs + contradicting + earnings)[0]
        return "NEEDS REVIEW", why.title[:120]
    return "UNCHANGED", "no new thesis-relevant information in this window"


def _background_line(session, inv) -> str | None:
    """One calm line of standing context (not an attention item)."""
    risks = sorted(
        session.scalars(select(Risk).where(Risk.investment_id == inv.id, Risk.status == "OPEN")),
        key=lambda r: {"CRITICAL": 3, "HIGH": 2, "MEDIUM": 1, "LOW": 0}.get(r.severity, 0),
        reverse=True,
    )
    if risks:
        return f"{risks[0].name} remains the most important monitored risk."
    return None


def _next_trigger(session, inv, now) -> str | None:
    events = [e for e in upcoming_events(session, days=180, today=now.date())
              if e["title"].startswith(inv.ticker)]
    if events:
        e = events[0]
        return f"{e['title'].split(': ', 1)[-1]} ({e['date']})"
    return "next quarterly earnings"


def _executive_summary(doc: BriefDocument, settings: Settings) -> list[str]:
    if doc.no_change:
        return [NO_CHANGE_SENTENCE]
    lines: list[str] = []
    # highest severity items first, deduplicated by theme, max 5
    all_items = (
        [d for s in doc.investment_sections for d in s.items]
        + doc.portfolio_items + doc.macro_items + doc.judgment_items
        + doc.opportunity_items + doc.other_items
    )
    seen_keys = set()
    for d in sorted(all_items, key=lambda x: -SEV_ORDER.get(x.severity, 0)):
        if d.item_key in seen_keys:
            continue
        seen_keys.add(d.item_key)
        lines.append(d.title)
        if len(lines) == 5:
            break
    # unchanged owned investments get one calm summary line if room remains
    unchanged = [s.ticker for s in doc.investment_sections if s.thesis_status == "UNCHANGED" and not s.items]
    if unchanged and len(lines) < 5:
        lines.append(f"{', '.join(unchanged)}: thesis unchanged; no new fundamental evidence in this window.")
    return lines


# --- rendering ---------------------------------------------------------------


def render_markdown(doc: BriefDocument) -> str:
    L: list[str] = [f"# Investor OS {doc.brief_type.capitalize()} Brief — {doc.brief_date}", ""]
    if doc.mode == "preview":
        L.append("*(preview re-render of today's brief - checkpoint unchanged)*\n")
    if doc.ai_note:
        L.append(f"*{doc.ai_note}*\n")

    L.append("## 1. Executive Summary\n")
    for s in doc.executive_summary:
        L.append(f"* {s}")
    L.append("")

    if doc.ai_synthesis:
        L.append("## Mentor synthesis (AI)\n")
        for key, label in (
            ("today_in_one_minute", "Today in one minute"), ("portfolio", "Portfolio"),
            ("thesis_changes", "Thesis changes"), ("macro", "Macro"),
            ("new_opportunities", "New opportunities"), ("needs_your_judgment", "What needs your judgment"),
            ("can_be_ignored", "What can be ignored"),
        ):
            text = doc.ai_synthesis.get(key)
            if text:
                L.append(f"**{label}.** {text}\n")

    L.append("## Portfolio\n")
    for line in doc.portfolio_lines:
        L.append(f"* {line}")
    for d in doc.portfolio_items:
        L.append(f"* {d.title}" + (f" — {d.detail}" if d.detail else ""))
        L.append(f"  * *why: {d.reason}*")
    if not doc.portfolio_items:
        L.append("* No portfolio changes above thresholds.")
    L.append("")

    L.append("## Investments\n")
    for s in doc.investment_sections:
        L.append(f"### {s.ticker} — thesis: {s.thesis_status}")
        L.append(f"*{s.status_reason}*\n")
        if s.items:
            L.append("New information:")
            for d in s.items:
                L.append(f"* [{d.severity}] {d.title}" + (f" — {d.detail}" if d.detail else ""))
                L.append(f"  * *why: {d.reason}*")
        else:
            L.append("New information: none material.")
        if s.background:
            L.append(f"\nBackground: {s.background}")
        if s.next_trigger:
            L.append(f"Next trigger: {s.next_trigger}")
        L.append("")

    if doc.macro_items:
        L.append("## Macro\n")
        for d in doc.macro_items:
            L.append(f"* {d.title}" + (f" — {d.detail}" if d.detail else ""))
            L.append(f"  * *why: {d.reason}*")
        L.append("")

    if doc.opportunity_items:
        L.append("## New opportunities\n")
        for d in doc.opportunity_items:
            L.append(f"* {d.title}" + (f" — {d.detail}" if d.detail else ""))
        L.append("")

    L.append("## Needs your judgment\n")
    if doc.judgment_items:
        for d in doc.judgment_items:
            L.append(f"* {d.title}")
            L.append(f"  * *why: {d.reason}*")
    else:
        L.append("* Nothing awaits your decision today.")
    L.append("")

    for label, lines in doc.weekly_extras.items():
        L.append(f"## {label}\n")
        for line in lines:
            L.append(f"* {line}")
        L.append("")

    if doc.upcoming:
        L.append("## Upcoming\n")
        for e in doc.upcoming:
            L.append(f"* {e['date']} [{e['kind']}] {e['title']}")
        L.append("")

    if doc.suppressed_count:
        L.append(f"*{doc.suppressed_count} unchanged/low-signal item(s) suppressed - the system tracks them; "
                 "they will resurface only when something changes.*")
    return "\n".join(L)


def render_audio(doc: BriefDocument) -> str:
    """Natural prose for TTS: no tables, no markdown, short sentences."""
    P: list[str] = [f"Investor OS {doc.brief_type} brief for {doc.brief_date:%B %d, %Y}."]
    if doc.no_change:
        P.append(NO_CHANGE_SENTENCE)
    if doc.ai_synthesis and doc.ai_synthesis.get("today_in_one_minute"):
        P.append("Today in one minute. " + doc.ai_synthesis["today_in_one_minute"])
    elif not doc.no_change:
        P.append("Headlines. " + " ".join(doc.executive_summary))
    for line in doc.portfolio_lines:
        P.append(line + ".")
    for d in doc.portfolio_items:
        P.append(d.title + ("." if not d.title.endswith(".") else "") + (" " + d.detail if d.detail else ""))
    for s in doc.investment_sections:
        if s.items:
            P.append(f"{s.ticker}: thesis {s.thesis_status.lower()}. " + " ".join(d.title for d in s.items) + ".")
        else:
            P.append(f"{s.ticker}: thesis unchanged, no new fundamental information."
                     + (f" {s.background}" if s.background else "")
                     + (f" Next trigger: {s.next_trigger}." if s.next_trigger else ""))
    if doc.ai_synthesis:
        for key in ("thesis_changes", "macro", "needs_your_judgment", "can_be_ignored"):
            if doc.ai_synthesis.get(key):
                P.append(doc.ai_synthesis[key])
    elif doc.judgment_items:
        P.append("Needs your judgment: " + " ".join(d.title for d in doc.judgment_items) + ".")
    if doc.ai_note:
        P.append(doc.ai_note)
    P.append("End of brief.")
    return "\n\n".join(P)
