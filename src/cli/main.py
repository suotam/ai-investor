"""Investor OS command line interface (Typer).

    python -m src.main init-db
    python -m src.main sync-ibkr [--file report.xml]
    python -m src.main import-crypto path.csv
    python -m src.main update-prices
    python -m src.main rebuild-portfolio
    python -m src.main snapshot
    python -m src.main status
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import func, select

from src.config import Settings, get_secret, load_settings, mask_secret
from src.db.session import current_revision, run_migrations, session_scope
from src.logging_setup import get_logger, setup_logging

app = typer.Typer(
    name="investor-os",
    help="Investor OS v1 - local, read-only portfolio core. No trading functionality.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
log = get_logger("cli")


def _boot() -> Settings:
    settings = load_settings()
    setup_logging(settings.log_path, settings.log_level)
    return settings


def _fmt(v, places: int = 2) -> str:
    if v is None:
        return "Unavailable"
    return f"{float(v):,.{places}f}"


def _pct(v) -> str:
    return "Unavailable" if v is None else f"{float(v) * 100:.2f}%"


@app.command("init-db")
def init_db() -> None:
    """Create/upgrade the SQLite database via Alembic migrations (idempotent)."""
    settings = _boot()
    run_migrations(settings.db_url)
    console.print(f"[green]Database ready:[/green] {settings.db_path} (revision {current_revision(settings.db_url)})")


@app.command("sync-ibkr")
def sync_ibkr_cmd(
    file: Optional[Path] = typer.Option(None, "--file", "-f", help="Import a saved Flex XML instead of calling IBKR."),
) -> None:
    """Fetch the IBKR Flex statement (READ ONLY) and import it idempotently."""
    settings = _boot()
    from src.connectors.ibkr.sync import sync_ibkr

    if file is None and not (get_secret("IBKR_FLEX_TOKEN") and get_secret("IBKR_FLEX_QUERY_ID")):
        console.print("[red]IBKR_FLEX_TOKEN / IBKR_FLEX_QUERY_ID not set in .env.[/red] Use --file to import an XML.")
        raise typer.Exit(code=2)
    with session_scope(settings.db_url) as s:
        result = sync_ibkr(s, settings, xml_file=file)
    console.print(
        f"[green]IBKR sync done[/green]: transactions +{result.transactions_inserted} "
        f"(dup {result.transactions_duplicate}), cash flows +{result.cash_flows_inserted} "
        f"(dup {result.cash_flows_duplicate})"
    )
    for w in result.warnings:
        console.print(f"[yellow]warning:[/yellow] {w}")
    console.print("Next: [bold]rebuild-portfolio[/bold] then [bold]update-prices[/bold].")


@app.command("import-crypto")
def import_crypto(
    path: Path = typer.Argument(..., exists=True, readable=True, help="CSV export (see README for columns)."),
    account: str = typer.Option("crypto-csv", "--account", help="Account identifier for this CSV source."),
) -> None:
    """Import a generic crypto transactions CSV (idempotent)."""
    settings = _boot()
    from src.connectors.crypto.csv_importer import SOURCE, CsvCryptoImporter
    from src.portfolio.importer import finish_import_run, import_statement, start_import_run
    from src.core import sha256_bytes

    with session_scope(settings.db_url) as s:
        run = start_import_run(s, job="import-crypto", source=SOURCE)
        try:
            stmt = CsvCryptoImporter(path, account_external_id=account, base_currency=settings.base_currency).load()
            result = import_statement(s, stmt, source=SOURCE, source_file=str(path))
            finish_import_run(s, run, result, raw_path=str(path), raw_sha256=sha256_bytes(path.read_bytes()))
        except Exception as exc:
            finish_import_run(s, run, error=f"{type(exc).__name__}: {exc}")
            raise
    console.print(
        f"[green]Crypto CSV imported[/green]: transactions +{result.transactions_inserted} "
        f"(dup {result.transactions_duplicate}), cash flows +{result.cash_flows_inserted} "
        f"(dup {result.cash_flows_duplicate})"
    )
    for w in result.warnings:
        console.print(f"[yellow]warning:[/yellow] {w}")


@app.command("rebuild-portfolio")
def rebuild_portfolio() -> None:
    """Recompute positions, cost basis, realized P/L and FIFO lots from transactions."""
    settings = _boot()
    from src.portfolio.positions import rebuild_positions

    with session_scope(settings.db_url) as s:
        states = rebuild_positions(s)
    open_n = sum(1 for st in states.values() if st.quantity != 0)
    console.print(f"[green]Portfolio rebuilt[/green]: {len(states)} instruments, {open_n} open positions")


@app.command("update-prices")
def update_prices_cmd(
    end: Optional[str] = typer.Option(None, help="End date YYYY-MM-DD (default today)."),
) -> None:
    """Download missing historical prices / FX rates for traded instruments and benchmarks."""
    settings = _boot()
    from src.market_data import get_provider
    from src.market_data.service import update_prices

    provider = get_provider(settings.market_data_provider)
    with session_scope(settings.db_url) as s:
        summary = update_prices(s, provider, settings, end=date.fromisoformat(end) if end else None)
    n_ok = sum(v["inserted"] for v in summary["instruments"].values()) + sum(
        v["inserted"] for v in summary["fx"].values()
    )
    console.print(f"[green]Prices updated[/green]: {n_ok} new rows")
    for e in summary["errors"]:
        console.print(f"[yellow]warning:[/yellow] {e}")


@app.command("snapshot")
def snapshot_cmd(
    as_of: Optional[str] = typer.Option(None, help="Snapshot date YYYY-MM-DD (default today)."),
) -> None:
    """Value the portfolio in the base currency and store a daily snapshot."""
    settings = _boot()
    from src.portfolio.valuation import create_snapshot

    with session_scope(settings.db_url) as s:
        snap = create_snapshot(s, settings.base_currency, date.fromisoformat(as_of) if as_of else None)
    console.print(
        f"[green]Snapshot {snap.snapshot_date}[/green] ({settings.base_currency}): value={_fmt(snap.account_value)} "
        f"cash={_fmt(snap.cash)} invested={_fmt(snap.invested_value)} unrealized={_fmt(snap.unrealized_pnl)} "
        f"realized={_fmt(snap.realized_pnl)}"
        + (" [yellow](incomplete: some components unavailable)[/yellow]" if snap.incomplete else "")
    )
    if snap.incomplete and snap.details:
        for issue in json.loads(snap.details).get("issues", []):
            console.print(f"  [yellow]-[/yellow] {issue}")


@app.command("status")
def status_cmd() -> None:
    """Show database status, last syncs and a quick portfolio summary."""
    settings = _boot()
    from src.db.models import Account, CashFlow, ImportRun, Instrument, Position, Price, Transaction
    from src.portfolio.valuation import value_portfolio

    if not settings.db_path.exists():
        console.print(f"[red]Database not found:[/red] {settings.db_path}. Run init-db first.")
        raise typer.Exit(code=1)

    console.print(f"DB: {settings.db_path} | revision: {current_revision(settings.db_url)}")
    console.print(f"Base currency: {settings.base_currency} | provider: {settings.market_data_provider}")
    console.print(
        f"IBKR token: {mask_secret(get_secret('IBKR_FLEX_TOKEN'))} | query id: "
        f"{'set' if get_secret('IBKR_FLEX_QUERY_ID') else '<unset>'}"
    )
    with session_scope(settings.db_url) as s:
        counts = {
            "accounts": s.scalar(select(func.count(Account.id))),
            "instruments": s.scalar(select(func.count(Instrument.id))),
            "transactions": s.scalar(select(func.count(Transaction.id))),
            "cash_flows": s.scalar(select(func.count(CashFlow.id))),
            "positions(open)": s.scalar(select(func.count(Position.id)).where(Position.quantity != 0)),
            "prices": s.scalar(select(func.count(Price.id))),
        }
        console.print(" | ".join(f"{k}: {v}" for k, v in counts.items()))

        t = Table(title="Last jobs")
        for col in ("job", "status", "started", "inserted", "duplicates", "error"):
            t.add_column(col)
        seen = set()
        for run in s.scalars(select(ImportRun).order_by(ImportRun.started_at.desc())):
            if run.job in seen:
                continue
            seen.add(run.job)
            t.add_row(
                run.job,
                run.status,
                run.started_at.strftime("%Y-%m-%d %H:%M"),
                str(run.records_inserted),
                str(run.records_duplicate),
                (run.error or "")[:60],
            )
        console.print(t)

        if counts["positions(open)"]:
            val = value_portfolio(s, settings.base_currency)
            console.print(
                f"Portfolio value: {_fmt(val.total_value_base)} {settings.base_currency} | cash {_fmt(val.cash_base)} "
                f"| invested {_fmt(val.invested_value_base)} | unrealized {_fmt(val.unrealized_pnl_base)} "
                f"| realized {_fmt(val.realized_pnl_base)} | positions {val.positions_count}"
            )
            for i in val.issues:
                console.print(f"  [yellow]-[/yellow] {i}")


@app.command("reconcile")
def reconcile_cmd(
    as_of: Optional[str] = typer.Option(None, help="Reconcile as of date YYYY-MM-DD (default today)."),
) -> None:
    """Break the derived cash ledger into categories and show positions + equity for auditing."""
    settings = _boot()
    from src.portfolio.reconcile import format_report, reconcile

    with session_scope(settings.db_url) as s:
        recs, val = reconcile(s, settings.base_currency, date.fromisoformat(as_of) if as_of else None)
        console.print(format_report(recs, val, settings.base_currency))


@app.command("investments")
def investments_cmd() -> None:
    """List research investments with lifecycle status and thesis health."""
    settings = _boot()
    from src.research.health import thesis_health
    from src.research.investments import list_investments

    with session_scope(settings.db_url) as s:
        invs = list_investments(s)
        if not invs:
            console.print("No investments yet. Create them in the dashboard (Research page).")
            return
        t = Table(title="Research pipeline")
        for col in ("Ticker", "Name", "Status", "Linked", "Next review", "Health", "Notes"):
            t.add_column(col)
        for inv in invs:
            h = thesis_health(s, inv)
            t.add_row(
                inv.ticker,
                (inv.name or "")[:30],
                inv.status,
                "yes" if inv.instrument_id else "-",
                str(inv.next_review_date or "-"),
                h.state or "n/a",
                "; ".join(h.reasons)[:50],
            )
        console.print(t)


@app.command("thesis")
def thesis_cmd(
    action: str = typer.Argument(..., help="Only 'show' is supported."),
    ticker: str = typer.Argument(..., help="Investment ticker, e.g. NU."),
) -> None:
    """Show the current thesis and version history for an investment."""
    settings = _boot()
    if action != "show":
        console.print("[red]Only 'thesis show <ticker>' is supported.[/red]")
        raise typer.Exit(code=2)
    from src.research.assumptions import list_assumptions
    from src.research.investments import get_by_ticker
    from src.research.theses import active_thesis, current_version, version_history

    with session_scope(settings.db_url) as s:
        inv = get_by_ticker(s, ticker)
        if inv is None:
            console.print(f"[red]No investment {ticker.upper()} found.[/red]")
            raise typer.Exit(code=1)
        thesis = active_thesis(s, inv)
        if thesis is None:
            console.print(f"{inv.ticker}: no thesis yet.")
            return
        v = current_version(s, thesis)
        console.print(f"[bold]{thesis.title}[/bold] (v{v.version_number}, {v.created_at:%Y-%m-%d}, confidence {v.confidence if v.confidence is not None else 'n/a'})")
        for label, value in (
            ("Summary", v.summary), ("Core thesis", v.core_thesis),
            ("Market expects", v.market_expectation), ("We expect", v.our_expectation),
            ("Why market may be wrong", v.why_market_may_be_wrong),
            ("Time horizon", v.time_horizon),
        ):
            if value:
                console.print(f"[cyan]{label}:[/cyan] {value}")
        assumptions = list_assumptions(s, thesis)
        if assumptions:
            console.print("[cyan]Assumptions:[/cyan]")
            for a in assumptions:
                console.print(f"  [{a.status}] ({a.importance}) {a.name}")
        history = version_history(s, thesis)
        if len(history) > 1:
            console.print("[cyan]History:[/cyan]")
            for hv in history:
                console.print(f"  v{hv.version_number} {hv.created_at:%Y-%m-%d} - {hv.reason_for_revision}")


@app.command("review")
def review_cmd() -> None:
    """Show the Needs Attention report (reviews due, broken assumptions, triggered breakers...)."""
    settings = _boot()
    from src.research.reviews import needs_attention

    with session_scope(settings.db_url) as s:
        na = needs_attention(s)
        if na.total == 0:
            console.print("[green]Nothing needs attention.[/green]")
            return
        def section(title, rows):
            if rows:
                console.print(f"[bold]{title}[/bold]")
                for r in rows:
                    console.print(f"  - {r}")
        section("Reviews due", [f"{i.ticker} (due {i.next_review_date})" for i in na.reviews_due])
        section("Broken assumptions", [f"{i.ticker}: {a.name}" for i, a in na.broken_assumptions])
        section("Challenged assumptions", [f"{i.ticker}: {a.name}" for i, a in na.challenged_assumptions])
        section("Triggered thesis breakers", [f"{i.ticker}: {b.name}" for i, b in na.triggered_breakers])
        section("High severity risks", [f"{i.ticker}: {r.name} ({r.severity})" for i, r in na.high_risks])
        section("Expired catalysts", [f"{i.ticker}: {c.name} (expected {c.expected_date})" for i, c in na.expired_catalysts])
        section("Predictions awaiting resolution", [f"#{p.id}: {p.statement[:60]} (p={p.probability}%)" for p in na.predictions_awaiting])
        section("Stale valuations", [f"{i.ticker}: {m.name} ({age}d old)" for i, m, age in na.stale_valuations])
        section("Stale theses", [f"{i.ticker}: {t.title} ({age}d since revision)" for i, t, age in na.stale_theses])


@app.command("import-research")
def import_research_cmd(
    path: Path = typer.Argument(..., exists=True, readable=True, help="Research YAML (or JSON) file."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Validate and show the plan; write nothing."),
    allow_existing: bool = typer.Option(
        False, "--allow-existing",
        help="Allow adding records to an existing investment. A thesis is NEVER overwritten or revised.",
    ),
) -> None:
    """Import a structured research file (investment, thesis v1, assumptions, risks, KPIs,
    valuation, evidence, predictions, decisions) through the v2 service layer.

    Conservative by default: aborts on an existing investment; runs in one transaction;
    never touches v1 portfolio data; never creates thesis revisions."""
    settings = _boot()
    from src.portfolio.importer import finish_import_run, start_import_run
    from src.research.importer import ImportError_, apply_import, load_research_file, plan_import

    try:
        spec, digest = load_research_file(path)
    except ImportError_ as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2)

    try:
        if dry_run:
            with session_scope(settings.db_url) as s:
                report = plan_import(s, spec)
                s.rollback()  # belt and braces: a dry run performs no writes anyway
            console.print(report.format())
            return
        # ONE transaction: on any failure session_scope rolls everything back (audit of
        # failed attempts goes to logs/investor.log only, to keep the import strictly atomic)
        with session_scope(settings.db_url) as s:
            run = start_import_run(s, job="import-research", source="research_file")
            report = apply_import(
                s, spec, base_currency=settings.base_currency,
                allow_existing=allow_existing, source_file=str(path),
            )
            run.records_inserted = sum(report.counts.values())
            finish_import_run(s, run, raw_path=str(path), raw_sha256=digest,
                              details={"ticker": report.ticker, "counts": report.counts})
        console.print(report.format())
    except ImportError_ as exc:
        console.print(f"[red]Import aborted, nothing was written:[/red] {exc}")
        raise typer.Exit(code=1)


# ============================================================ v3 intelligence CLI
sec_app = typer.Typer(help="SEC/EDGAR (read-only, rate-limited). DOWNLOAD + PROCESS steps.")
app.add_typer(sec_app, name="sec")
macro_app = typer.Typer(help="Macro series sync (FRED, official sources).")
app.add_typer(macro_app, name="macro")
insiders_app = typer.Typer(help="Insider (Form 4) ingestion. Context, not signals.")
app.add_typer(insiders_app, name="insiders")
inst_app = typer.Typer(help="Institutional 13F tracking (delayed/incomplete by nature).")
app.add_typer(inst_app, name="institutional")
congress_app = typer.Typer(help="Congressional disclosure import (CSV provider; see README limitations).")
app.add_typer(congress_app, name="congress")
intel_app = typer.Typer(help="Intelligence events inbox.")
app.add_typer(intel_app, name="intelligence")
ai_app = typer.Typer(help="AI ANALYZE step: creates PENDING proposals only; nothing is applied.")
app.add_typer(ai_app, name="ai")
proposals_app = typer.Typer(help="APPLY step: human review of AI proposals (accept/reject).")
app.add_typer(proposals_app, name="proposals")
brief_app = typer.Typer(help="Deterministic daily/weekly briefs.")
app.add_typer(brief_app, name="brief")
discovery_app = typer.Typer(help="Research candidate discovery (never purchase advice).")
app.add_typer(discovery_app, name="discovery")


@sec_app.command("sync")
def sec_sync(
    ticker: str = typer.Argument(..., help="Ticker, e.g. NU"),
    facts: bool = typer.Option(True, help="Also sync XBRL companyfacts and run the KPI bridge."),
    download_documents: bool = typer.Option(False, help="Also download primary filing documents."),
) -> None:
    """DOWNLOAD+PROCESS: filing index (foreign-issuer aware), XBRL facts, KPI bridge."""
    settings = _boot()
    from src.intelligence.connectors.sec import SecClient, sync_filings
    from src.intelligence.connectors.xbrl import sync_companyfacts
    from src.intelligence.kpi_mapping import apply_kpi_bridge
    from src.research.investments import get_by_ticker

    with session_scope(settings.db_url) as s:
        client = SecClient(settings)
        summary = sync_filings(s, settings, ticker, client=client, download_documents=download_documents)
        console.print(f"[green]SEC filings[/green]: {summary['issuer']} (CIK {summary['cik']}) "
                      f"forms seen {summary['forms_seen']} | +{summary['filings_inserted']} new, "
                      f"{summary['filings_duplicate']} known")
        if facts:
            fres = sync_companyfacts(s, settings, summary["cik"], client=client)
            console.print(f"[green]XBRL facts[/green]: +{fres.get('facts_inserted', 0)} "
                          f"(mapped to metrics: {fres.get('facts_mapped_to_metrics', 0)})")
            inv = get_by_ticker(s, ticker)
            if inv is not None:
                bres = apply_kpi_bridge(s, inv, summary["cik"])
                console.print(f"KPI bridge: deterministic {bres.deterministic} "
                              f"(+{bres.observations_created} observations), "
                              f"suggested {bres.suggested}, unsupported {len(bres.unsupported)}")


@sec_app.command("filings")
def sec_filings(ticker: str = typer.Argument(...)) -> None:
    """List archived filings for an issuer."""
    settings = _boot()
    from src.intelligence.connectors.sec import list_filings
    from src.intelligence.entities import instrument_by_cik
    from src.db.intelligence import SourceDocument
    from src.research.investments import get_by_ticker

    with session_scope(settings.db_url) as s:
        inv = get_by_ticker(s, ticker)
        docs = list(s.scalars(select(SourceDocument).where(
            SourceDocument.provider == "sec_edgar", SourceDocument.source_type == "filing"
        ).order_by(SourceDocument.published_at.desc())))
        if not docs:
            console.print("No filings archived yet. Run: python -m src.main sec sync " + ticker.upper())
            return
        t = Table(title=f"Archived SEC filings")
        for col in ("Form", "Filed", "Period", "Issuer", "Accession"):
            t.add_column(col)
        import json as _json
        for d in docs[:40]:
            meta = _json.loads(d.metadata_json) if d.metadata_json else {}
            t.add_row(meta.get("form", "?"), str(d.published_at.date() if d.published_at else "-"),
                      str(d.period_end or "-"), (d.issuer or "")[:30], d.external_id)
        console.print(t)


@macro_app.command("sync")
def macro_sync() -> None:
    """DOWNLOAD: refresh configured macro series (vintage-aware, never overwrites history)."""
    settings = _boot()
    from src.intelligence.connectors.macro import sync_macro

    with session_scope(settings.db_url) as s:
        summary = sync_macro(s, settings)
    n = sum(v["inserted"] for v in summary["series"].values())
    console.print(f"[green]Macro sync[/green]: {len(summary['series'])} series, +{n} observations")
    for e in summary["errors"]:
        console.print(f"[yellow]warning:[/yellow] {e}")


@insiders_app.command("sync")
def insiders_sync_cmd(ticker: str = typer.Argument(...)) -> None:
    """DOWNLOAD+PROCESS: recent Form 4 filings for the issuer."""
    settings = _boot()
    from src.intelligence.connectors.insiders import aggregate_insiders, sync_insiders
    from src.intelligence.connectors.sec import SecClient

    with session_scope(settings.db_url) as s:
        summary = sync_insiders(s, settings, ticker, client=SecClient(settings))
        console.print(f"[green]Insiders[/green]: {summary['form4_seen']} Form 4 seen, "
                      f"+{summary['inserted']} transactions")
        agg = aggregate_insiders(s, summary["cik"])
        for w in ("30d", "90d", "365d"):
            a = agg[w]
            console.print(f"  {w}: {a['insiders_buying']} buying / {a['insiders_selling']} selling | "
                          f"net {a['net_shares']:+,.0f} sh (${a['net_value_usd']:+,.0f})")
        console.print(f"  [dim]{agg['note']}[/dim]")


@inst_app.command("add-manager")
def inst_add_manager(name: str = typer.Argument(...), cik: str = typer.Argument(...)) -> None:
    settings = _boot()
    from src.intelligence.connectors.institutional import add_manager

    with session_scope(settings.db_url) as s:
        m = add_manager(s, name, cik)
    console.print(f"Tracking manager {m.name} (CIK {m.cik}). Run: institutional sync")


@inst_app.command("sync")
def inst_sync() -> None:
    """DOWNLOAD+PROCESS: 13F-HR holdings for all tracked managers."""
    settings = _boot()
    from src.db.intelligence import InstitutionalManager
    from src.intelligence.connectors.institutional import DISCLAIMER, sync_manager
    from src.intelligence.connectors.sec import SecClient

    with session_scope(settings.db_url) as s:
        managers = list(s.scalars(select(InstitutionalManager).where(InstitutionalManager.active.is_(True))))
        if not managers:
            console.print("No tracked managers. Add one: institutional add-manager \"Name\" CIK")
            return
        client = SecClient(settings)
        for m in managers:
            summary = sync_manager(s, settings, m, client=client)
            console.print(f"[green]{m.name}[/green]: {summary['reports_seen']} reports, "
                          f"+{summary['holdings_inserted']} holdings")
            for e in summary["errors"]:
                console.print(f"  [yellow]{e}[/yellow]")
    console.print(f"[dim]{DISCLAIMER}[/dim]")


@congress_app.command("import")
def congress_import_cmd(path: Path = typer.Argument(..., exists=True)) -> None:
    """PROCESS: import a congressional disclosure CSV export (see README for format/limits)."""
    settings = _boot()
    from src.intelligence.connectors.congress import CsvCongressProvider, import_congress

    with session_scope(settings.db_url) as s:
        res = import_congress(s, settings, CsvCongressProvider(path), source_file=str(path))
    console.print(f"[green]Congress import[/green]: +{res['inserted']} ({res['duplicates']} duplicates). "
                  "Amounts are ranges; disclosure lags transactions.")


@intel_app.command("events")
def intel_events(
    state: Optional[str] = typer.Option(None, help="NEW | PROCESSED | DISMISSED"),
    severity: Optional[str] = typer.Option(None, help="minimum severity LOW|MEDIUM|HIGH"),
) -> None:
    settings = _boot()
    from src.intelligence.events import list_events

    with session_scope(settings.db_url) as s:
        events = list_events(s, state=state, min_severity=severity)
        t = Table(title=f"Intelligence events ({len(events)})")
        for col in ("Id", "When", "Sev", "Type", "Title", "State", "AI"):
            t.add_column(col)
        for e in events[:50]:
            t.add_row(str(e.id), f"{e.occurred_at:%Y-%m-%d}", e.severity, e.event_type,
                      e.title[:60], e.processing_state, e.ai_state)
        console.print(t)


@ai_app.command("status")
def ai_status() -> None:
    """Show which AI provider is active and whether the local server responds."""
    settings = _boot()
    from src.intelligence.ai.provider import AIUnavailable, get_ai_provider

    console.print(f"AI enabled: {settings.ai_enabled} | provider: {settings.ai_provider} | "
                  f"model: {settings.ai_model} | endpoint: {settings.ai_base_url} (local)")
    if not settings.ai_enabled:
        console.print("Enable in config/settings.yaml (ai.enabled: true). The app works without AI.")
        return
    try:
        provider = get_ai_provider(settings)
        health = provider.health()
        console.print("[green]available[/green]" if health["available"]
                      else f"[yellow]unavailable:[/yellow] {health.get('error')}")
    except AIUnavailable as exc:
        console.print(f"[yellow]{exc}[/yellow]")


@ai_app.command("analyze")
def ai_analyze(
    ticker: str = typer.Argument(...),
    event_id: int = typer.Option(..., "--event", help="Intelligence event id to analyze."),
) -> None:
    """AI ANALYZE: contradiction-first analysis of one event. Creates PENDING proposals only."""
    settings = _boot()
    from src.db.intelligence import IntelligenceEvent
    from src.intelligence.ai.analysis import analyze_event
    from src.intelligence.ai.provider import AIInvalidOutput, AIUnavailable, get_ai_provider
    from src.research.investments import get_by_ticker

    try:
        provider = get_ai_provider(settings)
    except AIUnavailable as exc:
        console.print(f"[yellow]{exc}[/yellow]")
        raise typer.Exit(code=1)
    with session_scope(settings.db_url) as s:
        inv = get_by_ticker(s, ticker)
        event = s.get(IntelligenceEvent, event_id)
        if inv is None or event is None:
            console.print("[red]Investment or event not found.[/red]")
            raise typer.Exit(code=1)
        try:
            analysis, proposals = analyze_event(s, provider, event, inv)
        except (AIUnavailable, AIInvalidOutput) as exc:
            console.print(f"[yellow]AI analysis failed cleanly (no data written):[/yellow] {exc}")
            raise typer.Exit(code=1)
        console.print("[bold]Supports thesis:[/bold] " + analysis.what_supports_thesis)
        console.print("[bold]Contradicts thesis:[/bold] " + analysis.what_contradicts_thesis)
        console.print("[bold]Skeptical view:[/bold] " + analysis.skeptical_view)
        console.print("[bold]Missing info:[/bold] " + analysis.missing_information)
        console.print(f"[green]{len(proposals)} proposal(s) created - PENDING, nothing applied.[/green] "
                      "Review: python -m src.main proposals list")


@ai_app.command("redteam")
def ai_redteam(ticker: str = typer.Argument(...)) -> None:
    """AI ANALYZE: adversarial red-team attack on the thesis. Proposals only."""
    settings = _boot()
    from src.intelligence.ai.analysis import run_red_team
    from src.intelligence.ai.provider import AIInvalidOutput, AIUnavailable, get_ai_provider
    from src.research.investments import get_by_ticker

    try:
        provider = get_ai_provider(settings)
    except AIUnavailable as exc:
        console.print(f"[yellow]{exc}[/yellow]")
        raise typer.Exit(code=1)
    with session_scope(settings.db_url) as s:
        inv = get_by_ticker(s, ticker)
        if inv is None:
            console.print("[red]Investment not found.[/red]")
            raise typer.Exit(code=1)
        try:
            output, proposals = run_red_team(s, provider, inv)
        except (AIUnavailable, AIInvalidOutput) as exc:
            console.print(f"[yellow]Red team failed cleanly (no data written):[/yellow] {exc}")
            raise typer.Exit(code=1)
        console.print("[bold]Strongest bear case:[/bold] " + output.strongest_bear_case)
        for f in output.fragile_assumptions:
            console.print(f"  fragile: {f}")
        console.print(f"[green]{len(proposals)} red-team proposal(s) PENDING review - nothing applied.[/green]")


@ai_app.command("earnings")
def ai_earnings(
    ticker: str = typer.Argument(...),
    event_id: Optional[int] = typer.Option(None, "--event", help="Earnings event id (optional)."),
) -> None:
    """AI ANALYZE: structured earnings review. Proposals only."""
    settings = _boot()
    from src.db.intelligence import IntelligenceEvent
    from src.intelligence.ai.analysis import run_earnings_review
    from src.intelligence.ai.provider import AIInvalidOutput, AIUnavailable, get_ai_provider
    from src.research.investments import get_by_ticker

    try:
        provider = get_ai_provider(settings)
    except AIUnavailable as exc:
        console.print(f"[yellow]{exc}[/yellow]")
        raise typer.Exit(code=1)
    with session_scope(settings.db_url) as s:
        inv = get_by_ticker(s, ticker)
        if inv is None:
            console.print("[red]Investment not found.[/red]")
            raise typer.Exit(code=1)
        event = s.get(IntelligenceEvent, event_id) if event_id else None
        try:
            review, proposals = run_earnings_review(s, provider, inv, event)
        except (AIUnavailable, AIInvalidOutput) as exc:
            console.print(f"[yellow]Earnings review failed cleanly (no data written):[/yellow] {exc}")
            raise typer.Exit(code=1)
        console.print(f"[bold]Verdict:[/bold] {review.thesis_verdict}")
        console.print(review.executive_summary)
        for row in review.kpi_table:
            console.print(f"  {row.kpi}: {row.previous} -> {row.current} ({row.change})")
        console.print(f"[green]{len(proposals)} proposal(s) PENDING review - nothing applied.[/green]")


@proposals_app.command("list")
def proposals_list(status: str = typer.Option("PENDING", help="PENDING|ACCEPTED|REJECTED|...")) -> None:
    settings = _boot()
    from src.intelligence.ai.proposals import list_proposals

    with session_scope(settings.db_url) as s:
        rows = list_proposals(s, status=status)
        t = Table(title=f"AI proposals ({status})")
        for col in ("Id", "Type", "Title", "Conf", "Model", "Created"):
            t.add_column(col)
        for pr in rows[:40]:
            t.add_row(str(pr.id), pr.proposal_type, pr.title[:50],
                      str(pr.confidence or "-"), pr.model or "-", f"{pr.created_at:%Y-%m-%d}")
        console.print(t)
        if status == "PENDING" and rows:
            console.print("APPLY: python -m src.main proposals accept <id> | reject <id>")


@proposals_app.command("accept")
def proposals_accept(
    proposal_id: int = typer.Argument(...),
    reason: Optional[str] = typer.Option(None, help="Required for THESIS_REVISION proposals."),
) -> None:
    """APPLY: human acceptance - calls the existing v2 service for this proposal type."""
    settings = _boot()
    from src.db.intelligence import AiProposal
    from src.intelligence.ai.proposals import accept_proposal

    with session_scope(settings.db_url) as s:
        pr = s.get(AiProposal, proposal_id)
        if pr is None:
            console.print("[red]Proposal not found.[/red]")
            raise typer.Exit(code=1)
        res = accept_proposal(s, pr, base_currency=settings.base_currency, reason_for_revision=reason)
    console.print(f"[green]Accepted:[/green] {res['action']}")


@proposals_app.command("reject")
def proposals_reject(proposal_id: int = typer.Argument(...), note: Optional[str] = typer.Option(None)) -> None:
    settings = _boot()
    from src.db.intelligence import AiProposal
    from src.intelligence.ai.proposals import reject_proposal

    with session_scope(settings.db_url) as s:
        pr = s.get(AiProposal, proposal_id)
        if pr is None:
            console.print("[red]Proposal not found.[/red]")
            raise typer.Exit(code=1)
        reject_proposal(s, pr, note=note)
    console.print("Rejected. No research data was changed.")


@brief_app.command("daily")
def brief_daily() -> None:
    """Deterministic daily brief (no AI content; pending proposals listed for review)."""
    settings = _boot()
    from src.intelligence.briefs import daily_brief

    with session_scope(settings.db_url) as s:
        console.print(daily_brief(s, settings))


@brief_app.command("weekly")
def brief_weekly() -> None:
    settings = _boot()
    from src.intelligence.briefs import weekly_brief

    with session_scope(settings.db_url) as s:
        console.print(weekly_brief(s, settings))


@discovery_app.command("run")
def discovery_run() -> None:
    """PROCESS: refresh research candidates from 13F/insider factors."""
    settings = _boot()
    from src.db.intelligence import ResearchCandidate
    from src.intelligence.discovery import run_discovery

    with session_scope(settings.db_url) as s:
        res = run_discovery(s)
        rows = list(s.scalars(select(ResearchCandidate).where(ResearchCandidate.status == "NEW")))
        console.print(f"[green]Discovery[/green]: +{res['created']} new, {res['updated']} updated; "
                      f"{len(rows)} open candidates")
        import json as _json
        for c in rows[:15]:
            console.print(f"  {c.ticker} ({c.source}): " + "; ".join(_json.loads(c.reasons_json or "[]"))[:100])
    console.print("Promote in the dashboard Discovery tab or: discovery promote <ticker>")


@discovery_app.command("promote")
def discovery_promote(ticker: str = typer.Argument(...)) -> None:
    settings = _boot()
    from src.db.intelligence import ResearchCandidate
    from src.intelligence.discovery import promote_candidate

    with session_scope(settings.db_url) as s:
        c = s.scalars(select(ResearchCandidate).where(
            ResearchCandidate.ticker == ticker.upper(), ResearchCandidate.status == "NEW")).first()
        if c is None:
            console.print("[red]No open candidate with that ticker.[/red]")
            raise typer.Exit(code=1)
        inv = promote_candidate(s, c)
    console.print(f"[green]Promoted[/green] {inv.ticker} to research (status DISCOVERED).")


@app.command("technical")
def technical_cmd(ticker: str = typer.Argument(...)) -> None:
    """Deterministic technical/market CONTEXT for an instrument (never a signal)."""
    settings = _boot()
    from src.db.models import Instrument
    from src.intelligence.technical import technical_context_for_instrument

    with session_scope(settings.db_url) as s:
        inst = s.scalars(select(Instrument).where(
            Instrument.symbol == ticker.upper(), Instrument.asset_type != "cash")).first()
        if inst is None:
            console.print("[red]Instrument not found in the portfolio DB.[/red]")
            raise typer.Exit(code=1)
        ctx = technical_context_for_instrument(s, inst.id)
        for line in ctx.statements():
            console.print("  " + line)


@app.command("dashboard")
def dashboard_cmd() -> None:
    """Launch the local Streamlit dashboard (binds to 127.0.0.1 only)."""
    import subprocess
    import sys

    settings = _boot()
    from src.config import PROJECT_ROOT

    cmd = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(PROJECT_ROOT / "dashboard" / "app.py"),
        "--server.address",
        settings.dashboard_host,
        "--server.port",
        str(settings.dashboard_port),
        "--browser.gatherUsageStats",
        "false",
    ]
    console.print(f"Starting dashboard at http://{settings.dashboard_host}:{settings.dashboard_port}")
    subprocess.call(cmd)


def run() -> None:
    app()
