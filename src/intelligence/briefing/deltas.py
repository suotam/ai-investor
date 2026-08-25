"""Deterministic delta engine: compare current state against the last brief checkpoint.

Python decides what is new/changed - never the LLM. Every Delta carries:
  * item_key   - stable suppression identity,
  * reason     - "why am I seeing this" (trust),
  * source_refs- table:id provenance for auditability,
  * severity   - deterministic materiality.

Static state (long-standing risks, unchanged breakers, the thesis text) is deliberately NOT
emitted; it reappears only via a real change (new evidence targeting it, escalation, review
due, threshold crossing).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.config import Settings
from src.core import D
from src.db.briefing import BriefRun
from src.db.intelligence import (
    AiProposal,
    InsiderTransaction,
    IntelligenceEvent,
    InvestmentMacroLink,
    MacroSeries,
    ResearchCandidate,
)
from src.db.research import (
    Catalyst,
    Decision,
    Evidence,
    Investment,
    InvestmentKpi,
    KpiObservation,
    Prediction,
    Risk,
    ThesisAssumption,
    ThesisBreaker,
)
from src.intelligence.connectors.macro import latest_observations
from src.market_data.service import PriceStore
from src.portfolio.valuation import value_portfolio


@dataclass
class Delta:
    delta_type: str
    item_key: str
    title: str
    severity: str = "LOW"  # LOW | MEDIUM | HIGH
    investment_id: int | None = None
    detail: str | None = None
    reason: str | None = None
    source_refs: list[str] = field(default_factory=list)
    payload: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "delta_type": self.delta_type, "item_key": self.item_key, "title": self.title,
            "severity": self.severity, "investment_id": self.investment_id,
            "detail": self.detail, "reason": self.reason, "source_refs": self.source_refs,
        }


SEV_ORDER = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}


def _in_window(ts: datetime | None, since: datetime, until: datetime) -> bool:
    return ts is not None and since < ts <= until


def compute_deltas(
    session: Session,
    settings: Settings,
    since: datetime,
    until: datetime,
    previous_state: dict | None = None,
) -> tuple[list[Delta], dict]:
    """Returns (deltas, current_portfolio_state). current state is stored on the run and
    becomes `previous_state` for the next run (weights, prices, valuation states)."""
    deltas: list[Delta] = []
    investments = {i.id: i for i in session.scalars(select(Investment))}
    inv_by_instrument = {i.instrument_id: i for i in investments.values() if i.instrument_id}
    thesis_owner = _thesis_owner_map(session, investments)

    _detect_events(session, deltas, since, until, settings)
    _detect_evidence(session, deltas, since, until, investments, thesis_owner)
    _detect_kpi_observations(session, deltas, since, until, investments, thesis_owner)
    _detect_assumptions(session, deltas, since, until, thesis_owner)
    _detect_risks(session, deltas, since, until, investments)
    _detect_breakers(session, deltas, since, until, investments)
    _detect_proposals(session, deltas, since, until, investments)
    _detect_decisions(session, deltas, since, until, investments)
    _detect_predictions(session, deltas, since, until, investments)
    _detect_reviews(session, deltas, investments, until)
    _detect_candidates(session, deltas, since, until)
    _detect_macro(session, deltas, since, until, investments, settings)
    current_state = _detect_portfolio_and_prices(
        session, deltas, settings, until, previous_state or {}, investments, inv_by_instrument
    )

    deltas.sort(key=lambda d: (-SEV_ORDER.get(d.severity, 0), d.delta_type, d.item_key))
    return deltas, current_state


def _thesis_owner_map(session: Session, investments: dict) -> dict[int, Investment]:
    from src.db.research import Thesis

    return {
        t.id: investments[t.investment_id]
        for t in session.scalars(select(Thesis))
        if t.investment_id in investments
    }


# --- detectors ---------------------------------------------------------------


def _detect_events(session, deltas, since, until, settings) -> None:
    for e in session.scalars(
        select(IntelligenceEvent).where(
            IntelligenceEvent.created_at > since,
            IntelligenceEvent.created_at <= until,
            IntelligenceEvent.processing_state != "DISMISSED",
        )
    ):
        if e.event_type == "INSIDER_TRANSACTION":
            continue  # contextualized separately below (grouped, with ownership context)
        if e.event_type == "MACRO_RELEASE":
            continue  # macro handled with thresholds and link context
        deltas.append(
            Delta(
                delta_type="NEW_EVENT",
                item_key=f"event:{e.id}",
                title=e.title,
                severity=e.severity,
                investment_id=e.investment_id,
                detail=e.summary,
                reason=f"{e.event_type.replace('_', ' ').capitalize()} recorded in this window.",
                source_refs=[f"intelligence_events:{e.id}"]
                + ([f"source_documents:{e.source_document_id}"] if e.source_document_id else []),
            )
        )
    # insider transactions: grouped per issuer with deterministic context, materiality gate
    rows = [
        t
        for t in session.scalars(
            select(InsiderTransaction).where(
                InsiderTransaction.created_at > since, InsiderTransaction.created_at <= until
            )
        )
        if t.transaction_type in ("open_market_purchase", "open_market_sale")
    ]
    by_issuer: dict[str, list[InsiderTransaction]] = {}
    for t in rows:
        by_issuer.setdefault(t.issuer_cik, []).append(t)
    for cik, txs in by_issuer.items():
        total_value = sum(t.value or 0 for t in txs)
        material = total_value >= settings.brief_insider_min_value_usd
        buys = [t for t in txs if t.transaction_type == "open_market_purchase"]
        sells = [t for t in txs if t.transaction_type == "open_market_sale"]
        insiders = sorted({t.insider_name for t in txs})
        # context: size relative to remaining holdings where reported
        rel_parts = []
        for t in txs:
            if t.shares and t.shares_after is not None and (t.shares + t.shares_after) > 0:
                pct = 100 * t.shares / (t.shares + t.shares_after)
                rel_parts.append(f"{t.insider_name}: {pct:.1f}% of reported holdings")
        issuer = txs[0].issuer_name or cik
        kinds = []
        if buys:
            kinds.append(f"{len(buys)} open-market purchase(s)")
        if sells:
            kinds.append(f"{len(sells)} open-market sale(s)")
        deltas.append(
            Delta(
                delta_type="NEW_INSIDER_ACTIVITY",
                item_key=f"insider:{cik}:{min(t.id for t in txs)}-{max(t.id for t in txs)}",
                title=f"{issuer}: {' and '.join(kinds)} by {len(insiders)} insider(s), ~${total_value:,.0f}",
                severity="MEDIUM" if material else "LOW",
                investment_id=txs[0].investment_id,
                detail=(
                    ("; ".join(rel_parts) + ". " if rel_parts else "")
                    + "Open-market transactions; sales are not automatically bearish "
                    "(exercise/tax/10b5-1 context may apply)."
                ),
                reason=(
                    f"Insider open-market activity totaling ${total_value:,.0f} "
                    + ("meets" if material else "is below")
                    + f" the ${settings.brief_insider_min_value_usd:,.0f} materiality threshold."
                ),
                source_refs=[f"insider_transactions:{t.id}" for t in txs],
            )
        )


def _detect_evidence(session, deltas, since, until, investments, thesis_owner) -> None:
    for e in session.scalars(
        select(Evidence).where(Evidence.created_at > since, Evidence.created_at <= until)
    ):
        inv = investments.get(e.investment_id)
        target_note = ""
        reason = f"New {e.direction.lower()} evidence added in this window."
        if e.target_type == "assumption" and e.target_id:
            a = session.get(ThesisAssumption, e.target_id)
            if a:
                target_note = f" (assumption: {a.name})"
                reason = f"Shown because this evidence affects assumption '{a.name}'."
        elif e.target_type == "risk" and e.target_id:
            r = session.get(Risk, e.target_id)
            if r:
                target_note = f" (risk: {r.name})"
                reason = f"Shown because this evidence is relevant to risk '{r.name}'."
        title = f"{inv.ticker + ': ' if inv else ''}{e.direction.lower()} evidence — {e.title}{target_note}"
        deltas.append(
            Delta(
                delta_type="NEW_EVIDENCE",
                item_key=f"evidence:{e.id}",
                title=title,
                severity="HIGH" if e.direction == "CONTRADICTING" else "MEDIUM",
                investment_id=e.investment_id,
                detail=e.summary,
                reason=reason,
                source_refs=[f"evidence:{e.id}"],
                payload={"direction": e.direction},
            )
        )


def _detect_kpi_observations(session, deltas, since, until, investments, thesis_owner) -> None:
    kpis = {k.id: k for k in session.scalars(select(InvestmentKpi))}
    for o in session.scalars(
        select(KpiObservation).where(KpiObservation.created_at > since, KpiObservation.created_at <= until)
    ):
        kpi = kpis.get(o.kpi_id)
        if kpi is None:
            continue
        inv = investments.get(kpi.investment_id)
        prev = _previous_observation(session, o)
        change_txt = ""
        if prev is not None and prev.value:
            diff = o.value - prev.value
            if (kpi.unit or "").strip() == "%":
                change_txt = f" ({prev.value:g}% -> {o.value:g}%, {diff:+.2f}pp vs {prev.period})"
            else:
                change_txt = f" ({prev.value:g} -> {o.value:g}, {100 * diff / prev.value:+.1f}% vs {prev.period})"
        linked = [
            a for a in session.scalars(
                select(ThesisAssumption).where(ThesisAssumption.kpi_id == kpi.id, ThesisAssumption.active.is_(True))
            )
        ]
        reason = "New KPI observation stored in this window."
        if linked:
            reason = f"Shown because KPI '{kpi.name}' is linked to assumption '{linked[0].name}'."
        deltas.append(
            Delta(
                delta_type="NEW_KPI",
                item_key=f"kpi_obs:{o.id}",
                title=f"{inv.ticker if inv else '?'}: {kpi.name} = {o.value:g}{kpi.unit or ''} [{o.period}]{change_txt}",
                severity="MEDIUM" if linked else "LOW",
                investment_id=kpi.investment_id,
                detail=f"Source: {o.source or 'manual'}" + (f" ({o.source_reference})" if o.source_reference else ""),
                reason=reason,
                source_refs=[f"kpi_observations:{o.id}"],
            )
        )


def _previous_observation(session, obs: KpiObservation) -> KpiObservation | None:
    rows = [
        r
        for r in session.scalars(
            select(KpiObservation).where(KpiObservation.kpi_id == obs.kpi_id, KpiObservation.id != obs.id)
        )
        if (r.period_date or date.min) <= (obs.period_date or date.max)
    ]
    rows.sort(key=lambda r: (r.period_date or date.min, r.period))
    return rows[-1] if rows else None


def _detect_assumptions(session, deltas, since, until, thesis_owner) -> None:
    for a in session.scalars(
        select(ThesisAssumption).where(
            ThesisAssumption.status_updated_at.isnot(None),
            ThesisAssumption.status_updated_at > since,
            ThesisAssumption.status_updated_at <= until,
        )
    ):
        inv = thesis_owner.get(a.thesis_id)
        deltas.append(
            Delta(
                delta_type="ASSUMPTION_STATUS_CHANGE",
                item_key=f"assumption:{a.id}:{a.status}:{a.status_updated_at:%Y%m%d%H%M}",
                title=f"{inv.ticker if inv else '?'}: assumption '{a.name}' is now {a.status}",
                severity="HIGH" if a.status in ("CHALLENGED", "BROKEN") else "MEDIUM",
                investment_id=inv.id if inv else None,
                reason="Shown because an assumption status changed (user-controlled state).",
                source_refs=[f"thesis_assumptions:{a.id}"],
            )
        )


def _detect_risks(session, deltas, since, until, investments) -> None:
    for r in session.scalars(select(Risk)):
        inv = investments.get(r.investment_id)
        if _in_window(r.created_at, since, until):
            deltas.append(
                Delta(
                    delta_type="NEW_RISK",
                    item_key=f"risk:{r.id}:new",
                    title=f"{inv.ticker if inv else '?'}: new risk '{r.name}' [{r.severity}]",
                    severity="HIGH" if r.severity in ("HIGH", "CRITICAL") else "MEDIUM",
                    investment_id=r.investment_id,
                    detail=r.description,
                    reason="Shown once because this risk was newly created; it will not repeat daily.",
                    source_refs=[f"risks:{r.id}"],
                )
            )
        elif _in_window(r.updated_at, since, until) and r.updated_at != r.created_at:
            kind = "RISK_RESOLUTION" if r.status != "OPEN" else "RISK_ESCALATION"
            deltas.append(
                Delta(
                    delta_type=kind,
                    item_key=f"risk:{r.id}:{r.status}:{r.updated_at:%Y%m%d%H%M}",
                    title=f"{inv.ticker if inv else '?'}: risk '{r.name}' updated -> {r.status} [{r.severity}]",
                    severity="MEDIUM",
                    investment_id=r.investment_id,
                    reason="Shown because this risk's state changed in this window.",
                    source_refs=[f"risks:{r.id}"],
                )
            )


def _detect_breakers(session, deltas, since, until, investments) -> None:
    for b in session.scalars(select(ThesisBreaker)):
        inv = investments.get(b.investment_id)
        if _in_window(b.triggered_at, since, until):
            deltas.append(
                Delta(
                    delta_type="BREAKER_TRIGGER",
                    item_key=f"breaker:{b.id}:triggered:{b.triggered_at:%Y%m%d}",
                    title=f"{inv.ticker if inv else '?'}: THESIS BREAKER TRIGGERED — {b.name}",
                    severity="HIGH",
                    investment_id=b.investment_id,
                    detail=b.condition_text,
                    reason="Shown because a thesis breaker was triggered.",
                    source_refs=[f"thesis_breakers:{b.id}"],
                )
            )
        elif _in_window(b.created_at, since, until):
            deltas.append(
                Delta(
                    delta_type="NEW_BREAKER",
                    item_key=f"breaker:{b.id}:new",
                    title=f"{inv.ticker if inv else '?'}: new thesis breaker defined — {b.name}",
                    severity="MEDIUM",
                    investment_id=b.investment_id,
                    reason="Shown once because this breaker was newly defined.",
                    source_refs=[f"thesis_breakers:{b.id}"],
                )
            )


def _detect_proposals(session, deltas, since, until, investments) -> None:
    for p in session.scalars(
        select(AiProposal).where(AiProposal.created_at > since, AiProposal.created_at <= until)
    ):
        inv = investments.get(p.investment_id)
        deltas.append(
            Delta(
                delta_type="NEW_PROPOSAL",
                item_key=f"proposal:{p.id}",
                title=f"{inv.ticker + ': ' if inv else ''}[{p.proposal_type}] {p.title}",
                severity="MEDIUM",
                investment_id=p.investment_id,
                detail=p.why_it_matters,
                reason="Awaiting your judgment - nothing is applied until you accept it.",
                source_refs=[f"ai_proposals:{p.id}"],
            )
        )
    for p in session.scalars(
        select(AiProposal).where(
            AiProposal.resolved_at.isnot(None), AiProposal.resolved_at > since, AiProposal.resolved_at <= until
        )
    ):
        deltas.append(
            Delta(
                delta_type="PROPOSAL_RESOLVED",
                item_key=f"proposal:{p.id}:{p.status}",
                title=f"Proposal #{p.id} {p.status}: {p.title}",
                severity="LOW",
                investment_id=p.investment_id,
                reason="Shown because a pending proposal was resolved in this window.",
                source_refs=[f"ai_proposals:{p.id}"],
            )
        )


def _detect_decisions(session, deltas, since, until, investments) -> None:
    for d in session.scalars(
        select(Decision).where(Decision.created_at > since, Decision.created_at <= until)
    ):
        inv = investments.get(d.investment_id)
        deltas.append(
            Delta(
                delta_type="NEW_DECISION",
                item_key=f"decision:{d.id}",
                title=f"{inv.ticker if inv else '?'}: decision recorded — {d.decision_type}",
                severity="MEDIUM",
                investment_id=d.investment_id,
                detail=(d.reasoning or "")[:200],
                reason="Shown because a decision journal entry was recorded.",
                source_refs=[f"decisions:{d.id}"],
            )
        )


def _detect_predictions(session, deltas, since, until, investments) -> None:
    today = until.date()
    for p in session.scalars(
        select(Prediction).where(
            Prediction.resolved_at.isnot(None), Prediction.resolved_at > since, Prediction.resolved_at <= until
        )
    ):
        inv = investments.get(p.investment_id)
        deltas.append(
            Delta(
                delta_type="PREDICTION_RESOLVED",
                item_key=f"prediction:{p.id}:{p.status}",
                title=f"{inv.ticker + ': ' if inv else ''}prediction {p.status}: {p.statement[:80]} (p was {p.probability}%)",
                severity="MEDIUM",
                investment_id=p.investment_id,
                reason="Shown because a prediction was resolved - calibration data point recorded.",
                source_refs=[f"predictions:{p.id}"],
            )
        )
    for p in session.scalars(
        select(Prediction).where(
            Prediction.status == "OPEN",
            Prediction.resolution_date.isnot(None),
            Prediction.resolution_date <= today + timedelta(days=3),
        )
    ):
        inv = investments.get(p.investment_id)
        deltas.append(
            Delta(
                delta_type="PREDICTION_DUE",
                item_key=f"prediction_due:{p.id}:{p.resolution_date}",
                title=f"{inv.ticker + ': ' if inv else ''}prediction due {p.resolution_date}: {p.statement[:80]}",
                severity="MEDIUM",
                investment_id=p.investment_id,
                reason="Shown because this prediction's resolution date arrived (surfaced once).",
                source_refs=[f"predictions:{p.id}"],
            )
        )


def _detect_reviews(session, deltas, investments, until) -> None:
    today = until.date()
    for inv in investments.values():
        if inv.status in ("ARCHIVED", "REJECTED"):
            continue
        if inv.next_review_date is not None and inv.next_review_date <= today:
            deltas.append(
                Delta(
                    delta_type="REVIEW_DUE",
                    item_key=f"review_due:{inv.ticker}:{inv.next_review_date}",
                    title=f"{inv.ticker}: scheduled thesis review is due ({inv.next_review_date})",
                    severity="MEDIUM",
                    investment_id=inv.id,
                    reason="Shown because the scheduled review date arrived (surfaced once until reviewed).",
                    source_refs=[f"investments:{inv.id}"],
                )
            )


def _detect_candidates(session, deltas, since, until) -> None:
    for c in session.scalars(
        select(ResearchCandidate).where(
            ResearchCandidate.discovered_at > since, ResearchCandidate.discovered_at <= until,
            ResearchCandidate.status == "NEW",
        )
    ):
        reasons = json.loads(c.reasons_json or "[]")
        deltas.append(
            Delta(
                delta_type="NEW_DISCOVERY_CANDIDATE",
                item_key=f"candidate:{c.id}",
                title=f"Research candidate: {c.ticker} ({c.source})",
                severity="LOW",
                detail="; ".join(reasons)[:300],
                reason="Shown because discovery produced a new research candidate.",
                source_refs=[f"research_candidates:{c.id}"],
            )
        )


def _detect_macro(session, deltas, since, until, investments, settings) -> None:
    links = list(session.scalars(select(InvestmentMacroLink)))
    if not links:
        return
    by_series: dict[int, list[InvestmentMacroLink]] = {}
    for link in links:
        by_series.setdefault(link.series_id, []).append(link)
    for series_id, series_links in by_series.items():
        series = session.get(MacroSeries, series_id)
        obs = latest_observations(session, series, n=3)
        if len(obs) < 2:
            continue
        latest, prev = obs[-1], obs[-2]
        if not _in_window(latest.retrieved_at, since, until):
            continue
        if prev.value == 0:
            continue
        change_pct = 100 * (latest.value - prev.value) / abs(prev.value)
        if abs(change_pct) < settings.brief_macro_change_pct:
            continue
        for link in series_links:
            inv = investments.get(link.investment_id)
            deltas.append(
                Delta(
                    delta_type="NEW_MACRO_OBSERVATION",
                    item_key=f"macro:{series.series_code}:{latest.obs_date}:{link.investment_id}",
                    title=f"{series.name}: {prev.value:g} -> {latest.value:g} ({change_pct:+.1f}%)",
                    severity="MEDIUM" if link.importance == "HIGH" else "LOW",
                    investment_id=link.investment_id,
                    detail=link.why_it_matters,
                    reason=f"Shown because this series is linked to {inv.ticker if inv else '?'}"
                           + (f" ({link.relationship_})" if link.relationship_ else "")
                           + f" and moved {change_pct:+.1f}% (threshold {settings.brief_macro_change_pct}%).",
                    source_refs=[f"macro_observations:{latest.id}"],
                )
            )


def _detect_portfolio_and_prices(
    session, deltas, settings, until, previous_state, investments, inv_by_instrument
) -> dict:
    """Price moves, weight changes, portfolio value change, valuation threshold states.
    Returns current state to store on the run."""
    val = value_portfolio(session, settings.base_currency, until.date())
    prices = PriceStore(session)
    state: dict = {"weights": {}, "prices": {}, "valuation_states": {}, "value": None}
    state["value"] = float(val.total_value_base) if val.total_value_base is not None else None

    prev_weights = previous_state.get("weights", {})
    prev_prices = previous_state.get("prices", {})
    prev_val_states = previous_state.get("valuation_states", {})
    prev_value = previous_state.get("value")

    # portfolio-level move
    if prev_value and state["value"] is not None:
        move = 100 * (state["value"] - prev_value) / prev_value
        if abs(move) >= settings.brief_portfolio_move_pct:
            deltas.append(
                Delta(
                    delta_type="PORTFOLIO_MOVE",
                    item_key=f"portfolio:{until.date()}",
                    title=f"Portfolio value moved {move:+.1f}% since the last brief "
                          f"({prev_value:,.0f} -> {state['value']:,.0f} {settings.base_currency})",
                    severity="MEDIUM",
                    reason=f"Shown because the move exceeds the {settings.brief_portfolio_move_pct}% threshold.",
                    source_refs=["portfolio_valuation:derived"],
                )
            )

    # per-position: weights + price moves (business-vs-price discipline)
    window_evidence_invs = {d.investment_id for d in deltas if d.delta_type in ("NEW_EVIDENCE", "NEW_EVENT") and d.investment_id}
    for r in val.positions:
        ticker = r.symbol
        w = float(r.weight) if r.weight is not None else None
        px = float(r.price) if r.price is not None else None
        state["weights"][ticker] = w
        state["prices"][ticker] = px
        inv = inv_by_instrument.get(r.instrument_id)
        prev_px = prev_prices.get(ticker)
        if px and prev_px:
            move = 100 * (px - prev_px) / prev_px
            if abs(move) >= settings.brief_price_move_pct:
                has_news = inv is not None and inv.id in window_evidence_invs
                discipline = (
                    "New company evidence arrived in the same window - see thesis section."
                    if has_news
                    else "No new company evidence in this window: price changed; thesis did not."
                )
                deltas.append(
                    Delta(
                        delta_type="PRICE_MOVE",
                        item_key=f"price:{ticker}:{until.date()}",
                        title=f"{ticker} price moved {move:+.1f}% since the last brief "
                              f"({prev_px:g} -> {px:g} {r.price_currency})",
                        severity="MEDIUM",
                        investment_id=inv.id if inv else None,
                        detail=discipline,
                        reason=f"Shown because the move exceeds the {settings.brief_price_move_pct}% threshold.",
                        source_refs=[f"prices:instrument:{r.instrument_id}"],
                        payload={"business_change": has_news},
                    )
                )
        prev_w = prev_weights.get(ticker)
        if w is not None and prev_w is not None:
            diff_pp = 100 * (w - prev_w)
            if abs(diff_pp) >= settings.brief_weight_change_pp:
                deltas.append(
                    Delta(
                        delta_type="WEIGHT_CHANGE",
                        item_key=f"weight:{ticker}:{until.date()}",
                        title=f"{ticker} portfolio weight changed {diff_pp:+.1f}pp "
                              f"({100 * prev_w:.1f}% -> {100 * w:.1f}%)",
                        severity="LOW",
                        investment_id=inv.id if inv else None,
                        reason=f"Shown because the change exceeds {settings.brief_weight_change_pp}pp.",
                        source_refs=["portfolio_valuation:derived"],
                    )
                )

    # valuation threshold states (per investment with scenarios); surfaced only on STATE CHANGE
    from src.research.valuation import models_for, scenarios_for, summarize_model

    for inv in investments.values():
        if inv.instrument_id is None:
            continue
        found = prices.latest(inv.instrument_id)
        if found is None:
            continue
        px = float(found.close)
        for model in models_for(session, inv):
            summary = summarize_model(model, scenarios_for(session, model))
            vs = _valuation_state(px, model, summary, settings)
            key = f"{inv.ticker}:{model.id}"
            state["valuation_states"][key] = vs["state"]
            prev_vs = prev_val_states.get(key)
            if prev_vs is not None and prev_vs != vs["state"]:
                deltas.append(
                    Delta(
                        delta_type="VALUATION_REVIEW",
                        item_key=f"valuation:{key}:{vs['state']}",
                        title=f"{inv.ticker}: price {px:g} moved into zone '{vs['state']}' "
                              f"(was '{prev_vs}') — {vs['detail']}",
                        severity="MEDIUM",
                        investment_id=inv.id,
                        detail="This creates a VALUATION REVIEW, not a trade signal.",
                        reason="Shown because price crossed a valuation threshold defined by your scenarios.",
                        source_refs=[f"valuation_models:{model.id}"],
                    )
                )
    return state


def _valuation_state(px: float, model, summary, settings) -> dict:
    """Deterministic zone label from the user's own scenario targets."""
    ref = float(summary.reference_price) if summary.reference_price is not None else None
    weighted = float(summary.weighted_target) if summary.weighted_target is not None else None
    targets = {r.scenario_name: float(r.target_price) for r in summary.scenarios}
    bear = targets.get("bear")
    detail_parts = []
    if ref:
        detail_parts.append(f"{100 * (px / ref - 1):+.0f}% vs reference {ref:g}")
    if weighted:
        detail_parts.append(f"{100 * (px / weighted - 1):+.0f}% vs weighted target {weighted:g}")
    state = "between_reference_and_target"
    if bear is not None and px <= bear:
        state = "at_or_below_bear_target"
    elif ref is not None and px < ref:
        state = "below_reference"
    elif weighted is not None and px >= weighted:
        state = "at_or_above_weighted_target"
    elif ref is not None and px >= ref * (1 + settings.brief_valuation_band_pct / 100):
        state = "above_reference_band"
    return {"state": state, "detail": "; ".join(detail_parts) or "n/a"}
