"""Unified daily/weekly pipelines: one command runs the whole Investor OS day.

Design:
  * every stage runs in its own try/except and its own transaction scope - one provider
    failure never aborts the rest (unless a stage is marked critical);
  * everything is idempotent (all underlying syncs/briefs already are);
  * per-stage status is persisted (pipeline_runs / pipeline_stages) and summarized as
    SUCCESS / PARTIAL SUCCESS / FAILED;
  * AI stages are optional: a health check decides, and the brief completes without AI.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable

from sqlalchemy import select

from src.config import Settings
from src.core import utcnow
from src.db.operations import PipelineRun, PipelineStage
from src.db.session import session_scope
from src.logging_setup import get_logger

log = get_logger("operations.pipeline")


@dataclass
class StageResult:
    stage: str
    status: str  # OK | WARN | FAIL | SKIP
    items: int = 0
    message: str = ""


@dataclass
class PipelineReport:
    pipeline: str
    run_id: int | None = None
    stages: list[StageResult] = field(default_factory=list)
    output_path: str | None = None
    brief_headlines: list[str] = field(default_factory=list)
    portfolio_line: str | None = None

    @property
    def status(self) -> str:
        statuses = {s.status for s in self.stages}
        if "FAIL" in statuses and not statuses - {"FAIL", "SKIP"}:
            return "FAILED"
        if "FAIL" in statuses or "WARN" in statuses:
            return "PARTIAL"
        return "SUCCESS"

    def format(self) -> str:
        lines = [f"Investor OS {self.pipeline.capitalize()} Run — {self.status}", ""]
        width = max((len(s.stage) for s in self.stages), default=10) + 2
        for s in self.stages:
            msg = f"  {s.message}" if s.message else ""
            lines.append(f"{s.stage:<{width}}{s.status}{msg}")
        if self.portfolio_line:
            lines += ["", f"Portfolio: {self.portfolio_line}"]
        if self.brief_headlines:
            lines += ["", "Important today:"] + [f"  * {h}" for h in self.brief_headlines]
        if self.output_path:
            lines += ["", f"Output: {self.output_path}"]
        return "\n".join(lines)


def _run_stage(
    settings: Settings, report: PipelineReport, run_id: int, stage: str,
    fn: Callable, skip_reason: str | None = None,
) -> StageResult:
    started = utcnow()
    if skip_reason:
        result = StageResult(stage, "SKIP", 0, skip_reason)
    else:
        try:
            with session_scope(settings.db_url) as s:
                out = fn(s) or {}
            items = int(out.get("items", 0))
            warn = out.get("warning")
            result = StageResult(stage, "WARN" if warn else "OK", items, warn or out.get("message", ""))
        except Exception as exc:
            log.error("stage %s failed: %s", stage, exc)
            result = StageResult(stage, "FAIL", 0, f"{type(exc).__name__}: {exc}"[:300])
    report.stages.append(result)
    with session_scope(settings.db_url) as s:
        s.add(PipelineStage(
            run_id=run_id, stage=stage, status=result.status, started_at=started,
            finished_at=utcnow(), items_processed=result.items, message=result.message or None,
        ))
    return result


def _monitored_tickers(session) -> list[str]:
    """OWNED/RESEARCHING investments with a linked instrument = SEC-monitored universe."""
    from src.db.research import Investment

    return [
        i.ticker
        for i in session.scalars(
            select(Investment).where(Investment.status.in_(("OWNED", "RESEARCHING", "READY_FOR_DECISION")))
        )
        if i.instrument_id is not None
    ]


def run_daily(
    settings: Settings, use_ai: bool = True, audio: bool = True, sync_external: bool = True,
    ai_provider=None,
) -> PipelineReport:
    report = PipelineReport(pipeline="daily")
    with session_scope(settings.db_url) as s:
        run = PipelineRun(pipeline="daily")
        s.add(run)
        s.flush()
        run_id = run.id
        tickers = _monitored_tickers(s)
    report.run_id = run_id

    # 0. backup (critical-path safe: failure only warns)
    def _backup(s):
        from src.operations.backups import create_backup

        p = create_backup(settings, "daily")
        return {"message": p.name if p else "no db"}

    _run_stage(settings, report, run_id, "Backup", _backup)

    net_skip = None if sync_external else "external sync disabled (--no-sync)"

    def _prices(s):
        from src.market_data import get_provider
        from src.market_data.service import update_prices

        summary = update_prices(s, get_provider(settings.market_data_provider), settings)
        inserted = sum(v["inserted"] for v in summary["instruments"].values()) + sum(
            v["inserted"] for v in summary["fx"].values()
        )
        return {"items": inserted, "warning": "; ".join(summary["errors"][:2]) or None}

    _run_stage(settings, report, run_id, "Prices", _prices, net_skip)

    def _sec(s):
        from src.intelligence.connectors.sec import SecClient, sync_filings
        from src.intelligence.connectors.xbrl import sync_companyfacts

        client = SecClient(settings)
        total = 0
        warnings = []
        for t in _monitored_tickers(s):
            try:
                summary = sync_filings(s, settings, t, client=client)
                total += summary["filings_inserted"]
                sync_companyfacts(s, settings, summary["cik"], client=client)
            except Exception as exc:
                warnings.append(f"{t}: {exc}")
        return {"items": total, "warning": "; ".join(warnings)[:200] or None}

    _run_stage(settings, report, run_id, "SEC", _sec, net_skip)

    def _insiders(s):
        from src.intelligence.connectors.insiders import sync_insiders
        from src.intelligence.connectors.sec import SecClient

        client = SecClient(settings)
        total = 0
        warnings = []
        for t in _monitored_tickers(s):
            try:
                summary = sync_insiders(s, settings, t, client=client)
                total += summary["inserted"]
            except Exception as exc:
                warnings.append(f"{t}: {exc}")
        return {"items": total, "warning": "; ".join(warnings)[:200] or None}

    _run_stage(settings, report, run_id, "Insiders", _insiders, net_skip)

    def _macro(s):
        from src.intelligence.connectors.macro import sync_macro

        summary = sync_macro(s, settings)
        inserted = sum(v["inserted"] for v in summary["series"].values())
        return {"items": inserted, "warning": "; ".join(summary["errors"][:2]) or None}

    _run_stage(settings, report, run_id, "Macro", _macro, net_skip)

    def _institutional(s):
        from src.db.intelligence import InstitutionalManager
        from src.intelligence.connectors.institutional import sync_manager
        from src.intelligence.connectors.sec import SecClient

        managers = list(s.scalars(select(InstitutionalManager).where(InstitutionalManager.active.is_(True))))
        if not managers:
            return {"message": "no tracked managers"}
        client = SecClient(settings)
        total = 0
        for m in managers:
            total += sync_manager(s, settings, m, client=client)["holdings_inserted"]
        return {"items": total}

    _run_stage(settings, report, run_id, "Institutional", _institutional, net_skip)

    def _kpi_extract(s):
        """Extract from any NEW archived filing documents for issuers with extractors."""
        from src.intelligence.earnings import auto_extract_new_filings

        res = auto_extract_new_filings(s, settings)
        return {"items": res["stored"], "message": res["message"]}

    _run_stage(settings, report, run_id, "KPI extract", _kpi_extract)

    def _comparison(s):
        from src.db.research import Investment
        from src.intelligence.earnings import compare_kpis, flag_contradictions

        created = 0
        for inv in s.scalars(select(Investment).where(Investment.status == "OWNED")):
            created += len(flag_contradictions(s, inv, compare_kpis(s, inv)))
        return {"items": created, "message": f"{created} review proposal(s)" if created else ""}

    _run_stage(settings, report, run_id, "Earnings compare", _comparison)

    # AI stage: health-checked, optional
    ai_skip = None
    provider = ai_provider
    if not use_ai:
        ai_skip = "disabled (--no-ai)"
    elif provider is None:
        try:
            from src.intelligence.ai.provider import get_ai_provider

            provider = get_ai_provider(settings)
            health = provider.health()
            if not health.get("available"):
                ai_skip = f"AI server unavailable ({health.get('error', 'no response')[:80]})"
                provider = None
        except Exception as exc:
            ai_skip = str(exc)[:120]
            provider = None

    def _ai_events(s):
        from src.db.intelligence import IntelligenceEvent
        from src.db.research import Investment
        from src.intelligence.ai.analysis import analyze_event

        events = list(
            s.scalars(
                select(IntelligenceEvent).where(
                    IntelligenceEvent.ai_state == "NONE",
                    IntelligenceEvent.severity == "HIGH",
                    IntelligenceEvent.investment_id.isnot(None),
                    IntelligenceEvent.processing_state == "NEW",
                ).limit(3)  # local model is slow; analyze only the most material backlog
            )
        )
        proposals = 0
        for e in events:
            inv = s.get(Investment, e.investment_id)
            _analysis, props = analyze_event(s, provider, e, inv)
            proposals += len(props)
        return {"items": len(events), "message": f"{proposals} proposal(s) PENDING (nothing applied)"}

    _run_stage(settings, report, run_id, "AI analysis", _ai_events, ai_skip)

    def _brief(s):
        from src.intelligence.briefing.generate import generate_brief

        doc, brun, md_path, _audio = generate_brief(
            s, settings, "daily", use_ai=provider is not None, audio=audio, ai_provider=provider
        )
        report.output_path = str(md_path)
        report.brief_headlines = doc.executive_summary[:5]
        report.portfolio_line = "; ".join(doc.portfolio_lines)
        return {"items": brun.items_count if brun else 0,
                "message": "preview (already ran today)" if brun is None else ""}

    _run_stage(settings, report, run_id, "Brief", _brief)

    def _audio_stage(s):
        from src.intelligence.briefing.tts import synthesize_latest_brief

        out = synthesize_latest_brief(settings, "daily")
        if out is None:
            return {"message": "TTS disabled or unavailable (text version saved)"}
        return {"message": out.name}

    _run_stage(settings, report, run_id, "Audio", _audio_stage, None if audio else "audio disabled")

    with session_scope(settings.db_url) as s:
        run = s.get(PipelineRun, run_id)
        run.status = report.status
        run.finished_at = utcnow()
        run.output_path = report.output_path
        run.summary = json.dumps([s2.__dict__ for s2 in report.stages])
    log.info("daily pipeline %s: %s", run_id, report.status)
    return report


def run_weekly(settings: Settings, use_ai: bool = True, sync_external: bool = True, ai_provider=None) -> PipelineReport:
    # weekly = daily-type sync first, then the deeper review
    report = run_daily(settings, use_ai=use_ai, audio=False, sync_external=sync_external, ai_provider=ai_provider)
    report.pipeline = "weekly"
    run_id = report.run_id

    def _weekly_brief(s):
        from src.intelligence.briefing.generate import generate_brief

        doc, brun, md_path, _ = generate_brief(s, settings, "weekly", use_ai=False, audio=True)
        report.output_path = str(md_path)
        return {"items": brun.items_count if brun else 0}

    _run_stage(settings, report, run_id, "Weekly review", _weekly_brief)

    def _risk(s):
        from src.analytics.risk import portfolio_risk_report

        rep = portfolio_risk_report(s, settings)
        return {"message": f"concentration max {rep['max_weight_pct']}%" if rep.get("max_weight_pct") else ""}

    _run_stage(settings, report, run_id, "Risk decomposition", _risk)

    def _claims(s):
        from src.db.briefing import ManagementClaim

        open_claims = s.scalars(select(ManagementClaim).where(ManagementClaim.status == "OPEN")).all()
        return {"items": len(open_claims), "message": f"{len(open_claims)} open claim(s) to verify"}

    _run_stage(settings, report, run_id, "Management claims", _claims)

    with session_scope(settings.db_url) as s:
        run = s.get(PipelineRun, run_id)
        run.pipeline = "weekly"
        run.status = report.status
        run.finished_at = utcnow()
        run.output_path = report.output_path
        run.summary = json.dumps([s2.__dict__ for s2 in report.stages])
    return report
