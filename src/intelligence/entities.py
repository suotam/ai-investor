"""Entity resolution: map external identifiers (CIK, ticker, CUSIP, name) to internal
investments/instruments. Ticker alone is never trusted globally; ambiguity fails safely."""
from __future__ import annotations

import json
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import Instrument
from src.db.research import Investment
from src.research.investments import ResearchError


class AmbiguousEntityError(ResearchError):
    pass


def _provider_ids(inst: Instrument) -> dict:
    try:
        return json.loads(inst.provider_ids) if inst.provider_ids else {}
    except json.JSONDecodeError:
        return {}


def normalize_cik(cik: str | int) -> str:
    """Canonical CIK: digits without leading zeros (stored form), e.g. '1691493'."""
    return str(int(re.sub(r"\D", "", str(cik))))


def remember_cik(session: Session, instrument: Instrument, cik: str) -> None:
    ids = _provider_ids(instrument)
    if ids.get("sec_cik") != normalize_cik(cik):
        ids["sec_cik"] = normalize_cik(cik)
        instrument.provider_ids = json.dumps(ids)
        session.flush()


def instrument_by_cik(session: Session, cik: str) -> Instrument | None:
    want = normalize_cik(cik)
    for inst in session.scalars(select(Instrument).where(Instrument.provider_ids.isnot(None))):
        if _provider_ids(inst).get("sec_cik") == want:
            return inst
    return None


def resolve_investment(
    session: Session,
    ticker: str | None = None,
    cik: str | None = None,
    instrument_id: int | None = None,
    name: str | None = None,
) -> Investment | None:
    """Best-match investment using id > CIK > ticker > exact name. Ambiguity raises."""
    if instrument_id is not None:
        inv = session.scalars(select(Investment).where(Investment.instrument_id == instrument_id)).first()
        if inv:
            return inv
    if cik:
        inst = instrument_by_cik(session, cik)
        if inst is not None:
            inv = session.scalars(select(Investment).where(Investment.instrument_id == inst.id)).first()
            if inv:
                return inv
    if ticker:
        t = ticker.strip().upper()
        matches = list(session.scalars(select(Investment).where(Investment.ticker == t)))
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:  # pragma: no cover - ticker is unique in schema
            raise AmbiguousEntityError(f"multiple investments for ticker {t}")
    if name:
        n = name.strip().lower()
        matches = [i for i in session.scalars(select(Investment)) if (i.name or "").strip().lower() == n]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise AmbiguousEntityError(f"multiple investments named {name!r}; use ticker or CIK")
    return None


def resolve_instrument_loose(
    session: Session, ticker: str | None = None, cik: str | None = None, cusip: str | None = None
) -> Instrument | None:
    """Instrument lookup for external data linking. Ambiguity -> None + caller decides
    (external rows keep instrument_id NULL rather than guessing)."""
    if cik:
        inst = instrument_by_cik(session, cik)
        if inst is not None:
            return inst
    if cusip:
        inst = session.scalars(select(Instrument).where(Instrument.cusip == cusip)).first()
        if inst is not None:
            return inst
    if ticker:
        t = ticker.strip().upper()
        matches = list(
            session.scalars(
                select(Instrument).where(Instrument.symbol == t, Instrument.asset_type != "cash")
            )
        )
        if len(matches) == 1:
            return matches[0]
    return None
