"""Company Intelligence: per-investment view of filings, structured facts, insiders,
macro links and technical context. Read-only aggregation - research edits stay in Research."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from src.config import load_settings  # noqa: E402
from src.db.intelligence import (  # noqa: E402
    FinancialFact,
    InsiderTransaction,
    InstitutionalHolding,
    InstitutionalManager,
    InvestmentMacroLink,
    MacroSeries,
    SourceDocument,
)
from src.db.session import session_scope  # noqa: E402
from src.intelligence.connectors.insiders import aggregate_insiders  # noqa: E402
from src.intelligence.connectors.macro import latest_observations  # noqa: E402
from src.intelligence.entities import _provider_ids  # noqa: E402
from src.intelligence.events import list_events  # noqa: E402
from src.intelligence.technical import technical_context_for_instrument  # noqa: E402
from src.research.health import thesis_health  # noqa: E402
from src.research.investments import list_investments  # noqa: E402

st.set_page_config(page_title="Investor OS - Company Intelligence", layout="wide", page_icon="🏢")
settings = load_settings()
st.title("Company Intelligence")

with session_scope(settings.db_url) as s:
    investments = list_investments(s)
if not investments:
    st.info("No investments in the research pipeline yet (see the Research page).")
    st.stop()

ticker = st.selectbox("Investment", [i.ticker for i in investments])

with session_scope(settings.db_url) as s:
    inv = next(i for i in list_investments(s) if i.ticker == ticker)
    health = thesis_health(s, inv)
    cik = None
    if inv.instrument_id is not None:
        from src.db.models import Instrument

        inst = s.get(Instrument, inv.instrument_id)
        cik = _provider_ids(inst).get("sec_cik") if inst else None
    events = list_events(s, investment_id=inv.id, limit=30)

    filings = list(
        s.scalars(
            select(SourceDocument)
            .where(SourceDocument.provider == "sec_edgar", SourceDocument.source_type == "filing",
                   SourceDocument.entity_key == cik)
            .order_by(SourceDocument.published_at.desc())
        )
    ) if cik else []

    facts = list(
        s.scalars(
            select(FinancialFact)
            .where(FinancialFact.cik == cik, FinancialFact.metric.isnot(None))
            .order_by(FinancialFact.period_end.desc())
        )
    ) if cik else []

    insiders_rows = list(
        s.scalars(
            select(InsiderTransaction)
            .where(InsiderTransaction.investment_id == inv.id)
            .order_by(InsiderTransaction.transaction_date.desc())
        )
    )
    insider_agg = aggregate_insiders(s, cik) if cik else None

    holdings = list(
        s.scalars(
            select(InstitutionalHolding)
            .where(InstitutionalHolding.instrument_id == inv.instrument_id)
            .order_by(InstitutionalHolding.period.desc())
        )
    ) if inv.instrument_id else []
    managers = {m.id: m for m in s.scalars(select(InstitutionalManager))}

    macro_links = list(
        s.scalars(select(InvestmentMacroLink).where(InvestmentMacroLink.investment_id == inv.id))
    )
    macro_data = []
    for link in macro_links:
        series = s.get(MacroSeries, link.series_id)
        obs = latest_observations(s, series, n=13)
        macro_data.append((link, series, obs))

    tech = technical_context_for_instrument(s, inv.instrument_id) if inv.instrument_id else None

c = st.columns(4)
c[0].metric("Lifecycle", inv.status)
c[1].metric("Thesis health", health.state or "n/a")
c[2].metric("SEC CIK", cik or "not resolved (run `sec sync`)")
c[3].metric("Intelligence events", len(events))

tabs = st.tabs(["Latest intelligence", "Filings", "Structured financials", "Insiders",
                "Institutional (13F)", "Macro context", "Technical context"])

with tabs[0]:
    if events:
        st.dataframe(
            pd.DataFrame(
                [{"When": e.occurred_at.date(), "Severity": e.severity, "Type": e.event_type,
                  "Title": e.title, "AI": e.ai_state} for e in events]
            ),
            hide_index=True, width="stretch",
        )
    else:
        st.info("No events yet. Sync sources via CLI (sec sync / insiders sync / macro sync).")

with tabs[1]:
    if filings:
        rows = []
        for d in filings:
            meta = json.loads(d.metadata_json) if d.metadata_json else {}
            rows.append({"Form": meta.get("form"), "Filed": d.published_at.date() if d.published_at else None,
                         "Period": d.period_end, "Accession": d.external_id, "URL": d.url,
                         "Raw archived": bool(d.raw_path)})
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        st.caption("Foreign private issuers file 20-F/6-K instead of 10-K/10-Q - both are supported.")
    else:
        st.info(f"No filings archived. Run: python -m src.main sec sync {inv.ticker}")

with tabs[2]:
    if facts:
        st.dataframe(
            pd.DataFrame(
                [{"Metric": f.metric, "Value": f.value, "Unit": f.unit, "Period end": f.period_end,
                  "FY/FP": f"{f.fiscal_year or ''}{f.fiscal_period or ''}", "Form": f.form,
                  "Concept": f"{f.taxonomy}:{f.concept}", "Accession": f.accession}
                 for f in facts[:200]]
            ),
            hide_index=True, width="stretch",
        )
        st.caption("Deterministic XBRL facts (Tier 1). Issuer-specific KPIs not present in XBRL "
                   "(customers, ARPAC, NPL...) remain manual - values are never invented.")
    else:
        st.info("No structured facts. Run `sec sync` (XBRL coverage varies by issuer).")

with tabs[3]:
    if insider_agg:
        cc = st.columns(3)
        for col, w in zip(cc, ("30d", "90d", "365d")):
            a = insider_agg[w]
            col.metric(f"{w}: buyers / sellers", f"{a['insiders_buying']} / {a['insiders_selling']}",
                       delta=f"net ${a['net_value_usd']:+,.0f}", delta_color="off")
        st.caption(insider_agg["note"])
    if insiders_rows:
        st.dataframe(
            pd.DataFrame(
                [{"Insider": r.insider_name, "Role": r.insider_role, "Type": r.transaction_type,
                  "Date": r.transaction_date, "Filed": r.filing_date, "Shares": r.shares,
                  "Price": r.price, "Value": r.value, "After": r.shares_after,
                  "Direct": r.direct_ownership} for r in insiders_rows[:100]]
            ),
            hide_index=True, width="stretch",
        )
        st.caption("Transaction types distinguish open-market vs option exercise vs awards vs tax - "
                   "a sale is not automatically bearish.")
    else:
        st.info(f"No insider data. Run: python -m src.main insiders sync {inv.ticker}")

with tabs[4]:
    if holdings:
        st.dataframe(
            pd.DataFrame(
                [{"Manager": managers[h.manager_id].name if h.manager_id in managers else "?",
                  "Period": h.period, "Shares": h.shares, "Value USD": h.value_usd}
                 for h in holdings]
            ),
            hide_index=True, width="stretch",
        )
    else:
        st.info("No tracked-manager holdings touch this instrument yet (institutional sync).")
    st.caption("13F is delayed and incomplete; never an endorsement.")

with tabs[5]:
    if macro_data:
        for link, series, obs in macro_data:
            st.markdown(f"**{series.name}** ({series.series_code}) — importance {link.importance}"
                        + (f" · {link.relationship_}" if link.relationship_ else ""))
            if link.why_it_matters:
                st.caption(link.why_it_matters)
            if obs:
                st.line_chart(pd.DataFrame({"value": [o.value for o in obs]},
                                           index=[o.obs_date for o in obs]), height=140)
            else:
                st.caption("No observations yet - run `macro sync`.")
    else:
        st.info("No macro series linked. Link them below.")
    with session_scope(settings.db_url) as s2:
        all_series = list(s2.scalars(select(MacroSeries).where(MacroSeries.active.is_(True))))
    if all_series:
        with st.form("link_macro"):
            cc = st.columns(4)
            pick = cc[0].selectbox("Series", {f"{x.name} ({x.series_code})": x.id for x in all_series})
            rel = cc[1].text_input("Relationship (e.g. funding costs)")
            imp = cc[2].selectbox("Importance", ("LOW", "MEDIUM", "HIGH"), index=1)
            why = cc[3].text_input("Why it matters")
            if st.form_submit_button("Link series to this investment"):
                from src.intelligence.connectors.macro import link_macro
                from src.research.investments import get_by_ticker

                sid = {f"{x.name} ({x.series_code})": x.id for x in all_series}[pick]
                with session_scope(settings.db_url) as s3:
                    series = s3.get(MacroSeries, sid)
                    link_macro(s3, get_by_ticker(s3, ticker), series, relationship=rel or None,
                               why_it_matters=why or None, importance=imp)
                st.rerun()

with tabs[6]:
    if tech is None:
        st.info("No linked instrument - technical context unavailable.")
    else:
        for line in tech.statements():
            st.markdown(f"- {line}")
        st.caption("Deterministic context from cached closes (OHLC approximated by closes); "
                   "available when recording decisions. Never a signal.")
