"""Discovery engine: research candidates from modular deterministic factors.

Finds companies worth RESEARCHING - never recommends purchases. v3 ships two data-backed
factors (13F activity of tracked managers, insider open-market buying) plus manual entry;
fundamental screens need broad fundamentals data we do not ingest yet (documented limitation)
and plug in later via the same DiscoveryFactor interface.
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.core import utcnow
from src.db.intelligence import (
    InsiderTransaction,
    InstitutionalHolding,
    InstitutionalManager,
    ResearchCandidate,
)
from src.logging_setup import get_logger
from src.research.investments import ResearchError, create_investment, get_by_ticker

log = get_logger("intelligence.discovery")


class DiscoveryFactor(ABC):
    name: str = "abstract"

    @abstractmethod
    def find(self, session: Session) -> list[dict]:
        """Return candidate dicts: {ticker, name, reasons: [str]}."""
        raise NotImplementedError  # pragma: no cover


class ThirteenFNewPositionsFactor(DiscoveryFactor):
    """New/increased positions of tracked managers in their latest 13F."""

    name = "13f"

    def find(self, session: Session) -> list[dict]:
        from src.intelligence.connectors.institutional import holding_changes

        out: dict[str, dict] = {}
        for mgr in session.scalars(select(InstitutionalManager).where(InstitutionalManager.active.is_(True))):
            for ch in holding_changes(session, mgr):
                if ch["change"] not in ("NEW_POSITION", "INCREASED") or not ch["ticker"]:
                    continue
                c = out.setdefault(ch["ticker"], {"ticker": ch["ticker"], "name": ch["issuer_name"], "reasons": []})
                c["reasons"].append(
                    f"{mgr.name}: {ch['change']} in 13F for {ch['period']} "
                    f"(13F is delayed and incomplete - context, not endorsement)"
                )
        return list(out.values())


class InsiderBuyingFactor(DiscoveryFactor):
    """Clusters of open-market insider purchases in the last 90 days."""

    name = "insider"

    def find(self, session: Session) -> list[dict]:
        since = date.today() - timedelta(days=90)
        buys: dict[str, dict] = {}
        for t in session.scalars(
            select(InsiderTransaction).where(
                InsiderTransaction.transaction_type == "open_market_purchase",
                InsiderTransaction.transaction_date >= since,
            )
        ):
            key = t.issuer_name or t.issuer_cik
            c = buys.setdefault(key, {"ticker": None, "name": t.issuer_name, "cik": t.issuer_cik,
                                      "insiders": set(), "value": 0.0})
            c["insiders"].add(t.insider_name)
            c["value"] += t.value or 0
        out = []
        for c in buys.values():
            if len(c["insiders"]) >= 2:  # cluster = at least two distinct insiders buying
                from src.intelligence.entities import resolve_instrument_loose

                inst = resolve_instrument_loose(session, cik=c["cik"])
                out.append(
                    {
                        "ticker": inst.symbol if inst else (c["name"] or c["cik"]),
                        "name": c["name"],
                        "reasons": [
                            f"{len(c['insiders'])} insiders bought ~${c['value']:,.0f} on the open market in 90d"
                        ],
                    }
                )
        return out


DEFAULT_FACTORS: list[DiscoveryFactor] = [ThirteenFNewPositionsFactor(), InsiderBuyingFactor()]


def run_discovery(session: Session, factors: list[DiscoveryFactor] | None = None) -> dict:
    factors = factors if factors is not None else DEFAULT_FACTORS
    created = updated = 0
    for factor in factors:
        try:
            candidates = factor.find(session)
        except Exception as exc:  # one factor failure never breaks the run
            log.warning("discovery factor %s failed: %s", factor.name, exc)
            continue
        for c in candidates:
            ticker = (c.get("ticker") or "").upper()[:20]
            if not ticker:
                continue
            if get_by_ticker(session, ticker) is not None:
                continue  # already in the research pipeline
            existing = session.scalars(
                select(ResearchCandidate).where(
                    ResearchCandidate.ticker == ticker, ResearchCandidate.source == factor.name
                )
            ).first()
            if existing:
                reasons = set(json.loads(existing.reasons_json or "[]"))
                new_reasons = reasons | set(c["reasons"])
                if new_reasons != reasons and existing.status == "NEW":
                    existing.reasons_json = json.dumps(sorted(new_reasons))
                    updated += 1
                continue
            session.add(
                ResearchCandidate(
                    ticker=ticker, name=c.get("name"), source=factor.name,
                    reasons_json=json.dumps(c["reasons"]),
                )
            )
            created += 1
    session.flush()
    return {"created": created, "updated": updated}


def add_manual_candidate(session: Session, ticker: str, name: str | None, reasons: list[str]) -> ResearchCandidate:
    ticker = ticker.strip().upper()
    existing = session.scalars(
        select(ResearchCandidate).where(ResearchCandidate.ticker == ticker, ResearchCandidate.source == "manual")
    ).first()
    if existing:
        raise ResearchError(f"manual candidate {ticker} already exists")
    c = ResearchCandidate(ticker=ticker, name=name, source="manual", reasons_json=json.dumps(reasons))
    session.add(c)
    session.flush()
    return c


def promote_candidate(session: Session, candidate: ResearchCandidate) -> "object":
    """PROMOTE TO RESEARCH: creates the investment through the existing v2 service."""
    if candidate.status != "NEW":
        raise ResearchError(f"candidate {candidate.ticker} is {candidate.status}")
    inv = create_investment(
        session, candidate.ticker, name=candidate.name, status="DISCOVERED",
        notes="Discovered by: " + "; ".join(json.loads(candidate.reasons_json or "[]")),
        created_by="SYSTEM",
    )
    candidate.status = "PROMOTED"
    candidate.promoted_investment_id = inv.id
    session.flush()
    return inv


def dismiss_candidate(session: Session, candidate: ResearchCandidate, note: str | None = None) -> None:
    candidate.status = "DISMISSED"
    candidate.notes = note
    session.flush()

CATEGORY_BY_SOURCE = {"13f": "INSTITUTIONAL", "insider": "INSIDER", "manual": "SPECIAL_SITUATION",
                      "screen": "QUALITY/GROWTH/VALUATION"}


def candidate_detail(session: Session, candidate: ResearchCandidate) -> dict:
    """WHY FOUND / DATA QUALITY / KEY NUMBERS / KNOWN / UNKNOWN / NEXT STEP - no opaque score."""
    reasons = json.loads(candidate.reasons_json or "[]")
    from src.db.intelligence import FinancialFact
    from src.intelligence.entities import resolve_instrument_loose, _provider_ids

    inst = resolve_instrument_loose(session, ticker=candidate.ticker)
    cik = _provider_ids(inst).get("sec_cik") if inst else None
    key_numbers = []
    if cik:
        from src.intelligence.connectors.xbrl import latest_metrics

        for f in latest_metrics(session, cik)[:6]:
            key_numbers.append(f"{f.metric}: {f.value:,.0f} {f.unit} [{f.period_end}]")
    return {
        "ticker": candidate.ticker,
        "category": CATEGORY_BY_SOURCE.get(candidate.source, candidate.source.upper()),
        "why_found": reasons,
        "data_quality": "primary (SEC-derived)" if key_numbers else "identifier only - no structured data yet",
        "key_numbers": key_numbers or ["none stored"],
        "what_we_know": reasons + key_numbers,
        "what_we_dont_know": ["thesis", "valuation", "management quality", "unit economics"],
        "next_research_step": ("PROMOTE TO RESEARCH and draft a thesis skeleton"
                               if key_numbers else "run `company onboard " + candidate.ticker + "` first"),
    }
