"""Investments / Research page: pipeline overview + investment cockpit with manual entry forms.

All business logic lives in src/research/*; this page only renders and collects input.
The layout deliberately prioritizes thesis, evidence, risk and decision quality over
price movement - no BUY/SELL coloring, no alerts.
"""
from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import load_settings  # noqa: E402
from src.db.research import (  # noqa: E402
    ASSUMPTION_CATEGORIES,
    ASSUMPTION_STATUSES,
    BREAKER_STATUSES,
    CATALYST_STATUSES,
    DECISION_TYPES,
    DIRECTIONS,
    EVIDENCE_TYPES,
    IMPORTANCES,
    INVESTMENT_STATUSES,
    REVIEW_FREQUENCIES,
    RISK_CATEGORIES,
    SEVERITIES,
    VALUATION_MODEL_TYPES,
    Catalyst,
    PreMortem,
    RedTeamEntry,
    Risk,
    ThesisBreaker,
)
from src.db.session import session_scope  # noqa: E402
from src.research import assumptions as asm  # noqa: E402
from src.research import evidence as ev  # noqa: E402
from src.research import items, kpis  # noqa: E402
from src.research.decisions import decision_snapshot, list_decisions, record_decision  # noqa: E402
from src.research.health import thesis_health  # noqa: E402
from src.research.investments import (  # noqa: E402
    ResearchError,
    create_investment,
    get_by_ticker,
    list_investments,
    mark_reviewed,
    portfolio_link,
    set_status,
)
from src.research.predictions import (  # noqa: E402
    create_prediction,
    list_predictions,
    resolve_prediction,
)
from src.research.theses import (  # noqa: E402
    active_thesis,
    create_thesis,
    current_version,
    revise_thesis,
    version_history,
)
from src.research.valuation import (  # noqa: E402
    add_scenario,
    create_model,
    models_for,
    scenarios_for,
    set_reference_price,
    summarize_model,
)

st.set_page_config(page_title="Investor OS - Research", layout="wide", page_icon="📚")
settings = load_settings()
BASE = settings.base_currency
DB = settings.db_url

st.title("Investments / Research")


def _try(fn, *args, **kwargs):
    """Run a service call inside a session; show validation errors instead of crashing."""
    try:
        with session_scope(DB) as s:
            fn(s, *args, **kwargs)
        st.rerun()
    except ResearchError as exc:
        st.error(str(exc))


def fmt(v, suffix=""):
    return "Unavailable" if v is None else f"{v}{suffix}"


def pct(v):
    return "Unavailable" if v is None else f"{float(v) * 100:+.1f}%"


# ---------------------------------------------------------------- pipeline table
with session_scope(DB) as s:
    investments = list_investments(s)
    pipeline_rows = []
    for inv in investments:
        h = thesis_health(s, inv)
        thesis = active_thesis(s, inv)
        v = current_version(s, thesis) if thesis else None
        pipeline_rows.append(
            {
                "Ticker": inv.ticker,
                "Company": inv.name or "",
                "Status": inv.status,
                "Owned": "yes" if inv.status == "OWNED" else "",
                "Thesis": f"v{v.version_number}" if v else "-",
                "Confidence": v.confidence if v else None,
                "Health": h.state or "n/a",
                "Broken/Weakening": f"{h.broken}/{h.weakening}",
                "High risks": h.high_risks,
                "Next review": inv.next_review_date,
            }
        )

with st.expander("➕ Add investment to the research universe"):
    with st.form("new_investment"):
        c = st.columns(4)
        ticker = c[0].text_input("Ticker*")
        name = c[1].text_input("Company name")
        status = c[2].selectbox("Status", INVESTMENT_STATUSES, index=0)
        freq = c[3].selectbox("Review frequency", REVIEW_FREQUENCIES, index=2)
        notes = st.text_area("Notes", height=68)
        if st.form_submit_button("Create") and ticker:
            _try(create_investment, ticker, name=name or None, status=status, review_frequency=freq, notes=notes or None)

if not pipeline_rows:
    st.info("No investments yet - add one above. The research layer is independent of holdings.")
    st.stop()

st.dataframe(pd.DataFrame(pipeline_rows), hide_index=True, width="stretch")

# ---------------------------------------------------------------- detail selection
tickers = [r["Ticker"] for r in pipeline_rows]
selected = st.selectbox("Open investment detail", tickers, key="detail_ticker")

with session_scope(DB) as s:
    inv = get_by_ticker(s, selected)
    thesis = active_thesis(s, inv)
    version = current_version(s, thesis) if thesis else None
    history = version_history(s, thesis) if thesis else []
    assumptions = asm.list_assumptions(s, thesis) if thesis else []
    inv_kpis = kpis.list_kpis(s, inv)
    kpi_data = [kpis.kpi_vs_expectation(s, k) for k in inv_kpis]
    risks = items.list_for_investment(s, Risk, inv)
    catalysts = items.list_for_investment(s, Catalyst, inv)
    breakers = items.list_for_investment(s, ThesisBreaker, inv)
    premortems = items.list_for_investment(s, PreMortem, inv)
    redteam = items.list_for_investment(s, RedTeamEntry, inv)
    evidence_by_dir = ev.evidence_by_direction(s, inv)
    models = models_for(s, inv)
    model_summaries = [(m, summarize_model(m, scenarios_for(s, m))) for m in models]
    decisions = list_decisions(s, inv)
    predictions = list_predictions(s, inv)
    health = thesis_health(s, inv)
    link = None
    if inv.instrument_id is not None:
        from src.portfolio.valuation import value_portfolio

        try:
            val = value_portfolio(s, BASE)
            link = portfolio_link(val, inv)
        except Exception:
            link = None
    inv_id = inv.id
    thesis_id = thesis.id if thesis else None

st.divider()
head = st.columns([2, 1, 1, 1, 1])
head[0].subheader(f"{inv.ticker} — {inv.name or ''}")
head[1].metric("Lifecycle", inv.status)
head[2].metric("Thesis health", health.state or "n/a")
head[3].metric("Next review", str(inv.next_review_date or "-"))
if head[4].button("Mark reviewed today"):
    _try(lambda s2: mark_reviewed(s2, get_by_ticker(s2, selected)))
if health.reasons:
    st.caption("Health components: " + "; ".join(health.reasons))
hc = st.columns(7)
for col, (label, value) in zip(
    hc,
    [
        ("Supported", health.supported), ("Weakening", health.weakening), ("Unknown", health.unknown),
        ("Challenged", health.challenged), ("Broken", health.broken),
        ("Breakers triggered", health.breakers_triggered), ("High risks", health.high_risks),
    ],
):
    col.metric(label, value)

lifecycle_col, _ = st.columns([1, 3])
new_status = lifecycle_col.selectbox("Move lifecycle to", INVESTMENT_STATUSES, index=INVESTMENT_STATUSES.index(inv.status))
if new_status != inv.status:
    _try(lambda s2: set_status(s2, get_by_ticker(s2, selected), new_status))

tabs = st.tabs(
    ["Thesis", "Assumptions & KPIs", "Valuation", "Risks / Breakers / Catalysts",
     "Evidence", "Decisions", "Predictions", "Red Team & Pre-mortem", "Portfolio", "History"]
)

# ---------------------------------------------------------------- Thesis
with tabs[0]:
    if version:
        st.markdown(f"### {thesis.title}  \n*v{version.version_number} · {version.created_at:%Y-%m-%d} · "
                    f"confidence {fmt(version.confidence, '/100')} · horizon {version.time_horizon or '-'}*")
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**Why we own/watch this (core thesis)**")
            st.write(version.core_thesis or "-")
            st.markdown("**Our expectation**")
            st.write(version.our_expectation or "-")
            st.markdown("**Expected return summary**")
            st.write(version.expected_return_summary or "-")
        with c2:
            st.markdown("**What the market appears to believe**")
            st.write(version.market_expectation or "-")
            st.markdown("**Why the market may be wrong (variant perception)**")
            st.write(version.why_market_may_be_wrong or version.variant_perception or "-")
            st.markdown("**Summary**")
            st.write(version.summary or "-")
        with st.expander("Revise thesis (creates a NEW immutable version)"):
            with st.form("revise_thesis"):
                reason = st.text_input("Reason for revision* (required, becomes part of history)")
                fields = {}
                fields["core_thesis"] = st.text_area("Core thesis", value=version.core_thesis or "")
                fields["market_expectation"] = st.text_area("Market expectation", value=version.market_expectation or "")
                fields["our_expectation"] = st.text_area("Our expectation", value=version.our_expectation or "")
                fields["why_market_may_be_wrong"] = st.text_area("Why market may be wrong", value=version.why_market_may_be_wrong or "")
                fields["summary"] = st.text_area("Summary", value=version.summary or "")
                cc = st.columns(2)
                fields["confidence"] = cc[0].slider("Confidence (0-100)", 0, 100, int(version.confidence or 50))
                fields["time_horizon"] = cc[1].text_input("Time horizon", value=version.time_horizon or "")
                if st.form_submit_button("Create new version") and reason.strip():
                    _try(lambda s2: revise_thesis(s2, active_thesis(s2, get_by_ticker(s2, selected)), reason, **fields))
    else:
        st.info("No thesis yet.")
        with st.form("create_thesis"):
            title = st.text_input("Thesis title*")
            core = st.text_area("Core thesis")
            market = st.text_area("What does the market appear to believe?")
            ours = st.text_area("What do we believe?")
            wrong = st.text_area("Why might the market be wrong?")
            cc = st.columns(2)
            conf = cc[0].slider("Confidence (0-100)", 0, 100, 50)
            horizon = cc[1].text_input("Time horizon (e.g. 3y)")
            if st.form_submit_button("Create thesis v1") and title.strip():
                _try(
                    lambda s2: create_thesis(
                        s2, get_by_ticker(s2, selected), title, core_thesis=core or None,
                        market_expectation=market or None, our_expectation=ours or None,
                        why_market_may_be_wrong=wrong or None, confidence=conf, time_horizon=horizon or None,
                    )
                )

# ---------------------------------------------------------------- Assumptions & KPIs
with tabs[1]:
    if not thesis:
        st.info("Create a thesis first - assumptions belong to a thesis.")
    else:
        if assumptions:
            st.dataframe(
                pd.DataFrame(
                    [
                        {"Status": a.status, "Importance": a.importance, "Name": a.name,
                         "Category": a.category, "Expected": f"{a.expected_min or ''}–{a.expected_max or ''} {a.unit or ''}".strip("– "),
                         "Breaker condition": a.breaker_condition or "", "Confidence": a.confidence}
                        for a in assumptions
                    ]
                ),
                hide_index=True, width="stretch",
            )
            ac = st.columns(3)
            names = {f"{a.name} (#{a.id})": a.id for a in assumptions}
            pick = ac[0].selectbox("Assumption", list(names))
            new_st = ac[1].selectbox("New status (user-controlled)", ASSUMPTION_STATUSES)
            note = ac[2].text_input("Status note")
            if st.button("Update assumption status"):
                aid = names[pick]
                def _upd(s2):
                    a2 = s2.get(type(assumptions[0]), aid)
                    asm.set_assumption_status(s2, a2, new_st, note or None)
                _try(_upd)
        with st.expander("➕ Add assumption"):
            with st.form("add_assumption"):
                c = st.columns(3)
                a_name = c[0].text_input("Name*")
                a_cat = c[1].selectbox("Category", ASSUMPTION_CATEGORIES)
                a_imp = c[2].selectbox("Importance", IMPORTANCES, index=1)
                c = st.columns(4)
                a_min = c[0].number_input("Expected min", value=None)
                a_max = c[1].number_input("Expected max", value=None)
                a_unit = c[2].text_input("Unit (%, USD...)")
                kpi_opts = {"(none)": None} | {k.name: k.id for k in inv_kpis}
                a_kpi = c[3].selectbox("Link to KPI", list(kpi_opts))
                a_break = st.text_input("Breaker condition (e.g. '> 7% for 2 quarters')")
                a_desc = st.text_area("Description", height=68)
                if st.form_submit_button("Add") and a_name.strip():
                    _try(
                        lambda s2: asm.add_assumption(
                            s2, active_thesis(s2, get_by_ticker(s2, selected)), a_name,
                            description=a_desc or None, category=a_cat, importance=a_imp,
                            expected_min=a_min, expected_max=a_max, unit=a_unit or None,
                            breaker_condition=a_break or None, kpi_id=kpi_opts[a_kpi],
                        )
                    )
    st.markdown("#### KPIs")
    for item in kpi_data:
        k = item["kpi"]
        latest = item["latest_observation"]
        exp = item["expectations"]
        cols = st.columns([1, 1, 2, 2])
        cols[0].markdown(f"**{k.name}** ({k.unit or '-'})")
        cols[1].metric("Latest", f"{latest.value} ({latest.period})" if latest else "no data")
        exp_txt = "; ".join(f"{e.expected_min or '?'}–{e.expected_max or '?'} {e.unit or ''}" for e in exp) or "-"
        cols[2].write(f"Our expectation: {exp_txt}")
        if item["observations"]:
            cols[3].line_chart(
                pd.DataFrame({"value": [o.value for o in item["observations"]]},
                             index=[o.period for o in item["observations"]]),
                height=120,
            )
    with st.expander("➕ Add KPI / observation"):
        with st.form("add_kpi"):
            c = st.columns(4)
            k_name = c[0].text_input("KPI name*")
            k_unit = c[1].text_input("Unit")
            k_freq = c[2].selectbox("Frequency", ("quarterly", "monthly", "semiannual", "annual", "irregular"))
            k_dir = c[3].selectbox("Good direction", ("up", "down", "range"))
            if st.form_submit_button("Add KPI") and k_name.strip():
                _try(lambda s2: kpis.add_kpi(s2, get_by_ticker(s2, selected), k_name, unit=k_unit or None, frequency=k_freq, direction_good=k_dir))
        if inv_kpis:
            with st.form("add_obs"):
                c = st.columns(4)
                obs_kpi = c[0].selectbox("KPI", {k.name: k.id for k in inv_kpis})
                obs_period = c[1].text_input("Period* (e.g. 2026Q2)")
                obs_value = c[2].number_input("Value*", value=0.0, format="%.4f")
                obs_source = c[3].text_input("Source", value="company")
                if st.form_submit_button("Add observation") and obs_period.strip():
                    kid = {k.name: k.id for k in inv_kpis}[obs_kpi]
                    def _obs(s2):
                        from src.db.research import InvestmentKpi
                        kpis.add_observation(s2, s2.get(InvestmentKpi, kid), obs_period, obs_value, source=obs_source or None)
                    _try(_obs)

# ---------------------------------------------------------------- Valuation
with tabs[2]:
    for m, summary in model_summaries:
        st.markdown(f"**{m.name}** ({m.model_type}) · reference price: "
                    f"{fmt(m.reference_price)} {m.reference_currency or ''} ({m.reference_date or 'no date'})")
        if summary.scenarios:
            st.dataframe(
                pd.DataFrame(
                    [
                        {"Scenario": r.scenario_name, "Probability %": float(r.probability) if r.probability is not None else None,
                         "Target": float(r.target_price), "Dividends": float(r.expected_dividends),
                         "Upside": pct(r.expected_return), "Annualized": pct(r.annualized_return),
                         "Horizon (m)": r.time_horizon_months}
                        for r in summary.scenarios
                    ]
                ),
                hide_index=True, width="stretch",
            )
            c = st.columns(4)
            c[0].metric("Probability-weighted target", fmt(round(float(summary.weighted_target), 2) if summary.weighted_target is not None else None))
            c[1].metric("Weighted upside", pct(summary.weighted_return))
            c[2].metric("Margin of safety (base)", pct(summary.margin_of_safety_base))
            c[3].metric("Margin of safety (weighted)", pct(summary.margin_of_safety_weighted))
            st.caption("Margin of safety is information, not a BUY/SELL signal. Individual scenarios always shown above the weighted number.")
        with st.form(f"add_scenario_{m.id}"):
            c = st.columns(5)
            s_name = c[0].text_input("Scenario", value="base", key=f"sn{m.id}")
            s_prob = c[1].number_input("Probability %", 0.0, 100.0, value=None, key=f"sp{m.id}")
            s_target = c[2].number_input("Target price*", min_value=0.0, value=None, key=f"st{m.id}")
            s_months = c[3].number_input("Horizon months", min_value=0, value=36, key=f"sm{m.id}")
            s_div = c[4].number_input("Dividends over horizon", value=0.0, key=f"sd{m.id}")
            new_ref = st.number_input("Update reference price (0 = keep)", min_value=0.0, value=0.0, key=f"rp{m.id}")
            if st.form_submit_button("Add scenario / update reference"):
                def _sc(s2, mid=m.id):
                    from src.db.research import ValuationModel
                    m2 = s2.get(ValuationModel, mid)
                    if new_ref > 0:
                        set_reference_price(s2, m2, new_ref)
                    if s_target:
                        add_scenario(s2, m2, s_name, s_target, probability=s_prob,
                                     time_horizon_months=int(s_months) or None, expected_dividends=s_div or None)
                _try(_sc)
    with st.expander("➕ New valuation model"):
        with st.form("add_model"):
            c = st.columns(4)
            m_name = c[0].text_input("Name*", value="Scenario price targets")
            m_type = c[1].selectbox("Type", VALUATION_MODEL_TYPES, index=len(VALUATION_MODEL_TYPES) - 1)
            m_ref = c[2].number_input("Reference price", min_value=0.0, value=None)
            m_ccy = c[3].text_input("Currency", value="USD")
            if st.form_submit_button("Create model") and m_name.strip():
                _try(lambda s2: create_model(s2, get_by_ticker(s2, selected), m_name, model_type=m_type,
                                             reference_price=m_ref, reference_currency=m_ccy or None,
                                             reference_date=date.today() if m_ref else None))

# ---------------------------------------------------------------- Risks / Breakers / Catalysts
with tabs[3]:
    c1, c2, c3 = st.columns(3)
    with c1:
        st.markdown("#### Risks")
        for r in risks:
            st.markdown(f"- **{r.name}** [{r.severity}, {r.status}] ({r.category})"
                        + (f" — p={r.probability}%" if r.probability is not None else ""))
        with st.form("add_risk"):
            r_name = st.text_input("Risk name*")
            r_cat = st.selectbox("Category", RISK_CATEGORIES)
            r_sev = st.selectbox("Severity", SEVERITIES, index=1)
            r_prob = st.number_input("Probability %", 0, 100, value=None)
            r_mit = st.text_input("Mitigation")
            if st.form_submit_button("Add risk") and r_name.strip():
                _try(lambda s2: items.add_risk(s2, get_by_ticker(s2, selected), r_name, category=r_cat,
                                               severity=r_sev, probability=r_prob, mitigation=r_mit or None))
    with c2:
        st.markdown("#### Thesis breakers")
        st.caption("Conditions that invalidate the investment case (≠ risks).")
        for b in breakers:
            st.markdown(f"- **{b.name}** [{b.severity}, {b.status}] {b.condition_text or ''}")
        with st.form("add_breaker"):
            b_name = st.text_input("Breaker name*")
            b_cond = st.text_input("Condition")
            b_sev = st.selectbox("Severity", SEVERITIES, index=2)
            if st.form_submit_button("Add breaker") and b_name.strip():
                _try(lambda s2: items.add_breaker(s2, get_by_ticker(s2, selected), b_name,
                                                  condition_text=b_cond or None, severity=b_sev,
                                                  thesis_id=thesis_id))
        if breakers:
            with st.form("breaker_status"):
                bnames = {f"{b.name} (#{b.id})": b.id for b in breakers}
                b_pick = st.selectbox("Breaker", list(bnames))
                b_st = st.selectbox("Status", BREAKER_STATUSES)
                b_note = st.text_input("Note")
                if st.form_submit_button("Update breaker"):
                    def _bs(s2):
                        items.set_breaker_status(s2, s2.get(ThesisBreaker, bnames[b_pick]), b_st, b_note or None)
                    _try(_bs)
    with c3:
        st.markdown("#### Catalysts")
        for cat in catalysts:
            st.markdown(f"- **{cat.name}** [{cat.status}] expected {cat.expected_date or '?'}"
                        + (f", p={cat.probability}%" if cat.probability is not None else ""))
        with st.form("add_catalyst"):
            c_name = st.text_input("Catalyst name*")
            c_date = st.date_input("Expected date", value=None)
            c_prob = st.number_input("Probability %", 0, 100, value=None, key="catp")
            if st.form_submit_button("Add catalyst") and c_name.strip():
                _try(lambda s2: items.add_catalyst(s2, get_by_ticker(s2, selected), c_name,
                                                   expected_date=c_date, probability=c_prob))
        if catalysts:
            with st.form("catalyst_status"):
                cnames = {f"{c.name} (#{c.id})": c.id for c in catalysts}
                c_pick = st.selectbox("Catalyst", list(cnames))
                c_st = st.selectbox("Status", CATALYST_STATUSES)
                c_out = st.text_input("Outcome")
                if st.form_submit_button("Update catalyst"):
                    def _cs(s2):
                        items.resolve_catalyst(s2, s2.get(Catalyst, cnames[c_pick]), c_st,
                                               actual_date=date.today() if c_st == "OCCURRED" else None,
                                               outcome=c_out or None)
                    _try(_cs)

# ---------------------------------------------------------------- Evidence
with tabs[4]:
    st.caption("Raw evidence is preserved independently of interpretation - supporting and "
               "contradicting shown side by side, never merged into a verdict.")
    e1, e2 = st.columns(2)
    with e1:
        st.markdown(f"#### Supporting ({len(evidence_by_dir['SUPPORTING'])})")
        for e in evidence_by_dir["SUPPORTING"]:
            st.markdown(f"- **{e.title}** ({e.evidence_type}, {e.source_date or e.created_at:%Y-%m-%d})"
                        + (f" — {e.summary}" if e.summary else ""))
    with e2:
        st.markdown(f"#### Contradicting ({len(evidence_by_dir['CONTRADICTING'])})")
        for e in evidence_by_dir["CONTRADICTING"]:
            st.markdown(f"- **{e.title}** ({e.evidence_type}, {e.source_date or e.created_at:%Y-%m-%d})"
                        + (f" — {e.summary}" if e.summary else ""))
    if evidence_by_dir["NEUTRAL"]:
        st.markdown(f"#### Neutral ({len(evidence_by_dir['NEUTRAL'])})")
        for e in evidence_by_dir["NEUTRAL"]:
            st.markdown(f"- {e.title}")
    with st.expander("➕ Add evidence"):
        with st.form("add_evidence"):
            e_title = st.text_input("Title*")
            c = st.columns(4)
            e_dir = c[0].selectbox("Direction", DIRECTIONS)
            e_type = c[1].selectbox("Type", EVIDENCE_TYPES)
            e_rel = c[2].selectbox("Reliability", ("", *IMPORTANCES))
            e_srcdate = c[3].date_input("Source date", value=None)
            e_sum = st.text_area("Summary", height=68)
            c = st.columns(2)
            e_url = c[0].text_input("Source URL")
            e_srcname = c[1].text_input("Source name")
            if st.form_submit_button("Add evidence") and e_title.strip():
                _try(lambda s2: ev.add_evidence(s2, get_by_ticker(s2, selected), e_title, direction=e_dir,
                                                evidence_type=e_type, summary=e_sum or None,
                                                source_url=e_url or None, source_name=e_srcname or None,
                                                source_date=e_srcdate, reliability=e_rel or None))

# ---------------------------------------------------------------- Decisions
with tabs[5]:
    st.caption("Decision journal is immutable - including decisions NOT to act.")
    for d in decisions:
        snap = decision_snapshot(d)
        with st.expander(f"{d.decided_at:%Y-%m-%d} — {d.decision_type}"
                         + (f" (thesis v{snap.get('research', {}).get('thesis_version', {}).get('number')})"
                            if snap.get("research", {}).get("thesis_version") else "")):
            c = st.columns(4)
            c[0].metric("Price", fmt(d.instrument_price) + (f" {d.price_currency}" if d.price_currency else ""))
            c[1].metric(f"Portfolio value ({BASE})", fmt(d.portfolio_value))
            c[2].metric("Weight before", pct(d.position_weight_before) if d.position_weight_before is not None else "Unavailable")
            c[3].metric("Confidence", fmt(d.confidence, "/100"))
            for label, value in (
                ("Reasoning", d.reasoning), ("Expected outcome", d.expected_outcome),
                ("What would prove us wrong", d.what_would_make_this_wrong),
                ("Alternatives considered", d.alternative_considered), ("Key risks", d.key_risks),
                ("Information available", d.information_available_summary),
            ):
                if value:
                    st.markdown(f"**{label}:** {value}")
    with st.expander("➕ Record decision (snapshot of current context is frozen automatically)"):
        with st.form("add_decision"):
            c = st.columns(3)
            d_type = c[0].selectbox("Decision*", DECISION_TYPES)
            d_conf = c[1].slider("Confidence (0-100)", 0, 100, 60)
            d_horizon = c[2].text_input("Time horizon")
            d_reason = st.text_area("Reasoning*")
            d_expected = st.text_area("Expected outcome", height=68)
            d_wrong = st.text_area("What would prove us wrong?", height=68)
            d_alt = st.text_input("Alternatives considered")
            d_info = st.text_input("Information available (summary)")
            if st.form_submit_button("Record decision") and d_reason.strip():
                _try(lambda s2: record_decision(s2, get_by_ticker(s2, selected), d_type, base_currency=BASE,
                                                reasoning=d_reason, confidence=d_conf,
                                                time_horizon=d_horizon or None, expected_outcome=d_expected or None,
                                                what_would_make_this_wrong=d_wrong or None,
                                                alternative_considered=d_alt or None,
                                                information_available_summary=d_info or None))

# ---------------------------------------------------------------- Predictions
with tabs[6]:
    if predictions:
        st.dataframe(
            pd.DataFrame(
                [
                    {"Statement": p.statement, "p %": p.probability, "Status": p.status,
                     "Resolve by": p.resolution_date, "Outcome": p.outcome or "", "Created": p.created_at.date()}
                    for p in predictions
                ]
            ),
            hide_index=True, width="stretch",
        )
        open_preds = {f"#{p.id}: {p.statement[:50]}": p.id for p in predictions if p.status == "OPEN"}
        if open_preds:
            with st.form("resolve_pred"):
                c = st.columns(3)
                pr_pick = c[0].selectbox("Open prediction", list(open_preds))
                pr_status = c[1].selectbox("Resolution", ("RESOLVED_TRUE", "RESOLVED_FALSE", "AMBIGUOUS", "CANCELLED"))
                pr_outcome = c[2].text_input("Outcome note")
                if st.form_submit_button("Resolve"):
                    def _rp(s2):
                        from src.db.research import Prediction
                        resolve_prediction(s2, s2.get(Prediction, open_preds[pr_pick]), pr_status, pr_outcome or None)
                    _try(_rp)
    with st.expander("➕ New prediction"):
        with st.form("add_pred"):
            p_stmt = st.text_input("Statement* (falsifiable, with a deadline)")
            c = st.columns(3)
            p_prob = c[0].slider("Probability %", 0, 100, 70)
            p_date = c[1].date_input("Resolution date", value=None)
            p_cat = c[2].text_input("Category (growth, macro...)")
            p_cond = st.text_input("Resolution condition (how will we judge it?)")
            if st.form_submit_button("Create") and p_stmt.strip():
                _try(lambda s2: create_prediction(s2, p_stmt, p_prob, investment=get_by_ticker(s2, selected),
                                                  resolution_date=p_date, resolution_condition=p_cond or None,
                                                  category=p_cat or None))

# ---------------------------------------------------------------- Red team & Pre-mortem
with tabs[7]:
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("#### Red team (independent bearish case)")
        for e in redteam:
            st.markdown(f"- [{e.severity}, {e.status}] {e.argument}" + (f" → {e.resolution}" if e.resolution else ""))
        with st.form("add_redteam"):
            rt_arg = st.text_area("Bearish argument*")
            rt_sev = st.selectbox("Severity", SEVERITIES, index=1, key="rtsev")
            if st.form_submit_button("Add") and rt_arg.strip():
                _try(lambda s2: items.add_red_team_entry(s2, get_by_ticker(s2, selected), rt_arg, severity=rt_sev))
    with c2:
        st.markdown("#### Pre-mortem")
        st.caption("Imagine this investment lost 60% over three years. What most likely happened?")
        for p in premortems:
            st.markdown(f"- {p.scenario}" + (f" (p={p.probability}%)" if p.probability is not None else ""))
            if p.early_warning_signs:
                st.caption(f"  early warnings: {p.early_warning_signs}")
        with st.form("add_premortem"):
            pm_scenario = st.text_area("Scenario*")
            pm_prob = st.number_input("Probability %", 0, 100, value=None, key="pmp")
            pm_warn = st.text_input("Early warning signs")
            pm_mit = st.text_input("Possible mitigation")
            if st.form_submit_button("Add") and pm_scenario.strip():
                _try(lambda s2: items.add_premortem(s2, get_by_ticker(s2, selected), pm_scenario,
                                                    probability=pm_prob, early_warning_signs=pm_warn or None,
                                                    possible_mitigation=pm_mit or None))

# ---------------------------------------------------------------- Portfolio
with tabs[8]:
    if link is None:
        st.info("Not currently held (or no linked instrument). Research exists independently of holdings.")
    else:
        c = st.columns(4)
        c[0].metric("Quantity", f"{float(link['quantity']):,.4f}".rstrip("0").rstrip("."))
        c[1].metric("Current price", f"{float(link['price']):,.2f} {link['price_currency']}" if link["price"] is not None else "Unavailable")
        c[2].metric(f"Market value ({BASE})", f"{float(link['market_value_base']):,.0f}" if link["market_value_base"] is not None else "Unavailable")
        c[3].metric("Portfolio weight", pct(link["weight"]) if link["weight"] is not None else "Unavailable")
        c = st.columns(4)
        c[0].metric(f"Cost basis ({link['currency']})", f"{float(link['cost_basis_local']):,.2f}" if link["cost_basis_local"] is not None else "Unavailable")
        c[1].metric(f"Avg cost ({link['currency']})", f"{float(link['average_cost']):,.4f}" if link["average_cost"] is not None else "Unavailable")
        c[2].metric(f"Unrealized P/L ({BASE})", f"{float(link['unrealized_pnl_base']):,.0f}" if link["unrealized_pnl_base"] is not None else "Unavailable")
        c[3].metric(f"Realized P/L ({link['currency']})", f"{float(link['realized_pnl_local']):,.2f}")
        st.caption("Live view from the v1 portfolio engine (source of truth for holdings); nothing is copied into research tables.")

# ---------------------------------------------------------------- History
with tabs[9]:
    if len(history) <= 1:
        st.info("No revisions yet." if history else "No thesis yet.")
    else:
        st.caption("Every version is immutable - old thesis text is never overwritten.")
        for v in reversed(history):
            st.markdown(f"**v{v.version_number}** · {v.created_at:%Y-%m-%d} · {v.reason_for_revision} · "
                        f"confidence {fmt(v.confidence)}")
        pick_cols = st.columns(2)
        nums = [v.version_number for v in history]
        left = pick_cols[0].selectbox("Compare version", nums, index=0)
        right = pick_cols[1].selectbox("with version", nums, index=len(nums) - 1)
        va = next(v for v in history if v.version_number == left)
        vb = next(v for v in history if v.version_number == right)
        cc1, cc2 = st.columns(2)
        for col, v in ((cc1, va), (cc2, vb)):
            with col:
                st.markdown(f"##### v{v.version_number} ({v.created_at:%Y-%m-%d})")
                for label, value in (
                    ("Core thesis", v.core_thesis), ("Market expectation", v.market_expectation),
                    ("Our expectation", v.our_expectation), ("Why market may be wrong", v.why_market_may_be_wrong),
                    ("Confidence", v.confidence), ("Horizon", v.time_horizon),
                ):
                    st.markdown(f"**{label}:**")
                    st.write(value if value is not None else "-")
