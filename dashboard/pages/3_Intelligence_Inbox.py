"""Intelligence Inbox: events + AI proposals with the human review workflow.

Nothing on this page mutates research data except the explicit ACCEPT button, which calls
the v2 service layer through the proposal acceptance service.
"""
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
from src.db.intelligence import AiProposal, IntelligenceEvent, SourceDocument  # noqa: E402
from src.db.research import Investment, ThesisVersion  # noqa: E402
from src.db.session import session_scope  # noqa: E402
from src.intelligence.ai.proposals import accept_proposal, defer_proposal, reject_proposal  # noqa: E402
from src.intelligence.events import list_events, set_event_state  # noqa: E402
from src.research.investments import ResearchError  # noqa: E402

st.set_page_config(page_title="Investor OS - Intelligence Inbox", layout="wide", page_icon="📥")
settings = load_settings()

st.title("Intelligence Inbox")
st.caption(
    f"AI provider: {settings.ai_provider} @ {settings.ai_base_url} (local) · "
    f"{'enabled' if settings.ai_enabled else 'DISABLED - the app works without AI'} · "
    "AI creates proposals only; nothing is applied without your acceptance."
)

tab_proposals, tab_events = st.tabs(["AI Proposals", "Events"])

with tab_proposals:
    with session_scope(settings.db_url) as s:
        pending = list(
            s.scalars(
                select(AiProposal)
                .where(AiProposal.status.in_(("PENDING", "DEFERRED")))
                .order_by(AiProposal.created_at.desc())
            )
        )
        investments = {i.id: i for i in s.scalars(select(Investment))}
        rows = []
        for p in pending:
            inv = investments.get(p.investment_id)
            event = s.get(IntelligenceEvent, p.event_id) if p.event_id else None
            source = s.get(SourceDocument, event.source_document_id) if event and event.source_document_id else None
            thesis_current = None
            if p.proposal_type == "THESIS_REVISION" and inv:
                from src.research.theses import active_thesis, current_version

                t = active_thesis(s, inv)
                thesis_current = current_version(s, t) if t else None
            rows.append((p, inv, event, source, thesis_current))

    if not rows:
        st.success("No pending AI proposals. Run `ai analyze` / `ai redteam` after a sync to generate some.")
    for p, inv, event, source, thesis_current in rows:
        header = f"#{p.id} [{p.proposal_type}] {p.title}"
        if inv:
            header += f" — {inv.ticker}"
        with st.expander(header, expanded=False):
            c = st.columns(4)
            c[0].metric("Status", p.status)
            c[1].metric("AI confidence", f"{p.confidence}/100" if p.confidence is not None else "n/a")
            c[2].metric("Model", p.model or "-")
            c[3].metric("Prompt", p.prompt_version or "-")
            if event:
                st.markdown(f"**What happened (event):** [{event.severity}] {event.title}")
            if p.what_happened:
                st.markdown(f"**Analysis summary:** {p.what_happened[:600]}")
            if p.why_it_matters:
                st.markdown(f"**Why it matters:** {p.why_it_matters}")
            if source:
                tier = {1: "Tier 1 (primary)", 2: "Tier 2", 3: "Tier 3", 4: "Tier 4 (unverified)"}.get(source.source_tier)
                st.markdown(f"**Source:** {source.title or source.external_id} · {tier} · {source.url or 'archived locally'}")
            if p.reasoning:
                st.markdown(f"**AI reasoning:** {p.reasoning}")
            if p.proposed_change_json:
                st.markdown("**Proposed change:**")
                st.json(json.loads(p.proposed_change_json))
            if p.proposal_type == "THESIS_REVISION" and thesis_current is not None:
                st.markdown("**Side-by-side (CURRENT vs PROPOSED):**")
                cur_col, prop_col = st.columns(2)
                proposed = json.loads(p.proposed_change_json or "{}")
                for field in ("core_thesis", "market_expectation", "our_expectation", "why_market_may_be_wrong", "confidence"):
                    with cur_col:
                        st.markdown(f"*{field} (current v{thesis_current.version_number})*")
                        st.write(getattr(thesis_current, field, None) or "-")
                    with prop_col:
                        st.markdown(f"*{field} (proposed)*")
                        st.write(proposed.get(field) if proposed.get(field) is not None else "(unchanged)")

            reason = ""
            if p.proposal_type == "THESIS_REVISION":
                reason = st.text_input("Reason for revision (REQUIRED to accept)", key=f"reason{p.id}")
            note = st.text_input("Note (optional)", key=f"note{p.id}")
            b = st.columns(4)
            if b[0].button("ACCEPT", key=f"acc{p.id}", type="primary"):
                try:
                    with session_scope(settings.db_url) as s2:
                        res = accept_proposal(
                            s2, s2.get(AiProposal, p.id), base_currency=settings.base_currency,
                            reason_for_revision=reason or None, note=note or None,
                        )
                    st.success(res["action"])
                    st.rerun()
                except ResearchError as exc:
                    st.error(str(exc))
            if b[1].button("REJECT", key=f"rej{p.id}"):
                with session_scope(settings.db_url) as s2:
                    reject_proposal(s2, s2.get(AiProposal, p.id), note=note or None)
                st.rerun()
            if b[2].button("DEFER", key=f"def{p.id}") and p.status == "PENDING":
                with session_scope(settings.db_url) as s2:
                    defer_proposal(s2, s2.get(AiProposal, p.id), note=note or None)
                st.rerun()
            b[3].caption("Edit & accept: change the JSON via CLI (`proposals accept`) or edit fields after acceptance in Research.")

    with st.expander("Resolved proposals (history)"):
        with session_scope(settings.db_url) as s:
            resolved = list(
                s.scalars(
                    select(AiProposal)
                    .where(AiProposal.status.in_(("ACCEPTED", "REJECTED", "EDITED", "EXPIRED")))
                    .order_by(AiProposal.resolved_at.desc())
                    .limit(50)
                )
            )
        if resolved:
            st.dataframe(
                pd.DataFrame(
                    [{"Id": q.id, "Type": q.proposal_type, "Title": q.title[:60], "Status": q.status,
                      "Resolved": q.resolved_at, "Note": (q.resolution_note or "")[:60]} for q in resolved]
                ),
                hide_index=True, width="stretch",
            )
        else:
            st.write("None yet.")

with tab_events:
    with session_scope(settings.db_url) as s:
        events = list_events(s, limit=150)
        investments = {i.id: i for i in s.scalars(select(Investment))}
    if not events:
        st.info("No intelligence events yet. Run: `sec sync <ticker>`, `macro sync`, `insiders sync <ticker>`.")
    else:
        df = pd.DataFrame(
            [
                {"Id": e.id, "When": e.occurred_at.date(), "Severity": e.severity, "Type": e.event_type,
                 "Investment": investments[e.investment_id].ticker if e.investment_id in investments else "",
                 "Title": e.title, "State": e.processing_state, "AI": e.ai_state}
                for e in events
            ]
        )
        st.dataframe(df, hide_index=True, width="stretch")
        c = st.columns(3)
        ev_id = c[0].number_input("Event id", min_value=0, value=0, step=1)
        new_state = c[1].selectbox("Set state", ("PROCESSED", "DISMISSED", "NEW"))
        if c[2].button("Update event state") and ev_id:
            with session_scope(settings.db_url) as s2:
                e = s2.get(IntelligenceEvent, int(ev_id))
                if e:
                    set_event_state(s2, e, new_state)
            st.rerun()
        st.caption("Materiality is deterministic (see src/intelligence/events.py); AI never overrides it.")
