"""Phase E tests: briefs, calendar, discovery, migration preservation."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import func, select

from src.db.intelligence import InstitutionalManager, ResearchCandidate
from src.intelligence.briefs import daily_brief, weekly_brief
from src.intelligence.calendar import upcoming_events
from src.intelligence.discovery import (
    InsiderBuyingFactor,
    ThirteenFNewPositionsFactor,
    add_manual_candidate,
    dismiss_candidate,
    promote_candidate,
    run_discovery,
)
from src.intelligence.events import record_event
from src.research.investments import ResearchError, create_investment, get_by_ticker
from src.research.items import add_catalyst
from src.research.predictions import create_prediction

TODAY = date(2026, 8, 24)


def test_calendar_known_dates_only(session) -> None:
    inv = create_investment(session, "NU", status="OWNED")
    inv.next_review_date = TODAY + timedelta(days=5)
    add_catalyst(session, inv, "Mexico license", expected_date=TODAY + timedelta(days=20))
    add_catalyst(session, inv, "No date catalyst")  # no expected_date -> never fabricated
    create_prediction(session, "NPL < 6%", 70, investment=inv, resolution_date=TODAY + timedelta(days=3))
    session.commit()
    week = upcoming_events(session, days=7, today=TODAY)
    assert [e["kind"] for e in week] == ["prediction due", "thesis review"]
    month = upcoming_events(session, days=30, today=TODAY)
    assert len(month) == 3
    assert all(e["date"] is not None for e in month)


def test_daily_and_weekly_brief(session, settings) -> None:
    inv = create_investment(session, "NU", status="OWNED")
    from src.research.theses import create_thesis

    create_thesis(session, inv, "T", core_thesis="Growth")
    record_event(session, "EARNINGS_RELEASE", "e1", "NU Q2 results", datetime.now(), investment_id=inv.id)
    from src.intelligence.ai.proposals import create_proposal

    create_proposal(session, "NEW_RISK", "Credit risk", investment=inv,
                    proposed_change={"name": "Credit", "category": "financial", "severity": "HIGH"})
    session.commit()
    daily = daily_brief(session, settings)
    assert "INVESTOR OS DAILY BRIEF" in daily
    assert "NU Q2 results" in daily  # HIGH severity earnings event surfaces
    assert "AI PROPOSALS AWAITING YOUR REVIEW (1)" in daily
    assert "Nothing has been applied" in daily
    weekly = weekly_brief(session, settings)
    assert "WEEKLY REVIEW" in weekly and "THESIS HEALTH" in weekly
    assert "WHAT CHANGED THIS WEEK THAT ACTUALLY MATTERS?" in weekly
    assert "NU Q2 results" in weekly
    # briefs never contain trade advice
    for text in (daily, weekly):
        assert "BUY NOW" not in text.upper() and "SELL NOW" not in text.upper()


def test_discovery_factors_and_promotion(session) -> None:
    # 13F factor: tracked manager with NEW position in latest period
    from src.db.models import Instrument
    from src.intelligence.connectors.institutional import add_manager, store_holdings

    inst = Instrument(symbol="MELI", asset_type="stock", exchange="NASDAQ", currency="USD", cusip="58733R102")
    session.add(inst)
    session.flush()
    mgr = add_manager(session, "Tracked Cap", "123456")
    store_holdings(session, mgr, date(2026, 3, 31), [
        {"cusip": "037833100", "issuer_name": "APPLE INC", "shares": 100.0, "value_usd": 1.0}], None)
    store_holdings(session, mgr, date(2026, 6, 30), [
        {"cusip": "037833100", "issuer_name": "APPLE INC", "shares": 100.0, "value_usd": 1.0},
        {"cusip": "58733R102", "issuer_name": "MERCADOLIBRE INC", "shares": 500.0, "value_usd": 2.0}], None)
    session.commit()
    res = run_discovery(session, factors=[ThirteenFNewPositionsFactor()])
    session.commit()
    assert res["created"] == 1
    c = session.scalars(select(ResearchCandidate)).one()
    assert c.ticker == "MELI" and "NEW_POSITION" in c.reasons_json
    assert "not endorsement" in c.reasons_json
    # idempotent
    res2 = run_discovery(session, factors=[ThirteenFNewPositionsFactor()])
    assert res2["created"] == 0
    # promote -> creates investment via v2 service
    inv = promote_candidate(session, c)
    assert inv.status == "DISCOVERED" and c.status == "PROMOTED"
    assert get_by_ticker(session, "MELI") is not None
    with pytest.raises(ResearchError):
        promote_candidate(session, c)  # already promoted
    # discovery skips tickers already in the research pipeline
    res3 = run_discovery(session, factors=[ThirteenFNewPositionsFactor()])
    assert res3["created"] == 0


def test_discovery_insider_cluster(session) -> None:
    from src.db.intelligence import InsiderTransaction
    from src.db.models import Instrument
    from src.intelligence.entities import remember_cik

    inst = Instrument(symbol="XYZ", asset_type="stock", exchange="NYSE", currency="USD")
    session.add(inst)
    session.flush()
    remember_cik(session, inst, "999")
    for i, name in enumerate(["A", "B"]):
        session.add(InsiderTransaction(
            issuer_cik="999", issuer_name="XYZ Corp", insider_name=name,
            transaction_date=date.today() - timedelta(days=10), transaction_type="open_market_purchase",
            shares=1000, price=10.0, value=10000.0, dedup_key=f"d{i}",
        ))
    # single insider elsewhere -> no cluster
    session.add(InsiderTransaction(
        issuer_cik="888", issuer_name="Solo Corp", insider_name="C",
        transaction_date=date.today() - timedelta(days=10), transaction_type="open_market_purchase",
        shares=1, price=1.0, value=1.0, dedup_key="d9",
    ))
    session.commit()
    res = run_discovery(session, factors=[InsiderBuyingFactor()])
    cands = list(session.scalars(select(ResearchCandidate)))
    assert res["created"] == 1 and cands[0].ticker == "XYZ"
    assert "2 insiders bought" in cands[0].reasons_json


def test_manual_candidate_and_dismiss(session) -> None:
    c = add_manual_candidate(session, "pltr", "Palantir", ["adjacent to portfolio theme"])
    assert c.ticker == "PLTR"
    with pytest.raises(ResearchError):
        add_manual_candidate(session, "PLTR", None, [])
    dismiss_candidate(session, c, note="too expensive to research now")
    assert c.status == "DISMISSED"


def test_migration_0004_preserves_v2_data(settings) -> None:
    """0003 -> 0004 with populated v1+v2 data: everything identical after upgrade."""
    import sqlite3

    from alembic import command
    from src.db.session import alembic_config, dispose_engine

    command.upgrade(alembic_config(settings.db_url), "0003_research_layer")
    con = sqlite3.connect(settings.db_path)
    con.execute("INSERT INTO accounts (id,name,provider,account_external_id,base_currency,active,created_at)"
                " VALUES (1,'a','ibkr','U1','CZK',1,'2026-01-01')")
    con.execute("INSERT INTO instruments (id,symbol,asset_type,currency,created_at)"
                " VALUES (1,'NU','stock','USD','2026-01-01')")
    con.execute("INSERT INTO transactions (account_id,instrument_id,transaction_type,trade_date,quantity,price,"
                "currency,gross_amount,commission,fees,net_amount,source,source_hash,imported_at)"
                " VALUES (1,1,'buy','2026-08-19',30,14.36,'USD',-430.8,-1.0,0,-431.8,'ibkr','h1','2026-01-01')")
    con.execute("INSERT INTO investments (id,ticker,name,status,review_frequency,created_by,created_at,updated_at)"
                " VALUES (1,'NU','Nu Holdings','OWNED','quarterly','USER','2026-01-01','2026-01-01')")
    con.execute("INSERT INTO theses (id,investment_id,title,active,created_by,created_at)"
                " VALUES (1,1,'NU thesis',1,'USER','2026-01-01')")
    con.execute("INSERT INTO thesis_versions (id,thesis_id,version_number,reason_for_revision,core_thesis,"
                "status,created_by,created_at) VALUES (1,1,1,'original','Growth > 30%','ACTIVE','USER','2026-01-01')")
    con.commit()
    con.close()

    command.upgrade(alembic_config(settings.db_url), "head")

    con = sqlite3.connect(settings.db_path)
    assert con.execute("SELECT count(*) FROM transactions").fetchone()[0] == 1
    assert con.execute("SELECT quantity, net_amount FROM transactions").fetchone() == (30.0, -431.8)
    assert con.execute("SELECT core_thesis FROM thesis_versions").fetchone()[0] == "Growth > 30%"
    assert con.execute("SELECT count(*) FROM intelligence_events").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM ai_proposals").fetchone()[0] == 0
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"source_documents", "financial_facts", "insider_transactions", "macro_series"} <= tables
    con.close()
    dispose_engine(settings.db_url)
