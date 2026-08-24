"""Structured research import: YAML/JSON file -> v2 research layer, via the service layer only.

Safety contract (see README):
  * conservative by default: aborts if the investment already exists (unless --allow-existing);
  * NEVER creates a thesis revision and NEVER touches existing thesis versions - if a thesis
    already exists, the thesis section is refused even with --allow-existing;
  * whole import runs in ONE transaction (the caller's session_scope): any failure rolls
    everything back;
  * portfolio data (v1) is never written - only read for instrument linking;
  * validation happens BEFORE any database write (pydantic, extra fields forbidden);
  * decision snapshots use the existing record_decision service: historical context is taken
    deterministically from cached prices/valuation as of decided_at, and anything unavailable
    stays None - never fabricated from today's state.

Input enums are case-insensitive and normalized to the v2 vocabulary; a few friendly aliases
are accepted (KPI direction_good HIGHER/LOWER -> up/down, catalyst status OPEN -> PENDING).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field as dfield
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy.orm import Session

from src.core import InstrumentRef, sha256_bytes
from src.db.models import Instrument
from src.db.research import (
    ASSUMPTION_CATEGORIES,
    ASSUMPTION_STATUSES,
    DECISION_TYPES,
    DIRECTIONS,
    EVIDENCE_TARGETS,
    EVIDENCE_TYPES,
    IMPORTANCES,
    INVESTMENT_STATUSES,
    REVIEW_FREQUENCIES,
    RISK_CATEGORIES,
    SEVERITIES,
    VALUATION_MODEL_TYPES,
)
from src.logging_setup import get_logger
from src.research import assumptions as asm
from src.research import evidence as ev
from src.research import items, kpis
from src.research.decisions import record_decision
from src.research.investments import ResearchError, create_investment, get_by_ticker
from src.research.kpis import KPI_FREQUENCIES
from src.research.predictions import create_prediction
from src.research.theses import active_thesis, create_thesis
from src.research.valuation import PROBABILITY_TOLERANCE, add_scenario, create_model

log = get_logger("research.importer")

CREATED_BY = "IMPORT"


class ImportError_(ResearchError):
    """Raised for any import-level failure (parsing, resolution, safety rules)."""


# --------------------------------------------------------------------------- schema


def _upper(v: str | None) -> str | None:
    return v.strip().upper() if isinstance(v, str) else v


def _lower(v: str | None) -> str | None:
    return v.strip().lower() if isinstance(v, str) else v


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")  # unknown fields are a validation error


class InvestmentSpec(_Model):
    symbol: str
    name: Optional[str] = None
    lifecycle_status: str = "RESEARCHING"
    review_frequency: str = "quarterly"
    next_review_date: Optional[date] = None
    time_horizon: Optional[str] = None  # informational; stored in notes
    notes: Optional[str] = None

    @field_validator("lifecycle_status")
    @classmethod
    def _status(cls, v: str) -> str:
        v = _upper(v)
        if v not in INVESTMENT_STATUSES:
            raise ValueError(f"invalid lifecycle_status {v!r}; allowed: {INVESTMENT_STATUSES}")
        return v

    @field_validator("review_frequency")
    @classmethod
    def _freq(cls, v: str) -> str:
        v = _lower(v)
        if v not in REVIEW_FREQUENCIES:
            raise ValueError(f"invalid review_frequency {v!r}; allowed: {REVIEW_FREQUENCIES}")
        return v


class InstrumentSpec(_Model):
    symbol: str
    exchange: Optional[str] = None
    currency: Optional[str] = None
    asset_type: str = "stock"


class ThesisSpec(_Model):
    title: str
    summary: Optional[str] = None
    core_thesis: Optional[str] = None
    market_expectation: Optional[str] = None
    our_expectation: Optional[str] = None
    variant_perception: Optional[str] = None
    why_market_may_be_wrong: Optional[str] = None
    expected_return_summary: Optional[str] = None
    confidence: Optional[int] = Field(None, ge=0, le=100)
    time_horizon: Optional[str] = None
    review_date: Optional[date] = None
    reason_for_revision: Optional[str] = None  # informational; v1 is always "original"


class AssumptionSpec(_Model):
    name: str
    description: Optional[str] = None
    category: str = "other"
    importance: str = "MEDIUM"
    status: str = "UNKNOWN"
    confidence: Optional[int] = Field(None, ge=0, le=100)
    expected_value: Optional[float] = None
    expected_min: Optional[float] = None
    expected_max: Optional[float] = None
    unit: Optional[str] = None
    time_horizon: Optional[str] = None
    breaker_condition: Optional[str] = None
    kpi_name: Optional[str] = None  # link to a KPI defined in the same file (or existing)
    notes: Optional[str] = None

    @field_validator("category")
    @classmethod
    def _cat(cls, v: str) -> str:
        v = _lower(v)
        if v not in ASSUMPTION_CATEGORIES:
            raise ValueError(f"invalid category {v!r}; allowed: {ASSUMPTION_CATEGORIES}")
        return v

    @field_validator("importance")
    @classmethod
    def _imp(cls, v: str) -> str:
        v = _upper(v)
        if v not in IMPORTANCES:
            raise ValueError(f"invalid importance {v!r}; allowed: {IMPORTANCES}")
        return v

    @field_validator("status")
    @classmethod
    def _st(cls, v: str) -> str:
        v = _upper(v)
        if v not in ASSUMPTION_STATUSES:
            raise ValueError(f"invalid status {v!r}; allowed: {ASSUMPTION_STATUSES}")
        return v


class RiskSpec(_Model):
    name: str
    description: Optional[str] = None
    category: str = "other"
    probability: Optional[int] = Field(None, ge=0, le=100)
    impact: Optional[str] = None
    severity: str = "MEDIUM"
    mitigation: Optional[str] = None
    status: Literal["OPEN"] = "OPEN"  # imports create open risks only

    @field_validator("category")
    @classmethod
    def _cat(cls, v: str) -> str:
        v = _lower(v)
        if v not in RISK_CATEGORIES:
            raise ValueError(f"invalid category {v!r}; allowed: {RISK_CATEGORIES}")
        return v

    @field_validator("severity")
    @classmethod
    def _sev(cls, v: str) -> str:
        v = _upper(v)
        if v not in SEVERITIES:
            raise ValueError(f"invalid severity {v!r}; allowed: {SEVERITIES}")
        return v

    @field_validator("impact")
    @classmethod
    def _impact(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = _upper(v)
        if v not in IMPORTANCES:
            raise ValueError(f"invalid impact {v!r}; allowed: {IMPORTANCES}")
        return v


class BreakerSpec(_Model):
    name: str
    description: Optional[str] = None
    severity: str = "HIGH"
    condition_text: Optional[str] = None

    @field_validator("severity")
    @classmethod
    def _sev(cls, v: str) -> str:
        v = _upper(v)
        if v not in SEVERITIES:
            raise ValueError(f"invalid severity {v!r}; allowed: {SEVERITIES}")
        return v


class CatalystSpec(_Model):
    name: str
    description: Optional[str] = None
    expected_date: Optional[date] = None
    probability: Optional[int] = Field(None, ge=0, le=100)
    potential_impact: Optional[str] = None
    status: str = "PENDING"

    @field_validator("status")
    @classmethod
    def _st(cls, v: str) -> str:
        v = _upper(v)
        if v == "OPEN":  # friendly alias from the authoring schema
            v = "PENDING"
        if v != "PENDING":
            raise ValueError("imported catalysts must be PENDING (resolve them later in the UI)")
        return v


class KpiObservationSpec(_Model):
    period: str
    value: float
    period_date: Optional[date] = None
    reported_at: Optional[date] = None
    source: Optional[str] = None
    source_reference: Optional[str] = None
    notes: Optional[str] = None


class KpiSpec(_Model):
    name: str
    description: Optional[str] = None
    unit: Optional[str] = None
    frequency: str = "quarterly"
    importance: str = "MEDIUM"
    direction_good: Optional[str] = None
    thesis_relevance: Optional[str] = None
    source_preference: Optional[str] = None
    observations: list[KpiObservationSpec] = Field(default_factory=list)

    @field_validator("frequency")
    @classmethod
    def _freq(cls, v: str) -> str:
        v = _lower(v)
        if v not in KPI_FREQUENCIES:
            raise ValueError(f"invalid frequency {v!r}; allowed: {KPI_FREQUENCIES}")
        return v

    @field_validator("importance")
    @classmethod
    def _imp(cls, v: str) -> str:
        v = _upper(v)
        if v not in IMPORTANCES:
            raise ValueError(f"invalid importance {v!r}; allowed: {IMPORTANCES}")
        return v

    @field_validator("direction_good")
    @classmethod
    def _dir(cls, v: str | None) -> str | None:
        if v is None:
            return None
        aliases = {"higher": "up", "lower": "down", "up": "up", "down": "down", "range": "range"}
        vl = _lower(v)
        if vl not in aliases:
            raise ValueError(f"invalid direction_good {v!r}; allowed: up/down/range (or HIGHER/LOWER)")
        return aliases[vl]


class EvidenceSpec(_Model):
    title: str
    target_type: str = "investment"
    target_name: Optional[str] = None  # human-readable reference (assumption/risk/catalyst name)
    direction: str = "NEUTRAL"
    evidence_type: str = "manual"
    summary: Optional[str] = None
    source_name: Optional[str] = None
    source_url: Optional[str] = None
    source_date: Optional[date] = None
    event_date: Optional[date] = None
    reliability: Optional[str] = None
    importance: str = "MEDIUM"
    notes: Optional[str] = None

    @field_validator("target_type")
    @classmethod
    def _tt(cls, v: str) -> str:
        v = _lower(v)
        if v not in EVIDENCE_TARGETS:
            raise ValueError(f"invalid target_type {v!r}; allowed: {EVIDENCE_TARGETS}")
        return v

    @field_validator("direction")
    @classmethod
    def _dir(cls, v: str) -> str:
        v = _upper(v)
        if v not in DIRECTIONS:
            raise ValueError(f"invalid direction {v!r}; allowed: {DIRECTIONS}")
        return v

    @field_validator("evidence_type")
    @classmethod
    def _et(cls, v: str) -> str:
        v = _lower(v)
        if v not in EVIDENCE_TYPES:
            raise ValueError(f"invalid evidence_type {v!r}; allowed: {EVIDENCE_TYPES}")
        return v

    @field_validator("reliability", "importance")
    @classmethod
    def _rel(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = _upper(v)
        if v not in IMPORTANCES:
            raise ValueError(f"invalid reliability/importance {v!r}; allowed: {IMPORTANCES} "
                             "(e.g. use HIGH for primary sources)")
        return v

    @model_validator(mode="after")
    def _needs_name(self):
        if self.target_type in ("assumption", "risk", "catalyst") and not self.target_name:
            raise ValueError(f"target_type {self.target_type} requires target_name")
        return self


class ScenarioSpec(_Model):
    scenario_name: str
    target_price: float = Field(gt=0)
    probability: Optional[float] = Field(None, ge=0, le=100)
    time_horizon_months: Optional[int] = Field(None, ge=0)
    dividends: Optional[float] = None
    notes: Optional[str] = None

    @field_validator("scenario_name")
    @classmethod
    def _name(cls, v: str) -> str:
        return _lower(v)


class ValuationModelSpec(_Model):
    name: str
    model_type: str = "custom"
    reference_price: Optional[float] = Field(None, gt=0)
    currency: Optional[str] = None
    reference_date: Optional[date] = None
    notes: Optional[str] = None
    scenarios: list[ScenarioSpec] = Field(default_factory=list)

    @field_validator("model_type")
    @classmethod
    def _mt(cls, v: str) -> str:
        v = _lower(v)
        if v not in VALUATION_MODEL_TYPES:
            raise ValueError(f"invalid model_type {v!r}; allowed: {VALUATION_MODEL_TYPES}")
        return v

    @model_validator(mode="after")
    def _probs(self):
        probs = [s.probability for s in self.scenarios]
        if probs and all(p is not None for p in probs):
            total = sum(Decimal(str(p)) for p in probs)
            if abs(total - 100) > PROBABILITY_TOLERANCE:
                raise ValueError(
                    f"scenario probabilities sum to {total}, must be 100 +/- {PROBABILITY_TOLERANCE}"
                )
        names = [s.scenario_name for s in self.scenarios]
        if len(names) != len(set(names)):
            raise ValueError("duplicate scenario_name within one model")
        return self


class PredictionSpec(_Model):
    statement: str
    probability: int = Field(ge=0, le=100)  # REQUIRED (v2 predictions always carry a probability)
    category: Optional[str] = None
    resolution_date: Optional[date] = None
    resolution_condition: Optional[str] = None
    notes: Optional[str] = None


class DecisionSpec(_Model):
    decision_type: str
    decided_at: Optional[datetime] = None
    reasoning: Optional[str] = None
    confidence: Optional[int] = Field(None, ge=0, le=100)
    time_horizon: Optional[str] = None
    expected_outcome: Optional[str] = None
    what_would_make_this_wrong: Optional[str] = None
    alternative_considered: Optional[str] = None
    key_risks: Optional[str] = None
    information_available_summary: Optional[str] = None
    intended_position_change: Optional[str] = None
    instrument_price: Optional[float] = None  # informational only; snapshot uses the price cache

    @field_validator("decision_type")
    @classmethod
    def _dt(cls, v: str) -> str:
        v = _upper(v)
        if v not in DECISION_TYPES:
            raise ValueError(f"invalid decision_type {v!r}; allowed: {DECISION_TYPES}")
        return v

    @field_validator("decided_at", mode="before")
    @classmethod
    def _date(cls, v):
        if isinstance(v, date) and not isinstance(v, datetime):
            return datetime(v.year, v.month, v.day)
        return v


class ResearchImportFile(_Model):
    schema_version: int = 1
    investment: InvestmentSpec
    instrument: Optional[InstrumentSpec] = None
    thesis: Optional[ThesisSpec] = None
    assumptions: list[AssumptionSpec] = Field(default_factory=list)
    risks: list[RiskSpec] = Field(default_factory=list)
    thesis_breakers: list[BreakerSpec] = Field(default_factory=list)
    catalysts: list[CatalystSpec] = Field(default_factory=list)
    kpis: list[KpiSpec] = Field(default_factory=list)
    evidence: list[EvidenceSpec] = Field(default_factory=list)
    valuation: Optional[dict] = None  # {"models": [...]} - normalized below
    predictions: list[PredictionSpec] = Field(default_factory=list)
    decisions: list[DecisionSpec] = Field(default_factory=list)

    @field_validator("schema_version")
    @classmethod
    def _ver(cls, v: int) -> int:
        if v != 1:
            raise ValueError(f"unsupported schema_version {v}; this importer supports 1")
        return v

    @model_validator(mode="after")
    def _cross(self):
        if self.assumptions and self.thesis is None:
            # allowed only when importing into an investment that already has a thesis
            pass
        kpi_names = {k.name for k in self.kpis}
        if len(kpi_names) != len(self.kpis):
            raise ValueError("duplicate KPI names in file")
        for a in self.assumptions:
            if a.kpi_name and a.kpi_name not in kpi_names:
                raise ValueError(f"assumption {a.name!r} references unknown kpi_name {a.kpi_name!r}")
        return self

    @property
    def valuation_models(self) -> list[ValuationModelSpec]:
        if not self.valuation:
            return []
        models = self.valuation.get("models")
        if models is None or set(self.valuation) - {"models"}:
            raise ImportError_("valuation section must be exactly {models: [...]}")
        return [ValuationModelSpec.model_validate(m) for m in models]


# --------------------------------------------------------------------------- loading


def load_research_file(path: str | Path) -> tuple[ResearchImportFile, str]:
    """Parse + validate a YAML/JSON research file. Returns (spec, sha256 of the raw bytes)."""
    p = Path(path)
    raw = p.read_bytes()
    digest = sha256_bytes(raw)
    try:
        if p.suffix.lower() == ".json":
            data = json.loads(raw.decode("utf-8"))
        else:
            data = yaml.safe_load(raw.decode("utf-8"))
    except (yaml.YAMLError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ImportError_(f"cannot parse {p.name}: {exc}") from exc
    if not isinstance(data, dict):
        raise ImportError_(f"{p.name}: top level must be a mapping, got {type(data).__name__}")
    try:
        spec = ResearchImportFile.model_validate(data)
        spec.valuation_models  # force validation of the valuation section
    except ValidationError as exc:
        lines = [f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()]
        raise ImportError_(f"{p.name} failed validation:\n  " + "\n  ".join(lines)) from exc
    return spec, digest


# --------------------------------------------------------------------------- instrument resolution


def resolve_import_instrument(
    session: Session, spec: ResearchImportFile
) -> tuple[Instrument | None, str]:
    """Link to an existing v1 instrument via the existing resolution layer. Never creates one.
    Returns (instrument | None, human-readable note). Ambiguity aborts."""
    from sqlalchemy import select

    symbol = (spec.instrument.symbol if spec.instrument else spec.investment.symbol).strip().upper()
    candidates = list(
        session.scalars(
            select(Instrument).where(Instrument.symbol == symbol, Instrument.asset_type != "cash")
        )
    )
    if spec.instrument and (spec.instrument.exchange or spec.instrument.currency):
        ref = InstrumentRef(
            symbol=symbol,
            currency=(spec.instrument.currency or "USD").upper(),
            asset_type=spec.instrument.asset_type,
            exchange=spec.instrument.exchange,
        )
        from src.portfolio.instruments import resolve_instrument

        inst = resolve_instrument(session, ref, create=False)
        if inst is not None:
            return inst, f"linked to existing instrument id={inst.id} ({inst.symbol}/{inst.exchange}/{inst.currency})"
        # explicit identity given but not found: fall back to unambiguous symbol match
    if len(candidates) == 1:
        inst = candidates[0]
        return inst, f"linked to existing instrument id={inst.id} ({inst.symbol}/{inst.exchange}/{inst.currency})"
    if len(candidates) > 1:
        detail = ", ".join(f"id={i.id} {i.symbol}/{i.exchange}/{i.currency}" for i in candidates)
        raise ImportError_(
            f"instrument resolution ambiguous for symbol {symbol}: {detail}. "
            "Specify instrument.exchange and instrument.currency to disambiguate."
        )
    return None, "no matching portfolio instrument - investment created without instrument link"


# --------------------------------------------------------------------------- apply


@dataclass
class ImportReport:
    ticker: str = ""
    name: str | None = None
    instrument_note: str = ""
    thesis_note: str = "none"
    counts: dict[str, int] = dfield(default_factory=dict)
    warnings: list[str] = dfield(default_factory=list)
    dry_run: bool = False

    def format(self) -> str:
        head = "DRY RUN — no changes written" if self.dry_run else "Research import successful"
        lines = [head, "", f"Investment: {self.ticker} / {self.name or '-'}", f"Instrument: {self.instrument_note}",
                 f"Thesis: {self.thesis_note}"]
        labels = [
            ("assumptions", "Assumptions"), ("risks", "Risks"), ("breakers", "Breakers"),
            ("catalysts", "Catalysts"), ("kpis", "KPIs"), ("kpi_observations", "KPI observations"),
            ("evidence", "Evidence"), ("valuation_models", "Valuation models"), ("scenarios", "Scenarios"),
            ("predictions", "Predictions"), ("decisions", "Decisions"),
        ]
        for key, label in labels:
            if self.counts.get(key):
                lines.append(f"{label}: {self.counts[key]}")
        for w in self.warnings:
            lines.append(f"warning: {w}")
        return "\n".join(lines)


def plan_import(session: Session, spec: ResearchImportFile) -> ImportReport:
    """Dry run: validate, resolve instrument, report what WOULD be created. No writes."""
    report = _precheck(session, spec, allow_existing=False, dry=True)
    return report


def _precheck(session: Session, spec: ResearchImportFile, allow_existing: bool, dry: bool) -> ImportReport:
    report = ImportReport(ticker=spec.investment.symbol.strip().upper(), name=spec.investment.name, dry_run=dry)
    existing = get_by_ticker(session, spec.investment.symbol)
    if existing is not None:
        if not allow_existing:
            raise ImportError_(
                f"investment {report.ticker} already exists (id={existing.id}, status={existing.status}). "
                "Re-run with --allow-existing to add missing non-thesis records; a thesis is never overwritten."
            )
        report.warnings.append(f"investment {report.ticker} already exists - reusing it (status unchanged)")
    if spec.thesis is not None:
        has_thesis = existing is not None and active_thesis(session, existing) is not None
        if has_thesis:
            raise ImportError_(
                f"investment {report.ticker} already has a thesis. The importer never overwrites or "
                "revises a thesis - remove the thesis section from the file, or revise the thesis "
                "manually in the dashboard (immutable versioning)."
            )
        report.thesis_note = "v1 would be created" if dry else "v1 created"
    elif spec.assumptions:
        has_thesis = existing is not None and active_thesis(session, existing) is not None
        if not has_thesis:
            raise ImportError_("assumptions require a thesis: add a thesis section to the file")
        report.thesis_note = "existing thesis reused for assumptions"
    inst, note = resolve_import_instrument(session, spec)
    report.instrument_note = note
    models = spec.valuation_models
    report.counts = {
        "assumptions": len(spec.assumptions),
        "risks": len(spec.risks),
        "breakers": len(spec.thesis_breakers),
        "catalysts": len(spec.catalysts),
        "kpis": len(spec.kpis),
        "kpi_observations": sum(len(k.observations) for k in spec.kpis),
        "evidence": len(spec.evidence),
        "valuation_models": len(models),
        "scenarios": sum(len(m.scenarios) for m in models),
        "predictions": len(spec.predictions),
        "decisions": len(spec.decisions),
    }
    return report


def apply_import(
    session: Session,
    spec: ResearchImportFile,
    base_currency: str,
    allow_existing: bool = False,
    source_file: str | None = None,
) -> ImportReport:
    """Create everything through the v2 service layer. Caller provides the transaction scope."""
    report = _precheck(session, spec, allow_existing=allow_existing, dry=False)
    inv = get_by_ticker(session, spec.investment.symbol)
    inst, _note = resolve_import_instrument(session, spec)

    if inv is None:
        notes = spec.investment.notes or ""
        if spec.investment.time_horizon:
            notes = (notes + f"\nTime horizon: {spec.investment.time_horizon}").strip()
        inv = create_investment(
            session,
            spec.investment.symbol,
            name=spec.investment.name,
            status=spec.investment.lifecycle_status,
            instrument_id=inst.id if inst else None,
            review_frequency=spec.investment.review_frequency,
            next_review_date=spec.investment.next_review_date,
            notes=notes or None,
            created_by=CREATED_BY,
        )

    thesis = active_thesis(session, inv)
    if spec.thesis is not None:
        t = spec.thesis
        thesis, _v1 = create_thesis(
            session, inv, t.title, created_by=CREATED_BY,
            summary=t.summary, core_thesis=t.core_thesis,
            market_expectation=t.market_expectation, our_expectation=t.our_expectation,
            variant_perception=t.variant_perception, why_market_may_be_wrong=t.why_market_may_be_wrong,
            expected_return_summary=t.expected_return_summary, confidence=t.confidence,
            time_horizon=t.time_horizon, review_date=t.review_date,
        )

    kpi_by_name: dict[str, Any] = {k.name: k for k in kpis.list_kpis(session, inv)}
    n_obs = 0
    for k in spec.kpis:
        kpi = kpis.add_kpi(
            session, inv, k.name, description=k.description, unit=k.unit, frequency=k.frequency,
            importance=k.importance, direction_good=k.direction_good,
            thesis_relevance=k.thesis_relevance, source_preference=k.source_preference,
            created_by=CREATED_BY,
        )
        kpi_by_name[k.name] = kpi
        for o in k.observations:
            kpis.add_observation(
                session, kpi, o.period, o.value, period_date=o.period_date, reported_at=o.reported_at,
                source=o.source, source_reference=o.source_reference, notes=o.notes, created_by=CREATED_BY,
            )
            n_obs += 1
    report.counts["kpi_observations"] = n_obs

    assumption_by_name: dict[str, Any] = {}
    if spec.assumptions:
        if thesis is None:
            raise ImportError_("assumptions require a thesis")
        for a in spec.assumptions:
            row = asm.add_assumption(
                session, thesis, a.name, description=a.description, category=a.category,
                importance=a.importance, status=a.status, confidence=a.confidence,
                expected_value=a.expected_value, expected_min=a.expected_min, expected_max=a.expected_max,
                unit=a.unit, time_horizon=a.time_horizon, breaker_condition=a.breaker_condition,
                kpi_id=kpi_by_name[a.kpi_name].id if a.kpi_name else None,
                notes=a.notes, created_by=CREATED_BY,
            )
            if a.name in assumption_by_name:
                raise ImportError_(f"duplicate assumption name {a.name!r} in file")
            assumption_by_name[a.name] = row

    risk_by_name: dict[str, Any] = {}
    for r in spec.risks:
        risk_by_name[r.name] = items.add_risk(
            session, inv, r.name, description=r.description, category=r.category,
            probability=r.probability, impact=r.impact, severity=r.severity,
            mitigation=r.mitigation, created_by=CREATED_BY,
        )

    for b in spec.thesis_breakers:
        items.add_breaker(
            session, inv, b.name, condition_text=b.condition_text, description=b.description,
            severity=b.severity, thesis_id=thesis.id if thesis else None, created_by=CREATED_BY,
        )

    catalyst_by_name: dict[str, Any] = {}
    for c in spec.catalysts:
        catalyst_by_name[c.name] = items.add_catalyst(
            session, inv, c.name, description=c.description, expected_date=c.expected_date,
            probability=c.probability, potential_impact=c.potential_impact, created_by=CREATED_BY,
        )

    for e in spec.evidence:
        target_id = None
        if e.target_type == "assumption":
            target = assumption_by_name.get(e.target_name)
            if target is None:
                raise ImportError_(
                    f"evidence {e.title!r}: assumption {e.target_name!r} not found in this file"
                )
            target_id = target.id
        elif e.target_type == "risk":
            target = risk_by_name.get(e.target_name)
            if target is None:
                raise ImportError_(f"evidence {e.title!r}: risk {e.target_name!r} not found in this file")
            target_id = target.id
        elif e.target_type == "catalyst":
            target = catalyst_by_name.get(e.target_name)
            if target is None:
                raise ImportError_(f"evidence {e.title!r}: catalyst {e.target_name!r} not found in this file")
            target_id = target.id
        elif e.target_type == "thesis":
            if thesis is None:
                raise ImportError_(f"evidence {e.title!r} targets the thesis but no thesis exists")
            target_id = thesis.id
        ev.add_evidence(
            session, inv, e.title, direction=e.direction, evidence_type=e.evidence_type,
            target_type=e.target_type, target_id=target_id,
            thesis_version_id=thesis.current_version_id if thesis else None,
            summary=e.summary, source_url=e.source_url, source_name=e.source_name,
            source_date=e.source_date, event_date=e.event_date, reliability=e.reliability,
            importance=e.importance, notes=e.notes, created_by=CREATED_BY,
        )

    n_scen = 0
    for m in spec.valuation_models:
        model = create_model(
            session, inv, m.name, model_type=m.model_type, reference_price=m.reference_price,
            reference_currency=m.currency, reference_date=m.reference_date, notes=m.notes,
            created_by=CREATED_BY,
        )
        for sc in m.scenarios:
            add_scenario(
                session, model, sc.scenario_name, sc.target_price, probability=sc.probability,
                time_horizon_months=sc.time_horizon_months, expected_dividends=sc.dividends,
                notes=sc.notes,
            )
            n_scen += 1
    report.counts["scenarios"] = n_scen

    for p in spec.predictions:
        create_prediction(
            session, p.statement, p.probability, investment=inv,
            thesis_id=thesis.id if thesis else None, category=p.category,
            resolution_date=p.resolution_date, resolution_condition=p.resolution_condition,
            notes=p.notes, created_by=CREATED_BY,
        )

    for d in spec.decisions:
        # Snapshot comes from record_decision: deterministic context as of decided_at from the
        # price/valuation cache; unavailable values stay None (never fabricated from today).
        record_decision(
            session, inv, d.decision_type, base_currency=base_currency,
            decided_at=d.decided_at, created_by=CREATED_BY,
            reasoning=d.reasoning, confidence=d.confidence, time_horizon=d.time_horizon,
            expected_outcome=d.expected_outcome, what_would_make_this_wrong=d.what_would_make_this_wrong,
            alternative_considered=d.alternative_considered, key_risks=d.key_risks,
            information_available_summary=d.information_available_summary,
            intended_position_change=d.intended_position_change,
        )
        if d.instrument_price is not None:
            when = d.decided_at.strftime("%Y-%m-%d") if d.decided_at else "today"
            report.warnings.append(
                f"decision {d.decision_type} ({when}): instrument_price in the file is informational "
                "only; the snapshot uses the deterministic price cache"
            )

    report.name = inv.name
    log.info(
        "import-research: %s from %s -> thesis=%s counts=%s",
        report.ticker, source_file, report.thesis_note, report.counts,
    )
    return report
