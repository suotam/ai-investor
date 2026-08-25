"""Mentor & briefing layer ORM models (Investor OS v4).

Core idea: DELTA, NOT STATE. brief_runs are persistent checkpoints ("what was known at the
last brief"); brief_items record what was actually surfaced (and why) so nothing appears as
"new" forever; attention_states carry explicit human triage (SEEN/DEFERRED/RESOLVED);
management_claims store sourced management statements for promise-vs-outcome tracking;
brief_feedback stores human ratings (never used for automatic retraining).
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

BRIEF_TYPES = ("daily", "weekly")
BRIEF_STATUSES = ("completed", "preview", "failed", "superseded")
ATTENTION_STATUSES = ("NEW", "SEEN", "DEFERRED", "RESOLVED")
FEEDBACK_RATINGS = ("USEFUL", "NOT_USEFUL", "TOO_NOISY", "MORE_LIKE_THIS")
# v5 vocabulary (v4 legacy statuses remain accepted for stored rows)
CLAIM_STATUSES = (
    "OPEN", "CONFIRMED", "PARTIALLY_CONFIRMED", "MISSED", "SUPERSEDED", "AMBIGUOUS",
    "FULFILLED", "BROKEN", "UNCLEAR", "WITHDRAWN",
)


class BriefRun(Base):
    """One brief generation. Completed non-preview runs are the checkpoints the delta engine
    compares against; previews and superseded runs never advance the checkpoint."""

    __tablename__ = "brief_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    brief_type: Mapped[str] = mapped_column(String(10), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    period_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)  # previous cutoff
    period_end: Mapped[datetime] = mapped_column(DateTime, nullable=False)  # this cutoff
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="completed")
    ai_used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ai_model: Mapped[str | None] = mapped_column(String(100))
    ai_prompt_version: Mapped[str | None] = mapped_column(String(50))
    ai_context_hash: Mapped[str | None] = mapped_column(String(64))
    output_path: Mapped[str | None] = mapped_column(String(400))
    audio_path: Mapped[str | None] = mapped_column(String(400))
    # frozen portfolio context for the NEXT run's deltas (value, weights, prices)
    portfolio_value: Mapped[float | None] = mapped_column(Float)
    base_currency: Mapped[str | None] = mapped_column(String(10))
    portfolio_state_json: Mapped[str | None] = mapped_column(Text)
    research_state_hash: Mapped[str | None] = mapped_column(String(64))
    items_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    suppressed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    items = relationship("BriefItem", back_populates="run")

    __table_args__ = (
        Index("ix_brief_run_type_status", "brief_type", "status"),
        Index("ix_brief_run_period_end", "period_end"),
    )


class BriefItem(Base):
    """One surfaced brief item, with a stable item_key (suppression identity), the reason it
    was shown ("why am I seeing this") and traceable source references."""

    __tablename__ = "brief_items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    brief_run_id: Mapped[int] = mapped_column(ForeignKey("brief_runs.id"), nullable=False)
    delta_type: Mapped[str] = mapped_column(String(40), nullable=False)
    item_key: Mapped[str] = mapped_column(String(160), nullable=False)
    investment_id: Mapped[int | None] = mapped_column(ForeignKey("investments.id"))
    severity: Mapped[str] = mapped_column(String(10), nullable=False, default="LOW")
    title: Mapped[str] = mapped_column(String(400), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)  # "shown because ..."
    source_refs: Mapped[str | None] = mapped_column(Text)  # JSON: table:id references
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    run = relationship("BriefRun", back_populates="items")

    __table_args__ = (
        Index("ix_brief_item_key", "item_key"),
        Index("ix_brief_item_run", "brief_run_id"),
        Index("ix_brief_item_investment", "investment_id"),
    )


class AttentionState(Base):
    """Human triage of a surfaced item (per item_key). DEFERRED items resurface at defer_until;
    RESOLVED items stay suppressed unless the underlying delta_type re-fires with a new key."""

    __tablename__ = "attention_states"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_key: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="SEEN")
    investment_id: Mapped[int | None] = mapped_column(ForeignKey("investments.id"))
    defer_until: Mapped[date | None] = mapped_column(Date)
    note: Mapped[str | None] = mapped_column(Text)
    first_surfaced_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (UniqueConstraint("item_key", name="uq_attention_item_key"),)


class ManagementClaim(Base):
    """Sourced management statement/guidance for promise-vs-outcome tracking. Never from
    unsourced LLM memory - source_document_id / source_reference required at service level."""

    __tablename__ = "management_claims"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    investment_id: Mapped[int] = mapped_column(ForeignKey("investments.id"), nullable=False)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    speaker: Mapped[str | None] = mapped_column(String(200))
    speaker_role: Mapped[str | None] = mapped_column(String(100))
    claim_type: Mapped[str] = mapped_column(String(20), nullable=False, default="OTHER")
    stated_confidence: Mapped[str | None] = mapped_column(String(100))  # only if explicitly stated
    claim_date: Mapped[date | None] = mapped_column(Date)
    topic: Mapped[str | None] = mapped_column(String(100))
    time_horizon: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="OPEN")
    outcome_note: Mapped[str | None] = mapped_column(Text)
    source_document_id: Mapped[int | None] = mapped_column(ForeignKey("source_documents.id"))
    source_reference: Mapped[str | None] = mapped_column(String(400))
    created_by: Mapped[str] = mapped_column(String(10), nullable=False, default="USER")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (
        Index("ix_claim_investment", "investment_id"),
        Index("ix_claim_status", "status"),
    )


class BriefFeedback(Base):
    """Human feedback on surfaced items. Stored only; no automatic retraining."""

    __tablename__ = "brief_feedback"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    brief_item_id: Mapped[int | None] = mapped_column(ForeignKey("brief_items.id"))
    item_key: Mapped[str] = mapped_column(String(160), nullable=False)
    rating: Mapped[str] = mapped_column(String(15), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (Index("ix_feedback_item_key", "item_key"),)
