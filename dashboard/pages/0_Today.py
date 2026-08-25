"""TODAY - the mentor homepage. Useful in under 30 seconds:
what changed, what needs your judgment, what can be ignored. Delta, not state."""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from src.config import load_settings  # noqa: E402
from src.db.briefing import FEEDBACK_RATINGS, BriefFeedback, BriefItem, BriefRun  # noqa: E402
from src.db.intelligence import AiProposal  # noqa: E402
from src.db.session import session_scope  # noqa: E402
from src.intelligence.briefing.hygiene import set_attention  # noqa: E402
from src.intelligence.calendar import upcoming_events  # noqa: E402

st.set_page_config(page_title="Investor OS - Today", layout="wide", page_icon="🌅")
settings = load_settings()

st.title(f"Today — {date.today()}")

with session_scope(settings.db_url) as s:
    run = s.scalars(
        select(BriefRun).where(BriefRun.status == "completed", BriefRun.brief_type == "daily")
        .order_by(BriefRun.period_end.desc())
    ).first()
    items = list(s.scalars(select(BriefItem).where(BriefItem.brief_run_id == run.id))) if run else []
    pending = list(s.scalars(select(AiProposal).where(AiProposal.status == "PENDING")))
    upcoming = upcoming_events(s, days=7)
    run_data = None
    if run:
        run_data = {
            "date": run.period_end.date(), "items": run.items_count, "suppressed": run.suppressed_count,
            "ai_used": run.ai_used, "ai_model": run.ai_model, "output_path": run.output_path,
        }

if run_data is None:
    st.info("No daily brief yet. Run: `python -m src.main brief daily`")
    st.stop()

fresh = run_data["date"] == date.today()
c = st.columns(4)
c[0].metric("Last brief", str(run_data["date"]) + ("" if fresh else " (stale - run brief daily)"))
c[1].metric("Items surfaced", run_data["items"])
c[2].metric("Suppressed (unchanged)", run_data["suppressed"])
c[3].metric("Mentor AI", (run_data["ai_model"] or "used") if run_data["ai_used"] else "deterministic only")

# --- Today's summary (from the saved brief markdown) -------------------------
md_text = ""
if run_data["output_path"] and Path(run_data["output_path"]).exists():
    md_text = Path(run_data["output_path"]).read_text(encoding="utf-8")

st.subheader("Today's summary")
if run_data["items"] == 0:
    st.success("No material thesis-relevant developments since the previous brief.")
if md_text:
    with st.expander("Full brief", expanded=run_data["items"] > 0):
        st.markdown(md_text)

# --- Needs your judgment -----------------------------------------------------
st.subheader(f"Needs your judgment ({len(pending)})")
if pending:
    for p in pending[:10]:
        st.markdown(f"* **#{p.id} [{p.proposal_type}]** {p.title} — review in the Intelligence Inbox")
else:
    st.write("Nothing awaits your decision.")

# --- Surfaced items with 'why' + triage + feedback ---------------------------
if items:
    st.subheader("Surfaced items — why am I seeing this?")
    for it in items:
        with st.expander(f"[{it.severity}] {it.title}"):
            if it.detail:
                st.write(it.detail)
            st.caption(f"Why shown: {it.reason}")
            st.caption(f"Sources: {', '.join(json.loads(it.source_refs or '[]')) or 'derived'} · key: `{it.item_key}`")
            cc = st.columns(4)
            if cc[0].button("Defer 7 days", key=f"defer{it.id}"):
                from datetime import timedelta

                with session_scope(settings.db_url) as s2:
                    set_attention(s2, it.item_key, "DEFERRED", defer_until=date.today() + timedelta(days=7),
                                  investment_id=it.investment_id)
                st.rerun()
            if cc[1].button("Resolve", key=f"res{it.id}"):
                with session_scope(settings.db_url) as s2:
                    set_attention(s2, it.item_key, "RESOLVED", investment_id=it.investment_id)
                st.rerun()
            rating = cc[2].selectbox("Feedback", ("", *FEEDBACK_RATINGS), key=f"fb{it.id}")
            if cc[3].button("Send", key=f"fbs{it.id}") and rating:
                with session_scope(settings.db_url) as s2:
                    s2.add(BriefFeedback(brief_item_id=it.id, item_key=it.item_key, rating=rating))
                st.rerun()

# --- Upcoming ---------------------------------------------------------------
st.subheader(f"Upcoming 7 days ({len(upcoming)})")
for e in upcoming:
    st.markdown(f"* **{e['date']}** [{e['kind']}] {e['title']}")
if not upcoming:
    st.write("Nothing scheduled.")

st.caption("Delta, not state: unchanged risks and theses are tracked but not repeated. "
           "They resurface only when something about them changes.")
