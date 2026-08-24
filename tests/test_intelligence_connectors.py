"""Offline tests for the v3 intelligence connectors (Phases A-C). All network mocked."""
from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select

from src.db.intelligence import (
    CongressTransaction,
    FinancialFact,
    InsiderTransaction,
    InstitutionalHolding,
    IntelligenceEvent,
    MacroObservation,
    SourceDocument,
    WatchlistEntry,
)
from src.intelligence.connectors import macro as macro_mod
from src.intelligence.connectors.congress import CsvCongressProvider, import_congress, parse_amount_range
from src.intelligence.connectors.insiders import aggregate_insiders, parse_form4, store_form4, sync_insiders
from src.intelligence.connectors.institutional import (
    add_manager,
    holding_changes,
    parse_13f_infotable,
    store_holdings,
    sync_manager,
)
from src.intelligence.connectors.sec import SecClient, SecError, parse_submissions, resolve_cik, sync_filings
from src.intelligence.connectors.xbrl import parse_companyfacts, store_facts, sync_companyfacts
from src.intelligence.events import list_events, record_event
from src.intelligence.provenance import archive_raw, register_source
from src.intelligence.technical import compute_technical_context
from src.research.investments import create_investment

# ---------------------------------------------------------------- fixtures (foreign issuer!)

TICKERS_JSON = json.dumps(
    {"0": {"cik_str": 1691493, "ticker": "NU", "title": "Nu Holdings Ltd."},
     "1": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}}
).encode()

# NU-like foreign private issuer: files 20-F / 6-K, never 10-K/10-Q
SUBMISSIONS_JSON = json.dumps(
    {
        "cik": "1691493",
        "name": "Nu Holdings Ltd.",
        "sicDescription": "Finance Services",
        "fiscalYearEnd": "1231",
        "tickers": ["NU"],
        "exchanges": ["NYSE"],
        "filings": {
            "recent": {
                "accessionNumber": ["0001691493-26-000010", "0001691493-26-000008", "0001691493-25-000090", "0001691493-26-000011"],
                "form": ["6-K", "20-F", "6-K", "4"],
                "filingDate": ["2026-08-14", "2026-04-20", "2025-11-13", "2026-08-15"],
                "reportDate": ["2026-06-30", "2025-12-31", "2025-09-30", "2026-08-13"],
                "primaryDocument": ["nu6k.htm", "nu20f.htm", "nu6kq3.htm", "form4.xml"],
                "primaryDocDescription": ["Q2 2026 results", "Annual report", "Q3 2025 results", "Form 4"],
                "isXBRL": [1, 1, 1, 0],
            }
        },
    }
).encode()

COMPANYFACTS_JSON = json.dumps(
    {
        "cik": 1691493,
        "entityName": "Nu Holdings Ltd.",
        "facts": {
            "ifrs-full": {
                "Revenue": {
                    "units": {
                        "USD": [
                            {"val": 2700000000, "start": "2025-01-01", "end": "2025-06-30", "fy": 2025, "fp": "H1", "form": "6-K", "accn": "a-1", "filed": "2025-08-14"},
                            {"val": 3100000000, "start": "2026-01-01", "end": "2026-06-30", "fy": 2026, "fp": "H1", "form": "6-K", "accn": "a-2", "filed": "2026-08-14"},
                        ]
                    }
                },
                "ProfitLoss": {
                    "units": {
                        "USD": [
                            {"val": 553000000, "start": "2026-01-01", "end": "2026-06-30", "fy": 2026, "fp": "H1", "form": "6-K", "accn": "a-2", "filed": "2026-08-14"}
                        ]
                    }
                },
                "Assets": {
                    "units": {
                        "USD": [
                            {"val": 52000000000, "end": "2026-06-30", "fy": 2026, "fp": "H1", "form": "6-K", "accn": "a-2", "filed": "2026-08-14"}
                        ]
                    }
                },
                "SomeObscureTag": {
                    "units": {"USD": [{"val": 1, "end": "2026-06-30", "fy": 2026, "fp": "H1", "form": "6-K", "accn": "a-2"}]}
                },
            }
        },
    }
).encode()

FORM4_XML = """<?xml version="1.0"?>
<ownershipDocument>
  <periodOfReport>2026-08-13</periodOfReport>
  <issuer><issuerCik>0001691493</issuerCik><issuerName>Nu Holdings Ltd.</issuerName></issuer>
  <reportingOwner>
    <reportingOwnerId><rptOwnerName>VELEZ DAVID</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship><isDirector>1</isDirector><isOfficer>1</isOfficer><officerTitle>CEO</officerTitle></reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <securityTitle><value>Class A Ordinary Shares</value></securityTitle>
      <transactionDate><value>2026-08-13</value></transactionDate>
      <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>100000</value></transactionShares>
        <transactionPricePerShare><value>13.50</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      <postTransactionAmounts><sharesOwnedFollowingTransaction><value>5000000</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
      <ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <securityTitle><value>Class A Ordinary Shares</value></securityTitle>
      <transactionDate><value>2026-08-13</value></transactionDate>
      <transactionCoding><transactionCode>F</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>20000</value></transactionShares>
        <transactionPricePerShare><value>13.50</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      <postTransactionAmounts><sharesOwnedFollowingTransaction><value>4980000</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
      <ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>"""

F13_XML = """<?xml version="1.0"?>
<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
  <infoTable>
    <nameOfIssuer>NU HOLDINGS LTD</nameOfIssuer>
    <cusip>G6683N103</cusip>
    <value>135000000</value>
    <shrsOrPrnAmt><sshPrnamt>10000000</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
  </infoTable>
  <infoTable>
    <nameOfIssuer>APPLE INC</nameOfIssuer>
    <cusip>037833100</cusip>
    <value>90000000</value>
    <shrsOrPrnAmt><sshPrnamt>300000</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
  </infoTable>
</informationTable>"""

FRED_CSV = b"DATE,FEDFUNDS\n2026-05-01,4.33\n2026-06-01,4.33\n2026-07-01,4.08\n2026-08-01,.\n"


def make_client(settings, urls: dict[str, bytes]) -> SecClient:
    def fetch(url: str) -> bytes:
        for frag, payload in urls.items():
            if frag in url:
                return payload
        raise SecError(f"mock 404: {url}")

    return SecClient(settings, fetch=fetch)


# ---------------------------------------------------------------- provenance & events


def test_raw_archive_and_source_registration_idempotent(session, settings) -> None:
    p1, h1 = archive_raw(settings, "sec", "test_doc", b"hello")
    p2, h2 = archive_raw(settings, "sec", "test_doc", b"hello")
    assert p1 == p2 and h1 == h2 and p1.exists()
    doc, created = register_source(session, settings, "sec_edgar", "filing", "acc-1", raw=b"hello", category="sec", source_tier=1)
    assert created and doc.sha256 == h1 and doc.source_tier == 1
    doc2, created2 = register_source(session, settings, "sec_edgar", "filing", "acc-1", raw=b"hello", category="sec")
    assert not created2 and doc2.id == doc.id
    # changed payload for same external id: original kept, revision archived in metadata
    doc3, created3 = register_source(session, settings, "sec_edgar", "filing", "acc-1", raw=b"changed", category="sec")
    assert not created3 and doc3.sha256 == h1
    assert "revised_payloads" in (doc3.metadata_json or "")


def test_event_dedup_and_severity(session) -> None:
    ev1, c1 = record_event(session, "NEW_FILING", "k1", "t", datetime(2026, 8, 1), form="20-F")
    ev2, c2 = record_event(session, "NEW_FILING", "k1", "t", datetime(2026, 8, 1))
    assert c1 and not c2 and ev1.id == ev2.id
    assert ev1.severity == "HIGH"  # 20-F is a major form
    ev3, _ = record_event(session, "INSIDER_TRANSACTION", "k2", "t", datetime(2026, 8, 1), transaction_type="award_grant")
    assert ev3.severity == "LOW"
    ev4, _ = record_event(session, "INSIDER_TRANSACTION", "k3", "t", datetime(2026, 8, 1), transaction_type="open_market_purchase")
    assert ev4.severity == "MEDIUM"
    assert len(list_events(session, min_severity="MEDIUM")) == 2


# ---------------------------------------------------------------- SEC (foreign issuer)


def test_sec_foreign_issuer_sync(session, settings) -> None:
    inv = create_investment(session, "NU", name="Nu Holdings", status="OWNED")
    client = make_client(settings, {"company_tickers.json": TICKERS_JSON, "submissions/CIK": SUBMISSIONS_JSON})
    cik, title = resolve_cik(client, "nu")
    assert cik == "1691493" and "Nu Holdings" in title
    summary = sync_filings(session, settings, "NU", client=client)
    session.commit()
    # foreign issuer: 20-F and 6-K captured; Form 4 not in material forms; NO 10-K assumption
    assert summary["forms_seen"] == ["20-F", "4", "6-K"]
    assert summary["filings_inserted"] == 3 and summary["events_created"] == 3
    events = list_events(session, investment_id=inv.id)
    assert {e.payload_json and "20-F" in e.payload_json for e in events}
    annual = [e for e in events if "20-F" in (e.payload_json or "")][0]
    assert annual.severity == "HIGH"
    # idempotent re-sync
    summary2 = sync_filings(session, settings, "NU", client=client)
    assert summary2["filings_inserted"] == 0 and summary2["filings_duplicate"] == 3
    assert session.scalar(select(func.count(IntelligenceEvent.id))) == 3


def test_sec_unknown_ticker(settings) -> None:
    client = make_client(settings, {"company_tickers.json": TICKERS_JSON})
    with pytest.raises(SecError, match="not found"):
        resolve_cik(client, "NOPE")


# ---------------------------------------------------------------- XBRL


def test_xbrl_parse_and_store_idempotent(session, settings) -> None:
    payload = json.loads(COMPANYFACTS_JSON)
    facts = parse_companyfacts(payload)
    assert len(facts) == 5
    by_concept = {f["concept"]: f for f in facts if f["concept"] != "Revenue"}
    assert by_concept["ProfitLoss"]["metric"] == "net_income"  # IFRS mapping
    assert by_concept["Assets"]["metric"] == "total_assets" and by_concept["Assets"]["is_instant"]
    assert by_concept["SomeObscureTag"]["metric"] is None  # unmapped preserved, not guessed
    r1 = store_facts(session, facts, None)
    assert r1["facts_inserted"] == 5
    r2 = store_facts(session, facts, None)
    assert r2["facts_inserted"] == 0 and r2["facts_duplicate"] == 5
    # restatement (same period, new value) is a NEW row - history preserved
    restated = dict(facts[0]) | {"value": facts[0]["value"] + 1, "accession": "a-3"}
    r3 = store_facts(session, [restated], None)
    assert r3["facts_inserted"] == 1
    assert session.scalar(select(func.count(FinancialFact.id))) == 6


def test_xbrl_sync_via_client(session, settings) -> None:
    client = make_client(settings, {"companyfacts": COMPANYFACTS_JSON})
    result = sync_companyfacts(session, settings, "1691493", client=client)
    assert result["facts_inserted"] == 5 and result["facts_mapped_to_metrics"] == 4
    doc = session.scalars(select(SourceDocument).where(SourceDocument.source_type == "companyfacts")).one()
    assert doc.source_tier == 1 and doc.raw_path


def test_kpi_bridge_deterministic_vs_suggested_vs_unsupported(session, settings) -> None:
    from src.db.intelligence import AiProposal
    from src.intelligence.kpi_mapping import apply_kpi_bridge
    from src.research import kpis

    inv = create_investment(session, "NU", status="OWNED")
    kpis.add_kpi(session, inv, "Net income", unit="USD")           # deterministic bridge
    kpis.add_kpi(session, inv, "Adjusted net income margin")       # suggested (name contains 'income')
    kpis.add_kpi(session, inv, "NPL 90+", unit="%")                # unsupported (issuer-specific)
    store_facts(session, parse_companyfacts(json.loads(COMPANYFACTS_JSON)), None)
    session.commit()
    res = apply_kpi_bridge(session, inv, "1691493")
    session.commit()
    assert res.deterministic == ["Net income"] and res.observations_created == 1
    assert res.suggested == ["Adjusted net income margin"]
    assert "NPL 90+" in res.unsupported
    from src.db.research import KpiObservation

    obs = session.scalars(select(KpiObservation)).one()
    assert obs.value == 553000000 and obs.source == "sec_xbrl" and obs.created_by == "SYSTEM"
    assert "a-2" in obs.source_reference
    prop = session.scalars(select(AiProposal).where(AiProposal.proposal_type == "KPI_MAPPING")).one()
    assert prop.status == "PENDING"
    # re-run: no duplicate observations, no duplicate proposals (existing never overwritten)
    res2 = apply_kpi_bridge(session, inv, "1691493")
    assert res2.observations_created == 0 and res2.observations_duplicate == 1
    assert session.scalar(select(func.count(AiProposal.id))) == 1


# ---------------------------------------------------------------- insiders


def test_form4_parse_and_aggregate(session, settings) -> None:
    inv = create_investment(session, "NU", status="OWNED")
    from src.db.models import Instrument
    from src.intelligence.entities import remember_cik

    inst = Instrument(symbol="NU", asset_type="stock", exchange="NYSE", currency="USD")
    session.add(inst)
    session.flush()
    inv.instrument_id = inst.id
    remember_cik(session, inst, "1691493")

    parsed = parse_form4(FORM4_XML)
    assert parsed.insider_name == "VELEZ DAVID" and "CEO" in parsed.insider_role
    assert parsed.issuer_cik == "1691493"
    assert [t["transaction_type"] for t in parsed.transactions] == ["open_market_purchase", "tax_withholding"]
    res = store_form4(session, parsed, "0001691493-26-000011", None, filing_date=date(2026, 8, 15))
    session.commit()
    assert res["inserted"] == 2
    # idempotent
    res2 = store_form4(session, parsed, "0001691493-26-000011", None)
    assert res2["inserted"] == 0 and res2["duplicates"] == 2
    # only the open-market transaction produced an event; F (tax) did not
    events = list_events(session, investment_id=inv.id)
    assert len(events) == 1 and events[0].severity == "MEDIUM"
    agg = aggregate_insiders(session, "1691493", as_of=date(2026, 8, 23))
    assert agg["30d"]["insiders_buying"] == 1 and agg["30d"]["insiders_selling"] == 0
    assert agg["30d"]["net_shares"] == 100000
    assert agg["30d"]["net_value_usd"] == 100000 * 13.50
    assert "not a trading signal" in agg["note"]


def test_insiders_sync_via_client(session, settings) -> None:
    client = make_client(settings, {
        "company_tickers.json": TICKERS_JSON,
        "submissions/CIK": SUBMISSIONS_JSON,
        "form4.xml": FORM4_XML.encode(),
    })
    summary = sync_insiders(session, settings, "NU", client=client)
    assert summary["form4_seen"] == 1 and summary["inserted"] == 2
    assert session.scalar(select(func.count(InsiderTransaction.id))) == 2
    summary2 = sync_insiders(session, settings, "NU", client=client)
    assert summary2["inserted"] == 0 and summary2["duplicates"] == 1  # source doc already archived


# ---------------------------------------------------------------- 13F


def test_13f_parse_store_and_changes(session, settings) -> None:
    mgr = add_manager(session, "Test Capital", "1067983")
    holdings = parse_13f_infotable(F13_XML)
    assert len(holdings) == 2 and holdings[0]["cusip"] == "G6683N103"
    store_holdings(session, mgr, date(2026, 3, 31), holdings, None)
    # next quarter: NU increased, AAPL exited, new position appears
    q2 = [
        {"cusip": "G6683N103", "issuer_name": "NU HOLDINGS LTD", "shares": 15000000.0, "value_usd": 200000000.0},
        {"cusip": "594918104", "issuer_name": "MICROSOFT CORP", "shares": 50000.0, "value_usd": 20000000.0},
    ]
    store_holdings(session, mgr, date(2026, 6, 30), q2, None)
    session.commit()
    changes = {c["cusip"]: c for c in holding_changes(session, mgr)}
    assert changes["G6683N103"]["change"] == "INCREASED" and changes["G6683N103"]["shares_delta"] == 5000000
    assert changes["594918104"]["change"] == "NEW_POSITION"
    assert changes["037833100"]["change"] == "EXITED"
    # idempotent store
    r = store_holdings(session, mgr, date(2026, 6, 30), q2, None)
    assert r["inserted"] == 0
    assert session.scalar(select(func.count(InstitutionalHolding.id))) == 4


# ---------------------------------------------------------------- congress


def test_congress_csv_import_ranges_and_matching(session, settings, tmp_path) -> None:
    inv = create_investment(session, "NU", status="OWNED")
    session.add(WatchlistEntry(kind="congress_member", key="Jane Sample", label="Jane Sample"))
    session.commit()
    assert parse_amount_range("$1,001 - $15,000") == (1001.0, 15000.0)
    assert parse_amount_range("$50,000,001+") == (50000001.0, 50000001.0)
    assert parse_amount_range(None) == (None, None)
    csv_file = tmp_path / "ptr.csv"
    csv_file.write_text(
        "person,chamber,owner,transaction_date,disclosure_date,asset,ticker,type,amount,source_reference\n"
        'Jane Sample,house,spouse,2026-07-01,2026-08-10,"Nu Holdings Ltd Class A",NU,purchase,"$1,001 - $15,000",ptr-1\n'
        'John Other,senate,self,2026-07-15,2026-08-12,"Random Corp",ZZZQ,sale,"$15,001 - $50,000",ptr-2\n',
        encoding="utf-8",
    )
    res = import_congress(session, settings, CsvCongressProvider(csv_file), source_file=str(csv_file))
    session.commit()
    assert res["inserted"] == 2
    rows = list(session.scalars(select(CongressTransaction).order_by(CongressTransaction.id)))
    nu_row = rows[0]
    assert nu_row.amount_low == 1001 and nu_row.amount_high == 15000
    assert nu_row.investment_id == inv.id and nu_row.owner == "spouse"
    assert nu_row.disclosure_date != nu_row.transaction_date  # both preserved
    assert rows[1].investment_id is None  # unresolvable ticker stays unlinked, never guessed
    # events only for matched investment or watched person (both apply to row 1; row 2 none)
    assert session.scalar(select(func.count(IntelligenceEvent.id))) == 1
    res2 = import_congress(session, settings, CsvCongressProvider(csv_file), source_file=str(csv_file))
    assert res2["inserted"] == 0 and res2["duplicates"] == 2


# ---------------------------------------------------------------- macro


def test_macro_sync_vintage_and_events(session, settings) -> None:
    settings.macro_default_series = [{"code": "FEDFUNDS", "name": "Federal Funds Rate", "unit": "%", "frequency": "monthly"}]
    calls = []

    def fetch(url: str) -> bytes:
        calls.append(url)
        return FRED_CSV

    s1 = macro_mod.sync_macro(session, settings, fetch=fetch)
    session.commit()
    assert s1["series"]["FEDFUNDS"]["inserted"] == 3  # '.' value skipped
    # second run: identical data -> nothing inserted
    s2 = macro_mod.sync_macro(session, settings, fetch=fetch)
    assert s2["series"]["FEDFUNDS"]["inserted"] == 0
    # revision: same date, new value -> NEW vintage row, old kept
    revised = FRED_CSV.replace(b"2026-07-01,4.08", b"2026-07-01,4.10")
    s3 = macro_mod.sync_macro(session, settings, fetch=lambda url: revised)
    session.commit()
    assert s3["series"]["FEDFUNDS"]["inserted"] == 1
    assert session.scalar(select(func.count(MacroObservation.id))) == 4
    series = session.scalars(select(macro_mod.MacroSeries)).one()
    latest = macro_mod.latest_observations(session, series)
    by_date = {o.obs_date: o.value for o in latest}
    assert by_date[date(2026, 7, 1)] == 4.10  # latest vintage wins on read


def test_macro_provider_failure_does_not_corrupt(session, settings) -> None:
    settings.macro_default_series = [
        {"code": "GOOD", "name": "Good"}, {"code": "BAD", "name": "Bad"},
    ]

    def fetch(url: str) -> bytes:
        if "BAD" in url:
            raise macro_mod.ResearchError("boom")
        return b"DATE,GOOD\n2026-08-01,1.0\n"

    s = macro_mod.sync_macro(session, settings, fetch=fetch)
    assert s["series"]["GOOD"]["inserted"] == 1
    assert any("BAD" in e for e in s["errors"])


# ---------------------------------------------------------------- technical


def test_technical_context_deterministic() -> None:
    from datetime import timedelta

    start = date(2025, 1, 1)
    closes = {}
    for i in range(300):
        closes[start + timedelta(days=i)] = Decimal(100) + Decimal(i) * Decimal("0.1")
    as_of = start + timedelta(days=299)
    ctx = compute_technical_context(closes, as_of)
    assert ctx.price == Decimal("129.9")
    assert ctx.sma20 == sum(Decimal(100) + Decimal(i) * Decimal("0.1") for i in range(280, 300)) / 20
    assert ctx.high_52w == ctx.price and ctx.distance_from_52w_high == 0
    assert ctx.drawdown_from_ath == 0
    assert ctx.rsi14 == 100  # monotonic uptrend
    stmts = ctx.statements()
    assert any("above the 200d moving average" in s for s in stmts)
    assert not any(w in " ".join(stmts).upper() for w in ("BUY", "SELL"))  # context, not signals
    # declining series -> below SMAs, drawdown negative
    closes2 = {start + timedelta(days=i): Decimal(200) - Decimal(i) * Decimal("0.3") for i in range(300)}
    ctx2 = compute_technical_context(closes2, as_of)
    assert ctx2.drawdown_from_ath < 0 and ctx2.distance_from_52w_high < 0
    assert ctx2.rsi14 == 0
    # empty series
    assert compute_technical_context({}, as_of).statements() == ["No cached price data available."]
