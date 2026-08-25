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


def _run_brief(brief_type: str, no_ai: bool, audio: bool, force: bool, preview: bool) -> None:
    settings = _boot()
    from src.intelligence.briefing.generate import generate_brief

    with session_scope(settings.db_url) as s:
        doc, run, md_path, audio_path = generate_brief(
            s, settings, brief_type=brief_type, use_ai=not no_ai, audio=audio,
            force=force, preview=preview,
        )
    from src.intelligence.briefing.assemble import render_markdown

    console.print(render_markdown(doc))
    console.print(f"\n[green]Saved:[/green] {md_path}" + (f" | audio: {audio_path}" if audio_path else ""))
    if run is None:
        console.print("[dim]Preview only - checkpoint not advanced; re-run without --preview "
                      "on a new day (or with --force today) to record it.[/dim]")


@brief_app.command("daily")
def brief_daily(
    no_ai: bool = typer.Option(False, "--no-ai", help="Skip the mentor synthesis call."),
    audio: bool = typer.Option(True, "--audio/--no-audio", help="Also write the TTS-friendly text."),
    force: bool = typer.Option(False, "--force", help="Regenerate today's brief (supersedes the earlier run)."),
    preview: bool = typer.Option(False, "--preview", help="Render without recording a checkpoint."),
) -> None:
    """Delta-aware daily brief: what CHANGED since the last brief (static state is suppressed)."""
    _run_brief("daily", no_ai, audio, force, preview)


@brief_app.command("weekly")
def brief_weekly(
    no_ai: bool = typer.Option(False, "--no-ai"),
    audio: bool = typer.Option(True, "--audio/--no-audio"),
    force: bool = typer.Option(False, "--force"),
    preview: bool = typer.Option(False, "--preview"),
) -> None:
    """Weekly mentor review: deltas since last weekly + performance, health, KPIs, regime, calibration."""
    _run_brief("weekly", no_ai, audio, force, preview)


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


earnings_app = typer.Typer(help="Issuer-specific earnings extraction & comparison (deterministic).")
app.add_typer(earnings_app, name="earnings")
mentor_app = typer.Typer(help="AI mentor reviews (proposals/text only; never mutations).")
app.add_typer(mentor_app, name="mentor")


@earnings_app.command("extract")
def earnings_extract(
    ticker: str = typer.Argument(...),
    file: Optional[Path] = typer.Option(None, "--file", help="Local HTML/text earnings release."),
    doc_id: Optional[int] = typer.Option(None, "--doc", help="Archived source_documents id."),
    accession: Optional[str] = typer.Option(None, "--accession", help="SEC accession: download all filing documents (incl. exhibits) and extract."),
    period: str = typer.Option(..., "--period", help="e.g. 2026Q2"),
    period_end: Optional[str] = typer.Option(None, help="Period end date YYYY-MM-DD."),
) -> None:
    """Extract issuer-specific KPIs from a primary document into KPI observations (with provenance)."""
    settings = _boot()
    from datetime import date as _date

    from src.db.intelligence import SourceDocument
    from src.intelligence.earnings import compare_kpis, comparison_table_text, flag_contradictions, run_extraction
    from src.research.investments import get_by_ticker

    with session_scope(settings.db_url) as s:
        inv = get_by_ticker(s, ticker)
        if inv is None:
            console.print("[red]Investment not found.[/red]")
            raise typer.Exit(code=1)
        source_doc = None
        if accession is not None:
            from src.db.models import Instrument
            from src.intelligence.connectors.sec import SecClient, filing_url, list_filing_files
            from src.intelligence.entities import _provider_ids
            from src.intelligence.provenance import register_source

            inst = s.get(Instrument, inv.instrument_id) if inv.instrument_id else None
            cik = _provider_ids(inst).get("sec_cik") if inst else None
            if not cik:
                console.print("[red]No CIK known for this investment - run `sec sync` first.[/red]")
                raise typer.Exit(code=1)
            client = SecClient(settings)
            files = [f for f in list_filing_files(client, cik, accession)
                     if f.lower().endswith((".htm", ".html", ".txt")) and "index" not in f.lower()][:12]
            parts = []
            for fname in files:
                try:
                    parts.append(client.get(filing_url(cik, accession, fname)).decode("utf-8", errors="replace"))
                except Exception as exc:
                    console.print(f"  [yellow]skip {fname}: {exc}[/yellow]")
            raw = "\n".join(parts)
            source_doc, _created = register_source(
                s, settings, provider="sec_edgar", source_type="filing", external_id=accession,
                raw=raw.encode("utf-8"), category="sec", entity_key=cik, source_tier=1,
                title=f"filing {accession} (all documents)",
            )
            console.print(f"Downloaded {len(files)} document(s) from accession {accession}.")
        elif doc_id is not None:
            source_doc = s.get(SourceDocument, doc_id)
            if source_doc is None or not source_doc.raw_path:
                console.print("[red]Source document not found or has no archived raw payload.[/red]")
                raise typer.Exit(code=1)
            raw = Path(source_doc.raw_path).read_text(encoding="utf-8", errors="replace")
        elif file is not None:
            raw = file.read_text(encoding="utf-8", errors="replace")
        else:
            console.print("[red]Provide --file or --doc.[/red]")
            raise typer.Exit(code=2)
        res = run_extraction(
            s, inv, raw, period=period,
            period_date=_date.fromisoformat(period_end) if period_end else None,
            source_document=source_doc,
        )
        comps = compare_kpis(s, inv)
        created = flag_contradictions(s, inv, comps)
        console.print(f"[green]Extracted[/green] ({res.extractor}): stored {len(res.stored)}, "
                      f"duplicates {len(res.duplicates)}, ambiguous {len(res.ambiguous)}, "
                      f"unmatched {len(res.unmatched_kpi_names)}")
        for line in res.stored:
            console.print(f"  + {line}")
        for a in res.ambiguous:
            console.print(f"  [yellow]ambiguous -> review proposal:[/yellow] {a}")
        if created:
            console.print(f"[yellow]{len(created)} KPI value(s) outside thesis expectations -> "
                          "ASSUMPTION review proposal(s) created (nothing changed automatically).[/yellow]")
        console.print("\n" + comparison_table_text(comps))


@earnings_app.command("compare")
def earnings_compare(ticker: str = typer.Argument(...)) -> None:
    """Deterministic KPI comparison: current vs previous quarter vs year-ago vs thesis expectation."""
    settings = _boot()
    from src.intelligence.earnings import compare_kpis, comparison_table_text
    from src.research.investments import get_by_ticker

    with session_scope(settings.db_url) as s:
        inv = get_by_ticker(s, ticker)
        if inv is None:
            console.print("[red]Investment not found.[/red]")
            raise typer.Exit(code=1)
        console.print(comparison_table_text(compare_kpis(s, inv)) or "No KPI observations with period dates.")


@mentor_app.command("review")
def mentor_review(ticker: str = typer.Argument(...)) -> None:
    """Decision-quality review: process over outcome (deterministic discipline stats + AI mentor)."""
    settings = _boot()
    from src.intelligence.ai.provider import AIInvalidOutput, AIUnavailable, get_ai_provider
    from src.intelligence.briefing.decision_quality import journal_discipline, run_decision_review
    from src.research.investments import get_by_ticker

    with session_scope(settings.db_url) as s:
        inv = get_by_ticker(s, ticker)
        if inv is None:
            console.print("[red]Investment not found.[/red]")
            raise typer.Exit(code=1)
        disc = journal_discipline(s, inv)
        console.print(f"Journal discipline: {disc.decisions} decision(s); reasoning {disc.with_reasoning}, "
                      f"falsifier {disc.with_falsifier}, alternatives {disc.with_alternatives}, "
                      f"confidence {disc.with_confidence}, key risks {disc.with_key_risks}")
        for n in disc.notes:
            console.print(f"  [yellow]-[/yellow] {n}")
        try:
            provider = get_ai_provider(settings)
            review = run_decision_review(s, provider, inv)
        except (AIUnavailable, AIInvalidOutput) as exc:
            console.print(f"[yellow]AI mentor unavailable:[/yellow] {exc}")
            return
        for label, text in (
            ("Overall", review.overall_observations), ("Reasoning consistency", review.reasoning_consistency),
            ("Luck vs skill", review.luck_vs_skill), ("Ignored risks", review.ignored_risks),
            ("Confidence justified?", review.confidence_justified),
            ("Followed original thesis?", review.followed_original_thesis),
            ("Story drift", review.story_drift), ("Sizing vs uncertainty", review.sizing_vs_uncertainty),
        ):
            console.print(f"[bold]{label}:[/bold] {text}")
        for q in review.questions_for_the_investor:
            console.print(f"  ? {q}")


@mentor_app.command("red-team")
def mentor_red_team(ticker: str = typer.Argument(...)) -> None:
    """Alias for `ai redteam` (adversarial thesis attack -> proposals)."""
    ai_redteam(ticker)


@app.command("calibration")
def calibration_cmd() -> None:
    """Prediction calibration (honest about small samples)."""
    settings = _boot()
    from src.intelligence.briefing.calibration import calibration_report

    with session_scope(settings.db_url) as s:
        rep = calibration_report(s)
    if not rep.sufficient:
        console.print(rep.note)
        return
    console.print(f"Resolved: {rep.resolved} | Brier score: {rep.brier_score} | hit rate {rep.hit_rate:.0%}")
    for b in rep.buckets:
        console.print(f"  {b['bucket']}: stated ~{b['avg_stated_probability']}% vs observed "
                      f"{b['observed_frequency_pct']}% (n={b['n']})")
    console.print(f"[dim]{rep.note}[/dim]")


@app.command("feedback")
def feedback_cmd(
    item_key: str = typer.Argument(..., help="Brief item key (shown in the Today page)."),
    rating: str = typer.Argument(..., help="USEFUL | NOT_USEFUL | TOO_NOISY | MORE_LIKE_THIS"),
    note: Optional[str] = typer.Option(None),
) -> None:
    """Record human feedback on a surfaced item (stored only; nothing retrains automatically)."""
    settings = _boot()
    from src.db.briefing import FEEDBACK_RATINGS, BriefFeedback

    rating = rating.upper()
    if rating not in FEEDBACK_RATINGS:
        console.print(f"[red]rating must be one of {FEEDBACK_RATINGS}[/red]")
        raise typer.Exit(code=2)
    with session_scope(settings.db_url) as s:
        s.add(BriefFeedback(item_key=item_key, rating=rating, note=note))
    console.print("Feedback stored.")


run_app = typer.Typer(help="Unified pipelines: one command runs the Investor OS day/week.")
app.add_typer(run_app, name="run")


@run_app.command("daily")
def run_daily_cmd(
    no_ai: bool = typer.Option(False, "--no-ai", help="Skip AI stages."),
    no_sync: bool = typer.Option(False, "--no-sync", help="Skip external provider syncs (offline mode)."),
    no_audio: bool = typer.Option(False, "--no-audio"),
) -> None:
    """DOWNLOAD -> PROCESS -> AI ANALYZE -> BRIEF, with per-stage status. Idempotent."""
    settings = _boot()
    from src.operations.pipeline import run_daily

    report = run_daily(settings, use_ai=not no_ai, audio=not no_audio, sync_external=not no_sync)
    console.print(report.format())
    raise typer.Exit(code=0 if report.status in ("SUCCESS", "PARTIAL") else 1)


@run_app.command("weekly")
def run_weekly_cmd(
    no_ai: bool = typer.Option(False, "--no-ai"),
    no_sync: bool = typer.Option(False, "--no-sync"),
) -> None:
    """Daily-type sync first, then the weekly mentor review + risk + claims check."""
    settings = _boot()
    from src.operations.pipeline import run_weekly

    report = run_weekly(settings, use_ai=not no_ai, sync_external=not no_sync)
    console.print(report.format())
    raise typer.Exit(code=0 if report.status in ("SUCCESS", "PARTIAL") else 1)


@app.command("doctor")
def doctor_cmd() -> None:
    """Data-integrity and configuration checks (read-only; OK / WARN / FAIL)."""
    settings = _boot()
    from src.operations.doctor import overall_status, run_doctor

    checks = run_doctor(settings)
    colors = {"OK": "green", "WARN": "yellow", "FAIL": "red"}
    for c in checks:
        console.print(f"[{colors[c.status]}]{c.status:<5}[/{colors[c.status]}] {c.name}"
                      + (f" — {c.detail}" if c.detail else ""))
    overall = overall_status(checks)
    console.print(f"\nOverall: [{colors[overall]}]{overall}[/{colors[overall]}]")
    raise typer.Exit(code=0 if overall != "FAIL" else 1)


@app.command("backup")
def backup_cmd(kind: str = typer.Option("daily", help="daily | weekly (affects rotation slot)")) -> None:
    """Create a rotating local SQLite backup (kept in data/backups, never committed)."""
    settings = _boot()
    from src.operations.backups import create_backup

    path = create_backup(settings, kind)
    console.print(f"[green]Backup:[/green] {path}" if path else "[red]No database to back up.[/red]")


transcript_app = typer.Typer(help="Earnings transcript / management text import (manual, sourced).")
app.add_typer(transcript_app, name="transcript")
claims_app = typer.Typer(help="Management claims: extraction, outcome linking, track record.")
app.add_typer(claims_app, name="claims")
company_app = typer.Typer(help="Company onboarding utilities.")
app.add_typer(company_app, name="company")
extractor_app = typer.Typer(help="Issuer-extractor development tooling.")
app.add_typer(extractor_app, name="extractor")
stress_app = typer.Typer(help="Deterministic scenario stress tests (mechanical, not forecasts).")
app.add_typer(stress_app, name="stress")
research_app = typer.Typer(help="Open research questions and local source search.")
app.add_typer(research_app, name="research")


@transcript_app.command("import")
def transcript_import(
    ticker: str = typer.Argument(...),
    file: Path = typer.Argument(..., exists=True, readable=True),
    source_name: str = typer.Option(..., "--source", help="e.g. 'Q2 2026 earnings call (company webcast)'"),
    claim_date: Optional[str] = typer.Option(None, help="Statement date YYYY-MM-DD."),
    speaker: Optional[str] = typer.Option(None),
    speaker_role: Optional[str] = typer.Option(None),
) -> None:
    """Import a transcript/management text: archive with provenance + extract forward-looking
    claims deterministically (verbatim quotes). Only store what source rights permit."""
    settings = _boot()
    from datetime import date as _date

    from src.intelligence.claims import ingest_claims_from_source
    from src.intelligence.issuer_extractors import html_to_text
    from src.intelligence.provenance import register_source
    from src.research.investments import get_by_ticker

    with session_scope(settings.db_url) as s:
        inv = get_by_ticker(s, ticker)
        if inv is None:
            console.print("[red]Investment not found.[/red]")
            raise typer.Exit(code=1)
        raw = file.read_bytes()
        doc, _created = register_source(
            s, settings, provider="manual", source_type="transcript",
            external_id=f"{ticker.upper()}:{file.name}", raw=raw, category="news",
            title=source_name, entity_key=ticker.upper(), source_tier=1,
        )
        text = html_to_text(raw.decode("utf-8", errors="replace"))
        claims = ingest_claims_from_source(
            s, inv, text, source_document_id=doc.id, source_reference=source_name,
            claim_date=_date.fromisoformat(claim_date) if claim_date else None,
            speaker=speaker, speaker_role=speaker_role,
        )
    console.print(f"[green]Transcript archived[/green] (source_documents:{doc.id}); "
                  f"{len(claims)} forward-looking claim(s) captured:")
    for c in claims:
        console.print(f"  [{c.claim_type}] {c.statement[:100]}")


@claims_app.command("list")
def claims_list(ticker: str = typer.Argument(...), status: Optional[str] = typer.Option(None)) -> None:
    settings = _boot()
    from src.intelligence.claims import list_claims
    from src.research.investments import get_by_ticker

    with session_scope(settings.db_url) as s:
        inv = get_by_ticker(s, ticker)
        rows = list_claims(s, inv, status=status) if inv else []
        t = Table(title=f"Management claims — {ticker.upper()}")
        for col in ("Id", "Type", "Status", "Date", "Horizon", "Statement"):
            t.add_column(col)
        for c in rows:
            t.add_row(str(c.id), c.claim_type, c.status, str(c.claim_date or "-"),
                      c.time_horizon or "-", c.statement[:70])
        console.print(t)


@claims_app.command("link")
def claims_link(
    claim_id: int = typer.Argument(...),
    target: str = typer.Argument(..., help="e.g. kpi_observation:22 or evidence:5"),
    status: Optional[str] = typer.Option(None, help="CONFIRMED | PARTIALLY_CONFIRMED | MISSED | SUPERSEDED | AMBIGUOUS"),
    note: Optional[str] = typer.Option(None),
) -> None:
    """Link a claim to later evidence/KPI outcome and (optionally) judge it."""
    settings = _boot()
    from src.db.briefing import ManagementClaim
    from src.intelligence.claims import link_claim_outcome

    ttype, _, tid = target.partition(":")
    with session_scope(settings.db_url) as s:
        claim = s.get(ManagementClaim, claim_id)
        if claim is None:
            console.print("[red]Claim not found.[/red]")
            raise typer.Exit(code=1)
        link_claim_outcome(s, claim, ttype, int(tid), note=note, new_status=status)
    console.print(f"[green]Linked[/green] claim #{claim_id} -> {target}"
                  + (f" (status {status})" if status else ""))


@claims_app.command("track-record")
def claims_track_record(ticker: str = typer.Argument(...)) -> None:
    settings = _boot()
    from src.intelligence.claims import track_record
    from src.research.investments import get_by_ticker

    with session_scope(settings.db_url) as s:
        inv = get_by_ticker(s, ticker)
        rep = track_record(s, inv) if inv else None
    if not rep:
        console.print("[red]Investment not found.[/red]")
        raise typer.Exit(code=1)
    console.print(f"Claims: {rep['total']} total, {rep['open']} open, {rep['resolved']} resolved"
                  + (f", hit rate {rep['hit_rate']:.0%}" if rep["hit_rate"] is not None else ""))
    for k, v in rep["by_status"].items():
        console.print(f"  {k}: {v}")
    for e in rep["examples"][:6]:
        console.print(f"  [{e['type']}/{e['status']}] {e['statement'][:90]}")
    console.print(f"[dim]{rep['note']}[/dim]")


@earnings_app.command("preview")
def earnings_preview_cmd(ticker: str = typer.Argument(...)) -> None:
    """Pre-earnings checklist (deterministic; nothing fabricated)."""
    settings = _boot()
    from src.intelligence.earnings_workflows import earnings_preview
    from src.research.investments import get_by_ticker

    with session_scope(settings.db_url) as s:
        inv = get_by_ticker(s, ticker)
        if inv is None:
            console.print("[red]Investment not found.[/red]")
            raise typer.Exit(code=1)
        console.print(earnings_preview(s, settings, inv))


@earnings_app.command("postmortem")
def earnings_postmortem_cmd(
    ticker: str = typer.Argument(...),
    period: Optional[str] = typer.Option(None, help="e.g. 2026Q2 (default: latest)"),
) -> None:
    """Post-earnings review: actual vs previous vs our expectation vs consensus (if stored)."""
    settings = _boot()
    from src.intelligence.earnings_workflows import earnings_postmortem
    from src.research.investments import get_by_ticker

    with session_scope(settings.db_url) as s:
        inv = get_by_ticker(s, ticker)
        if inv is None:
            console.print("[red]Investment not found.[/red]")
            raise typer.Exit(code=1)
        console.print(earnings_postmortem(s, settings, inv, period))


@company_app.command("onboard")
def company_onboard(ticker: str = typer.Argument(...)) -> None:
    """Resolve identity, sync filings/facts, report coverage gaps, write extractor TODO."""
    settings = _boot()
    from src.intelligence.onboarding import onboard_company

    with session_scope(settings.db_url) as s:
        report = onboard_company(s, settings, ticker)
    console.print(f"[bold]{report['ticker']}[/bold] — {report.get('issuer', 'unknown issuer')}")
    console.print(f"  Instrument: {report['instrument']}")
    console.print(f"  CIK: {report.get('cik')} | filer type: {report.get('filer_type')}")
    console.print(f"  Forms: {', '.join(report.get('forms_seen', []))}")
    console.print(f"  XBRL facts: {report.get('xbrl_facts', 0)} | standard metrics: "
                  f"{', '.join(report.get('standard_metrics_available', [])) or 'none'}")
    console.print(f"  Issuer extractor: {report.get('issuer_extractor') or '[yellow]none - see TODO[/yellow]'}")
    if report.get("kpis_without_automatic_source"):
        console.print("  KPIs without automatic source: " + ", ".join(report["kpis_without_automatic_source"]))
    for f in report.get("files", []):
        console.print(f"  [green]written:[/green] {f}")
    for w in report.get("warnings", []):
        console.print(f"  [yellow]{w}[/yellow]")


@extractor_app.command("inspect")
def extractor_inspect(
    ticker: str = typer.Argument(...),
    file: Optional[Path] = typer.Option(None, "--file"),
    doc_id: Optional[int] = typer.Option(None, "--doc"),
) -> None:
    """Show numeric KPI-candidate contexts in a source document (extractor development aid)."""
    settings = _boot()
    from src.db.intelligence import SourceDocument
    from src.intelligence.extractor_assistant import format_inspection, inspect_candidates
    from src.research.investments import get_by_ticker
    from src.research.kpis import list_kpis

    with session_scope(settings.db_url) as s:
        inv = get_by_ticker(s, ticker)
        kpi_names = [k.name for k in list_kpis(s, inv)] if inv else []
        if doc_id is not None:
            doc = s.get(SourceDocument, doc_id)
            if doc is None or not doc.raw_path:
                console.print("[red]Document not found or no raw payload.[/red]")
                raise typer.Exit(code=1)
            raw = Path(doc.raw_path).read_text(encoding="utf-8", errors="replace")
        elif file is not None:
            raw = file.read_text(encoding="utf-8", errors="replace")
        else:
            console.print("[red]Provide --file or --doc.[/red]")
            raise typer.Exit(code=2)
    console.print(format_inspection(inspect_candidates(raw, kpi_names)))


@mentor_app.command("add-review")
def mentor_add_review(ticker: str = typer.Argument(...)) -> None:
    """'Should I add?' — deterministic facts + AI considerations. Never an order."""
    _position_review(ticker, "add")


@mentor_app.command("exit-review")
def mentor_exit_review(ticker: str = typer.Argument(...)) -> None:
    """'Should I sell?' — thesis broken? valuation extreme? or just price noise?"""
    _position_review(ticker, "exit")


def _position_review(ticker: str, mode: str) -> None:
    settings = _boot()
    from src.intelligence.ai.provider import AIInvalidOutput, AIUnavailable, get_ai_provider
    from src.intelligence.briefing.mentor_workflows import add_review, exit_review
    from src.research.investments import get_by_ticker

    with session_scope(settings.db_url) as s:
        inv = get_by_ticker(s, ticker)
        if inv is None:
            console.print("[red]Investment not found.[/red]")
            raise typer.Exit(code=1)
        from src.intelligence.briefing.mentor_workflows import _position_facts

        facts = _position_facts(s, settings, inv)
        console.print("[bold]Deterministic facts:[/bold]")
        console.print_json(json.dumps(facts, default=str))
        try:
            provider = get_ai_provider(settings)
            _f, review = (add_review if mode == "add" else exit_review)(s, settings, provider, inv)
        except (AIUnavailable, AIInvalidOutput) as exc:
            console.print(f"[yellow]AI mentor unavailable ({exc}) - facts above remain valid.[/yellow]")
            return
        console.print(f"[bold]Summary:[/bold] {review.summary}")
        for label, items in (("For", review.arguments_for), ("Against", review.arguments_against),
                             ("Unknowns", review.unknowns)):
            for x in items:
                console.print(f"  {label}: {x}")
        if review.what_would_improve_entry:
            console.print(f"  Better entry: {review.what_would_improve_entry}")
        if review.what_would_make_waiting_better:
            console.print(f"  Waiting: {review.what_would_make_waiting_better}")
        console.print("[dim]Considerations only - no order, no size.[/dim]")


@mentor_app.command("replay")
def mentor_replay(
    ticker: str = typer.Argument(...),
    decision_id: Optional[int] = typer.Option(None, "--decision"),
    reveal: bool = typer.Option(False, "--reveal", help="Second pass: show the outcome."),
) -> None:
    """DECISION REPLAY: blind first pass (only information known at decision time)."""
    settings = _boot()
    from src.intelligence.briefing.replay import replay_view, reveal_outcome
    from src.research.decisions import list_decisions
    from src.research.investments import get_by_ticker

    with session_scope(settings.db_url) as s:
        inv = get_by_ticker(s, ticker)
        decisions = list_decisions(s, inv) if inv else []
        if not decisions:
            console.print("[red]No decisions found.[/red]")
            raise typer.Exit(code=1)
        decision = next((d for d in decisions if d.id == decision_id), decisions[0])
        if reveal:
            console.print_json(json.dumps(reveal_outcome(s, decision, settings), default=str))
        else:
            console.print_json(json.dumps(replay_view(s, decision), default=str))
            console.print("Answer the question, THEN run with --reveal. Rate with: "
                          f"mentor rate-decision {decision.id} GOOD_PROCESS|MIXED|POOR_PROCESS")


@mentor_app.command("rate-decision")
def mentor_rate_decision(
    decision_id: int = typer.Argument(...),
    rating: str = typer.Argument(..., help="GOOD_PROCESS | MIXED | POOR_PROCESS"),
    would_repeat: Optional[bool] = typer.Option(None, "--would-repeat/--would-not-repeat"),
    note: Optional[str] = typer.Option(None),
) -> None:
    """Outcome-independent process rating for a decision."""
    settings = _boot()
    from src.db.research import Decision
    from src.intelligence.briefing.replay import rate_decision

    with session_scope(settings.db_url) as s:
        d = s.get(Decision, decision_id)
        if d is None:
            console.print("[red]Decision not found.[/red]")
            raise typer.Exit(code=1)
        rate_decision(s, d, rating.upper(), would_repeat=would_repeat, replay_used=True, notes=note)
    console.print("Rating stored (process, not outcome).")


@mentor_app.command("opportunity")
def mentor_opportunity() -> None:
    """Opportunity-cost table: owned positions vs research candidates (no composite score)."""
    settings = _boot()
    from src.intelligence.briefing.mentor_workflows import opportunity_table

    with session_scope(settings.db_url) as s:
        rows = opportunity_table(s, settings)
    t = Table(title="Opportunity set (dimensions visible; no opaque score)")
    for col in ("Ticker", "Status", "Weight %", "Exp. return %", "Confidence", "Health", "Open risks", "Horizon"):
        t.add_column(col)
    for r in rows:
        t.add_row(r["ticker"], r["status"], str(r["weight_pct"]),
                  str(r["expected_return_pct"] if r["expected_return_pct"] is not None else "-"),
                  str(r["thesis_confidence"] or "-"), r["thesis_health"],
                  str(r["open_risks"] if r["open_risks"] is not None else "-"), r["time_horizon"] or "-")
    console.print(t)


@mentor_app.command("prefs")
def mentor_prefs() -> None:
    """Inspect deterministic feedback-derived preferences (ordering hints only)."""
    settings = _boot()
    from src.intelligence.briefing.mentor_workflows import feedback_preferences

    with session_scope(settings.db_url) as s:
        prefs = feedback_preferences(s)
    if not prefs:
        console.print("No feedback stored yet.")
        return
    for dt, p in prefs.items():
        console.print(f"{dt}: {p['hint']} {p['counts']}")
    console.print("[dim]Used only as ordering hints in the brief; never silent filtering.[/dim]")


@stress_app.command("run")
def stress_run(
    file: Optional[Path] = typer.Option(None, "--file", help="YAML with scenarios (name + shocks)."),
    builtin: bool = typer.Option(False, "--builtin", help="Run the built-in scenario set."),
) -> None:
    """Mechanical portfolio stress tests. Clearly not forecasts."""
    settings = _boot()
    from src.analytics.stress import BUILTIN_SCENARIOS, format_result, load_scenarios_yaml, run_stress

    scenarios = BUILTIN_SCENARIOS if builtin or file is None else load_scenarios_yaml(file)
    with session_scope(settings.db_url) as s:
        for sc in scenarios:
            try:
                console.print(format_result(run_stress(s, settings, sc["name"], sc["shocks"])))
            except Exception as exc:
                console.print(f"[yellow]{sc['name']}: {exc}[/yellow]")
            console.print("")


@research_app.command("questions")
def research_questions() -> None:
    """Open AI research questions, ranked."""
    settings = _boot()
    from src.intelligence.briefing.mentor_workflows import open_research_questions

    with session_scope(settings.db_url) as s:
        rows = open_research_questions(s)
    if not rows:
        console.print("No open research questions.")
    for r in rows:
        console.print(f"#{r['id']} ({r['confidence'] or '-'}): {r['question']}")
        if r["why"]:
            console.print(f"    {r['why'][:120]}")


@research_app.command("search")
def research_search(keywords: str = typer.Argument(..., help="Comma-separated keywords.")) -> None:
    """Search locally archived primary sources (no web browsing)."""
    settings = _boot()
    from src.intelligence.briefing.mentor_workflows import search_local_sources

    with session_scope(settings.db_url) as s:
        hits = search_local_sources(s, [k.strip() for k in keywords.split(",") if k.strip()])
    if not hits:
        console.print("No matches in the local archive.")
    for h in hits:
        console.print(f"[bold]{h['title']}[/bold] (source_documents:{h['source_document_id']})")
        console.print(f"  ...{h['excerpt']}...")


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
