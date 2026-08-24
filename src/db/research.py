"""Research layer ORM models (Investor OS v2): investments, theses, assumptions, evidence,
KPIs, valuation, decisions, predictions, reviews.

Design rules (see README "Research layer"):
  * The portfolio DB (models.py) stays the source of truth for holdings; nothing here
    duplicates accounting data - investments reference `instruments.id`.
  * FACTS/OBSERVATIONS live in evidence & kpi_observations; ASSUMPTIONS, FORECASTS
    (predictions), INTERPRETATIONS (thesis text) and DECISIONS are separate tables and
    never silently interchangeable.
  * No hindsight rewriting: `thesis_versions` and `decisions` are immutable - the service
    layer offers no update path; revisions create new rows, corrections are amendment rows.
  * confidence and probability are both stored as 0-100 numbers. Confidence = subjective
    strength of belief; probability = explicit likelihood of a defined event. Never mixed.
  * created_by is one of USER | SYSTEM | AI | IMPORT.
  * Temporal fields: event_date (when it happened), source_published_at (when reported),
    observed_at (when Investor OS learned it), created_at (row creation).
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

# --- vocabulary (plain strings, validated in the service layer) ---------------
INVESTMENT_STATUSES = (
    "DISCOVERED", "WATCHLIST", "RESEARCHING", "READY_FOR_DECISION",
    "OWNED", "EXITED", "REJECTED", "ARCHIVED",
)
ASSUMPTION_STATUSES = ("SUPPORTED", "WEAKENING", "UNKNOWN", "CHALLENGED", "BROKEN")
ASSUMPTION_CATEGORIES = (
    "growth", "margin", "unit_economics", "balance_sheet", "management", "industry", "macro",
    "valuation", "competitive_advantage", "capital_allocation", "regulation", "other",
)
RISK_CATEGORIES = (
    "business", "financial", "valuation", "management", "competitive", "regulatory", "macro",
    "currency", "execution", "technology", "geopolitical", "other",
)
SEVERITIES = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
IMPORTANCES = ("LOW", "MEDIUM", "HIGH")
DIRECTIONS = ("SUPPORTING", "CONTRADICTING", "NEUTRAL")
EVIDENCE_TYPES = (
    "manual", "filing", "earnings", "transcript", "news", "macro", "market_data", "insider",
    "congress", "institutional", "research", "other",
)
EVIDENCE_TARGETS = ("investment", "thesis", "assumption", "risk", "catalyst", "valuation")
DECISION_TYPES = ("BUY", "ADD", "HOLD", "TRIM", "SELL", "WATCH", "REJECT", "NO_ACTION")
PREDICTION_STATUSES = ("OPEN", "RESOLVED_TRUE", "RESOLVED_FALSE", "AMBIGUOUS", "CANCELLED")
BREAKER_STATUSES = ("ACTIVE", "TRIGGERED", "RESOLVED")
RISK_STATUSES = ("OPEN", "MITIGATED", "CLOSED")
CATALYST_STATUSES = ("PENDING", "OCCURRED", "EXPIRED", "CANCELLED")
REDTEAM_STATUSES = ("OPEN", "ADDRESSED", "ACCEPTED", "DISMISSED")
VALUATION_MODEL_TYPES = (
    "dcf", "pe", "ev_ebitda", "ev_sales", "pb", "fcf_yield", "sotp", "custom",
)
CREATED_BY = ("USER", "SYSTEM", "AI", "IMPORT")
REVIEW_FREQUENCIES = ("after_earnings", "monthly", "quarterly", "semiannual", "annual", "manual")


class Investment(Base):
    """Research entity; may exist without any holding (watchlist, candidate, rejected...)."""

    __tablename__ = "investments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # nullable: an investment can be researched before any tradable instrument exists in v1 tables
    instrument_id: Mapped[int | None] = mapped_column(ForeignKey("instruments.id"))
    ticker: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="DISCOVERED")
    notes: Mapped[str | None] = mapped_column(Text)
    review_frequency: Mapped[str] = mapped_column(String(30), nullable=False, default="quarterly")
    next_review_date: Mapped[date | None] = mapped_column(Date)
    last_review_date: Mapped[date | None] = mapped_column(Date)
    created_by: Mapped[str] = mapped_column(String(10), nullable=False, default="USER")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    instrument = relationship("Instrument")
    theses = relationship("Thesis", back_populates="investment")

    __table_args__ = (
        UniqueConstraint("ticker", name="uq_investment_ticker"),
        Index("ix_investment_status", "status"),
        Index("ix_investment_instrument", "instrument_id"),
        Index("ix_investment_next_review", "next_review_date"),
    )


class Thesis(Base):
    """Stable thesis identity; the content lives in immutable thesis_versions."""

    __tablename__ = "theses"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    investment_id: Mapped[int] = mapped_column(ForeignKey("investments.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # denormalized pointer to the latest version (content itself is never updated)
    current_version_id: Mapped[int | None] = mapped_column(Integer)
    created_by: Mapped[str] = mapped_column(String(10), nullable=False, default="USER")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    investment = relationship("Investment", back_populates="theses")
    versions = relationship("ThesisVersion", back_populates="thesis", order_by="ThesisVersion.version_number")

    __table_args__ = (Index("ix_thesis_investment", "investment_id"),)


class ThesisVersion(Base):
    """IMMUTABLE. Never UPDATE a row of this table; a revision inserts a new row."""

    __tablename__ = "thesis_versions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    thesis_id: Mapped[int] = mapped_column(ForeignKey("theses.id"), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    previous_version_id: Mapped[int | None] = mapped_column(ForeignKey("thesis_versions.id"))
    reason_for_revision: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    core_thesis: Mapped[str | None] = mapped_column(Text)
    variant_perception: Mapped[str | None] = mapped_column(Text)
    market_expectation: Mapped[str | None] = mapped_column(Text)
    our_expectation: Mapped[str | None] = mapped_column(Text)
    why_market_may_be_wrong: Mapped[str | None] = mapped_column(Text)
    expected_return_summary: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[int | None] = mapped_column(Integer)  # 0-100
    time_horizon: Mapped[str | None] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="ACTIVE")
    review_date: Mapped[date | None] = mapped_column(Date)
    # decision that prompted this version, if any (no FK: decisions also reference versions)
    created_from_decision_id: Mapped[int | None] = mapped_column(Integer)
    created_by: Mapped[str] = mapped_column(String(10), nullable=False, default="USER")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    thesis = relationship("Thesis", back_populates="versions")

    __table_args__ = (
        UniqueConstraint("thesis_id", "version_number", name="uq_thesis_version_number"),
        Index("ix_thesis_version_thesis", "thesis_id"),
    )


class ThesisAssumption(Base):
    """Living state of an explicit assumption; linked to the stable thesis, with the version
    that introduced it recorded for history. Status changes are user-controlled in v2."""

    __tablename__ = "thesis_assumptions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    thesis_id: Mapped[int] = mapped_column(ForeignKey("theses.id"), nullable=False)
    introduced_in_version_id: Mapped[int | None] = mapped_column(ForeignKey("thesis_versions.id"))
    kpi_id: Mapped[int | None] = mapped_column(ForeignKey("investment_kpis.id"))  # KPI expectation link
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(40), nullable=False, default="other")
    importance: Mapped[str] = mapped_column(String(10), nullable=False, default="MEDIUM")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="UNKNOWN")
    status_updated_at: Mapped[datetime | None] = mapped_column(DateTime)
    confidence: Mapped[int | None] = mapped_column(Integer)  # 0-100
    expected_value: Mapped[float | None] = mapped_column(Float)
    expected_min: Mapped[float | None] = mapped_column(Float)
    expected_max: Mapped[float | None] = mapped_column(Float)
    unit: Mapped[str | None] = mapped_column(String(30))
    time_horizon: Mapped[str | None] = mapped_column(String(50))
    breaker_condition: Mapped[str | None] = mapped_column(Text)
    source_type: Mapped[str | None] = mapped_column(String(30))
    notes: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by: Mapped[str] = mapped_column(String(10), nullable=False, default="USER")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (
        Index("ix_assumption_thesis", "thesis_id"),
        Index("ix_assumption_status", "status"),
    )


class ThesisBreaker(Base):
    """Condition that materially invalidates the investment case (distinct from a risk)."""

    __tablename__ = "thesis_breakers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    investment_id: Mapped[int] = mapped_column(ForeignKey("investments.id"), nullable=False)
    thesis_id: Mapped[int | None] = mapped_column(ForeignKey("theses.id"))
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(String(10), nullable=False, default="HIGH")
    condition_text: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ACTIVE")
    triggered_at: Mapped[datetime | None] = mapped_column(DateTime)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(10), nullable=False, default="USER")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (
        Index("ix_breaker_investment", "investment_id"),
        Index("ix_breaker_status", "status"),
    )


class Risk(Base):
    __tablename__ = "risks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    investment_id: Mapped[int] = mapped_column(ForeignKey("investments.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(30), nullable=False, default="other")
    probability: Mapped[int | None] = mapped_column(Integer)  # 0-100
    impact: Mapped[str | None] = mapped_column(String(10))  # LOW/MEDIUM/HIGH
    severity: Mapped[str] = mapped_column(String(10), nullable=False, default="MEDIUM")
    mitigation: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="OPEN")
    created_by: Mapped[str] = mapped_column(String(10), nullable=False, default="USER")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (
        Index("ix_risk_investment", "investment_id"),
        Index("ix_risk_status", "status"),
    )


class Catalyst(Base):
    __tablename__ = "catalysts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    investment_id: Mapped[int] = mapped_column(ForeignKey("investments.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    expected_date: Mapped[date | None] = mapped_column(Date)
    probability: Mapped[int | None] = mapped_column(Integer)  # 0-100
    potential_impact: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING")
    actual_date: Mapped[date | None] = mapped_column(Date)
    outcome: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(10), nullable=False, default="USER")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (
        Index("ix_catalyst_investment", "investment_id"),
        Index("ix_catalyst_expected", "expected_date"),
    )


class Evidence(Base):
    """Raw evidence, kept independent of interpretation. Manually entered in v2; v3 will
    populate it from filings/earnings/news/insider/congress/... via evidence_type/source."""

    __tablename__ = "evidence"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    investment_id: Mapped[int] = mapped_column(ForeignKey("investments.id"), nullable=False)
    thesis_version_id: Mapped[int | None] = mapped_column(ForeignKey("thesis_versions.id"))
    target_type: Mapped[str] = mapped_column(String(20), nullable=False, default="investment")
    target_id: Mapped[int | None] = mapped_column(Integer)  # id within the target table
    direction: Mapped[str] = mapped_column(String(15), nullable=False, default="NEUTRAL")
    evidence_type: Mapped[str] = mapped_column(String(20), nullable=False, default="manual")
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(String(500))
    source_name: Mapped[str | None] = mapped_column(String(200))
    source_date: Mapped[date | None] = mapped_column(Date)  # date on the source document
    event_date: Mapped[date | None] = mapped_column(Date)  # when the underlying event happened
    source_published_at: Mapped[datetime | None] = mapped_column(DateTime)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime)  # when Investor OS learned it
    reliability: Mapped[str | None] = mapped_column(String(10))  # LOW/MEDIUM/HIGH
    importance: Mapped[str] = mapped_column(String(10), nullable=False, default="MEDIUM")
    raw_reference: Mapped[str | None] = mapped_column(Text)  # pointer to raw archive (v3)
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(10), nullable=False, default="USER")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (
        Index("ix_evidence_investment", "investment_id"),
        Index("ix_evidence_target", "target_type", "target_id"),
        Index("ix_evidence_direction", "direction"),
    )


class InvestmentKpi(Base):
    __tablename__ = "investment_kpis"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    investment_id: Mapped[int] = mapped_column(ForeignKey("investments.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    unit: Mapped[str | None] = mapped_column(String(30))
    frequency: Mapped[str] = mapped_column(String(20), nullable=False, default="quarterly")
    importance: Mapped[str] = mapped_column(String(10), nullable=False, default="MEDIUM")
    direction_good: Mapped[str | None] = mapped_column(String(10))  # up | down | range
    thesis_relevance: Mapped[str | None] = mapped_column(Text)
    source_preference: Mapped[str | None] = mapped_column(String(100))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by: Mapped[str] = mapped_column(String(10), nullable=False, default="USER")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (
        UniqueConstraint("investment_id", "name", name="uq_kpi_investment_name"),
        Index("ix_kpi_investment", "investment_id"),
    )


class KpiObservation(Base):
    """FACT layer: observed KPI values per period. Manual in v2, automatic in v3."""

    __tablename__ = "kpi_observations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kpi_id: Mapped[int] = mapped_column(ForeignKey("investment_kpis.id"), nullable=False)
    period: Mapped[str] = mapped_column(String(20), nullable=False)  # e.g. "2026Q2", "FY2026"
    period_date: Mapped[date | None] = mapped_column(Date)  # period end, for ordering
    value: Mapped[float] = mapped_column(Float, nullable=False)
    reported_at: Mapped[date | None] = mapped_column(Date)  # when the company reported it
    observed_at: Mapped[datetime | None] = mapped_column(DateTime)  # when Investor OS learned it
    source: Mapped[str | None] = mapped_column(String(100))
    source_reference: Mapped[str | None] = mapped_column(String(300))
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(10), nullable=False, default="USER")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (
        UniqueConstraint("kpi_id", "period", "source", name="uq_kpi_obs_period_source"),
        Index("ix_kpi_obs_kpi", "kpi_id"),
    )


class ValuationModel(Base):
    __tablename__ = "valuation_models"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    investment_id: Mapped[int] = mapped_column(ForeignKey("investments.id"), nullable=False)
    model_type: Mapped[str] = mapped_column(String(20), nullable=False, default="custom")
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    reference_price: Mapped[float | None] = mapped_column(Float)
    reference_currency: Mapped[str | None] = mapped_column(String(10))
    reference_date: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by: Mapped[str] = mapped_column(String(10), nullable=False, default="USER")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    scenarios = relationship("ValuationScenario", back_populates="model", order_by="ValuationScenario.id")

    __table_args__ = (Index("ix_valuation_investment", "investment_id"),)


class ValuationScenario(Base):
    __tablename__ = "valuation_scenarios"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    model_id: Mapped[int] = mapped_column(ForeignKey("valuation_models.id"), nullable=False)
    scenario_name: Mapped[str] = mapped_column(String(50), nullable=False)  # bear/base/bull/custom...
    probability: Mapped[float | None] = mapped_column(Float)  # 0-100
    target_price: Mapped[float] = mapped_column(Float, nullable=False)
    currency: Mapped[str | None] = mapped_column(String(10))
    time_horizon_months: Mapped[int | None] = mapped_column(Integer)
    expected_dividends: Mapped[float | None] = mapped_column(Float)  # total over horizon, explicit only
    assumptions_json: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    model = relationship("ValuationModel", back_populates="scenarios")

    __table_args__ = (
        UniqueConstraint("model_id", "scenario_name", name="uq_scenario_model_name"),
        Index("ix_scenario_model", "model_id"),
    )


class Decision(Base):
    """IMMUTABLE decision journal entry with a frozen context snapshot. Never updated;
    corrections reference the original via amends_decision_id."""

    __tablename__ = "decisions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    investment_id: Mapped[int] = mapped_column(ForeignKey("investments.id"), nullable=False)
    decision_type: Mapped[str] = mapped_column(String(15), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    thesis_version_id: Mapped[int | None] = mapped_column(ForeignKey("thesis_versions.id"))
    amends_decision_id: Mapped[int | None] = mapped_column(ForeignKey("decisions.id"))
    # frozen deterministic context (None where genuinely unavailable - never fabricated)
    instrument_price: Mapped[float | None] = mapped_column(Float)
    price_currency: Mapped[str | None] = mapped_column(String(10))
    portfolio_value: Mapped[float | None] = mapped_column(Float)
    cash_value: Mapped[float | None] = mapped_column(Float)
    position_quantity_before: Mapped[float | None] = mapped_column(Float)
    position_weight_before: Mapped[float | None] = mapped_column(Float)  # fraction of invested value
    snapshot_json: Mapped[str | None] = mapped_column(Text)  # full frozen context
    # reasoning
    intended_position_change: Mapped[str | None] = mapped_column(Text)
    actual_position_change: Mapped[str | None] = mapped_column(Text)
    reasoning: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[int | None] = mapped_column(Integer)  # 0-100
    time_horizon: Mapped[str | None] = mapped_column(String(50))
    expected_outcome: Mapped[str | None] = mapped_column(Text)
    alternative_considered: Mapped[str | None] = mapped_column(Text)
    key_risks: Mapped[str | None] = mapped_column(Text)
    what_would_make_this_wrong: Mapped[str | None] = mapped_column(Text)
    information_available_summary: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(10), nullable=False, default="USER")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (
        Index("ix_decision_investment", "investment_id"),
        Index("ix_decision_decided_at", "decided_at"),
    )


class Prediction(Base):
    __tablename__ = "predictions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    investment_id: Mapped[int | None] = mapped_column(ForeignKey("investments.id"))
    thesis_id: Mapped[int | None] = mapped_column(ForeignKey("theses.id"))
    decision_id: Mapped[int | None] = mapped_column(ForeignKey("decisions.id"))
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str | None] = mapped_column(String(50))
    probability: Mapped[int] = mapped_column(Integer, nullable=False)  # 0-100
    resolution_date: Mapped[date | None] = mapped_column(Date)
    resolution_condition: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="OPEN")
    outcome: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(10), nullable=False, default="USER")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (
        Index("ix_prediction_investment", "investment_id"),
        Index("ix_prediction_status", "status"),
        Index("ix_prediction_resolution_date", "resolution_date"),
    )


class PreMortem(Base):
    """'Imagine this investment lost 60%. What happened?' - manual in v2."""

    __tablename__ = "premortems"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    investment_id: Mapped[int] = mapped_column(ForeignKey("investments.id"), nullable=False)
    thesis_version_id: Mapped[int | None] = mapped_column(ForeignKey("thesis_versions.id"))
    decision_id: Mapped[int | None] = mapped_column(ForeignKey("decisions.id"))
    scenario: Mapped[str] = mapped_column(Text, nullable=False)
    probability: Mapped[int | None] = mapped_column(Integer)  # 0-100
    impact: Mapped[str | None] = mapped_column(String(10))
    early_warning_signs: Mapped[str | None] = mapped_column(Text)
    possible_mitigation: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(10), nullable=False, default="USER")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (Index("ix_premortem_investment", "investment_id"),)


class RedTeamEntry(Base):
    """Independent bearish argument; manually entered in v2, AI red-team agent in the future."""

    __tablename__ = "red_team_entries"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    investment_id: Mapped[int] = mapped_column(ForeignKey("investments.id"), nullable=False)
    thesis_version_id: Mapped[int | None] = mapped_column(ForeignKey("thesis_versions.id"))
    argument: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(String(10), nullable=False, default="MEDIUM")
    evidence_reference: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="OPEN")
    resolution: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(10), nullable=False, default="USER")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (Index("ix_redteam_investment", "investment_id"),)
