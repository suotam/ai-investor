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
