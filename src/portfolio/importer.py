"""Idempotent import of normalized records into the database.

Idempotency strategy (two layers):
  1. (account_id, source, external_transaction_id) unique when the source provides an id.
  2. source_hash = SHA-256 over identifying economic fields - catches duplicates even when
     the provider omits ids, and guards against re-importing the same file twice.
Existing rows are never modified by an import.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.core import ParsedStatement, f, utcnow
from src.db.models import Account, CashFlow, ImportRun, Transaction
from src.logging_setup import get_logger
from src.portfolio.instruments import resolve_instrument

log = get_logger("importer")


@dataclass
class ImportResult:
    account_id: int
    transactions_seen: int = 0
    transactions_inserted: int = 0
    transactions_duplicate: int = 0
    cash_flows_seen: int = 0
    cash_flows_inserted: int = 0
    cash_flows_duplicate: int = 0
    skipped: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def seen(self) -> int:
        return self.transactions_seen + self.cash_flows_seen

    @property
    def inserted(self) -> int:
        return self.transactions_inserted + self.cash_flows_inserted

    @property
    def duplicates(self) -> int:
        return self.transactions_duplicate + self.cash_flows_duplicate

    def to_dict(self) -> dict:
        return {
            "account_id": self.account_id,
            "transactions": {
                "seen": self.transactions_seen,
                "inserted": self.transactions_inserted,
                "duplicate": self.transactions_duplicate,
            },
            "cash_flows": {
                "seen": self.cash_flows_seen,
                "inserted": self.cash_flows_inserted,
                "duplicate": self.cash_flows_duplicate,
            },
            "skipped": self.skipped,
            "warnings": self.warnings,
        }


def get_or_create_account(
    session: Session, provider: str, external_id: str, base_currency: str, name: str | None = None
) -> Account:
    acc = session.scalars(
        select(Account).where(Account.provider == provider, Account.account_external_id == external_id)
    ).first()
    if acc is None:
        acc = Account(
            name=name or f"{provider.upper()} {external_id}",
            provider=provider,
            account_external_id=external_id,
            base_currency=base_currency.upper(),
        )
        session.add(acc)
        session.flush()
        log.info("Created account id=%s provider=%s external=%s", acc.id, provider, external_id)
    return acc


def import_statement(
    session: Session, statement: ParsedStatement, source: str, source_file: str | None = None
) -> ImportResult:
    account = get_or_create_account(
        session,
        provider=source,
        external_id=statement.account_external_id,
        base_currency=statement.account_base_currency,
        name=statement.account_name,
    )
    result = ImportResult(account_id=account.id, warnings=list(statement.warnings))
    account_key = f"{source}:{statement.account_external_id}"
    now = utcnow()

    existing_tx_ext = set(
        session.execute(
            select(Transaction.external_transaction_id).where(
                Transaction.account_id == account.id, Transaction.source == source
            )
        ).scalars()
    )
    existing_tx_hash = set(session.execute(select(Transaction.source_hash)).scalars())
    existing_cf_ext = set(
        session.execute(
            select(CashFlow.external_id).where(CashFlow.account_id == account.id, CashFlow.source == source)
        ).scalars()
    )
    existing_cf_hash = set(session.execute(select(CashFlow.source_hash)).scalars())

    for ntx in statement.transactions:
        result.transactions_seen += 1
        h = ntx.identity_hash(account_key, source)
        if (ntx.external_id and ntx.external_id in existing_tx_ext) or h in existing_tx_hash:
            result.transactions_duplicate += 1
            continue
        instrument = resolve_instrument(session, ntx.instrument) if ntx.instrument else None
        session.add(
            Transaction(
                account_id=account.id,
                instrument_id=instrument.id if instrument else None,
                external_transaction_id=ntx.external_id,
                transaction_type=ntx.transaction_type,
                trade_date=ntx.trade_date,
                trade_datetime=ntx.trade_datetime,
                settlement_date=ntx.settlement_date,
                quantity=float(ntx.quantity),
                price=f(ntx.price),
                currency=ntx.currency,
                gross_amount=f(ntx.gross_amount),
                commission=float(ntx.commission),
                fees=float(ntx.fees),
                net_amount=f(ntx.net_amount),
                fx_rate=f(ntx.fx_rate),
                source=source,
                source_hash=h,
                source_file=source_file,
                notes=ntx.notes,
                imported_at=now,
            )
        )
        existing_tx_hash.add(h)
        if ntx.external_id:
            existing_tx_ext.add(ntx.external_id)
        result.transactions_inserted += 1

    for ncf in statement.cash_flows:
        result.cash_flows_seen += 1
        h = ncf.identity_hash(account_key, source)
        if (ncf.external_id and ncf.external_id in existing_cf_ext) or h in existing_cf_hash:
            result.cash_flows_duplicate += 1
            continue
        instrument = resolve_instrument(session, ncf.instrument) if ncf.instrument else None
        session.add(
            CashFlow(
                account_id=account.id,
                instrument_id=instrument.id if instrument else None,
                external_id=ncf.external_id,
                flow_type=ncf.flow_type,
                flow_date=ncf.flow_date,
                amount=float(ncf.amount),
                currency=ncf.currency,
                is_external=ncf.is_external,
                description=ncf.description,
                source=source,
                source_hash=h,
                source_file=source_file,
                imported_at=now,
            )
        )
        existing_cf_hash.add(h)
        if ncf.external_id:
            existing_cf_ext.add(ncf.external_id)
        result.cash_flows_inserted += 1

    session.flush()
    log.info(
        "Import %s account=%s: tx seen=%d inserted=%d dup=%d | cash seen=%d inserted=%d dup=%d",
        source,
        account.id,
        result.transactions_seen,
        result.transactions_inserted,
        result.transactions_duplicate,
        result.cash_flows_seen,
        result.cash_flows_inserted,
        result.cash_flows_duplicate,
    )
    for w in result.warnings:
        log.warning("Import warning: %s", w)
    return result


def start_import_run(session: Session, job: str, source: str) -> ImportRun:
    run = ImportRun(job=job, source=source, started_at=utcnow(), status="running")
    session.add(run)
    session.flush()
    return run


def finish_import_run(
    session: Session,
    run: ImportRun,
    result: ImportResult | None = None,
    error: str | None = None,
    raw_path: str | None = None,
    raw_sha256: str | None = None,
    details: dict | None = None,
) -> None:
    run.finished_at = utcnow()
    run.status = "error" if error else "success"
    run.error = error
    run.raw_path = raw_path
    run.raw_sha256 = raw_sha256
    if result is not None:
        run.records_seen = result.seen
        run.records_inserted = result.inserted
        run.records_duplicate = result.duplicates
        run.records_skipped = result.skipped
        run.details = json.dumps(result.to_dict() | (details or {}))
    elif details is not None:
        run.details = json.dumps(details)
    session.flush()
