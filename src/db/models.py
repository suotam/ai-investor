"""SQLAlchemy ORM models - the portfolio database is the SOURCE OF TRUTH.

Monetary values are stored as REAL in SQLite; all arithmetic in the engine uses
Decimal (converted via str) to stay deterministic. Schema is managed by Alembic
migrations in src/db/migrations - do NOT use Base.metadata.create_all in prod code.
"""
from __future__ import annotations

from datetime import date, datetime

from src.core import utcnow
from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# --- Enumerations kept as plain strings (migration-friendly) -----------------
ASSET_TYPES = ("stock", "etf", "crypto", "cash", "bond", "option", "index", "other")
TRANSACTION_TYPES = ("buy", "sell", "dividend", "fee", "interest", "tax", "transfer_in", "transfer_out", "other")
CASH_FLOW_TYPES = ("deposit", "withdrawal", "dividend", "interest", "fee", "tax", "transfer", "other")


class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)  # ibkr | crypto_csv | manual
    account_external_id: Mapped[str | None] = mapped_column(String(100))
    base_currency: Mapped[str] = mapped_column(String(10), nullable=False, default="USD")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (UniqueConstraint("provider", "account_external_id", name="uq_account_provider_ext"),)


class Instrument(Base):
    __tablename__ = "instruments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str | None] = mapped_column(String(200))
    asset_type: Mapped[str] = mapped_column(String(20), nullable=False, default="stock")
    exchange: Mapped[str | None] = mapped_column(String(50))
    currency: Mapped[str] = mapped_column(String(10), nullable=False)
    isin: Mapped[str | None] = mapped_column(String(20))
    cusip: Mapped[str | None] = mapped_column(String(20))
    figi: Mapped[str | None] = mapped_column(String(20))
    # JSON: {"ibkr_conid": "...", "yahoo": "AAPL", ...}
    provider_ids: Mapped[str | None] = mapped_column(Text)
    sector: Mapped[str | None] = mapped_column(String(100))
    industry: Mapped[str | None] = mapped_column(String(100))
    # Symbol used for price lookups by the market data provider (may differ from broker symbol)
    price_symbol: Mapped[str | None] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    # Ticker is NOT globally unique; identity is (symbol, exchange, currency, asset_type)
    __table_args__ = (
        UniqueConstraint("symbol", "exchange", "currency", "asset_type", name="uq_instrument_identity"),
        Index("ix_instrument_symbol", "symbol"),
        Index("ix_instrument_isin", "isin"),
    )


class Transaction(Base):
    __tablename__ = "transactions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    instrument_id: Mapped[int | None] = mapped_column(ForeignKey("instruments.id"))
    external_transaction_id: Mapped[str | None] = mapped_column(String(100))
    transaction_type: Mapped[str] = mapped_column(String(20), nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    trade_datetime: Mapped[datetime | None] = mapped_column(DateTime)
    settlement_date: Mapped[date | None] = mapped_column(Date)
    # Signed: positive = buy (long increase), negative = sell
    quantity: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    price: Mapped[float | None] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(10), nullable=False)
    # gross_amount: cash impact before costs (negative for buy, positive for sell)
    gross_amount: Mapped[float | None] = mapped_column(Float)
    commission: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)  # signed (negative = cost)
    fees: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)  # signed (negative = cost)
    # net cash impact on the account in `currency` (negative = cash out)
    net_amount: Mapped[float | None] = mapped_column(Float)
    fx_rate: Mapped[float | None] = mapped_column(Float)  # to account base currency at trade time, if provided
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_file: Mapped[str | None] = mapped_column(String(300))
    notes: Mapped[str | None] = mapped_column(Text)
    imported_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    account = relationship("Account")
    instrument = relationship("Instrument")

    __table_args__ = (
        UniqueConstraint("account_id", "source", "external_transaction_id", name="uq_tx_external"),
        UniqueConstraint("source_hash", name="uq_tx_source_hash"),
        Index("ix_tx_account_date", "account_id", "trade_date"),
        Index("ix_tx_instrument", "instrument_id"),
    )


class CashFlow(Base):
    __tablename__ = "cash_flows"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    instrument_id: Mapped[int | None] = mapped_column(ForeignKey("instruments.id"))
    external_id: Mapped[str | None] = mapped_column(String(100))
    flow_type: Mapped[str] = mapped_column(String(20), nullable=False)
    flow_date: Mapped[date] = mapped_column(Date, nullable=False)
    amount: Mapped[float] = mapped_column(Float, nullable=False)  # signed cash impact
    currency: Mapped[str] = mapped_column(String(10), nullable=False)
    # True for investor deposits/withdrawals (external flows affecting TWR/MWR), False for internal
    is_external: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    description: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_file: Mapped[str | None] = mapped_column(String(300))
    imported_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (
        UniqueConstraint("account_id", "source", "external_id", name="uq_cf_external"),
        UniqueConstraint("source_hash", name="uq_cf_source_hash"),
        Index("ix_cf_account_date", "account_id", "flow_date"),
    )


class Position(Base):
    """Materialized position; always rebuildable from transactions (rebuild-portfolio)."""

    __tablename__ = "positions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"), nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    average_cost: Mapped[float | None] = mapped_column(Float)  # per unit, instrument currency, incl. costs
    cost_basis: Mapped[float | None] = mapped_column(Float)  # total, instrument currency
    realized_pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)  # lifetime, instr. currency
    currency: Mapped[str] = mapped_column(String(10), nullable=False)
    first_trade_date: Mapped[date | None] = mapped_column(Date)
    last_trade_date: Mapped[date | None] = mapped_column(Date)
    rebuilt_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    account = relationship("Account")
    instrument = relationship("Instrument")

    __table_args__ = (UniqueConstraint("account_id", "instrument_id", name="uq_position_account_instrument"),)


class TaxLot(Base):
    """FIFO lots, rebuilt from transactions. Not used for Czech tax logic yet."""

    __tablename__ = "tax_lots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"), nullable=False)
    open_transaction_id: Mapped[int | None] = mapped_column(ForeignKey("transactions.id"))
    open_date: Mapped[date] = mapped_column(Date, nullable=False)
    quantity_open: Mapped[float] = mapped_column(Float, nullable=False)
    quantity_remaining: Mapped[float] = mapped_column(Float, nullable=False)
    unit_cost: Mapped[float] = mapped_column(Float, nullable=False)
    currency: Mapped[str] = mapped_column(String(10), nullable=False)
    closed_date: Mapped[date | None] = mapped_column(Date)
    realized_pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    __table_args__ = (Index("ix_lot_account_instrument", "account_id", "instrument_id"),)


class Price(Base):
    __tablename__ = "prices"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"), nullable=False)
    price_date: Mapped[date] = mapped_column(Date, nullable=False)
    open: Mapped[float | None] = mapped_column(Float)
    high: Mapped[float | None] = mapped_column(Float)
    low: Mapped[float | None] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    adjusted_close: Mapped[float | None] = mapped_column(Float)
    volume: Mapped[float | None] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(10), nullable=False)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (
        UniqueConstraint("instrument_id", "price_date", "source", name="uq_price_instrument_date_source"),
        Index("ix_price_instrument_date", "instrument_id", "price_date"),
    )


class FxRate(Base):
    """rate = how many quote_currency per 1 base_currency (base=USD, quote=CZK, rate=23.5)."""

    __tablename__ = "fx_rates"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rate_date: Mapped[date] = mapped_column(Date, nullable=False)
    base_currency: Mapped[str] = mapped_column(String(10), nullable=False)
    quote_currency: Mapped[str] = mapped_column(String(10), nullable=False)
    rate: Mapped[float] = mapped_column(Float, nullable=False)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (
        UniqueConstraint("rate_date", "base_currency", "quote_currency", "source", name="uq_fx"),
        Index("ix_fx_pair_date", "base_currency", "quote_currency", "rate_date"),
    )


class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    # NULL account_id = consolidated across all accounts
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"))
    base_currency: Mapped[str] = mapped_column(String(10), nullable=False)
    account_value: Mapped[float | None] = mapped_column(Float)
    cash: Mapped[float | None] = mapped_column(Float)
    invested_value: Mapped[float | None] = mapped_column(Float)
    cost_basis: Mapped[float | None] = mapped_column(Float)
    unrealized_pnl: Mapped[float | None] = mapped_column(Float)
    realized_pnl: Mapped[float | None] = mapped_column(Float)
    net_external_flows_to_date: Mapped[float | None] = mapped_column(Float)
    positions_count: Mapped[int | None] = mapped_column(Integer)
    # True when some component could not be valued (missing price/FX) -> values are partial
    incomplete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    details: Mapped[str | None] = mapped_column(Text)  # JSON with per-instrument valuation
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (UniqueConstraint("snapshot_date", "account_id", name="uq_snapshot_date_account"),)


class Benchmark(Base):
    """Benchmark definition; its price history lives in `prices` via instrument_id."""

    __tablename__ = "benchmarks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(20), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"), nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    instrument = relationship("Instrument")


class ImportRun(Base):
    """Audit trail for every sync/import job."""

    __tablename__ = "import_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job: Mapped[str] = mapped_column(String(50), nullable=False)  # sync-ibkr | import-crypto | update-prices
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="running")  # running|success|error
    raw_path: Mapped[str | None] = mapped_column(String(300))
    raw_sha256: Mapped[str | None] = mapped_column(String(64))
    records_seen: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    records_inserted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    records_duplicate: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    records_skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    details: Mapped[str | None] = mapped_column(Text)  # JSON
