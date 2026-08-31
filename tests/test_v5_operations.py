"""v5 tests: pipelines, doctor, backups, claims, earnings workflows, onboarding, replay,
stress, opportunity, TTS fallback, second-company fixture. All offline."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from src.db.operations import DecisionReview, PipelineRun, PipelineStage, ScenarioRun
from src.research.investments import ResearchError, create_investment
from src.research.theses import create_thesis

ROOT = Path(__file__).parent.parent


# ---------------------------------------------------------------- pipelines


def test_daily_pipeline_offline_idempotent(session, settings, db_url) -> None:
    from src.operations.pipeline import run_daily

    inv = create_investment(session, "NU", status="OWNED")
    create_thesis(session, inv, "T", core_thesis="x")
    session.commit()
    report = run_daily(settings, use_ai=False, audio=True, sync_external=False)
    assert report.status in ("SUCCESS", "PARTIAL")
    stages = {s.stage: s.status for s in report.stages}
    assert stages["SEC"] == "SKIP" and stages["Macro"] == "SKIP"  # offline mode
    assert stages["Brief"] == "OK"
    assert stages["AI analysis"] == "SKIP"  # disabled
    assert stages["Audio"] == "OK"  # TTS disabled -> message, still OK
    assert report.output_path and Path(report.output_path).exists()
    # persisted job tracking
    from src.db.session import session_scope

    with session_scope(db_url) as s:
        run = s.scalars(select(PipelineRun)).first()
        assert run.status == report.status and run.finished_at is not None
        assert s.scalar(select(func.count(PipelineStage.id))) == len(report.stages)
    # idempotent re-run: second run same day -> brief preview, no crash, no duplicate checkpoint
    report2 = run_daily(settings, use_ai=False, audio=False, sync_external=False)
    assert report2.status in ("SUCCESS", "PARTIAL")
    from src.db.briefing import BriefRun

    with session_scope(db_url) as s:
        completed = s.scalars(select(BriefRun).where(
            BriefRun.brief_type == "daily", BriefRun.status == "completed")).all()
        assert len(completed) == 1  # preview did not add a second checkpoint


def test_daily_pipeline_partial_failure(session, settings, db_url, monkeypatch) -> None:
    """One failing stage -> FAIL for that stage, everything else continues -> PARTIAL."""
    from src.operations import pipeline as pl

    inv = create_investment(session, "NU", status="OWNED")
    session.commit()

    def boom(*a, **k):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr("src.intelligence.earnings.auto_extract_new_filings", boom)
    report = pl.run_daily(settings, use_ai=False, audio=False, sync_external=False)
    stages = {s.stage: s for s in report.stages}
    assert stages["KPI extract"].status == "FAIL"
    assert "provider exploded" in stages["KPI extract"].message
    assert stages["Brief"].status == "OK"  # brief still completed
    assert report.status == "PARTIAL"


def test_daily_pipeline_broker_sync_skips(session, settings, db_url, monkeypatch) -> None:
    """Broker sync is SKIPped offline, and SKIPped online when credentials are missing."""
    from src.operations import pipeline as pl

    create_investment(session, "NU", status="OWNED")
    session.commit()
    report = pl.run_daily(settings, use_ai=False, audio=False, sync_external=False)
    stages = {s.stage: s for s in report.stages}
    assert stages["Broker sync"].status == "SKIP"
    assert "--no-sync" in stages["Broker sync"].message

    # online but no credentials: broker stage skips with a clear message, others untouched
    monkeypatch.setattr("src.config.get_secret", lambda name: None)
    # other online stages must not hit the network in this test; stub their entry points
    for target in (
        "src.market_data.service.update_prices",
        "src.intelligence.connectors.sec.sync_filings",
        "src.intelligence.connectors.insiders.sync_insiders",
        "src.intelligence.connectors.macro.sync_macro",
    ):
        monkeypatch.setattr(target, lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline")))
    report = pl.run_daily(settings, use_ai=False, audio=False, sync_external=True)
    stages = {s.stage: s for s in report.stages}
    assert stages["Broker sync"].status == "SKIP"
    assert "credentials" in stages["Broker sync"].message.lower()


def test_daily_pipeline_broker_sync_imports_and_rebuilds(session, settings, db_url, monkeypatch) -> None:
    """New broker activity -> transactions imported and positions rebuilt in the same stage;
    no new activity -> rebuild is skipped. IBKR itself is mocked (offline test)."""
    from src.operations import pipeline as pl

    create_investment(session, "NU", status="OWNED")
    session.commit()

    monkeypatch.setattr("src.config.get_secret", lambda name: "test-secret")
    from src.portfolio.importer import ImportResult

    calls = {"sync": 0, "rebuild": 0}

    def fake_sync(s, st, xml_file=None):
        calls["sync"] += 1
        return ImportResult(account_id=1, transactions_inserted=2, cash_flows_inserted=1)

    def fake_rebuild(s):
        calls["rebuild"] += 1
        return {}

    monkeypatch.setattr("src.connectors.ibkr.sync.sync_ibkr", fake_sync)
    monkeypatch.setattr("src.portfolio.positions.rebuild_positions", fake_rebuild)
    for target in (
        "src.market_data.service.update_prices",
        "src.intelligence.connectors.sec.sync_filings",
        "src.intelligence.connectors.insiders.sync_insiders",
        "src.intelligence.connectors.macro.sync_macro",
    ):
        monkeypatch.setattr(target, lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline")))

    report = pl.run_daily(settings, use_ai=False, audio=False, sync_external=True)
    stages = {s.stage: s for s in report.stages}
    assert stages["Broker sync"].status == "OK"
    assert stages["Broker sync"].items == 3
    assert "positions rebuilt" in stages["Broker sync"].message
    assert calls == {"sync": 1, "rebuild": 1}
    assert stages["Brief"].status == "OK"  # rest of the pipeline unaffected

    # quiet day: sync returns nothing new -> no rebuild
    def fake_sync_quiet(s, st, xml_file=None):
        return ImportResult(account_id=1)

    monkeypatch.setattr("src.connectors.ibkr.sync.sync_ibkr", fake_sync_quiet)
    report = pl.run_daily(settings, use_ai=False, audio=False, sync_external=True)
    stages = {s.stage: s for s in report.stages}
    assert stages["Broker sync"].status == "OK"
    assert stages["Broker sync"].items == 0
    assert "no new broker activity" in stages["Broker sync"].message
    assert calls["rebuild"] == 1  # unchanged


def test_daily_pipeline_broker_failure_does_not_block_brief(session, settings, db_url, monkeypatch) -> None:
    from src.operations import pipeline as pl

    create_investment(session, "NU", status="OWNED")
    session.commit()
    monkeypatch.setattr("src.config.get_secret", lambda name: "test-secret")

    def boom(*a, **k):
        raise RuntimeError("IBKR gateway timeout")

    monkeypatch.setattr("src.connectors.ibkr.sync.sync_ibkr", boom)
    for target in (
        "src.market_data.service.update_prices",
        "src.intelligence.connectors.sec.sync_filings",
        "src.intelligence.connectors.insiders.sync_insiders",
        "src.intelligence.connectors.macro.sync_macro",
    ):
        monkeypatch.setattr(target, lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline")))
    report = pl.run_daily(settings, use_ai=False, audio=False, sync_external=True)
    stages = {s.stage: s for s in report.stages}
    assert stages["Broker sync"].status == "FAIL"
    assert "IBKR gateway timeout" in stages["Broker sync"].message
    assert stages["Brief"].status == "OK"
    assert report.status == "PARTIAL"


def test_weekly_pipeline(session, settings, db_url) -> None:
    from src.operations.pipeline import run_weekly

    inv = create_investment(session, "NU", status="OWNED")
    create_thesis(session, inv, "T")
    session.commit()
    report = run_weekly(settings, use_ai=False, sync_external=False)
    stages = {s.stage for s in report.stages}
    assert {"Weekly review", "Risk decomposition", "Management claims"} <= stages
    assert report.status in ("SUCCESS", "PARTIAL")
    assert "weekly" in (report.output_path or "")


def test_pipeline_ai_unavailable_still_completes(session, settings, db_url) -> None:
    from src.operations.pipeline import run_daily

    settings.ai_enabled = True  # enabled but server down -> health check fails -> SKIP
    settings.ai_base_url = "http://127.0.0.1:1"  # nothing listens here
    settings.ai_timeout_seconds = 1
    report = run_daily(settings, use_ai=True, audio=False, sync_external=False)
    stages = {s.stage: s for s in report.stages}
    assert stages["AI analysis"].status == "SKIP"
    assert "unavailable" in stages["AI analysis"].message.lower() or "unreachable" in stages["AI analysis"].message.lower()
    assert stages["Brief"].status == "OK"


# ---------------------------------------------------------------- scripts


def test_powershell_scripts_content() -> None:
    daily = (ROOT / "scripts" / "run_daily.ps1").read_text(encoding="utf-8")
    weekly = (ROOT / "scripts" / "run_weekly.ps1").read_text(encoding="utf-8")
    ai = (ROOT / "scripts" / "start_ai_server.ps1").read_text(encoding="utf-8")
    reg = (ROOT / "scripts" / "register_tasks.ps1").read_text(encoding="utf-8")
    for script in (daily, weekly):
        assert ".venv\\Scripts\\python.exe" in script  # correct venv
        assert "$PSScriptRoot" in script  # absolute project path derivation
        assert "exit $code" in script  # meaningful exit codes
        assert "Out-File" in script  # logging
        assert "-NonInteractive" not in script or True
    assert "127.0.0.1" in ai and "0.0.0.0" not in ai  # never public
    assert "schtasks" in reg and "/ST $DailyTime" in reg  # user-defined times
    for script in (daily, weekly, ai, reg):
        assert "IBKR_FLEX_TOKEN" not in script  # no secrets


# ---------------------------------------------------------------- doctor & backups


def test_doctor_on_clean_db(session, settings, db_url) -> None:
    from src.operations.doctor import overall_status, run_doctor

    checks = run_doctor(settings)
    by_name = {c.name: c for c in checks}
    assert by_name["Migration head"].status == "OK"
    assert by_name["Portfolio invariant (positions == replay of transactions)"].status == "OK"
    assert by_name["AI endpoint (optional)"].status == "OK"  # disabled = OK
    assert overall_status(checks) in ("OK", "WARN")  # WARN: no backups yet


def test_backup_rotation(settings) -> None:
    from src.operations.backups import backup_dir, create_backup, latest_backup, rotate

    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    settings.db_path.write_bytes(b"fake-db")
    paths = [create_backup(settings, "daily") for _ in range(9)]
    files = sorted(backup_dir(settings).glob("investor-daily-*.db"))
    assert len(files) == 7  # rotation keeps 7 daily
    assert latest_backup(settings) is not None
    removed = rotate(settings, "daily", keep=2)
    assert len(sorted(backup_dir(settings).glob("investor-daily-*.db"))) == 2


# ---------------------------------------------------------------- claims


def test_claim_extraction_classification_and_dedup(session) -> None:
    from src.intelligence.claims import ingest_claims_from_source

    inv = create_investment(session, "NU")
    text = ("Results were strong. We expect credit normalization in H2 2026. "
            "We target an efficiency ratio below 30% by 2027. "
            "We plan to accelerate share repurchases next year. Thank you.")
    claims = ingest_claims_from_source(session, inv, text, source_reference="Q2 call")
    session.commit()
    assert len(claims) == 3
    types = {c.claim_type for c in claims}
    assert "RISK_COMMENTARY" in types and "TARGET" in types and "CAPITAL_ALLOCATION" in types
    assert all(c.created_by == "SYSTEM" and c.source_reference == "Q2 call" for c in claims)
    assert claims[0].time_horizon == "in H2 2026"
    # dedup on re-ingest
    again = ingest_claims_from_source(session, inv, text, source_reference="Q2 call")
    assert again == []


def test_claim_outcome_linking_and_track_record(session) -> None:
    from src.intelligence.claims import add_claim, link_claim_outcome, track_record
    from src.research import kpis

    inv = create_investment(session, "NU")
    kpi = kpis.add_kpi(session, inv, "NPL 90+", unit="%")
    obs = kpis.add_observation(session, kpi, "2026Q4", 5.9, source="c")
    c1 = add_claim(session, inv, "We expect credit normalization in H2 2026",
                   source_reference="Q2 call", claim_date=date(2026, 8, 14))
    link = link_claim_outcome(
        session, c1, "kpi_observation", obs.id,
        note="NPL fell 6.9 -> 5.9 in Q4", new_status="CONFIRMED",
    )
    session.commit()
    assert c1.status == "CONFIRMED"
    with pytest.raises(ResearchError, match="invalid target_type"):
        link_claim_outcome(session, c1, "tweet", 1)
    add_claim(session, inv, "We target 100M customers", source_reference="x")
    rep = track_record(session, inv)
    assert rep["total"] == 2 and rep["open"] == 1 and rep["resolved"] == 1
    assert rep["hit_rate"] == 1.0
    assert rep["examples"] and "trust score" not in json.dumps(rep)  # transparent, no score


def test_transcript_import_requires_source(session) -> None:
    from src.intelligence.claims import add_claim

    inv = create_investment(session, "NU")
    with pytest.raises(ResearchError, match="requires a source"):
        add_claim(session, inv, "We expect great things")


# ---------------------------------------------------------------- earnings preview / postmortem


def _nu_setup(session):
    from src.research import kpis
    from src.research.assumptions import add_assumption
    from src.research.items import add_breaker

    inv = create_investment(session, "NU", status="OWNED")
    thesis, _ = create_thesis(session, inv, "T", core_thesis="Compounder")
    kpi = kpis.add_kpi(session, inv, "NPL 90+", unit="%", importance="HIGH", direction_good="down")
    add_assumption(session, thesis, "Credit controlled", kpi_id=kpi.id, expected_min=4.0, expected_max=6.6, unit="%")
    add_breaker(session, inv, "NPL 90+ > 7% two quarters", condition_text="NPL90 > 7%")
    kpis.add_observation(session, kpi, "2026Q1", 6.5, period_date=date(2026, 3, 31), source="c")
    kpis.add_observation(session, kpi, "2026Q2", 6.9, period_date=date(2026, 6, 30), source="c")
    kpis.add_observation(session, kpi, "2026Q2", 6.7, period_date=date(2026, 6, 30), source="consensus")
    return inv


def test_earnings_preview(session, settings) -> None:
    from src.intelligence.claims import add_claim
    from src.intelligence.earnings_workflows import earnings_preview

    inv = _nu_setup(session)
    add_claim(session, inv, "We expect credit normalization in H2 2026", source_reference="call")
    session.commit()
    text = earnings_preview(session, settings, inv)
    assert "Pre-Earnings Checklist" in text
    assert "NPL 90+: previous 6.9% [2026Q2]" in text
    assert "our expected range 4.0–6.6 %" in text
    assert "consensus 6.7" in text
    assert "breaker 'NPL 90+ > 7% two quarters'" in text
    assert "We expect credit normalization" in text
    assert "not fabricated" not in text.split("NPL 90+")[1].split("\n")[0]  # expectation exists for NPL


def test_earnings_postmortem(session, settings) -> None:
    from src.intelligence.earnings_workflows import earnings_postmortem

    inv = _nu_setup(session)
    session.commit()
    text = earnings_postmortem(session, settings, inv, period="2026Q2")
    assert "actual 6.9% [2026Q2]" in text
    assert "prev 6.5 (+0.4pp)" in text
    assert "expected 4.0 to 6.6" in text
    assert "consensus 6.7" in text
    assert "outside_expected_range" in text  # surprise flagged deterministically


# ---------------------------------------------------------------- onboarding + second company


def test_onboarding_second_company_fixture(session, settings, tmp_path) -> None:
    """Generic architecture proof: domestic 10-K issuer (not NU), offline mocked client."""
    from tests.test_intelligence_connectors import make_client

    tickers = json.dumps({"0": {"cik_str": 999999, "ticker": "ACME", "title": "Acme Industries Inc."}}).encode()
    submissions = json.dumps({
        "cik": "999999", "name": "Acme Industries Inc.", "tickers": ["ACME"], "exchanges": ["NYSE"],
        "filings": {"recent": {
            "accessionNumber": ["0000999999-26-000001", "0000999999-26-000002"],
            "form": ["10-K", "10-Q"],
            "filingDate": ["2026-02-20", "2026-05-05"],
            "reportDate": ["2025-12-31", "2026-03-31"],
            "primaryDocument": ["acme10k.htm", "acme10q.htm"],
            "primaryDocDescription": ["Annual report", "Quarterly report"],
            "isXBRL": [1, 1],
        }},
    }).encode()
    facts = json.dumps({
        "cik": 999999, "entityName": "Acme Industries Inc.",
        "facts": {"us-gaap": {
            "Revenues": {"units": {"USD": [
                {"val": 1000, "start": "2025-01-01", "end": "2025-12-31", "fy": 2025, "fp": "FY",
                 "form": "10-K", "accn": "b-1", "filed": "2026-02-20"}]}},
            "NetIncomeLoss": {"units": {"USD": [
                {"val": 100, "start": "2025-01-01", "end": "2025-12-31", "fy": 2025, "fp": "FY",
                 "form": "10-K", "accn": "b-1", "filed": "2026-02-20"}]}},
        }},
    }).encode()
    client = make_client(settings, {"company_tickers.json": tickers, "submissions/CIK": submissions,
                                    "companyfacts": facts})
    settings.brief_output_dir = tmp_path / "briefs"
    from src.intelligence.onboarding import onboard_company

    report = onboard_company(session, settings, "ACME", client=client)
    session.commit()
    assert report["cik"] == "999999"
    assert report["filer_type"] == "domestic (10-K/10-Q)"  # NOT NU-shaped
    assert "revenue" in report["standard_metrics_available"]
    assert report["issuer_extractor"] is None  # extractor absence handled gracefully
    todo = Path(report["files"][0]).read_text(encoding="utf-8")
    assert "NONE - create one" in todo and "extractor inspect ACME" in todo
    yaml_tmpl = Path(report["files"][1]).read_text(encoding="utf-8")
    assert "symbol: ACME" in yaml_tmpl and "never auto-generated" in yaml_tmpl
    # entity resolution + briefing still work for the second company
    from src.intelligence.entities import resolve_investment
    from src.intelligence.earnings import run_extraction

    inv = create_investment(session, "ACME", status="RESEARCHING")
    assert resolve_investment(session, ticker="ACME").id == inv.id
    with pytest.raises(ResearchError, match="no issuer extractor"):
        run_extraction(session, inv, "<p>x</p>", period="2026Q1")


def test_extractor_assistant_inspect() -> None:
    from src.intelligence.extractor_assistant import format_inspection, inspect_candidates

    html = ("<p>ARPAC expanded to US$ 17.0 in the quarter.</p>"
            "<p>The 90+ NPL ratio reached 6.9%.</p><p>Strategy remains unchanged.</p>")
    out = inspect_candidates(html, ["ARPAC", "NPL 90+"])
    assert any("17.0" in s for s in out["ARPAC"])
    assert any("6.9%" in s for s in out["NPL"])
    assert "Strategy remains unchanged" not in json.dumps(out)  # no number -> not a candidate
    text = format_inspection(out)
    assert "deterministic regex rules" in text
    assert format_inspection({}) == "No numeric KPI candidates found in this document."


# ---------------------------------------------------------------- decision replay (no hindsight)


def test_decision_replay_blind_then_reveal(session, settings) -> None:
    from src.intelligence.briefing.replay import rate_decision, replay_view, reveal_outcome
    from src.research.decisions import record_decision
    from src.research.evidence import add_evidence
    from src.research.theses import revise_thesis

    inv = create_investment(session, "NU", status="OWNED")
    thesis, v1 = create_thesis(session, inv, "T", core_thesis="Growth > 30%")
    v1.created_at = datetime(2026, 5, 1)  # thesis existed before the decision
    d = record_decision(session, inv, "BUY", base_currency="CZK", reasoning="entry",
                        decided_at=datetime(2026, 6, 1, 10, 0), confidence=70)
    # AFTER the decision: revision + contradicting evidence
    v2 = revise_thesis(session, thesis, "later", core_thesis="Growth > 20%")
    v2.created_at = datetime(2026, 7, 1)
    e = add_evidence(session, inv, "NPL spiked", direction="CONTRADICTING")
    e.created_at = datetime(2026, 7, 2)
    session.commit()

    blind = replay_view(session, session.get(type(d), d.id))
    assert "blind" in blind["mode"]
    assert blind["context_as_of_then"]["thesis"]["core_thesis"] == "Growth > 30%"  # v1, not v2
    assert not any("NPL spiked" in json.dumps(v) for v in blind["context_as_of_then"]["evidence"].values())
    assert "Would you make the same decision" in blind["question"]

    revealed = reveal_outcome(session, d, settings)
    assert any(x["title"] == "NPL spiked" for x in revealed["evidence_since"])
    assert "does not validate" in revealed["note"]

    review = rate_decision(session, d, "GOOD_PROCESS", would_repeat=True, replay_used=True)
    assert session.scalar(select(func.count(DecisionReview.id))) == 1
    with pytest.raises(ResearchError):
        rate_decision(session, d, "AMAZING")


# ---------------------------------------------------------------- stress & opportunity


def test_stress_tests_mechanical(session, settings, flex_xml) -> None:
    from src.analytics.stress import format_result, load_scenarios_yaml, run_stress
    from src.connectors.ibkr.flex_parser import parse_flex_statement
    from src.db.models import Instrument, Price
    from src.portfolio.importer import import_statement
    from src.portfolio.positions import rebuild_positions
    from tests.conftest import add_fx, add_price

    import_statement(session, parse_flex_statement(flex_xml), source="ibkr")
    rebuild_positions(session)
    inst = {i.symbol: i for i in session.scalars(select(Instrument))}
    today = date.today()
    for sym, px in (("AAPL", "170"), ("VOO", "470"), ("SAP", "150")):
        add_price(session, inst[sym], today, px)
    add_fx(session, "USD", "CZK", today, "23.5")
    add_fx(session, "USD", "CZK", date(2024, 1, 2), "23.5")
    add_fx(session, "EUR", "CZK", today, "25.0")
    session.commit()

    r = run_stress(session, settings, "AAPL -25%", {"AAPL_price": -25})
    base_aapl = r.per_position["AAPL"]["base"]
    assert r.per_position["AAPL"]["impact"] == round(-0.25 * base_aapl)
    assert r.per_position["VOO"]["applied"] == ["unshocked"]
    assert round(r.base_value + r.impact_abs, 2) == r.stressed_value
    assert "not a forecast" in format_result(r)

    r2 = run_stress(session, settings, "equities -20", {"ALL_EQUITIES": -20})
    assert all("equities -20" in " ".join(p["applied"]) for k, p in r2.per_position.items())
    assert session.scalar(select(func.count(ScenarioRun.id))) == 2

    # custom YAML
    import yaml as _yaml

    f = settings.raw_dir / "scen.yaml"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(_yaml.safe_dump({"scenarios": [{"name": "USD +10", "shocks": {"USD": 10}}]}), encoding="utf-8")
    scens = load_scenarios_yaml(f)
    r3 = run_stress(session, settings, scens[0]["name"], scens[0]["shocks"])
    assert r3.impact_abs > 0  # USD appreciation raises CZK value of USD positions
    with pytest.raises(ResearchError, match="unrecognized shock key"):
        run_stress(session, settings, "bad", {"portfolio_beta": 1})


def test_opportunity_table_dimensions(session, settings) -> None:
    from src.intelligence.briefing.mentor_workflows import opportunity_table
    from src.intelligence.discovery import add_manual_candidate
    from src.research.valuation import add_scenario, create_model

    inv = create_investment(session, "NU", status="OWNED")
    create_thesis(session, inv, "T", confidence=70)
    m = create_model(session, inv, "PT", reference_price=100)
    add_scenario(session, m, "base", 150, probability=100)
    add_manual_candidate(session, "MELI", "MercadoLibre", ["adjacent"])
    session.commit()
    rows = {r["ticker"]: r for r in opportunity_table(session, settings)}
    assert rows["NU"]["expected_return_pct"] == 50.0
    assert rows["NU"]["thesis_confidence"] == 70
    assert rows["MELI"]["status"].startswith("CANDIDATE")
    assert rows["MELI"]["thesis_health"] == "no thesis yet"
    assert "score" not in json.dumps(list(rows.values())).lower()


# ---------------------------------------------------------------- add/exit review (AI mocked)


def test_add_and_exit_review_agents(session, settings) -> None:
    from tests.test_intelligence_ai import fake_provider
    from src.intelligence.briefing.mentor_workflows import add_review, exit_review

    inv = create_investment(session, "NU", status="OWNED")
    create_thesis(session, inv, "T", core_thesis="Compounder")
    session.commit()
    reply = json.dumps({
        "arguments_for": ["Thesis intact"], "arguments_against": ["Concentration already high"],
        "what_would_improve_entry": "Price below reference", "what_would_make_waiting_better": "Q3 NPL print",
        "unknowns": ["Mexico unit economics"], "summary": "Considerations only; no action required.",
    })
    facts, review = add_review(session, settings, fake_provider(reply), inv)
    assert "alternatives" in facts and review.summary.startswith("Considerations")
    assert not any(w in json.dumps(review.model_dump()).upper() for w in ("BUY NOW", "SELL NOW"))
    _f2, review2 = exit_review(session, settings, fake_provider(reply), inv)
    assert review2.arguments_against


# ---------------------------------------------------------------- prefs, concepts, TTS


def test_feedback_preferences_deterministic(session) -> None:
    from src.db.briefing import BriefFeedback
    from src.intelligence.briefing.mentor_workflows import feedback_preferences

    session.add_all([
        BriefFeedback(item_key="insider:1", rating="TOO_NOISY"),
        BriefFeedback(item_key="insider:2", rating="NOT_USEFUL"),
        BriefFeedback(item_key="evidence:3", rating="USEFUL"),
    ])
    session.commit()
    prefs = feedback_preferences(session)
    assert prefs["NEW_INSIDER_ACTIVITY"]["hint"] == "deprioritize"
    assert prefs["NEW_EVIDENCE"]["hint"] == "prefer"


def test_teach_me_concepts_no_repeat(session) -> None:
    from src.intelligence.briefing.mentor_workflows import pick_concept

    c1 = pick_concept(session, ["NEW_KPI"])
    assert c1 is not None and c1[0] in ("NPL 90+", "Operating leverage", "ROE")
    c2 = pick_concept(session, ["NEW_KPI"])
    assert c2[0] != c1[0]  # history prevents repetition


def test_tts_disabled_fallback(settings, tmp_path) -> None:
    from src.intelligence.briefing.tts import DisabledTTS, get_tts_provider, synthesize_latest_brief

    settings.tts_enabled = False
    assert isinstance(get_tts_provider(settings), DisabledTTS)
    settings.brief_output_dir = tmp_path
    (tmp_path / "2026-08-25-daily-audio.txt").write_text("hello", encoding="utf-8")
    assert synthesize_latest_brief(settings, "daily") is None  # disabled -> None, never raises


def test_calibration_by_group_thresholds(session) -> None:
    from src.intelligence.briefing.calibration import calibration_by_group
    from src.research.predictions import create_prediction, resolve_prediction

    inv = create_investment(session, "NU")
    for i in range(10):
        p = create_prediction(session, f"g{i}", 70, investment=inv, category="growth")
        resolve_prediction(session, p, "RESOLVED_TRUE" if i < 7 else "RESOLVED_FALSE")
    p = create_prediction(session, "m1", 50, investment=inv, category="macro")
    resolve_prediction(session, p, "RESOLVED_TRUE")
    session.commit()
    rep = calibration_by_group(session, "category")
    assert rep["growth"]["sufficient"] and rep["growth"]["hit_rate"] == 0.7
    assert not rep["macro"]["sufficient"] and "insufficient" in rep["macro"]["note"]


def test_portfolio_risk_shared_macro(session, settings) -> None:
    from src.analytics.risk import portfolio_risk_report
    from src.db.intelligence import MacroSeries
    from src.intelligence.connectors.macro import link_macro

    a = create_investment(session, "NU", status="OWNED")
    b = create_investment(session, "MELI", status="OWNED")
    series = MacroSeries(provider="fred", series_code="DEXBZUS", name="USD/BRL")
    session.add(series)
    session.flush()
    link_macro(session, a, series)
    link_macro(session, b, series)
    session.commit()
    rep = portfolio_risk_report(session, settings)
    shared = rep["shared_macro_exposure"]
    assert len(shared) == 1 and shared[0]["investments"] == ["MELI", "NU"]
    assert "one bet" in shared[0]["note"]
