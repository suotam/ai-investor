"""Insider transactions from SEC Form 4 (ownershipDocument XML). Context, never a signal.

Transaction-code normalization (SEC codes):
    P  open_market_purchase      S  open_market_sale
    M  option_exercise           A  award_grant
    F  tax_withholding           G  gift
    C  conversion                D  disposition_to_issuer
    other codes -> other
An automatic Rule 10b5-1 flag is not always present in the XML; where footnotes indicate it
we keep the raw code and never claim discretionary intent. BUY is never labeled bullish.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.config import Settings
from src.core import stable_hash
from src.db.intelligence import InsiderTransaction
from src.intelligence.connectors.sec import SecClient, SecError, filing_url, parse_submissions, SUBMISSIONS_URL
from src.intelligence.entities import normalize_cik, resolve_investment
from src.intelligence.events import record_event
from src.intelligence.provenance import register_source
from src.logging_setup import get_logger

log = get_logger("intelligence.insiders")

CODE_MAP = {
    "P": "open_market_purchase",
    "S": "open_market_sale",
    "M": "option_exercise",
    "A": "award_grant",
    "F": "tax_withholding",
    "G": "gift",
    "C": "conversion",
    "D": "disposition_to_issuer",
}


@dataclass
class ParsedForm4:
    issuer_cik: str
    issuer_name: str | None
    insider_name: str
    insider_role: str | None
    filing_date: date | None
    transactions: list[dict]


def _text(node, path: str) -> str | None:
    el = node.find(path)
    return el.text.strip() if el is not None and el.text else None


def _val(node, path: str) -> float | None:
    t = _text(node, f"{path}/value")
    try:
        return float(t) if t is not None else None
    except ValueError:
        return None


def parse_form4(xml_text: str) -> ParsedForm4:
    root = ET.fromstring(xml_text)
    issuer = root.find("issuer")
    owner = root.find("reportingOwner")
    roles = []
    if owner is not None:
        rel = owner.find("reportingOwnerRelationship")
        if rel is not None:
            if _text(rel, "isDirector") in ("1", "true"):
                roles.append("Director")
            if _text(rel, "isOfficer") in ("1", "true"):
                roles.append(_text(rel, "officerTitle") or "Officer")
            if _text(rel, "isTenPercentOwner") in ("1", "true"):
                roles.append("10% owner")
    txs = []
    for tx in root.findall(".//nonDerivativeTable/nonDerivativeTransaction"):
        code = _text(tx, "transactionCoding/transactionCode")
        shares = _val(tx, "transactionAmounts/transactionShares")
        price = _val(tx, "transactionAmounts/transactionPricePerShare")
        ad = _text(tx, "transactionAmounts/transactionAcquiredDisposedCode/value") or _text(
            tx, "transactionAmounts/transactionAcquiredDisposedCode"
        )
        txs.append(
            {
                "security": _text(tx, "securityTitle/value"),
                "transaction_date": _d(_text(tx, "transactionDate/value")),
                "transaction_code": code,
                "transaction_type": CODE_MAP.get(code or "", "other"),
                "shares": shares,
                "price": price,
                "value": (shares * price) if (shares is not None and price is not None) else None,
                "shares_after": _val(tx, "postTransactionAmounts/sharesOwnedFollowingTransaction"),
                "direct_ownership": (
                    (_text(tx, "ownershipNature/directOrIndirectOwnership/value") or "").upper() == "D"
                ),
                "acquired_disposed": ad,
            }
        )
    return ParsedForm4(
        issuer_cik=normalize_cik(_text(issuer, "issuerCik") or "0"),
        issuer_name=_text(issuer, "issuerName"),
        insider_name=_text(owner, "reportingOwnerId/rptOwnerName") or "Unknown",
        insider_role=", ".join(roles) or None,
        filing_date=_d(_text(root, "periodOfReport")),
        transactions=txs,
    )


def _d(v: str | None) -> date | None:
    if not v:
        return None
    try:
        return date.fromisoformat(v[:10])
    except ValueError:
        return None


def store_form4(
    session: Session, parsed: ParsedForm4, accession: str, source_document_id: int | None,
    filing_date: date | None = None,
) -> dict:
    inv = resolve_investment(session, cik=parsed.issuer_cik)
    inserted = duplicates = 0
    for i, tx in enumerate(parsed.transactions):
        dedup = stable_hash("form4", accession, i, tx["transaction_code"], tx["shares"], tx["transaction_date"])[:64]
        if session.scalars(
            select(InsiderTransaction).where(InsiderTransaction.dedup_key == dedup)
        ).first():
            duplicates += 1
            continue
        session.add(
            InsiderTransaction(
                issuer_cik=parsed.issuer_cik, issuer_name=parsed.issuer_name,
                investment_id=inv.id if inv else None,
                insider_name=parsed.insider_name, insider_role=parsed.insider_role,
                transaction_date=tx["transaction_date"], filing_date=filing_date or parsed.filing_date,
                security=tx["security"], transaction_code=tx["transaction_code"],
                transaction_type=tx["transaction_type"], shares=tx["shares"], price=tx["price"],
                value=tx["value"], shares_after=tx["shares_after"],
                direct_ownership=tx["direct_ownership"], acquired_disposed=tx["acquired_disposed"],
                source_document_id=source_document_id, dedup_key=dedup,
            )
        )
        inserted += 1
        if tx["transaction_type"] in ("open_market_purchase", "open_market_sale"):
            record_event(
                session, "INSIDER_TRANSACTION",
                dedup_key=f"insider:{dedup}",
                title=f"{parsed.insider_name}: {tx['transaction_type'].replace('_', ' ')} "
                      f"{tx['shares'] or '?'} {parsed.issuer_name or parsed.issuer_cik}",
                occurred_at=datetime.combine(tx["transaction_date"] or date.today(), datetime.min.time()),
                investment_id=inv.id if inv else None,
                source_document_id=source_document_id,
                payload=tx | {"insider": parsed.insider_name, "role": parsed.insider_role},
                transaction_type=tx["transaction_type"],
            )
    session.flush()
    return {"inserted": inserted, "duplicates": duplicates, "insider": parsed.insider_name}


def sync_insiders(
    session: Session, settings: Settings, ticker: str, client: SecClient | None = None, limit: int = 20
) -> dict:
    """Fetch recent Form 4 filings for the issuer and store parsed transactions. Idempotent."""
    from src.intelligence.connectors.sec import resolve_cik
    import json as _json

    client = client or SecClient(settings)
    cik, _title = resolve_cik(client, ticker)
    sub_raw = client.get(SUBMISSIONS_URL.format(cik10=cik.zfill(10)))
    _info, filings = parse_submissions(_json.loads(sub_raw.decode("utf-8")))
    form4s = [f for f in filings if f.form == "4"][:limit]
    summary = {"ticker": ticker.upper(), "cik": cik, "form4_seen": len(form4s), "inserted": 0,
               "duplicates": 0, "errors": []}
    for f in form4s:
        if not f.primary_document:
            continue
        # SEC lists the XSL-rendered document (xslF345X0x/...); the raw XML lives at the
        # same path without the xsl prefix.
        primary = f.primary_document.rsplit("/", 1)[-1]
        url = filing_url(cik, f.accession, primary)
        try:
            raw = client.get(url)
        except SecError as exc:
            summary["errors"].append(f"{f.accession}: {exc}")
            continue
        doc, created = register_source(
            session, settings, provider="sec_edgar", source_type="form4", external_id=f.accession,
            raw=raw, category="insiders", url=url, title=f"Form 4 {f.filing_date}",
            entity_key=cik, source_tier=1,
            published_at=datetime.combine(f.filing_date, datetime.min.time()) if f.filing_date else None,
        )
        if not created and doc.status != "error":
            summary["duplicates"] += 1
            continue
        try:
            parsed = parse_form4(raw.decode("utf-8", errors="replace"))
        except ET.ParseError as exc:
            doc.status, doc.error = "error", f"parse error: {exc}"
            summary["errors"].append(f"{f.accession}: parse error")
            continue
        res = store_form4(session, parsed, f.accession, doc.id, filing_date=f.filing_date)
        doc.status, doc.error = "parsed", None
        summary["inserted"] += res["inserted"]
    log.info("insiders sync %s: %s", ticker, summary)
    return summary


def aggregate_insiders(session: Session, issuer_cik: str, as_of: date | None = None) -> dict:
    """Deterministic 30/90/365-day aggregates of OPEN-MARKET transactions only."""
    as_of = as_of or date.today()
    rows = list(
        session.scalars(
            select(InsiderTransaction).where(InsiderTransaction.issuer_cik == normalize_cik(issuer_cik))
        )
    )
    out = {}
    for days in (30, 90, 365):
        since = as_of - timedelta(days=days)
        window = [
            r for r in rows
            if r.transaction_date and since <= r.transaction_date <= as_of
            and r.transaction_type in ("open_market_purchase", "open_market_sale")
        ]
        buys = [r for r in window if r.transaction_type == "open_market_purchase"]
        sells = [r for r in window if r.transaction_type == "open_market_sale"]
        out[f"{days}d"] = {
            "insiders_buying": len({r.insider_name for r in buys}),
            "insiders_selling": len({r.insider_name for r in sells}),
            "net_shares": sum(r.shares or 0 for r in buys) - sum(r.shares or 0 for r in sells),
            "net_value_usd": sum(r.value or 0 for r in buys) - sum(r.value or 0 for r in sells),
            "transactions": len(window),
        }
    out["note"] = "Open-market transactions only; context, not a trading signal."
    return out
