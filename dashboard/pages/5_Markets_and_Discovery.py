"""Macro / Insiders / Congress / Institutional / Discovery / Calendar overview page."""
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
    CongressTransaction,
    InsiderTransaction,
    InstitutionalManager,
    MacroSeries,
    ResearchCandidate,
    WatchlistEntry,
)
from src.db.session import session_scope  # noqa: E402
from src.intelligence.calendar import upcoming_events  # noqa: E402
from src.intelligence.connectors.institutional import DISCLAIMER, holding_changes  # noqa: E402
from src.intelligence.connectors.macro import latest_observations  # noqa: E402
from src.intelligence.discovery import dismiss_candidate, promote_candidate  # noqa: E402
from src.research.investments import ResearchError  # noqa: E402

st.set_page_config(page_title="Investor OS - Markets & Discovery", layout="wide", page_icon="🌍")
settings = load_settings()
st.title("Markets & Discovery")

tabs = st.tabs(["Macro", "Insiders", "Congress", "Institutional", "Discovery", "Calendar", "Watchlists"])

with tabs[0]:
    with session_scope(settings.db_url) as s:
        series_list = list(s.scalars(select(MacroSeries).where(MacroSeries.active.is_(True))))
        data = [(x, latest_observations(s, x, n=25)) for x in series_list]
    if not series_list:
        st.info("Run `python -m src.main macro sync` to populate configured FRED series.")
    cols = st.columns(2)
    for i, (series, obs) in enumerate(data):
        with cols[i % 2]:
            if obs:
                last = obs[-1]
                st.markdown(f"**{series.name}** · latest {last.value} ({last.obs_date})")
                st.line_chart(pd.DataFrame({"value": [o.value for o in obs]},
                                           index=[o.obs_date for o in obs]), height=140)
            else:
                st.markdown(f"**{series.name}** · no data yet")

with tabs[1]:
    with session_scope(settings.db_url) as s:
        rows = list(s.scalars(select(InsiderTransaction).order_by(InsiderTransaction.transaction_date.desc()).limit(200)))
    if rows:
        st.dataframe(
            pd.DataFrame(
                [{"Issuer": r.issuer_name, "Insider": r.insider_name, "Type": r.transaction_type,
                  "Date": r.transaction_date, "Shares": r.shares, "Price": r.price, "Value": r.value}
                 for r in rows]
            ),
            hide_index=True, width="stretch",
        )
    else:
        st.info("No insider data. Run: python -m src.main insiders sync <ticker>")
    st.caption("Open-market vs exercise vs award vs tax are distinguished; never a trading signal.")

with tabs[2]:
    with session_scope(settings.db_url) as s:
        rows = list(s.scalars(select(CongressTransaction).order_by(CongressTransaction.disclosure_date.desc()).limit(200)))
        matched = [r for r in rows if r.investment_id is not None]
    st.caption("Amounts are RANGES; disclosure lags the transaction; owner may be spouse/dependent. "
               "Context, not signal. Import CSV via: congress import <file>")
    if matched:
        st.markdown(f"**Disclosures involving companies we own or research ({len(matched)}):**")
        st.dataframe(
            pd.DataFrame(
                [{"Person": r.person, "Owner": r.owner, "Ticker": r.ticker, "Type": r.transaction_type,
                  "Amount": f"${r.amount_low:,.0f}-${r.amount_high:,.0f}" if r.amount_low is not None else "?",
                  "Transacted": r.transaction_date, "Disclosed": r.disclosure_date} for r in matched]
            ),
            hide_index=True, width="stretch",
        )
    if rows and not matched:
        st.write(f"{len(rows)} disclosures stored; none involve tracked companies.")
    if not rows:
        st.info("No congressional data imported.")

with tabs[3]:
    with session_scope(settings.db_url) as s:
        managers = list(s.scalars(select(InstitutionalManager)))
        changes_by_mgr = {m.name: holding_changes(s, m) for m in managers}
    st.caption(DISCLAIMER)
    if not managers:
        st.info('Track a manager: python -m src.main institutional add-manager "Name" CIK, then institutional sync')
    for name, changes in changes_by_mgr.items():
        st.markdown(f"**{name}** — {len(changes)} holdings in latest period")
        if changes:
            df = pd.DataFrame(changes)
            interesting = df[df["change"] != "UNCHANGED"]
            st.dataframe(interesting if not interesting.empty else df.head(10), hide_index=True, width="stretch")

with tabs[4]:
    with session_scope(settings.db_url) as s:
        candidates = list(s.scalars(select(ResearchCandidate).order_by(ResearchCandidate.discovered_at.desc())))
    open_c = [c for c in candidates if c.status == "NEW"]
    st.caption("Companies worth RESEARCHING - never purchase recommendations. "
               "Sources: 13F of tracked managers, insider clusters, manual. Run: discovery run")
    for c in open_c:
        with st.expander(f"{c.ticker} — {c.name or ''} ({c.source})"):
            for r in json.loads(c.reasons_json or "[]"):
                st.markdown(f"- {r}")
            b = st.columns(2)
            if b[0].button("PROMOTE TO RESEARCH", key=f"promo{c.id}"):
                try:
                    with session_scope(settings.db_url) as s2:
                        promote_candidate(s2, s2.get(ResearchCandidate, c.id))
                    st.rerun()
                except ResearchError as exc:
                    st.error(str(exc))
            if b[1].button("Dismiss", key=f"dis{c.id}"):
                with session_scope(settings.db_url) as s2:
                    dismiss_candidate(s2, s2.get(ResearchCandidate, c.id))
                st.rerun()
    if not open_c:
        st.write("No open candidates.")
    done = [c for c in candidates if c.status != "NEW"]
    if done:
        st.dataframe(
            pd.DataFrame([{"Ticker": c.ticker, "Source": c.source, "Status": c.status} for c in done]),
            hide_index=True, width="stretch",
        )

with tabs[5]:
    with session_scope(settings.db_url) as s:
        next7 = upcoming_events(s, days=7)
        next30 = upcoming_events(s, days=30)
    c1, c2 = st.columns(2)
    with c1:
        st.markdown(f"### Upcoming 7 days ({len(next7)})")
        for e in next7:
            st.markdown(f"- **{e['date']}** [{e['kind']}] {e['title']}")
        if not next7:
            st.write("Nothing scheduled.")
    with c2:
        st.markdown(f"### Upcoming 30 days ({len(next30)})")
        for e in next30:
            st.markdown(f"- **{e['date']}** [{e['kind']}] {e['title']}")
        if not next30:
            st.write("Nothing scheduled.")
    st.caption("Only KNOWN dates (catalysts, prediction deadlines, thesis reviews) - nothing fabricated.")

with tabs[6]:
    with session_scope(settings.db_url) as s:
        entries = list(s.scalars(select(WatchlistEntry).where(WatchlistEntry.active.is_(True))))
    if entries:
        st.dataframe(
            pd.DataFrame([{"Kind": w.kind, "Key": w.key, "Label": w.label} for w in entries]),
            hide_index=True, width="stretch",
        )
    with st.form("add_watch"):
        c = st.columns(3)
        kind = c[0].selectbox("Kind", ("company", "insider", "congress_member", "manager", "macro_series"))
        key = c[1].text_input("Key (ticker / name / CIK / series code)")
        label = c[2].text_input("Label")
        if st.form_submit_button("Add to watchlist") and key.strip():
            with session_scope(settings.db_url) as s2:
                from sqlalchemy import select as _sel

                exists = s2.scalars(_sel(WatchlistEntry).where(
                    WatchlistEntry.kind == kind, WatchlistEntry.key == key.strip())).first()
                if not exists:
                    s2.add(WatchlistEntry(kind=kind, key=key.strip(), label=label or None))
            st.rerun()
