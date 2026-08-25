"""`doctor`: data-integrity and configuration checks. Read-only - reports OK/WARN/FAIL and
never modifies data (repairs remain explicit user actions)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, select

from src.config import Settings
from src.db.session import current_revision, session_scope

EXPECTED_HEAD = "0006_v5_operations_mentor"


@dataclass
class Check:
    name: str
    status: str  # OK | WARN | FAIL
    detail: str = ""


def run_doctor(settings: Settings) -> list[Check]:
    checks: list[Check] = []

    def add(name, status, detail=""):
        checks.append(Check(name, status, detail))

    if not settings.db_path.exists():
        add("Database file", "FAIL", f"{settings.db_path} missing")
        return checks
    add("Database file", "OK", str(settings.db_path))

    rev = current_revision(settings.db_url)
    add("Migration head", "OK" if rev == EXPECTED_HEAD else "FAIL",
        f"{rev} (expected {EXPECTED_HEAD})")

    with session_scope(settings.db_url) as s:
        from src.db.models import CashFlow, Position, Transaction
        from src.db.research import Investment, Thesis, ThesisVersion

        # portfolio invariant: positions rebuildable == stored positions
        from src.portfolio.positions import compute_position, load_trades
        from src.core import D

        mismatches = []
        stored = {(p.account_id, p.instrument_id): p for p in s.scalars(select(Position))}
        for key, trades in load_trades(s).items():
            st = compute_position(trades)
            p = stored.get(key)
            if p is None or abs(D(p.quantity) - st.quantity) > D("0.0001"):
                mismatches.append(key)
        add("Portfolio invariant (positions == replay of transactions)",
            "OK" if not mismatches else "FAIL",
            f"{len(mismatches)} mismatching position(s) - run rebuild-portfolio" if mismatches else "")

        # cash reconciliation computes without error
        try:
            from src.portfolio.cash import cash_balances

            bal = cash_balances(s)
            add("Cash ledger", "OK", f"{len(bal)} account-currency balance(s)")
        except Exception as exc:
            add("Cash ledger", "FAIL", str(exc)[:120])

        # orphan research rows
        inv_ids = {i.id for i in s.scalars(select(Investment))}
        orphan_theses = [t.id for t in s.scalars(select(Thesis)) if t.investment_id not in inv_ids]
        thesis_ids = {t.id for t in s.scalars(select(Thesis))}
        orphan_versions = [v.id for v in s.scalars(select(ThesisVersion)) if v.thesis_id not in thesis_ids]
        add("Research integrity (no orphans)", "OK" if not (orphan_theses or orphan_versions) else "FAIL",
            f"orphan theses {orphan_theses} versions {orphan_versions}" if orphan_theses or orphan_versions else "")

        # provenance: external rows should reference source documents / references
        from src.db.intelligence import FinancialFact, InsiderTransaction, SourceDocument

        facts_wo = s.scalar(select(func.count(FinancialFact.id)).where(FinancialFact.source_document_id.is_(None)))
        add("Fact provenance", "OK" if not facts_wo else "WARN",
            f"{facts_wo} financial fact(s) without source document" if facts_wo else "")
        missing_raw = [
            d.id for d in s.scalars(select(SourceDocument).where(SourceDocument.raw_path.isnot(None)))
            if not Path(d.raw_path).exists()
        ]
        add("Raw archive paths", "OK" if not missing_raw else "WARN",
            f"{len(missing_raw)} archived file(s) missing on disk" if missing_raw else "")

        # duplicate external ids guard (should be impossible via constraints)
        dup_tx = s.execute(
            select(Transaction.source_hash, func.count()).group_by(Transaction.source_hash).having(func.count() > 1)
        ).all()
        add("Transaction dedup", "OK" if not dup_tx else "FAIL", f"{len(dup_tx)} duplicate hash groups" if dup_tx else "")

        # brief checkpoints sane
        from src.db.briefing import BriefRun

        runs = s.scalars(select(BriefRun).where(BriefRun.status == "completed")).all()
        bad = [r.id for r in runs if r.period_start >= r.period_end]
        add("Brief checkpoints", "OK" if not bad else "FAIL", f"invalid windows: {bad}" if bad else f"{len(runs)} completed run(s)")

    # AI endpoint (optional)
    if settings.ai_enabled:
        try:
            from src.intelligence.ai.provider import get_ai_provider

            health = get_ai_provider(settings).health()
            add("AI endpoint (optional)", "OK" if health["available"] else "WARN",
                health.get("error", "")[:100] if not health["available"] else f"{health['model']} @ {settings.ai_base_url}")
        except Exception as exc:
            add("AI endpoint (optional)", "WARN", str(exc)[:100])
    else:
        add("AI endpoint (optional)", "OK", "disabled by config - deterministic mode")

    from src.operations.backups import latest_backup

    lb = latest_backup(settings)
    add("Backups", "OK" if lb else "WARN", lb.name if lb else "no backup yet - run `run daily` or `backup`")

    add("Provider config", "OK" if "@" not in settings.sec_user_agent or "example.com" not in settings.sec_user_agent else "WARN",
        "set SEC_USER_AGENT with your contact" if "example.com" in settings.sec_user_agent else "")
    return checks


def overall_status(checks: list[Check]) -> str:
    if any(c.status == "FAIL" for c in checks):
        return "FAIL"
    if any(c.status == "WARN" for c in checks):
        return "WARN"
    return "OK"
