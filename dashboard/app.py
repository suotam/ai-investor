"""Investor OS dashboard (Streamlit). Local only: run via `python -m src.main dashboard`
or `streamlit run dashboard/app.py --server.address 127.0.0.1`.

The dashboard only DISPLAYS numbers computed by the Python engine. No trading functionality.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dashboard import data as dd  # noqa: E402

# Categorical palette (validated default, fixed order - never cycled)
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
NEUTRAL = "#9a9a96"
UNAVAILABLE = "Unavailable"

st.set_page_config(page_title="Investor OS", layout="wide", page_icon="📈")

settings = dd.get_settings()
BASE = settings.base_currency


@st.cache_data(show_spinner="Computing portfolio...")
def _overview(sig: float):
    return dd.load_overview(settings)


@st.cache_data(show_spinner=False)
def _stats(sig: float):
    return dd.load_trading_stats(settings)


@st.cache_data(show_spinner=False)
def _status(sig: float):
    return dd.load_status(settings)


def money(v, ccy: str = BASE) -> str:
    return UNAVAILABLE if v is None else f"{v:,.0f} {ccy}"


def pct(v) -> str:
    return UNAVAILABLE if v is None else f"{v * 100:+.2f}%"


def chart_layout(fig: go.Figure, title: str, ytitle: str = "") -> go.Figure:
    fig.update_layout(
        title=title,
        margin=dict(l=10, r=10, t=40, b=10),
        height=380,
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        yaxis=dict(title=ytitle, gridcolor="rgba(128,128,128,0.15)", zeroline=False),
        xaxis=dict(showgrid=False),
    )
    return fig


def other_fold(df: pd.DataFrame, label_col: str, value_col: str, max_slices: int = 7) -> pd.DataFrame:
    df = df.sort_values(value_col, ascending=False)
    if len(df) <= max_slices:
        return df
    head = df.iloc[:max_slices]
    other = pd.DataFrame({label_col: ["Other"], value_col: [df.iloc[max_slices:][value_col].sum()]})
    return pd.concat([head, other], ignore_index=True)


def donut(df: pd.DataFrame, label_col: str, value_col: str, title: str) -> None:
    df = df[df[value_col].notna() & (df[value_col] > 0)]
    if df.empty:
        st.info(f"{title}: {UNAVAILABLE} (no valued positions)")
        return
    df = other_fold(df.groupby(label_col, as_index=False)[value_col].sum(), label_col, value_col)
    colors = [SERIES[i] if lab != "Other" else NEUTRAL for i, lab in enumerate(df[label_col])]
    fig = go.Figure(
        go.Pie(
            labels=df[label_col],
            values=df[value_col],
            hole=0.55,
            marker=dict(colors=colors, line=dict(color="white", width=2)),
            textinfo="label+percent",
            sort=False,
        )
    )
    fig.update_layout(title=title, margin=dict(l=10, r=10, t=40, b=10), height=340, showlegend=False)
    st.plotly_chart(fig, width="stretch")


status = _status(dd.db_signature(settings))
if not status.get("db_exists"):
    st.error(f"Database not found at {settings.db_path}. Run `python -m src.main init-db` first.")
    st.stop()

st.title("Investor OS")
st.caption(
    f"Local portfolio core · base currency {BASE} · read-only · "
    f"last price date: {status.get('last_price_date') or UNAVAILABLE}"
)

if status["counts"]["transactions"] == 0:
    st.warning(
        "No transactions yet. Import data first: `python -m src.main sync-ibkr --file report.xml` "
        "or `python -m src.main import-crypto file.csv`, then `rebuild-portfolio` and `update-prices`."
    )

ov = _overview(dd.db_signature(settings))

tab_overview, tab_positions, tab_alloc, tab_perf, tab_trades, tab_sync = st.tabs(
    ["Overview", "Positions", "Allocation", "Performance", "Trading Statistics", "Import / Sync"]
)

# ---------------------------------------------------------------- Overview
with tab_overview:
    if ov["incomplete"]:
        with st.expander(f"⚠ Some values are unavailable ({len(ov['issues'])} issues)", expanded=False):
            for i in ov["issues"]:
                st.write(f"- {i}")
    c = st.columns(4)
    c[0].metric("Portfolio Value", money(ov["total_value"]))
    c[1].metric("Cash", money(ov["cash"]))
    c[2].metric("Invested Value", money(ov["invested"]))
    c[3].metric("Positions", ov["positions_count"])
    c = st.columns(4)
    c[0].metric("Today's P&L", money(ov["today_pnl"]))
    c[1].metric("Total P&L (unrealized + realized)", money(
        (ov["unrealized_pnl"] + ov["realized_pnl"]) if ov["unrealized_pnl"] is not None and ov["realized_pnl"] is not None else None
    ))
    c[2].metric("Total Return (TWR)", pct(ov["twr"]))
    c[3].metric(f"Benchmark Return ({ov['benchmark_label']})", pct(ov["benchmark_return"]))
    c = st.columns(4)
    c[0].metric("Net Deposits", money(ov["net_external_flows"]))
    c[1].metric("Simple Return", pct(ov["simple_return"]))
    c[2].metric("Money-Weighted Return (annualized XIRR)", pct(ov["mwr"]))
    c[3].metric("Max Drawdown (TWR index)", pct(ov["max_drawdown"]))
    if ov["cash_by_currency"]:
        st.caption("Cash by currency: " + ", ".join(f"{k} {v:,.2f}" for k, v in ov["cash_by_currency"].items()))

    hist = ov["history"]
    if hist.empty or hist["value"].isna().all():
        st.info("Portfolio value history is unavailable - run `update-prices` after importing transactions.")
    else:
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=hist["date"], y=hist["value"], name=f"Portfolio value ({BASE})", line=dict(color=SERIES[0], width=2)))
        fig.add_trace(go.Scatter(x=hist["date"], y=hist["cash"], name="Cash", line=dict(color=SERIES[2], width=1.5, dash="dot")))
        st.plotly_chart(chart_layout(fig, "Portfolio value over time", BASE), width="stretch")

        twr_df, bm_df = ov["twr_index"], ov["benchmark"]
        if not twr_df.empty:
            fig = go.Figure()
            fig.add_trace(go.Scatter(x=twr_df["date"], y=twr_df["level"], name="Portfolio (TWR, =100)", line=dict(color=SERIES[0], width=2)))
            if not bm_df.empty:
                fig.add_trace(go.Scatter(x=bm_df["date"], y=bm_df["level"], name=f"{ov['benchmark_label']} (=100)", line=dict(color=SERIES[1], width=2)))
            st.plotly_chart(chart_layout(fig, "Portfolio vs benchmark (normalized to 100)", "index"), width="stretch")
        else:
            st.info("TWR index unavailable: at least one day in the history cannot be valued (missing price or FX).")
        if ov["history_issues"]:
            with st.expander("History valuation issues"):
                for i in ov["history_issues"]:
                    st.write(f"- {i}")

# ---------------------------------------------------------------- Positions
with tab_positions:
    df = pd.DataFrame(ov["positions"])
    if df.empty:
        st.info("No open positions.")
    else:
        cols = ["Ticker", "Company", "Account", "Quantity", "Average Cost", "Current Price", "Price Currency", "Currency",
                "Market Value (local)", f"Market Value ({BASE})", "Weight", "Unrealized P/L (local)", "Return %", "Price Date", "Issues"]
        show = df[cols].copy()
        show["Weight"] = show["Weight"] * 100
        show["Return %"] = show["Return %"] * 100
        st.dataframe(
            show.sort_values(f"Market Value ({BASE})", ascending=False, na_position="last"),
            width="stretch",
            hide_index=True,
            column_config={
                "Weight": st.column_config.NumberColumn(format="%.2f%%"),
                "Return %": st.column_config.NumberColumn(format="%.2f%%"),
            },
        )
        st.caption("Average cost includes commissions (average-cost method). Blank cells = Unavailable.")

# ---------------------------------------------------------------- Allocation
with tab_alloc:
    df = pd.DataFrame(ov["positions"])
    if df.empty:
        st.info("No open positions.")
    else:
        mv = f"Market Value ({BASE})"
        c1, c2 = st.columns(2)
        with c1:
            donut(df, "Ticker", mv, "Allocation by instrument")
            donut(df, "Currency", mv, "Allocation by currency")
        with c2:
            donut(df, "Sector", mv, "Allocation by sector (Unknown = no metadata)")
            donut(df, "Asset", mv, "Allocation by asset class")
        st.caption("Sector metadata is not fetched automatically in v1; edit `instruments.sector` to classify.")

# ---------------------------------------------------------------- Performance
with tab_perf:
    c = st.columns(3)
    c[0].metric("Unrealized P/L", money(ov["unrealized_pnl"]))
    c[1].metric("Realized P/L (avg cost, lifetime)", money(ov["realized_pnl"]))
    c[2].metric("Total P/L from value history", money(ov["total_pnl_history"]))
    st.caption(
        "Realized + unrealized (positions only) differs from history P/L by dividends, interest, fees, "
        "taxes and FX moves on cash - both are shown on purpose."
    )
    attr = pd.DataFrame(ov["attribution"])
    if not attr.empty and attr["Total P/L"].notna().any():
        valid = attr[attr["Total P/L"].notna()].sort_values("Total P/L")
        fig = go.Figure(
            go.Bar(
                x=valid["Total P/L"],
                y=valid["Ticker"],
                orientation="h",
                marker=dict(color=[SERIES[2] if v >= 0 else SERIES[7] for v in valid["Total P/L"]]),
            )
        )
        fig.update_layout(height=max(250, 28 * len(valid) + 80))
        st.plotly_chart(chart_layout(fig, f"P/L per instrument ({BASE}, realized + unrealized)", ""), width="stretch")
        c1, c2 = st.columns(2)
        c1.subheader("Top contributors")
        c1.dataframe(valid.sort_values("Total P/L", ascending=False).head(5), hide_index=True, width="stretch")
        c2.subheader("Bottom contributors")
        c2.dataframe(valid.head(5), hide_index=True, width="stretch")
        st.caption("'Contribution (simple)' = instrument P/L / (initial value + net deposits). Time-weighted attribution: TODO v2.")
    else:
        st.info("Attribution unavailable (no valued positions).")

    ddf = ov["drawdown"]
    if not ddf.empty:
        fig = go.Figure(go.Scatter(x=ddf["date"], y=ddf["dd"], fill="tozeroy", line=dict(color=SERIES[7], width=1.5), name="Drawdown"))
        fig.update_yaxes(tickformat=".0%")
        st.plotly_chart(chart_layout(fig, "Drawdown of the TWR index", ""), width="stretch")
    else:
        st.info("Drawdown curve unavailable (TWR index cannot be computed for the full history).")

# ---------------------------------------------------------------- Trading statistics
with tab_trades:
    ts = _stats(dd.db_signature(settings))
    st.info("Outcome metrics do not necessarily measure decision quality.")
    if ts["closed_trades"] == 0:
        st.write("No closed trades yet.")
    else:
        if ts["incomplete"]:
            st.warning("Some closed trades lack FX rates; aggregate statistics are Unavailable.")
        c = st.columns(4)
        c[0].metric("Closed trades (realization events)", ts["closed_trades"])
        c[1].metric("Win rate", f"{ts['win_rate'] * 100:.1f}%" if ts["win_rate"] is not None else UNAVAILABLE)
        c[2].metric("Profit factor", f"{ts['profit_factor']:.2f}" if ts["profit_factor"] is not None else UNAVAILABLE)
        c[3].metric("Avg holding period (days)", f"{ts['average_holding_days']:.0f}" if ts["average_holding_days"] is not None else UNAVAILABLE)
        c = st.columns(4)
        c[0].metric("Average winner", money(ts["average_winner"]))
        c[1].metric("Average loser", money(ts["average_loser"]))
        c[2].metric("Largest winner", money(ts["largest_winner"]))
        c[3].metric("Largest loser", money(ts["largest_loser"]))
        st.dataframe(pd.DataFrame(ts["rows"]), hide_index=True, width="stretch")

# ---------------------------------------------------------------- Sync
with tab_sync:
    c = st.columns(4)
    c[0].metric("Last IBKR sync", str(status["last"]["sync-ibkr"] or "never")[:16])
    c[1].metric("Last crypto import", str(status["last"]["import-crypto"] or "never")[:16])
    c[2].metric("Last market data update", str(status["last"]["update-prices"] or "never")[:16])
    c[3].metric("DB revision", status["revision"] or UNAVAILABLE)
    st.write("**Database:**", status["db_path"])
    st.write(" | ".join(f"{k}: {v}" for k, v in status["counts"].items()))
    if status["accounts"]:
        st.dataframe(pd.DataFrame(status["accounts"]), hide_index=True, width="stretch")
    st.subheader("Recent jobs")
    runs = pd.DataFrame(status["runs"])
    if runs.empty:
        st.write("No jobs recorded yet.")
    else:
        st.dataframe(runs, hide_index=True, width="stretch")
    st.markdown(
        """
**Commands** (run in the project folder with the venv activated):

```
python -m src.main sync-ibkr            # IBKR Flex (read-only), needs .env
python -m src.main sync-ibkr --file report.xml
python -m src.main import-crypto export.csv
python -m src.main rebuild-portfolio
python -m src.main update-prices
python -m src.main snapshot
```
"""
    )
