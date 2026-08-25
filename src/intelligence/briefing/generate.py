"""Brief orchestration: window -> deltas -> hygiene -> document -> (optional AI) -> files.

The pipeline is deterministic end-to-end; the single optional Glimmer call only adds the
mentor narration. AI failure never fails the brief.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.config import Settings
from src.core import sha256_bytes, utcnow
from src.db.briefing import BriefRun
from src.intelligence.briefing.assemble import (
    BriefDocument,
    build_document,
    render_audio,
    render_markdown,
)
from src.intelligence.briefing.calibration import calibration_report
from src.intelligence.briefing.checkpoints import (
    complete_run,
    previous_portfolio_state,
    resolve_window,
    start_run,
)
from src.intelligence.briefing.deltas import compute_deltas
from src.intelligence.briefing.hygiene import apply_hygiene
from src.intelligence.briefing.mentor import MENTOR_PROMPT_VERSION, synthesize
from src.intelligence.briefing.regime import macro_regime
from src.logging_setup import get_logger

log = get_logger("briefing.generate")


def generate_brief(
    session: Session,
    settings: Settings,
    brief_type: str = "daily",
    use_ai: bool = True,
    audio: bool = True,
    force: bool = False,
    preview: bool = False,
    now: datetime | None = None,
    ai_provider=None,
) -> tuple[BriefDocument, BriefRun | None, Path | None, Path | None]:
    """Returns (document, run | None for previews, markdown path, audio path)."""
    now = now or utcnow()
    period_start, period_end, superseded, mode = resolve_window(session, brief_type, now=now, force=force)
    if preview and mode == "new":
        mode = "preview"  # explicit preview of what the next brief would contain
    if superseded is not None:
        # supersede immediately: the re-run must reproduce the same items, not suppress them
        superseded.status = "superseded"
        session.flush()
        superseded = None
    prev_state = previous_portfolio_state(session, brief_type)

    deltas, current_state = compute_deltas(session, settings, period_start, period_end, prev_state)
    surfaced, suppressed = apply_hygiene(session, deltas, now=now, brief_type=brief_type)
    doc = build_document(session, settings, brief_type, surfaced, suppressed, mode, now)

    if brief_type == "weekly":
        _add_weekly_extras(session, settings, doc, period_start, now)

    if getattr(settings, "mentor_teach_me", False) and mode != "preview":
        from src.intelligence.briefing.mentor_workflows import pick_concept

        concept = pick_concept(session, [d.delta_type for d in surfaced])
        if concept:
            doc.weekly_extras["Investment concept of the day"] = [f"{concept[0]}: {concept[1]}"]

    ai_used = False
    ai_model = ai_context_hash = None
    if use_ai:
        provider = ai_provider
        if provider is None:
            try:
                from src.intelligence.ai.provider import get_ai_provider

                provider = get_ai_provider(settings)
            except Exception as exc:
                doc.ai_note = f"AI mentor synthesis unavailable ({exc})."
                provider = None
        if provider is not None:
            synthesis, ai_context_hash, note = synthesize(session, provider, doc)
            if synthesis is not None:
                doc.ai_synthesis = synthesis
                ai_used = True
                ai_model = provider.model
            else:
                doc.ai_note = note
    else:
        doc.ai_note = "AI mentor synthesis skipped (--no-ai); deterministic brief."

    md = render_markdown(doc)
    audio_text = render_audio(doc) if audio else None
    md_path = audio_path = None
    settings.brief_output_dir.mkdir(parents=True, exist_ok=True)
    stamp = f"{now:%Y-%m-%d}-{brief_type}"
    md_path = settings.brief_output_dir / f"{stamp}.md"
    md_path.write_text(md, encoding="utf-8")
    if audio_text is not None:
        audio_path = settings.brief_output_dir / f"{stamp}-audio.txt"
        audio_path.write_text(audio_text, encoding="utf-8")

    run = None
    if mode != "preview":
        run = start_run(session, brief_type, period_start, period_end, "completed", superseded=superseded)
        run = complete_run(
            session, run,
            items=[d.to_dict() for d in surfaced],
            suppressed_count=len(suppressed),
            portfolio_value=current_state.get("value"),
            base_currency=settings.base_currency,
            portfolio_state=current_state,
            output_path=str(md_path),
            audio_path=str(audio_path) if audio_path else None,
            ai_used=ai_used,
            ai_model=ai_model,
            ai_prompt_version=MENTOR_PROMPT_VERSION if ai_used else None,
            ai_context_hash=ai_context_hash,
            research_state_hash=sha256_bytes(md.encode("utf-8")),
        )
    log.info(
        "brief %s (%s): %d surfaced, %d suppressed, ai=%s -> %s",
        brief_type, mode, len(surfaced), len(suppressed), ai_used, md_path,
    )
    return doc, run, md_path, audio_path


def _add_weekly_extras(session, settings, doc: BriefDocument, since, now) -> None:
    from src.analytics.performance import benchmark_series, build_value_history, normalize_to_100, twr_index
    from src.db.models import Benchmark
    from src.db.research import Decision, Evidence, Investment
    from src.intelligence.earnings import compare_kpis, comparison_table_text
    from src.research.health import thesis_health

    extras: dict[str, list[str]] = {}

    # performance vs benchmark (whole history TWR + benchmark, deterministic v1 engines)
    perf_lines: list[str] = []
    points = build_value_history(session, settings.base_currency)
    idx = twr_index(points)
    if idx:
        perf_lines.append(f"Portfolio TWR index: {float(idx[-1][1]):.1f} (start=100)")
    bm = session.scalars(select(Benchmark).where(Benchmark.is_default.is_(True))).first()
    if bm and points:
        series, label = benchmark_series(session, bm.instrument_id, points[0].day, points[-1].day, settings.base_currency)
        norm = normalize_to_100(series)
        if norm:
            perf_lines.append(f"Benchmark {bm.code} ({label}): {float(norm[-1][1]):.1f} (start=100)")
    extras["Performance vs benchmark"] = perf_lines or ["Unavailable (missing history)"]

    # thesis health snapshot per active investment
    health_lines = []
    for inv in session.scalars(select(Investment).where(Investment.status.notin_(("ARCHIVED", "REJECTED")))):
        h = thesis_health(session, inv)
        health_lines.append(
            f"{inv.ticker}: {h.state or 'n/a'} ({h.supported}S/{h.weakening}W/{h.challenged}C/{h.broken}B; "
            f"breakers triggered {h.breakers_triggered})"
        )
    extras["Thesis health"] = health_lines or ["No investments"]

    contradicting = [
        e for e in session.scalars(select(Evidence))
        if e.direction == "CONTRADICTING" and e.created_at > since
    ]
    extras["Contradicting evidence this week"] = (
        [f"{e.title}" for e in contradicting] or ["None recorded."]
    )

    kpi_lines = []
    for inv in session.scalars(select(Investment).where(Investment.status == "OWNED")):
        table = comparison_table_text(compare_kpis(session, inv))
        if table:
            kpi_lines.extend([f"{inv.ticker}:"] + table.split("\n"))
    extras["KPI comparison"] = kpi_lines or ["No KPI observations."]

    regime_lines = [
        f"{r.name}: {r.state}"
        + (f" ({r.series_code} {r.latest:g} vs baseline {r.baseline:g}, {r.change_pct:+.1f}%)"
           if r.latest is not None and r.baseline is not None else f" ({r.rule})")
        for r in macro_regime(session)
    ]
    extras["Macro regime (deterministic dimensions)"] = regime_lines

    decisions = [d for d in session.scalars(select(Decision)) if d.created_at > since]
    extras["Decisions this week"] = (
        [f"{d.decision_type} ({d.decided_at:%Y-%m-%d}): {(d.reasoning or '')[:100]}" for d in decisions]
        or ["No decisions recorded - inaction is a valid decision when nothing changed."]
    )

    cal = calibration_report(session)
    if cal.sufficient:
        extras["Prediction calibration"] = [
            f"Resolved: {cal.resolved} | Brier {cal.brier_score} | hit rate {cal.hit_rate:.0%}",
            *(f"{b['bucket']}: stated ~{b['avg_stated_probability']}% vs observed {b['observed_frequency_pct']}% (n={b['n']})"
              for b in cal.buckets),
        ]
    else:
        extras["Prediction calibration"] = [cal.note]

    extras["Questions for next week"] = _weekly_questions(session, doc)
    doc.weekly_extras = extras


def _weekly_questions(session, doc: BriefDocument) -> list[str]:
    """Deterministic reflective questions derived from the current state ("what would change
    my mind" is first-class: breakers + monitored assumptions)."""
    from src.db.research import Investment, ThesisAssumption, ThesisBreaker

    out = []
    for inv in session.scalars(select(Investment).where(Investment.status == "OWNED")):
        breakers = list(session.scalars(
            select(ThesisBreaker).where(ThesisBreaker.investment_id == inv.id, ThesisBreaker.status == "ACTIVE")
        ))
        if breakers:
            out.append(f"{inv.ticker} — what would change my mind: " + "; ".join(b.name for b in breakers[:3]))
        weak = [
            a.name for t_id in [t.id for t in inv.theses]
            for a in session.scalars(select(ThesisAssumption).where(
                ThesisAssumption.thesis_id == t_id, ThesisAssumption.status.in_(("WEAKENING", "UNKNOWN"))))
        ]
        if weak:
            out.append(f"{inv.ticker} — assumptions needing evidence: " + "; ".join(weak[:3]))
    return out or ["No open questions derived from current state."]
