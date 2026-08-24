"""Deterministic daily/weekly investor briefs. Aggregation + prioritization only - the brief
itself contains no AI-generated text; pending AI proposals are listed as items to review."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.config import Settings
from src.db.intelligence import AiProposal, IntelligenceEvent
from src.db.research import Investment
from src.intelligence.calendar import upcoming_events
from src.intelligence.events import list_events
from src.portfolio.valuation import value_portfolio
from src.research.health import thesis_health
from src.research.predictions import simple_stats
from src.research.reviews import needs_attention


def _fmt(v, ccy: str) -> str:
    return "Unavailable" if v is None else f"{float(v):,.0f} {ccy}"


def _portfolio_block(session: Session, settings: Settings, lines: list[str]) -> None:
    val = value_portfolio(session, settings.base_currency)
    lines.append("PORTFOLIO")
    lines.append(f"  Value: {_fmt(val.total_value_base, settings.base_currency)}"
                 f" | cash {_fmt(val.cash_base, settings.base_currency)}"
                 f" | invested {_fmt(val.invested_value_base, settings.base_currency)}")
    from src.analytics.performance import build_value_history

    pts = build_value_history(session, settings.base_currency)
    if len(pts) >= 2 and pts[-1].value is not None and pts[-2].value is not None:
        delta = pts[-1].value - pts[-2].value - pts[-1].external_flow
        lines.append(f"  Daily change: {float(delta):+,.0f} {settings.base_currency}")
    top = sorted(
        (r for r in val.positions if r.market_value_base is not None),
        key=lambda r: -(r.market_value_base or 0),
    )[:5]
    for r in top:
        w = f"{float(r.weight) * 100:.1f}%" if r.weight is not None else "?"
        lines.append(f"  {r.symbol}: {_fmt(r.market_value_base, settings.base_currency)} ({w})")


def _events_block(session: Session, lines: list[str], since: datetime, min_severity: str) -> None:
    events = [
        e for e in list_events(session, min_severity=min_severity)
        if e.created_at >= since and e.processing_state != "DISMISSED"
    ]
    owned = [e for e in events if e.investment_id is not None]
    other = [e for e in events if e.investment_id is None]
    lines.append(f"INTELLIGENCE EVENTS (material, {len(events)})")
    for e in owned[:12]:
        lines.append(f"  [{e.severity}] {e.occurred_at:%Y-%m-%d} {e.title}")
    if other:
        lines.append(f"  + {len(other)} events not linked to any investment (see dashboard)")
    if not events:
        lines.append("  (none)")


def _proposals_block(session: Session, lines: list[str]) -> None:
    pending = list(session.scalars(select(AiProposal).where(AiProposal.status == "PENDING")))
    lines.append(f"AI PROPOSALS AWAITING YOUR REVIEW ({len(pending)})")
    for p in pending[:10]:
        lines.append(f"  #{p.id} [{p.proposal_type}] {p.title}")
    if not pending:
        lines.append("  (none)")
    if pending:
        lines.append("  Nothing has been applied - review in the Intelligence Inbox.")


def _attention_block(session: Session, lines: list[str]) -> None:
    na = needs_attention(session)
    lines.append(f"NEEDS ATTENTION ({na.total})")
    for label, rows in (
        ("Triggered breakers", na.triggered_breakers),
        ("Broken assumptions", na.broken_assumptions),
        ("Challenged assumptions", na.challenged_assumptions),
        ("Reviews due", [(i, None) for i in na.reviews_due]),
        ("High risks", na.high_risks),
    ):
        for item in rows:
            inv = item[0] if isinstance(item, tuple) else item
            detail = getattr(item[1], "name", "") if isinstance(item, tuple) and item[1] is not None else ""
            lines.append(f"  {label}: {inv.ticker} {detail}".rstrip())
    if na.total == 0:
        lines.append("  (nothing)")


def _upcoming_block(session: Session, lines: list[str], days: int) -> None:
    events = upcoming_events(session, days=days)
    lines.append(f"UPCOMING {days} DAYS ({len(events)})")
    for e in events[:15]:
        lines.append(f"  {e['date']} [{e['kind']}] {e['title']}")
    if not events:
        lines.append("  (nothing scheduled)")


def daily_brief(session: Session, settings: Settings, today: date | None = None) -> str:
    today = today or date.today()
    since = datetime.combine(today - timedelta(days=1), datetime.min.time())
    lines = [f"INVESTOR OS DAILY BRIEF - {today}", "=" * 44]
    _portfolio_block(session, settings, lines)
    lines.append("")
    _events_block(session, lines, since, min_severity="MEDIUM")
    lines.append("")
    _proposals_block(session, lines)
    lines.append("")
    _attention_block(session, lines)
    lines.append("")
    _upcoming_block(session, lines, days=7)
    return "\n".join(lines)


def weekly_brief(session: Session, settings: Settings, today: date | None = None) -> str:
    today = today or date.today()
    since = datetime.combine(today - timedelta(days=7), datetime.min.time())
    lines = [f"INVESTOR OS WEEKLY REVIEW - week ending {today}", "=" * 50]
    _portfolio_block(session, settings, lines)

    # concentration (deterministic)
    val = value_portfolio(session, settings.base_currency)
    weights = [(r.symbol, r.weight) for r in val.positions if r.weight is not None]
    if weights:
        top = max(weights, key=lambda x: x[1])
        lines.append(f"  Largest concentration: {top[0]} at {float(top[1]) * 100:.1f}% of invested value")
    lines.append("")

    lines.append("THESIS HEALTH")
    for inv in session.scalars(select(Investment).where(Investment.status.notin_(("ARCHIVED", "REJECTED")))):
        h = thesis_health(session, inv)
        parts = f"{h.supported}S/{h.weakening}W/{h.challenged}C/{h.broken}B"
        lines.append(f"  {inv.ticker}: {h.state or 'n/a'} ({parts}; breakers triggered: {h.breakers_triggered})")
    lines.append("")

    from src.db.research import Evidence

    week_evidence = [
        e for e in session.scalars(select(Evidence)) if e.created_at >= since
    ]
    contradicting = [e for e in week_evidence if e.direction == "CONTRADICTING"]
    lines.append(f"EVIDENCE ADDED THIS WEEK: {len(week_evidence)} ({len(contradicting)} contradicting)")
    for e in contradicting[:8]:
        lines.append(f"  CONTRADICTING: {e.title}")
    lines.append("")

    _events_block(session, lines, since, min_severity="LOW")
    lines.append("")
    _proposals_block(session, lines)
    lines.append("")
    _attention_block(session, lines)
    lines.append("")

    stats = simple_stats(session)
    hit = f"{stats['hit_rate']:.0%}" if stats["hit_rate"] is not None else "n/a"
    lines.append("PREDICTION RECORD")
    lines.append(
        f"  total {stats['total']} | open {stats['open']} | resolved {stats['resolved']} | hit rate {hit}"
    )
    lines.append("")
    _upcoming_block(session, lines, days=30)
    lines.append("")
    lines.append("WHAT CHANGED THIS WEEK THAT ACTUALLY MATTERS?")
    material = [
        e for e in list_events(session, min_severity="HIGH")
        if e.created_at >= since and e.processing_state != "DISMISSED"
    ]
    if material:
        for e in material[:8]:
            lines.append(f"  - {e.title}")
    else:
        lines.append("  No HIGH-materiality events this week.")
    return "\n".join(lines)
