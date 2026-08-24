"""Shared deterministic helpers: Decimal conversion, hashing, normalized records."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

ZERO = Decimal("0")
QUANT = Decimal("0.00000001")  # 8 decimal places for all internal arithmetic


def D(value: Any) -> Decimal:
    """Convert any numeric-ish value to Decimal deterministically (via str for floats)."""
    if value is None or value == "":
        return ZERO
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(repr(value))
    try:
        return Decimal(str(value).replace(",", "").strip())
    except InvalidOperation as exc:  # pragma: no cover
        raise ValueError(f"Cannot convert {value!r} to Decimal") from exc


def q(value: Decimal | None, places: Decimal = QUANT) -> Decimal | None:
    if value is None:
        return None
    return value.quantize(places, rounding=ROUND_HALF_UP)


def f(value: Decimal | None) -> float | None:
    """Decimal -> float for DB storage (None-safe)."""
    return None if value is None else float(value)


def stable_hash(*parts: Any) -> str:
    """SHA-256 of a canonical JSON representation of the parts."""
    canonical = json.dumps([_canon(p) for p in parts], sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _canon(v: Any) -> Any:
    if isinstance(v, Decimal):
        return format(v.normalize(), "f")
    if isinstance(v, float):
        return format(Decimal(repr(v)).normalize(), "f")
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    if isinstance(v, dict):
        return {k: _canon(x) for k, x in sorted(v.items())}
    if isinstance(v, (list, tuple)):
        return [_canon(x) for x in v]
    return v


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- Normalized records produced by connectors, consumed by the importer -----


@dataclass
class InstrumentRef:
    symbol: str
    currency: str
    asset_type: str = "stock"
    exchange: str | None = None
    name: str | None = None
    isin: str | None = None
    cusip: str | None = None
    figi: str | None = None
    provider_ids: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class NormalizedTransaction:
    external_id: str | None
    transaction_type: str  # buy | sell | ...
    trade_date: date
    quantity: Decimal  # signed
    price: Decimal | None
    currency: str
    gross_amount: Decimal | None  # signed cash impact before costs
    commission: Decimal  # signed (negative = cost)
    fees: Decimal  # signed
    net_amount: Decimal | None  # signed cash impact
    instrument: InstrumentRef | None
    trade_datetime: datetime | None = None
    settlement_date: date | None = None
    fx_rate: Decimal | None = None
    notes: str | None = None

    def identity_hash(self, account_key: str, source: str) -> str:
        """Hash of the fields that identify this economic event; used as idempotency fallback."""
        return stable_hash(
            source,
            account_key,
            self.external_id,
            self.transaction_type,
            self.trade_date,
            self.instrument.symbol if self.instrument else None,
            self.instrument.exchange if self.instrument else None,
            self.instrument.currency if self.instrument else None,
            self.quantity,
            self.price,
            self.currency,
            self.net_amount,
        )


@dataclass
class NormalizedCashFlow:
    external_id: str | None
    flow_type: str
    flow_date: date
    amount: Decimal  # signed
    currency: str
    is_external: bool
    description: str | None = None
    instrument: InstrumentRef | None = None

    def identity_hash(self, account_key: str, source: str) -> str:
        return stable_hash(
            source,
            account_key,
            self.external_id,
            self.flow_type,
            self.flow_date,
            self.amount,
            self.currency,
            self.description,
            self.instrument.symbol if self.instrument else None,
        )


@dataclass
class ParsedStatement:
    account_external_id: str
    account_base_currency: str
    account_name: str | None
    period_from: date | None
    period_to: date | None
    transactions: list[NormalizedTransaction]
    cash_flows: list[NormalizedCashFlow]
    warnings: list[str] = field(default_factory=list)


def utcnow() -> datetime:
    """Naive UTC timestamp (SQLite-friendly); replaces deprecated datetime.utcnow()."""
    from datetime import timezone

    return datetime.now(timezone.utc).replace(tzinfo=None)
