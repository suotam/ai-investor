"""Operations & v5 mentor ORM models: pipeline runs, decision reviews, scenario runs,
concept history. Additive on top of v1-v4 tables.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.core import utcnow
from src.db.models import Base

PIPELINE_STATUSES = ("RUNNING", "SUCCESS", "PARTIAL", "FAILED")
STAGE_STATUSES = ("OK", "WARN", "FAIL", "SKIP")
DECISION_RATINGS = ("GOOD_PROCESS", "MIXED", "POOR_PROCESS")
CLAIM_TYPES = (
    "GUIDANCE", "TARGET", "EXPECTATION", "STRATEGIC_CLAIM", "RISK_COMMENTARY",
    "CAPITAL_ALLOCATION", "OTHER",
)
CLAIM_LINK_TARGETS = ("kpi_observation", "evidence", "intelligence_event", "financial_fact")


class PipelineRun(Base):
    """One `run daily` / `run weekly` execution with per-stage tracking."""

    __tablename__ = "pipeline_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pipeline: Mapped[str] = mapped_column(String(10), nullable=False)  # daily | weekly
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="RUNNING")
    output_path: Mapped[str | None] = mapped_column(String(400))
    summary: Mapped[str | None] = mapped_column(Text)

    stages = relationship("PipelineStage", back_populates="run", order_by="PipelineStage.id")

    __table_args__ = (Index("ix_pipeline_run_type", "pipeline", "started_at"),)


class PipelineStage(Base):
    __tablename__ = "pipeline_stages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("pipeline_runs.id"), nullable=False)
    stage: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    items_processed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    message: Mapped[str | None] = mapped_column(Text)

    run = relationship("PipelineRun", back_populates="stages")

    __table_args__ = (Index("ix_pipeline_stage_run", "run_id"),)


class ClaimLink(Base):
    """Management claim -> later evidence/outcome linkage (promise vs outcome)."""

    __tablename__ = "claim_links"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    claim_id: Mapped[int] = mapped_column(ForeignKey("management_claims.id"), nullable=False)
    target_type: Mapped[str] = mapped_column(String(30), nullable=False)
    target_id: Mapped[int] = mapped_column(Integer, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (Index("ix_claim_link_claim", "claim_id"),)


class DecisionReview(Base):
    """Outcome-independent human rating of a decision, optionally after a blind replay."""

    __tablename__ = "decision_reviews"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    decision_id: Mapped[int] = mapped_column(ForeignKey("decisions.id"), nullable=False)
    user_rating: Mapped[str] = mapped_column(String(15), nullable=False)
    would_repeat: Mapped[bool | None] = mapped_column(Boolean)  # answered during blind replay
    replay_used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    notes: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (Index("ix_decision_review_decision", "decision_id"),)


class ScenarioRun(Base):
    """Deterministic stress-test execution (mechanical, never a forecast)."""

    __tablename__ = "scenario_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    run_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    definition: Mapped[str] = mapped_column(Text, nullable=False)  # JSON shocks
    result: Mapped[str] = mapped_column(Text, nullable=False)  # JSON impact

    __table_args__ = (Index("ix_scenario_run_name", "name"),)


class ConceptHistory(Base):
    """'Teach me' concepts already shown (avoid repetition)."""

    __tablename__ = "concept_history"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    concept: Mapped[str] = mapped_column(String(100), nullable=False)
    shown_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    brief_run_id: Mapped[int | None] = mapped_column(ForeignKey("brief_runs.id"))
