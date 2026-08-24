"""13F tracking of selected institutional managers. Delayed, long-only, incomplete - stored
and displayed with that context, never interpreted as endorsement."""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.config import Settings
from src.db.intelligence import InstitutionalHolding, InstitutionalManager
from src.intelligence.connectors.sec import (
    SUBMISSIONS_URL,
    SecClient,
    SecError,
    filing_url,
    normalize_cik,
    parse_submissions,
)
from src.intelligence.entities import resolve_instrument_loose
from src.intelligence.events import record_event
from src.intelligence.provenance import register_source
from src.logging_setup import get_logger

log = get_logger("intelligence.institutional")

DISCLAIMER = (
    "13F data is delayed (filed up to 45 days after quarter end), excludes shorts and many "
    "instruments, and shows an incomplete portfolio. Context, not endorsement."
)


def add_manager(session: Session, name: str, cik: str, notes: str | None = None) -> InstitutionalManager:
    cik = normalize_cik(cik)
    existing = session.scalars(select(InstitutionalManager).where(InstitutionalManager.cik == cik)).first()
    if existing:
        return existing
    m = InstitutionalManager(name=name, cik=cik, notes=notes)
    session.add(m)
    session.flush()
    return m


def parse_13f_infotable(xml_text: str) -> list[dict]:
    """Parse a 13F information table XML into holding dicts (namespace-agnostic)."""
    root = ET.fromstring(xml_text)

    def local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1].lower()

    holdings: list[dict] = []
    for node in root.iter():
        if local(node.tag) != "infotable":
            continue
        row: dict = {}
        for child in node.iter():
            t = local(child.tag)
            text = (child.text or "").strip()
            if t == "nameofissuer":
                row["issuer_name"] = text
            elif t == "cusip":
                row["cusip"] = text.upper()
            elif t == "value":
                try:
                    row["value_usd"] = float(text)
                except ValueError:
                    pass
            elif t == "sshprnamt":
                try:
                    row["shares"] = float(text)
                except ValueError:
                    pass
        if row.get("cusip"):
            holdings.append(row)
    return holdings


def store_holdings(
    session: Session, manager: InstitutionalManager, period: date, holdings: list[dict],
    source_document_id: int | None, value_unit_hint: str = "auto",
) -> dict:
    existing = {
        h.cusip
        for h in session.scalars(
            select(InstitutionalHolding).where(
                InstitutionalHolding.manager_id == manager.id, InstitutionalHolding.period == period
            )
        )
    }
    inserted = 0
    for h in holdings:
        if h["cusip"] in existing:
            continue
        inst = resolve_instrument_loose(session, cusip=h["cusip"])
        session.add(
            InstitutionalHolding(
                manager_id=manager.id, period=period, cusip=h["cusip"],
                issuer_name=h.get("issuer_name"), instrument_id=inst.id if inst else None,
                ticker=inst.symbol if inst else None,
                shares=h.get("shares"), value_usd=h.get("value_usd"),
                source_document_id=source_document_id,
            )
        )
        existing.add(h["cusip"])
        inserted += 1
    session.flush()
    return {"inserted": inserted, "period": period.isoformat()}


def holding_changes(session: Session, manager: InstitutionalManager) -> list[dict]:
    """Deterministic change detection between the two most recent report periods."""
    periods = sorted(
        {
            p
            for (p,) in session.execute(
                select(InstitutionalHolding.period).where(InstitutionalHolding.manager_id == manager.id).distinct()
            )
        }
    )
    if not periods:
        return []
    latest = periods[-1]
    previous = periods[-2] if len(periods) > 1 else None

    def load(period: date) -> dict[str, InstitutionalHolding]:
        return {
            h.cusip: h
            for h in session.scalars(
                select(InstitutionalHolding).where(
                    InstitutionalHolding.manager_id == manager.id, InstitutionalHolding.period == period
                )
            )
        }

    cur = load(latest)
    prev = load(previous) if previous else {}
    changes: list[dict] = []
    for cusip, h in cur.items():
        p = prev.get(cusip)
        if p is None:
            change = "NEW_POSITION" if previous else "UNCHANGED"
            delta = None
        elif h.shares is None or p.shares is None:
            change, delta = "UNCHANGED", None
        elif h.shares > p.shares:
            change, delta = "INCREASED", h.shares - p.shares
        elif h.shares < p.shares:
            change, delta = "DECREASED", h.shares - p.shares
        else:
            change, delta = "UNCHANGED", 0.0
        changes.append(
            {
                "cusip": cusip, "issuer_name": h.issuer_name, "ticker": h.ticker,
                "instrument_id": h.instrument_id, "change": change, "shares": h.shares,
                "shares_delta": delta, "value_usd": h.value_usd, "period": latest.isoformat(),
                "previous_period": previous.isoformat() if previous else None,
            }
        )
    for cusip, p in prev.items():
        if cusip not in cur:
            changes.append(
                {
                    "cusip": cusip, "issuer_name": p.issuer_name, "ticker": p.ticker,
                    "instrument_id": p.instrument_id, "change": "EXITED", "shares": 0.0,
                    "shares_delta": -(p.shares or 0), "value_usd": 0.0,
                    "period": latest.isoformat(), "previous_period": previous.isoformat(),
                }
            )
    return changes


def sync_manager(
    session: Session, settings: Settings, manager: InstitutionalManager,
    client: SecClient | None = None, limit: int = 4,
) -> dict:
    """Download the manager's recent 13F-HR filings and store holdings. Idempotent."""
    client = client or SecClient(settings)
    sub_raw = client.get(SUBMISSIONS_URL.format(cik10=manager.cik.zfill(10)))
    _info, filings = parse_submissions(json.loads(sub_raw.decode("utf-8")))
    reports = [f for f in filings if f.form in ("13F-HR", "13F-HR/A")][:limit]
    summary = {"manager": manager.name, "reports_seen": len(reports), "holdings_inserted": 0, "errors": []}
    for f in reports:
        period = f.report_date
        if period is None:
            continue
        # the information table is a separate XML; primaryDocument sometimes IS the infotable
        candidates = [f.primary_document] if f.primary_document else []
        candidates += ["infotable.xml", "form13fInfoTable.xml"]
        raw = None
        used = None
        for candidate in candidates:
            if not candidate or not candidate.lower().endswith(".xml"):
                continue
            try:
                raw = client.get(filing_url(manager.cik, f.accession, candidate))
                used = candidate
                break
            except SecError:
                continue
        if raw is None:
            summary["errors"].append(f"{f.accession}: no information table XML found")
            continue
        doc, created = register_source(
            session, settings, provider="sec_edgar", source_type="13f", external_id=f.accession,
            raw=raw, category="institutional", url=filing_url(manager.cik, f.accession, used),
            title=f"13F-HR {manager.name} {period}", entity_key=manager.cik, source_tier=1,
            period_end=period,
        )
        if not created:
            continue
        try:
            holdings = parse_13f_infotable(raw.decode("utf-8", errors="replace"))
        except ET.ParseError as exc:
            doc.status, doc.error = "error", str(exc)
            summary["errors"].append(f"{f.accession}: parse error")
            continue
        res = store_holdings(session, manager, period, holdings, doc.id)
        doc.status = "parsed"
        summary["holdings_inserted"] += res["inserted"]
    # events for changes touching our instruments
    for ch in holding_changes(session, manager):
        if ch["instrument_id"] is not None and ch["change"] in ("NEW_POSITION", "EXITED", "INCREASED", "DECREASED"):
            from src.intelligence.entities import resolve_investment

            inv = resolve_investment(session, instrument_id=ch["instrument_id"])
            record_event(
                session, "INSTITUTIONAL_CHANGE",
                dedup_key=f"13f:{manager.cik}:{ch['period']}:{ch['cusip']}:{ch['change']}",
                title=f"{manager.name}: {ch['change']} {ch['ticker'] or ch['issuer_name']} ({ch['period']})",
                occurred_at=datetime.combine(date.fromisoformat(ch["period"]), datetime.min.time()),
                investment_id=inv.id if inv else None,
                instrument_id=ch["instrument_id"],
                summary=DISCLAIMER,
                payload=ch,
            )
    log.info("13f sync %s: %s", manager.name, summary)
    return summary
