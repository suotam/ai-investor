"""System Health: operational visibility so nothing fails silently."""
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

from sqlalchemy import func, select  # noqa: E402

from src.config import load_settings  # noqa: E402
from src.db.intelligence import SourceDocument  # noqa: E402
from src.db.operations import PipelineRun, PipelineStage  # noqa: E402
from src.db.session import current_revision, session_scope  # noqa: E402
from src.intelligence.briefing.calibration import calibration_by_group, calibration_report  # noqa: E402
from src.operations.backups import latest_backup  # noqa: E402
from src.operations.doctor import overall_status, run_doctor  # noqa: E402

st.set_page_config(page_title="Investor OS - System", layout="wide", page_icon="🩺")
settings = load_settings()
st.title("System Health")

with session_scope(settings.db_url) as s:
    last_daily = s.scalars(select(PipelineRun).where(PipelineRun.pipeline == "daily")
                           .order_by(PipelineRun.started_at.desc())).first()
    last_weekly = s.scalars(select(PipelineRun).where(PipelineRun.pipeline == "weekly")
                            .order_by(PipelineRun.started_at.desc())).first()
    runs = list(s.scalars(select(PipelineRun).order_by(PipelineRun.started_at.desc()).limit(10)))
    stages_by_run = {
        r.id: list(s.scalars(select(PipelineStage).where(PipelineStage.run_id == r.id)))
        for r in runs
    }
    provider_last = {}
    for provider, source_type in (("sec_edgar", "filing"), ("sec_edgar", "form4"), ("fred", "macro_csv")):
        latest = s.scalars(
            select(SourceDocument).where(SourceDocument.provider == provider,
                                         SourceDocument.source_type == source_type)
            .order_by(SourceDocument.retrieved_at.desc())
        ).first()
        provider_last[f"{provider}/{source_type}"] = latest.retrieved_at if latest else None
    daily_data = {"status": last_daily.status, "at": last_daily.started_at} if last_daily else None
    weekly_data = {"status": last_weekly.status, "at": last_weekly.started_at} if last_weekly else None
    run_rows = [
        {"Id": r.id, "Pipeline": r.pipeline, "Started": r.started_at, "Status": r.status,
         "Failed stages": ", ".join(st_.stage for st_ in stages_by_run[r.id] if st_.status == "FAIL") or "-"}
        for r in runs
    ]
    cal = calibration_report(s)
    cal_groups = calibration_by_group(s, "category")

c = st.columns(5)
c[0].metric("Last daily run", f"{daily_data['status']} ({daily_data['at']:%m-%d %H:%M})" if daily_data else "never")
c[1].metric("Last weekly run", f"{weekly_data['status']} ({weekly_data['at']:%m-%d})" if weekly_data else "never")
c[2].metric("DB revision", current_revision(settings.db_url) or "?")
lb = latest_backup(settings)
c[3].metric("Last backup", lb.name.split("-", 2)[-1][:15] if lb else "none")
c[4].metric("AI", f"{settings.ai_provider} ({'on' if settings.ai_enabled else 'off'})")

st.subheader("Provider freshness")
prov_rows = []
for name, ts in provider_last.items():
    age = (date.today() - ts.date()).days if ts else None
    prov_rows.append({"Provider": name, "Last retrieved": ts, "Age (days)": age,
                      "State": "stale" if (age or 999) > 7 else "fresh" if ts else "never synced"})
st.dataframe(pd.DataFrame(prov_rows), hide_index=True, width="stretch")

st.subheader("Recent pipeline runs")
if run_rows:
    st.dataframe(pd.DataFrame(run_rows), hide_index=True, width="stretch")
else:
    st.info("No pipeline runs yet. Run: `python -m src.main run daily`")

st.subheader("Doctor")
if st.button("Run doctor checks"):
    checks = run_doctor(settings)
    for chk in checks:
        icon = {"OK": "✅", "WARN": "🟡", "FAIL": "🔴"}[chk.status]
        st.markdown(f"{icon} **{chk.name}**" + (f" — {chk.detail}" if chk.detail else ""))
    st.markdown(f"**Overall: {overall_status(checks)}**")

st.subheader("Prediction calibration")
if cal.sufficient:
    st.markdown(f"Resolved: {cal.resolved} · Brier **{cal.brier_score}** · hit rate {cal.hit_rate:.0%}")
    st.dataframe(pd.DataFrame(cal.buckets), hide_index=True, width="stretch")
    st.caption(cal.note)
    if cal_groups:
        st.markdown("By category:")
        st.dataframe(pd.DataFrame([{"group": k, **v} for k, v in cal_groups.items()]),
                     hide_index=True, width="stretch")
else:
    st.info(cal.note)

st.caption("Task Scheduler: register with scripts\\register_tasks.ps1 (explicit action; "
           "inspect via `schtasks /Query /TN \"InvestorOS Daily\"`).")
