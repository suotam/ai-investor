"""Needs Attention page: deterministic review queue (no scores, no alerts, no signals)."""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import load_settings  # noqa: E402
from src.db.session import session_scope  # noqa: E402
from src.research.predictions import simple_stats  # noqa: E402
from src.research.reviews import needs_attention  # noqa: E402

st.set_page_config(page_title="Investor OS - Needs Attention", layout="wide", page_icon="🔎")
settings = load_settings()

st.title("Needs Attention")
st.caption("Deterministic review queue - it lists what to look at, never what to do.")

with session_scope(settings.db_url) as s:
    na = needs_attention(s)
    stats = simple_stats(s)

if na.total == 0:
    st.success("Nothing needs attention right now.")
else:
    def table(title: str, rows: list[dict]) -> None:
        if rows:
            st.subheader(f"{title} ({len(rows)})")
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")

    table("Reviews due", [
        {"Ticker": i.ticker, "Status": i.status, "Due": i.next_review_date, "Last review": i.last_review_date}
        for i in na.reviews_due
    ])
    table("Triggered thesis breakers", [
        {"Ticker": i.ticker, "Breaker": b.name, "Severity": b.severity, "Triggered": b.triggered_at,
         "Condition": b.condition_text or ""}
        for i, b in na.triggered_breakers
    ])
    table("Broken assumptions", [
        {"Ticker": i.ticker, "Assumption": a.name, "Importance": a.importance,
         "Since": a.status_updated_at, "Breaker condition": a.breaker_condition or ""}
        for i, a in na.broken_assumptions
    ])
    table("Challenged assumptions", [
        {"Ticker": i.ticker, "Assumption": a.name, "Importance": a.importance, "Since": a.status_updated_at}
        for i, a in na.challenged_assumptions
    ])
    table("High severity risks", [
        {"Ticker": i.ticker, "Risk": r.name, "Severity": r.severity, "Category": r.category,
         "Probability %": r.probability}
        for i, r in na.high_risks
    ])
    table("Expired catalysts (expected date passed, still PENDING)", [
        {"Ticker": i.ticker, "Catalyst": c.name, "Expected": c.expected_date, "Probability %": c.probability}
        for i, c in na.expired_catalysts
    ])
    table("Predictions awaiting resolution", [
        {"Id": p.id, "Statement": p.statement, "p %": p.probability, "Resolve by": p.resolution_date,
         "Condition": p.resolution_condition or ""}
        for p in na.predictions_awaiting
    ])
    table("Stale valuations", [
        {"Ticker": i.ticker, "Model": m.name, "Age (days)": age, "Reference price": m.reference_price}
        for i, m, age in na.stale_valuations
    ])
    table("Stale theses", [
        {"Ticker": i.ticker, "Thesis": t.title, "Days since revision": age}
        for i, t, age in na.stale_theses
    ])

st.divider()
st.subheader("Prediction record (storage for future calibration)")
c = st.columns(6)
c[0].metric("Total", stats["total"])
c[1].metric("Open", stats["open"])
c[2].metric("Resolved", stats["resolved"])
c[3].metric("Resolved TRUE", stats["resolved_true"])
c[4].metric("Hit rate", f"{stats['hit_rate'] * 100:.0f}%" if stats["hit_rate"] is not None else "n/a")
c[5].metric("Avg p when TRUE", f"{stats['avg_probability_when_true']:.0f}%" if stats["avg_probability_when_true"] is not None else "n/a")
st.caption(
    "Brier score and calibration curves will be added once enough predictions are resolved - "
    "the stored fields (probability, status, category, resolved_at) already support them."
)
