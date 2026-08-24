"""Congressional trading disclosures (House/Senate PTRs). Informational context ONLY.

LIMITATION (documented, deliberate): there is no stable, free, official machine-readable API
for PTRs; the official portals serve per-filing PDFs/HTML behind search forms. v3 therefore
ships a provider INTERFACE plus a CSV importer for exported datasets (e.g. manually
downloaded portal exports or third-party dumps you trust). A live scraper can be added later
behind the same interface without schema changes.

Data caveats preserved in the model and never glossed over:
  * transaction amounts are RANGES (amount_low..amount_high), not exact values;
  * disclosure_date lags transaction_date (up to 45 days, sometimes more);
  * the owner may be spouse/dependent/joint;
  * ticker resolution may fail -> ticker/instrument stay NULL, never guessed.

Expected CSV columns (case-insensitive): person, chamber, owner, transaction_date,
disclosure_date, asset, ticker, type, amount (e.g. "$1,001 - $15,000" or "1001-15000"),
source_reference.
"""
from __future__ import annotations

import csv
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.config import Settings
from src.core import stable_hash
from src.db.intelligence import CongressTransaction, WatchlistEntry
from src.intelligence.entities import resolve_instrument_loose, resolve_investment
from src.intelligence.events import record_event
from src.intelligence.provenance import register_source
from src.logging_setup import get_logger
from src.research.investments import ResearchError

log = get_logger("intelligence.congress")


@dataclass
class CongressRecord:
    person: str
    chamber: str | None
    owner: str | None
    transaction_date: date | None
    disclosure_date: date | None
    asset_description: str
    ticker: str | None
    transaction_type: str | None
    amount_low: float | None
    amount_high: float | None
    source_reference: str | None


class CongressProvider(ABC):
    """Future live providers implement this; v3 ships the CSV file provider."""

    name: str = "abstract"

    @abstractmethod
    def load(self) -> list[CongressRecord]:  # pragma: no cover - interface
        raise NotImplementedError


def parse_amount_range(raw: str | None) -> tuple[float | None, float | None]:
    if not raw:
        return None, None
    cleaned = raw.replace("$", "").replace(",", "").replace("+", "")
    m = re.findall(r"\d+(?:\.\d+)?", cleaned)
    if not m:
        return None, None
    if len(m) == 1:
        v = float(m[0])
        return v, v
    return float(m[0]), float(m[1])


class CsvCongressProvider(CongressProvider):
    name = "congress_csv"

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> list[CongressRecord]:
        with open(self.path, "r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            if reader.fieldnames is None:
                raise ResearchError("empty congress CSV")
            reader.fieldnames = [c.strip().lower() for c in reader.fieldnames]
            required = {"person", "asset"}
            missing = required - set(reader.fieldnames)
            if missing:
                raise ResearchError(f"congress CSV missing columns: {sorted(missing)}")
            out = []
            for row in reader:
                r = {k: (v or "").strip() for k, v in row.items() if k}
                if not r.get("person"):
                    continue
                low, high = parse_amount_range(r.get("amount"))
                out.append(
                    CongressRecord(
                        person=r["person"],
                        chamber=(r.get("chamber") or "").lower() or None,
                        owner=(r.get("owner") or "").lower() or None,
                        transaction_date=_d(r.get("transaction_date")),
                        disclosure_date=_d(r.get("disclosure_date")),
                        asset_description=r.get("asset", ""),
                        ticker=(r.get("ticker") or "").upper() or None,
                        transaction_type=(r.get("type") or "").lower() or None,
                        amount_low=low,
                        amount_high=high,
                        source_reference=r.get("source_reference") or None,
                    )
                )
            return out


def _d(v: str | None) -> date | None:
    if not v:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    return None


def import_congress(
    session: Session, settings: Settings, provider: CongressProvider, source_file: str | None = None
) -> dict:
    records = provider.load()
    raw_bytes = Path(source_file).read_bytes() if source_file else None
    doc = None
    if raw_bytes is not None:
        doc, _ = register_source(
            session, settings, provider=provider.name, source_type="congress_ptr",
            external_id=f"file:{Path(source_file).name}:{stable_hash(source_file, len(raw_bytes))[:12]}",
            raw=raw_bytes, category="congress", title=f"Congress PTR import {Path(source_file).name}",
            source_tier=1 if "official" in provider.name else 2,
        )
    watched = {
        w.key.lower()
        for w in session.scalars(
            select(WatchlistEntry).where(WatchlistEntry.kind == "congress_member", WatchlistEntry.active.is_(True))
        )
    }
    inserted = duplicates = 0
    for r in records:
        dedup = stable_hash(
            "congress", r.person, r.transaction_date, r.disclosure_date, r.asset_description,
            r.transaction_type, r.amount_low, r.amount_high,
        )[:64]
        if session.scalars(select(CongressTransaction).where(CongressTransaction.dedup_key == dedup)).first():
            duplicates += 1
            continue
        inst = resolve_instrument_loose(session, ticker=r.ticker) if r.ticker else None
        inv = resolve_investment(session, ticker=r.ticker) if r.ticker else None
        session.add(
            CongressTransaction(
                person=r.person, chamber=r.chamber, owner=r.owner,
                transaction_date=r.transaction_date, disclosure_date=r.disclosure_date,
                asset_description=r.asset_description, ticker=r.ticker,
                instrument_id=inst.id if inst else None, investment_id=inv.id if inv else None,
                transaction_type=r.transaction_type, amount_low=r.amount_low, amount_high=r.amount_high,
                source=provider.name, source_reference=r.source_reference,
                source_document_id=doc.id if doc else None, dedup_key=dedup,
            )
        )
        inserted += 1
        if inv is not None or r.person.lower() in watched:
            record_event(
                session, "CONGRESS_TRANSACTION",
                dedup_key=f"congress:{dedup}",
                title=f"{r.person}: {r.transaction_type or 'transaction'} {r.ticker or r.asset_description[:40]}"
                      f" (${r.amount_low:,.0f}-${r.amount_high:,.0f})" if r.amount_low is not None
                      else f"{r.person}: {r.transaction_type or 'transaction'} {r.ticker or r.asset_description[:40]}",
                occurred_at=datetime.combine(r.disclosure_date or r.transaction_date or date.today(), datetime.min.time()),
                investment_id=inv.id if inv else None,
                summary="Disclosure lags the transaction; amount is a range; may belong to spouse/dependent.",
                source_document_id=doc.id if doc else None,
                payload={"person": r.person, "owner": r.owner, "type": r.transaction_type,
                         "amount_low": r.amount_low, "amount_high": r.amount_high,
                         "transaction_date": r.transaction_date, "disclosure_date": r.disclosure_date},
            )
    session.flush()
    result = {"records": len(records), "inserted": inserted, "duplicates": duplicates}
    log.info("congress import: %s", result)
    return result


def matches_portfolio(session: Session) -> list[CongressTransaction]:
    """Tracked congressional disclosures involving companies we own or research."""
    return list(
        session.scalars(
            select(CongressTransaction)
            .where(CongressTransaction.investment_id.isnot(None))
            .order_by(CongressTransaction.disclosure_date.desc())
        )
    )
