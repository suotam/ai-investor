"""Company onboarding: make adding a new investment to the intelligence stack easy.

`company onboard TICKER` (network unless a client is injected):
  1. resolve the v1 instrument (if any) and SEC identity (CIK, filer type),
  2. inspect which forms the issuer actually files (domestic vs foreign),
  3. sync the filing index + XBRL company facts,
  4. report which standard normalized metrics are available,
  5. report whether an issuer-specific extractor exists,
  6. list the investment's KPIs that no automatic source covers,
  7. write an onboarding report + extractor TODO template to output/onboarding/,
  8. optionally write a research-import YAML skeleton.
Never invents a thesis and never creates the investment itself.
"""
from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.config import Settings
from src.db.intelligence import FinancialFact
from src.intelligence.connectors.sec import SecClient, resolve_cik, sync_filings
from src.intelligence.connectors.xbrl import sync_companyfacts
from src.intelligence.issuer_extractors import get_extractor
from src.research.investments import get_by_ticker


def onboard_company(
    session: Session, settings: Settings, ticker: str, client: SecClient | None = None,
    write_files: bool = True,
) -> dict:
    ticker = ticker.upper()
    client = client or SecClient(settings)
    report: dict = {"ticker": ticker, "steps": [], "warnings": []}

    inv = get_by_ticker(session, ticker)
    report["investment_exists"] = inv is not None
    from src.db.models import Instrument

    inst = session.scalars(
        select(Instrument).where(Instrument.symbol == ticker, Instrument.asset_type != "cash")
    ).first()
    report["instrument"] = f"id={inst.id} ({inst.exchange}/{inst.currency})" if inst else "not in portfolio DB"

    try:
        cik, title = resolve_cik(client, ticker)
        report["cik"], report["issuer"] = cik, title
    except Exception as exc:
        report["warnings"].append(f"SEC identity not resolved: {exc}")
        report["cik"] = None
        return report

    filings_summary = sync_filings(session, settings, ticker, client=client)
    report["forms_seen"] = filings_summary["forms_seen"]
    report["filer_type"] = (
        "foreign private issuer (20-F/6-K)" if "20-F" in filings_summary["forms_seen"]
        else "domestic (10-K/10-Q)" if "10-K" in filings_summary["forms_seen"]
        else "other/unknown"
    )
    facts_summary = sync_companyfacts(session, settings, cik, client=client)
    report["xbrl_facts"] = facts_summary.get("facts_inserted", 0) + facts_summary.get("facts_duplicate", 0)

    metrics = sorted({
        f.metric for f in session.scalars(
            select(FinancialFact).where(FinancialFact.cik == cik, FinancialFact.metric.isnot(None))
        )
    })
    report["standard_metrics_available"] = metrics

    extractor = get_extractor(ticker)
    report["issuer_extractor"] = extractor.source_tag if extractor else None

    uncovered = []
    if inv is not None:
        from src.research.kpis import list_kpis

        covered = set(k.lower() for k in (extractor.__class__.__dict__.get("RULES") and
                                          [r[0] for r in extractor.RULES] or [])) if extractor else set()
        for kpi in list_kpis(session, inv):
            if kpi.name.lower() not in covered and not any(m in kpi.name.lower() for m in metrics):
                uncovered.append(kpi.name)
    report["kpis_without_automatic_source"] = uncovered

    if write_files:
        out_dir = settings.brief_output_dir.parent / "onboarding"
        out_dir.mkdir(parents=True, exist_ok=True)
        todo = out_dir / f"{ticker}_extractor_todo.md"
        todo.write_text(_todo_template(report), encoding="utf-8")
        yaml_path = out_dir / f"{ticker}_research_template.yaml"
        yaml_path.write_text(_research_yaml_template(ticker, report), encoding="utf-8")
        report["files"] = [str(todo), str(yaml_path)]
    return report


def _todo_template(report: dict) -> str:
    lines = [
        f"# Extractor TODO — {report['ticker']} ({report.get('issuer', '?')})",
        "",
        f"* CIK: {report.get('cik')}",
        f"* Filer type: {report.get('filer_type')}",
        f"* Forms filed: {', '.join(report.get('forms_seen', []))}",
        f"* Standard XBRL metrics available: {', '.join(report.get('standard_metrics_available', [])) or 'none'}",
        f"* Existing extractor: {report.get('issuer_extractor') or 'NONE - create one'}",
        "",
        "## KPIs without an automatic source",
    ]
    for k in report.get("kpis_without_automatic_source", []) or ["(define investment KPIs first)"]:
        lines.append(f"* {k}")
    lines += [
        "",
        "## Next steps",
        "1. Inspect a quarterly source: python -m src.main extractor inspect "
        f"{report['ticker']} --accession <accn> (or --file release.htm)",
        "2. Copy src/intelligence/issuer_extractors/nu.py as a template; register the new class.",
        "3. Add offline tests with a fixture modeled on the issuer's release language.",
        "4. Ambiguous matches MUST return mode='ambiguous' - never guess values.",
    ]
    return "\n".join(lines)


def _research_yaml_template(ticker: str, report: dict) -> str:
    return f"""# Research import skeleton for {ticker} - fill in and run:
#   python -m src.main import-research {ticker.lower()}.yaml --dry-run
schema_version: 1
investment:
  symbol: {ticker}
  name: {report.get('issuer', '')}
  lifecycle_status: RESEARCHING
thesis:
  title: "{ticker} — thesis title"
  core_thesis: |
    (your reasoning - never auto-generated)
  confidence: 50
assumptions: []
risks: []
kpis: []
"""
