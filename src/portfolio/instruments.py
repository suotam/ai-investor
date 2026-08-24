"""Instrument resolution: map connector InstrumentRef -> DB Instrument (find-or-create).

Resolution order (deterministic):
  1. provider id (e.g. ibkr_conid) stored in provider_ids JSON
  2. ISIN + currency
  3. identity tuple (symbol, exchange, currency, asset_type)
Ticker alone is never assumed unique.
"""
from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.core import InstrumentRef
from src.db.models import Instrument


def _load_ids(inst: Instrument) -> dict[str, str]:
    if not inst.provider_ids:
        return {}
    try:
        return json.loads(inst.provider_ids)
    except json.JSONDecodeError:
        return {}


def resolve_instrument(session: Session, ref: InstrumentRef, create: bool = True) -> Instrument | None:
    symbol = ref.symbol.strip().upper()
    currency = ref.currency.strip().upper()
    exchange = (ref.exchange or "").strip().upper() or None

    # 1. provider ids
    for key, val in (ref.provider_ids or {}).items():
        if not val:
            continue
        # provider_ids is JSON text; scan candidates by symbol first for speed, then full scan
        candidates = session.scalars(select(Instrument).where(Instrument.symbol == symbol)).all()
        if not candidates:
            candidates = session.scalars(select(Instrument).where(Instrument.provider_ids.isnot(None))).all()
        for inst in candidates:
            if _load_ids(inst).get(key) == str(val):
                _enrich(inst, ref)
                return inst

    # 2. ISIN
    if ref.isin:
        inst = session.scalars(
            select(Instrument).where(Instrument.isin == ref.isin, Instrument.currency == currency)
        ).first()
        if inst:
            _enrich(inst, ref)
            return inst

    # 3. identity tuple
    inst = session.scalars(
        select(Instrument).where(
            Instrument.symbol == symbol,
            Instrument.exchange.is_(exchange) if exchange is None else Instrument.exchange == exchange,
            Instrument.currency == currency,
            Instrument.asset_type == ref.asset_type,
        )
    ).first()
    if inst:
        _enrich(inst, ref)
        return inst

    if not create:
        return None

    inst = Instrument(
        symbol=symbol,
        name=ref.name,
        asset_type=ref.asset_type,
        exchange=exchange,
        currency=currency,
        isin=ref.isin or None,
        cusip=ref.cusip or None,
        figi=ref.figi or None,
        provider_ids=json.dumps({k: str(v) for k, v in (ref.provider_ids or {}).items() if v}) or None,
        price_symbol=default_price_symbol(symbol, ref.asset_type, currency, exchange),
    )
    session.add(inst)
    session.flush()
    return inst


def _enrich(inst: Instrument, ref: InstrumentRef) -> None:
    """Fill missing metadata from the new reference without overwriting existing values."""
    changed = False
    if not inst.name and ref.name:
        inst.name, changed = ref.name, True
    if not inst.isin and ref.isin:
        inst.isin, changed = ref.isin, True
    if not inst.cusip and ref.cusip:
        inst.cusip, changed = ref.cusip, True
    ids = _load_ids(inst)
    for k, v in (ref.provider_ids or {}).items():
        if v and k not in ids:
            ids[k] = str(v)
            changed = True
    if changed:
        inst.provider_ids = json.dumps(ids) if ids else inst.provider_ids


def default_price_symbol(symbol: str, asset_type: str, currency: str, exchange: str | None) -> str:
    """Best-effort mapping of broker symbol to a Yahoo-style price symbol.

    Only well-known exchange suffixes are mapped; anything else keeps the raw symbol and
    may need a manual `price_symbol` override in the instruments table.
    """
    if asset_type == "crypto":
        # Yahoo quotes crypto in USD/EUR; the quote currency is stored with each price row
        # (prices.currency) and converted via FX, so instrument currency may differ (e.g. CZK).
        return f"{symbol}-{currency}" if currency in ("USD", "EUR") else f"{symbol}-USD"
    if asset_type == "cash":
        return symbol
    suffix_map = {
        "LSE": ".L", "LSEETF": ".L", "IBIS": ".DE", "IBIS2": ".DE", "FWB": ".F", "SBF": ".PA",
        "AEB": ".AS", "EBS": ".SW", "SWB": ".SG", "TSE": ".TO", "VSE": ".V", "ASX": ".AX",
        "BVME": ".MI", "BM": ".MC", "WSE": ".WA", "PRA": ".PR", "TSEJ": ".T", "SEHK": ".HK", "VIX": ".VI",
    }
    if exchange and exchange in suffix_map:
        return f"{symbol}{suffix_map[exchange]}"
    return symbol.replace(" ", "-")
