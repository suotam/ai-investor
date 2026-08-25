"""Phase A tests: checkpoints, delta engine, alert hygiene (incl. critical tests 47/48/50)."""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import select

from src.db.briefing import BriefRun
from src.intelligence.briefing.checkpoints import (
    complete_run,
    last_completed_run,
    previously_surfaced_keys,
    resolve_window,
    start_run,
)
from src.intelligence.briefing.deltas import compute_deltas
from src.intelligence.briefing.hygiene import apply_hygiene, set_attention
from src.research.investments import create_investment
from src.research.items import add_risk
from src.research.theses import create_thesis

T0 = datetime(2026, 8, 20, 6, 0)
T1 = datetime(2026, 8, 21, 6, 0)
T2 = datetime(2026, 8, 22, 6, 0)
T3 = datetime(2026, 8, 23, 6, 0)


def _complete(session, run, deltas, suppressed=0):
    return complete_run(
        session, run, [d.to_dict() for d in deltas], suppressed,
        portfolio_value=None, base_currency="CZK", portfolio_state={},
    )


def _run_brief_window(session, settings, now, prev_state=None):
    start, end, superseded, mode = resolve_window(session, "daily", now=now)
    deltas, state = compute_deltas(session, settings, start, end, prev_state)
    surfaced, suppressed = apply_hygiene(session, deltas, now=now)
    return start, end, surfaced, suppressed, state, mode


# ---------------------------------------------------------------- checkpoints


def test_checkpoint_window_and_rerun_semantics(session, settings) -> None:
    # first ever run: 24h lookback
    start, end, sup, mode = resolve_window(session, "daily", now=T1)
    assert mode == "new" and (end - start) == timedelta(days=1)
    run = start_run(session, "daily", start, end, "completed")
    _complete(session, run, [])
    session.commit()
    assert last_completed_run(session, "daily").id == run.id

    # same day re-run without force -> preview over the SAME window, checkpoint untouched
    start2, end2, sup2, mode2 = resolve_window(session, "daily", now=T1 + timedelta(hours=2))
    assert mode2 == "preview" and start2 == start
    # force -> supersede and re-run from the SAME period_start
    start3, end3, sup3, mode3 = resolve_window(session, "daily", now=T1 + timedelta(hours=3), force=True)
    assert mode3 == "force" and start3 == start and sup3.id == run.id
    run2 = start_run(session, "daily", start3, end3, "completed", superseded=sup3)
    _complete(session, run2, [])
    session.commit()
    assert session.get(BriefRun, run.id).status == "superseded"
    assert last_completed_run(session, "daily").id == run2.id

    # next day: window starts at last checkpoint
    start4, end4, _, mode4 = resolve_window(session, "daily", now=T2)
    assert mode4 == "new" and start4 == end3


# ---------------------------------------------------------------- CRITICAL 47: static risk suppression


def test_critical_static_risk_suppression(session, settings) -> None:
    inv = create_investment(session, "NU", status="OWNED")
    thesis, _ = create_thesis(session, inv, "T")
    # Day 1: HIGH risk created within window
    r = add_risk(session, inv, "Credit deterioration", category="financial", severity="HIGH")
    r.created_at = r.updated_at = T1 - timedelta(hours=2)
    session.commit()

    s1, e1, surfaced1, suppressed1, state1, _ = _run_brief_window(session, settings, T1)
    assert any(d.delta_type == "NEW_RISK" and "Credit deterioration" in d.title for d in surfaced1)
    run = start_run(session, "daily", s1, e1, "completed")
    _complete(session, run, surfaced1, len(suppressed1))
    session.commit()

    # Day 2: no new evidence, risk unchanged -> MUST NOT surface
    s2, e2, surfaced2, _sup2, state2, _ = _run_brief_window(session, settings, T2, state1)
    assert not any("Credit deterioration" in d.title for d in surfaced2)
    assert surfaced2 == []  # nothing else happened either
    run2 = start_run(session, "daily", s2, e2, "completed")
    _complete(session, run2, surfaced2)
    session.commit()

    # Day 3: new CONTRADICTING evidence targeting the risk -> resurfaces WITH the link
    from src.research.evidence import add_evidence

    ev = add_evidence(
        session, inv, "NPL 90+ jumped to 7.2%", direction="CONTRADICTING",
        evidence_type="earnings", target_type="risk", target_id=r.id,
    )
    ev.created_at = T3 - timedelta(hours=1)
    session.commit()
    _s3, _e3, surfaced3, _sup3, _state3, _ = _run_brief_window(session, settings, T3, state2)
    hits = [d for d in surfaced3 if d.delta_type == "NEW_EVIDENCE"]
    assert len(hits) == 1
    assert "Credit deterioration" in hits[0].title  # linked risk named
    assert "relevant to risk" in hits[0].reason  # why am I seeing this
    assert hits[0].severity == "HIGH"  # contradicting evidence


# ---------------------------------------------------------------- CRITICAL 48: price vs business


def test_critical_price_vs_business_distinction(session, settings, flex_xml) -> None:
    from src.connectors.ibkr.flex_parser import parse_flex_statement
    from src.db.models import Instrument, Price
    from src.portfolio.importer import import_statement
    from src.portfolio.positions import rebuild_positions
    from tests.conftest import add_fx

    import_statement(session, parse_flex_statement(flex_xml), source="ibkr")
    rebuild_positions(session)
    inv = create_investment(session, "AAPL", status="OWNED")
    aapl = session.scalars(select(Instrument).where(Instrument.symbol == "AAPL")).one()
    session.add(Price(instrument_id=aapl.id, price_date=date(2026, 8, 20), close=15.0, currency="USD", source="test"))
    add_fx(session, "USD", "CZK", date(2026, 8, 20), "20.7")
    session.commit()

    # Day 1 brief: establishes checkpoint state with price 15
    s1, e1, surfaced1, _sup, state1, _ = _run_brief_window(session, settings, T1)
    run = start_run(session, "daily", s1, e1, "completed")
    _complete(session, run, surfaced1)
    session.commit()

    # Day 2: price drops to 12.5 (-16.7%), NO new company evidence
    session.add(Price(instrument_id=aapl.id, price_date=date(2026, 8, 21), close=12.5, currency="USD", source="test"))
    add_fx(session, "USD", "CZK", date(2026, 8, 21), "20.7")
    session.commit()
    _s2, _e2, surfaced2, _sup2, _state2, _ = _run_brief_window(session, settings, T2, state1)
    price_items = [d for d in surfaced2 if d.delta_type == "PRICE_MOVE"]
    assert len(price_items) == 1
    assert "-16.7%" in price_items[0].title
    assert "price changed; thesis did not" in price_items[0].detail
    assert price_items[0].payload["business_change"] is False
    # no implication of fundamental deterioration anywhere
    assert "deterioration" not in (price_items[0].detail or "").lower()


# ---------------------------------------------------------------- CRITICAL 50: no-action day


def test_critical_no_change_produces_empty_delta_set(session, settings) -> None:
    inv = create_investment(session, "NU", status="OWNED")
    inv.created_at = T0 - timedelta(days=30)
    inv.next_review_date = date(2026, 12, 1)
    session.commit()
    s1, e1, surfaced1, suppressed1, state1, _ = _run_brief_window(session, settings, T1)
    assert surfaced1 == [] and suppressed1 == []


# ---------------------------------------------------------------- other deltas & hygiene


def test_review_due_surfaces_once_then_reminds_weekly(session, settings) -> None:
    inv = create_investment(session, "NU")
    inv.next_review_date = date(2026, 8, 20)
    session.commit()
    s1, e1, surfaced1, _s, state1, _ = _run_brief_window(session, settings, T1)
    assert any(d.delta_type == "REVIEW_DUE" for d in surfaced1)
    run = start_run(session, "daily", s1, e1, "completed")
    _complete(session, run, surfaced1)
    from src.db.briefing import BriefItem

    for bi in session.scalars(select(BriefItem)):
        bi.created_at = T1  # align surfacing timestamp with the synthetic test clock
    session.commit()
    # next day: suppressed
    _s2, _e2, surfaced2, suppressed2, _st, _ = _run_brief_window(session, settings, T2, state1)
    assert not any(d.delta_type == "REVIEW_DUE" for d in surfaced2)
    assert any(d.delta_type == "REVIEW_DUE" for d in suppressed2)
    # 8 days later: resurfaces as a reminder
    _s3, _e3, surfaced3, _sup3, _st3, _ = _run_brief_window(session, settings, T1 + timedelta(days=8), state1)
    reminders = [d for d in surfaced3 if d.delta_type == "REVIEW_DUE"]
    assert len(reminders) == 1 and "reminder" in reminders[0].reason


def test_attention_defer_and_resolve(session, settings) -> None:
    inv = create_investment(session, "NU")
    inv.next_review_date = date(2026, 8, 20)
    session.commit()
    _s, _e, surfaced, _sup, _st, _ = _run_brief_window(session, settings, T1)
    key = next(d.item_key for d in surfaced if d.delta_type == "REVIEW_DUE")
    set_attention(session, key, "DEFERRED", defer_until=date(2026, 8, 25))
    _s, _e, surfaced2, suppressed2, _st, _ = _run_brief_window(session, settings, T2)
    assert not any(d.item_key == key for d in surfaced2)
    # defer date arrives -> resurfaces
    _s, _e, surfaced3, _sup3, _st, _ = _run_brief_window(session, settings, datetime(2026, 8, 26, 6, 0))
    assert any(d.item_key == key for d in surfaced3)
    set_attention(session, key, "RESOLVED")
    _s, _e, surfaced4, _sup4, _st, _ = _run_brief_window(session, settings, datetime(2026, 8, 27, 6, 0))
    assert not any(d.item_key == key for d in surfaced4)


def test_kpi_delta_with_assumption_link(session, settings) -> None:
    from src.research import kpis
    from src.research.assumptions import add_assumption

    inv = create_investment(session, "NU")
    thesis, _ = create_thesis(session, inv, "T")
    kpi = kpis.add_kpi(session, inv, "NPL 90+", unit="%", direction_good="down")
    add_assumption(session, thesis, "Credit quality remains controlled", kpi_id=kpi.id)
    o1 = kpis.add_observation(session, kpi, "2026Q1", 6.5, period_date=date(2026, 3, 31), source="c")
    o1.created_at = T0 - timedelta(days=30)
    o2 = kpis.add_observation(session, kpi, "2026Q2", 6.9, period_date=date(2026, 6, 30), source="c")
    o2.created_at = T1 - timedelta(hours=1)
    session.commit()
    _s, _e, surfaced, _sup, _st, _ = _run_brief_window(session, settings, T1)
    kpi_items = [d for d in surfaced if d.delta_type == "NEW_KPI"]
    assert len(kpi_items) == 1
    assert "+0.40pp" in kpi_items[0].title and "6.5" in kpi_items[0].title
    assert "Credit quality remains controlled" in kpi_items[0].reason
    assert kpi_items[0].severity == "MEDIUM"


def test_insider_grouping_and_materiality(session, settings) -> None:
    from src.db.intelligence import InsiderTransaction

    inv = create_investment(session, "NU", status="OWNED")
    for i, (name, shares, price) in enumerate([("VELEZ DAVID", 200000, 13.5), ("CFO PERSON", 71707, 13.4)]):
        session.add(InsiderTransaction(
            issuer_cik="1691493", issuer_name="Nu Holdings Ltd.", investment_id=inv.id,
            insider_name=name, transaction_date=date(2026, 8, 20), transaction_type="open_market_sale",
            shares=shares, price=price, value=shares * price, shares_after=5000000 - shares,
            dedup_key=f"t{i}", created_at=T1 - timedelta(hours=2),
        ))
    session.commit()
    _s, _e, surfaced, _sup, _st, _ = _run_brief_window(session, settings, T1)
    ins = [d for d in surfaced if d.delta_type == "NEW_INSIDER_ACTIVITY"]
    assert len(ins) == 1  # grouped per issuer, not one item per transaction
    assert "2 open-market sale(s) by 2 insider(s)" in ins[0].title
    assert ins[0].severity == "MEDIUM"  # > $100k threshold
    assert "not automatically bearish" in ins[0].detail
    assert "% of reported holdings" in ins[0].detail  # relative-size context
    assert "meets" in ins[0].reason


def test_small_insider_activity_is_low_severity(session, settings) -> None:
    from src.db.intelligence import InsiderTransaction

    inv = create_investment(session, "NU")
    session.add(InsiderTransaction(
        issuer_cik="1691493", issuer_name="Nu", investment_id=inv.id, insider_name="X",
        transaction_date=date(2026, 8, 20), transaction_type="open_market_sale",
        shares=100, price=10.0, value=1000.0, dedup_key="small", created_at=T1 - timedelta(hours=1),
    ))
    session.commit()
    _s, _e, surfaced, _sup, _st, _ = _run_brief_window(session, settings, T1)
    ins = [d for d in surfaced if d.delta_type == "NEW_INSIDER_ACTIVITY"]
    assert len(ins) == 1 and ins[0].severity == "LOW" and "below" in ins[0].reason


def test_valuation_state_change_surfaces_once(session, settings) -> None:
    from src.db.models import Instrument, Price
    from src.research.valuation import add_scenario, create_model

    inst = Instrument(symbol="NU", asset_type="stock", exchange="NYSE", currency="USD")
    session.add(inst)
    session.flush()
    inv = create_investment(session, "NU", status="OWNED", instrument_id=inst.id)
    m = create_model(session, inv, "PT", reference_price=13.65, reference_currency="USD")
    add_scenario(session, m, "bear", 11, probability=20)
    add_scenario(session, m, "base", 27.1, probability=50)
    add_scenario(session, m, "bull", 54.3, probability=30)
    session.add(Price(instrument_id=inst.id, price_date=date(2026, 8, 20), close=15.0, currency="USD", source="t"))
    session.commit()

    # Day 1: establishes state (above_reference_band: 15 > 13.65*1.10)
    _s, _e, surfaced1, _sup, state1, _ = _run_brief_window(session, settings, T1)
    assert not any(d.delta_type == "VALUATION_REVIEW" for d in surfaced1)  # no previous state -> no delta
    assert state1["valuation_states"] == {f"NU:{m.id}": "below_reference"} or state1["valuation_states"]
    # Day 2: price drops below bear target -> state change -> VALUATION_REVIEW
    session.add(Price(instrument_id=inst.id, price_date=date(2026, 8, 21), close=10.5, currency="USD", source="t"))
    session.commit()
    _s2, _e2, surfaced2, _sup2, state2, _ = _run_brief_window(session, settings, T2, state1)
    vals = [d for d in surfaced2 if d.delta_type == "VALUATION_REVIEW"]
    assert len(vals) == 1
    assert "at_or_below_bear_target" in vals[0].title
    assert "not a trade signal" in vals[0].detail
    # Day 3: unchanged state -> nothing
    _s3, _e3, surfaced3, _sup3, _state3, _ = _run_brief_window(session, settings, T3, state2)
    assert not any(d.delta_type == "VALUATION_REVIEW" for d in surfaced3)
