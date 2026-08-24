"""Intelligence layer ORM models (Investor OS v3): provenance, external data, events, AI proposals.

Design rules:
  * Additive only - nothing here touches v1 portfolio or v2 research tables; links go through
    foreign keys (investments.id, instruments.id) and the existing service layer.
  * Every externally acquired row carries provenance via source_documents (provider, external
    id, URL, raw archive path, SHA-256, parser version, source tier).
  * Source tiers: 1 = primary (SEC, official statistics, company IR), 2 = reputable providers/
    press, 3 = aggregators/secondary analysis, 4 = social/unverified. Stored explicitly; AI
    context always includes the tier.
  * Historical observations are never overwritten: macro observations keep retrieval vintage,
    financial facts are keyed by content hash, insider/congress/13F rows are append-only.
  * ai_proposals is the ONLY place AI output lands. Accepting a proposal calls existing v2
    services; there is no code path where the LLM updates research tables directly.
"""
from __future__ import annotations

from datetime import date, datetime

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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.core import utcnow
from src.db.models import Base

SOURCE_TIERS = (1, 2, 3, 4)
SOURCE_STATUSES = ("archived", "parsed", "error")
EVENT_TYPES = (
    "NEW_FILING", "EARNINGS_RELEASE", "KPI_UPDATE", "GUIDANCE_CHANGE", "INSIDER_TRANSACTION",
    "CONGRESS_TRANSACTION", "INSTITUTIONAL_CHANGE", "MACRO_RELEASE", "PRICE_EVENT", "NEWS_EVENT",
)
EVENT_STATES = ("NEW", "PROCESSED", "DISMISSED")
AI_STATES = ("NONE", "PENDING", "DONE", "FAILED")
SEVERITIES3 = ("LOW", "MEDIUM", "HIGH")
PROPOSAL_TYPES = (
    "NEW_EVIDENCE", "KPI_MAPPING", "KPI_OBSERVATION", "ASSUMPTION_STATUS_CHANGE", "NEW_RISK",
    "RISK_UPDATE", "NEW_CATALYST", "BREAKER_WARNING", "THESIS_REVISION", "VALUATION_QUESTION",
    "RED_TEAM_ARGUMENT", "NEW_PREDICTION", "RESEARCH_QUESTION",
)
PROPOSAL_STATUSES = ("PENDING", "ACCEPTED", "REJECTED", "EDITED", "EXPIRED", "DEFERRED")
CANDIDATE_STATUSES = ("NEW", "PROMOTED", "DISMISSED")
WATCHLIST_KINDS = ("company", "insider", "congress_member", "manager", "macro_series", "candidate")


class SourceDocument(Base):
    """Provenance root for every externally acquired item. Raw payload archived on disk."""

    __tablename__ = "source_documents"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)  # sec_edgar | fred | file | ...
    source_type: Mapped[str] = mapped_column(String(50), nullable=False)  # filing | companyfacts | form4 | 13f | macro_csv | ...
    external_id: Mapped[str] = mapped_column(String(200), nullable=False)  # accession, series id, ...
    url: Mapped[str | None] = mapped_column(String(600))
    title: Mapped[str | None] = mapped_column(String(400))
    issuer: Mapped[str | None] = mapped_column(String(200))
    entity_key: Mapped[str | None] = mapped_column(String(100))  # e.g. CIK, ticker, series code
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date | None] = mapped_column(Date)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    raw_path: Mapped[str | None] = mapped_column(String(400))
    sha256: Mapped[str | None] = mapped_column(String(64))
    parser_version: Mapped[str | None] = mapped_column(String(30))
    source_tier: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    metadata_json: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="archived")
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("provider", "source_type", "external_id", name="uq_srcdoc_provider_ext"),
        Index("ix_srcdoc_entity", "entity_key"),
        Index("ix_srcdoc_sha", "sha256"),
        Index("ix_srcdoc_published", "published_at"),
    )


class FinancialFact(Base):
    """Normalized structured financial observation (XBRL company facts). Append-only."""

    __tablename__ = "financial_facts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_document_id: Mapped[int | None] = mapped_column(ForeignKey("source_documents.id"))
    cik: Mapped[str] = mapped_column(String(20), nullable=False)
    metric: Mapped[str | None] = mapped_column(String(100))  # normalized internal name, if mapped
    taxonomy: Mapped[str] = mapped_column(String(30), nullable=False)  # us-gaap | ifrs-full | dei | ...
    concept: Mapped[str] = mapped_column(String(200), nullable=False)  # raw XBRL tag
    value: Mapped[float] = mapped_column(Float, nullable=False)
    unit: Mapped[str] = mapped_column(String(30), nullable=False)
    period_start: Mapped[date | None] = mapped_column(Date)  # duration concepts
    period_end: Mapped[date | None] = mapped_column(Date)  # duration end OR instant date
    is_instant: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    fiscal_year: Mapped[int | None] = mapped_column(Integer)
    fiscal_period: Mapped[str | None] = mapped_column(String(10))  # FY, Q1..Q4, H1...
    form: Mapped[str | None] = mapped_column(String(20))  # filing form the fact came from
    accession: Mapped[str | None] = mapped_column(String(30))
    filed_at: Mapped[date | None] = mapped_column(Date)
    fact_hash: Mapped[str] = mapped_column(String(64), nullable=False)  # idempotency key
    retrieved_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (
        UniqueConstraint("fact_hash", name="uq_fact_hash"),
        Index("ix_fact_cik_concept", "cik", "concept"),
        Index("ix_fact_metric", "metric"),
        Index("ix_fact_period_end", "period_end"),
    )


class IntelligenceEvent(Base):
    """Central inbox: something meaningful happened. Deduplicated by dedup_key."""

    __tablename__ = "intelligence_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_type: Mapped[str] = mapped_column(String(30), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    investment_id: Mapped[int | None] = mapped_column(ForeignKey("investments.id"))
    instrument_id: Mapped[int | None] = mapped_column(ForeignKey("instruments.id"))
    title: Mapped[str] = mapped_column(String(400), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(String(10), nullable=False, default="LOW")
    materiality_source: Mapped[str] = mapped_column(String(15), nullable=False, default="deterministic")
    source_document_id: Mapped[int | None] = mapped_column(ForeignKey("source_documents.id"))
    payload_json: Mapped[str | None] = mapped_column(Text)
    dedup_key: Mapped[str] = mapped_column(String(120), nullable=False)
    processing_state: Mapped[str] = mapped_column(String(15), nullable=False, default="NEW")
    ai_state: Mapped[str] = mapped_column(String(10), nullable=False, default="NONE")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    source_document = relationship("SourceDocument")

    __table_args__ = (
        UniqueConstraint("dedup_key", name="uq_event_dedup"),
        Index("ix_event_investment", "investment_id"),
        Index("ix_event_type_time", "event_type", "occurred_at"),
        Index("ix_event_state", "processing_state"),
    )


class AiProposal(Base):
    """The ONLY table AI writes to. Human review moves status; acceptance calls v2 services."""

    __tablename__ = "ai_proposals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    proposal_type: Mapped[str] = mapped_column(String(30), nullable=False)
    investment_id: Mapped[int | None] = mapped_column(ForeignKey("investments.id"))
    event_id: Mapped[int | None] = mapped_column(ForeignKey("intelligence_events.id"))
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    what_happened: Mapped[str | None] = mapped_column(Text)
    why_it_matters: Mapped[str | None] = mapped_column(Text)
    proposed_change_json: Mapped[str | None] = mapped_column(Text)  # typed payload per proposal_type
    reasoning: Mapped[str | None] = mapped_column(Text)
    supporting_refs: Mapped[str | None] = mapped_column(Text)  # JSON list of evidence/source ids
    contradicting_refs: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[int | None] = mapped_column(Integer)  # 0-100
    provider: Mapped[str | None] = mapped_column(String(50))
    model: Mapped[str | None] = mapped_column(String(100))
    prompt_version: Mapped[str | None] = mapped_column(String(50))
    context_hash: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(15), nullable=False, default="PENDING")
    resolution_note: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_by: Mapped[str] = mapped_column(String(10), nullable=False, default="AI")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (
        Index("ix_proposal_status", "status"),
        Index("ix_proposal_investment", "investment_id"),
    )


class InsiderTransaction(Base):
    __tablename__ = "insider_transactions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    issuer_cik: Mapped[str] = mapped_column(String(20), nullable=False)
    issuer_name: Mapped[str | None] = mapped_column(String(200))
    investment_id: Mapped[int | None] = mapped_column(ForeignKey("investments.id"))
    insider_name: Mapped[str] = mapped_column(String(200), nullable=False)
    insider_role: Mapped[str | None] = mapped_column(String(200))
    transaction_date: Mapped[date | None] = mapped_column(Date)
    filing_date: Mapped[date | None] = mapped_column(Date)
    security: Mapped[str | None] = mapped_column(String(200))
    transaction_code: Mapped[str | None] = mapped_column(String(5))  # raw SEC code (P,S,M,A,F,G,...)
    transaction_type: Mapped[str] = mapped_column(String(30), nullable=False, default="other")
    shares: Mapped[float | None] = mapped_column(Float)
    price: Mapped[float | None] = mapped_column(Float)
    value: Mapped[float | None] = mapped_column(Float)
    shares_after: Mapped[float | None] = mapped_column(Float)
    direct_ownership: Mapped[bool | None] = mapped_column(Boolean)
    acquired_disposed: Mapped[str | None] = mapped_column(String(1))  # A | D
    source_document_id: Mapped[int | None] = mapped_column(ForeignKey("source_documents.id"))
    dedup_key: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (
        UniqueConstraint("dedup_key", name="uq_insider_dedup"),
        Index("ix_insider_issuer_date", "issuer_cik", "transaction_date"),
        Index("ix_insider_investment", "investment_id"),
    )


class CongressTransaction(Base):
    """Congressional disclosure rows. Values are RANGES; disclosure lags transactions;
    ticker resolution may fail (kept NULL). Context, never a signal."""

    __tablename__ = "congress_transactions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person: Mapped[str] = mapped_column(String(200), nullable=False)
    chamber: Mapped[str | None] = mapped_column(String(10))  # house | senate
    owner: Mapped[str | None] = mapped_column(String(50))  # self | spouse | dependent | joint
    transaction_date: Mapped[date | None] = mapped_column(Date)
    disclosure_date: Mapped[date | None] = mapped_column(Date)
    asset_description: Mapped[str] = mapped_column(String(400), nullable=False)
    ticker: Mapped[str | None] = mapped_column(String(20))
    instrument_id: Mapped[int | None] = mapped_column(ForeignKey("instruments.id"))
    investment_id: Mapped[int | None] = mapped_column(ForeignKey("investments.id"))
    transaction_type: Mapped[str | None] = mapped_column(String(30))  # purchase | sale | exchange...
    amount_low: Mapped[float | None] = mapped_column(Float)
    amount_high: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str | None] = mapped_column(String(100))
    source_reference: Mapped[str | None] = mapped_column(String(300))
    source_document_id: Mapped[int | None] = mapped_column(ForeignKey("source_documents.id"))
    dedup_key: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (
        UniqueConstraint("dedup_key", name="uq_congress_dedup"),
        Index("ix_congress_person", "person"),
        Index("ix_congress_ticker", "ticker"),
    )


class InstitutionalManager(Base):
    __tablename__ = "institutional_managers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    cik: Mapped[str] = mapped_column(String(20), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (UniqueConstraint("cik", name="uq_manager_cik"),)


class InstitutionalHolding(Base):
    """13F holdings per manager per report period. Delayed, long-only, incomplete by design."""

    __tablename__ = "institutional_holdings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    manager_id: Mapped[int] = mapped_column(ForeignKey("institutional_managers.id"), nullable=False)
    period: Mapped[date] = mapped_column(Date, nullable=False)  # report period end
    cusip: Mapped[str] = mapped_column(String(20), nullable=False)
    issuer_name: Mapped[str | None] = mapped_column(String(200))
    ticker: Mapped[str | None] = mapped_column(String(20))
    instrument_id: Mapped[int | None] = mapped_column(ForeignKey("instruments.id"))
    shares: Mapped[float | None] = mapped_column(Float)
    value_usd: Mapped[float | None] = mapped_column(Float)
    source_document_id: Mapped[int | None] = mapped_column(ForeignKey("source_documents.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (
        UniqueConstraint("manager_id", "period", "cusip", name="uq_holding_mgr_period_cusip"),
        Index("ix_holding_period", "period"),
        Index("ix_holding_cusip", "cusip"),
    )


class MacroSeries(Base):
    __tablename__ = "macro_series"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(30), nullable=False)  # fred | file | ...
    series_code: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    unit: Mapped[str | None] = mapped_column(String(50))
    frequency: Mapped[str | None] = mapped_column(String(20))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (UniqueConstraint("provider", "series_code", name="uq_macro_series"),)


class MacroObservation(Base):
    """Vintage-aware: the same obs_date may appear with several retrieved_at values (revisions).
    Reads take the latest vintage per date; history is never overwritten."""

    __tablename__ = "macro_observations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    series_id: Mapped[int] = mapped_column(ForeignKey("macro_series.id"), nullable=False)
    obs_date: Mapped[date] = mapped_column(Date, nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    source_document_id: Mapped[int | None] = mapped_column(ForeignKey("source_documents.id"))

    __table_args__ = (
        UniqueConstraint("series_id", "obs_date", "value", name="uq_macro_obs"),
        Index("ix_macro_obs_series_date", "series_id", "obs_date"),
    )


class InvestmentMacroLink(Base):
    __tablename__ = "investment_macro_links"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    investment_id: Mapped[int] = mapped_column(ForeignKey("investments.id"), nullable=False)
    series_id: Mapped[int] = mapped_column(ForeignKey("macro_series.id"), nullable=False)
    relationship_: Mapped[str | None] = mapped_column("relationship", String(200))
    why_it_matters: Mapped[str | None] = mapped_column(Text)
    expected_direction: Mapped[str | None] = mapped_column(String(100))
    importance: Mapped[str] = mapped_column(String(10), nullable=False, default="MEDIUM")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (
        UniqueConstraint("investment_id", "series_id", name="uq_inv_macro"),
    )


class WatchlistEntry(Base):
    __tablename__ = "watchlist_entries"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    key: Mapped[str] = mapped_column(String(200), nullable=False)  # ticker/CIK/person name/series code
    label: Mapped[str | None] = mapped_column(String(200))
    notes: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (UniqueConstraint("kind", "key", name="uq_watchlist_kind_key"),)


class ResearchCandidate(Base):
    __tablename__ = "research_candidates"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker: Mapped[str] = mapped_column(String(20), nullable=False)
    name: Mapped[str | None] = mapped_column(String(200))
    source: Mapped[str] = mapped_column(String(50), nullable=False)  # 13f | insider | screen | manual
    reasons_json: Mapped[str | None] = mapped_column(Text)  # list of human-readable reasons
    status: Mapped[str] = mapped_column(String(15), nullable=False, default="NEW")
    discovered_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    promoted_investment_id: Mapped[int | None] = mapped_column(ForeignKey("investments.id"))
    notes: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("ticker", "source", name="uq_candidate_ticker_source"),
        Index("ix_candidate_status", "status"),
    )
