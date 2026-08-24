"""Macro layer: official series (FRED CSV endpoint, no API key) with vintage-aware storage.

fredgraph.csv is a public official endpoint (Tier 1: Federal Reserve Bank of St. Louis
aggregating BLS/BEA/OECD sources). Revisions never overwrite: a changed value for the same
observation date is stored as a new row with its own retrieved_at (vintage).
"""
from __future__ import annotations

import csv
import io
import time
from datetime import date, datetime
from typing import Callable

import requests

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.config import Settings
from src.core import utcnow
from src.db.intelligence import InvestmentMacroLink, MacroObservation, MacroSeries
from src.intelligence.events import record_event
from src.intelligence.provenance import register_source
from src.logging_setup import get_logger
from src.research.investments import ResearchError

log = get_logger("intelligence.macro")

FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={code}"
FetchFn = Callable[[str], bytes]


def default_fetch(url: str) -> bytes:
    for attempt in range(3):
        try:
            resp = requests.get(url, timeout=30, headers={"User-Agent": "InvestorOS/3.0 macro sync"})
            if resp.status_code >= 500 or resp.status_code == 429:
                time.sleep(1 + attempt)
                continue
            resp.raise_for_status()
            return resp.content
        except requests.RequestException as exc:
            if attempt == 2:
                raise ResearchError(f"macro fetch failed: {url}: {exc}") from exc
            time.sleep(1 + attempt)
    raise ResearchError(f"macro fetch failed after retries: {url}")


def ensure_series(session: Session, settings: Settings) -> list[MacroSeries]:
    """Create MacroSeries rows from config (idempotent)."""
    out = []
    for cfg in settings.macro_default_series:
        code = cfg["code"]
        s = session.scalars(
            select(MacroSeries).where(MacroSeries.provider == "fred", MacroSeries.series_code == code)
        ).first()
        if s is None:
            s = MacroSeries(
                provider="fred", series_code=code, name=cfg.get("name", code),
                unit=cfg.get("unit"), frequency=cfg.get("frequency"),
            )
            session.add(s)
            session.flush()
        out.append(s)
    return out


def parse_fred_csv(raw: bytes) -> list[tuple[date, float]]:
    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows or len(rows[0]) < 2:
        raise ResearchError("unexpected FRED CSV format")
    out: list[tuple[date, float]] = []
    for row in rows[1:]:
        if len(row) < 2 or row[1] in (".", "", "NA"):
            continue
        try:
            out.append((date.fromisoformat(row[0]), float(row[1])))
        except ValueError:
            continue
    return out


def sync_macro(
    session: Session, settings: Settings, fetch: FetchFn | None = None, codes: list[str] | None = None
) -> dict:
    fetch = fetch or default_fetch
    series_list = ensure_series(session, settings)
    if codes:
        series_list = [s for s in series_list if s.series_code in codes]
    summary: dict = {"series": {}, "errors": []}
    for series in series_list:
        if not series.active:
            continue
        url = FRED_CSV_URL.format(code=series.series_code)
        try:
            raw = fetch(url)
            observations = parse_fred_csv(raw)
        except ResearchError as exc:
            summary["errors"].append(f"{series.series_code}: {exc}")
            continue  # one provider failure never corrupts other data
        doc, _ = register_source(
            session, settings, provider="fred", source_type="macro_csv",
            external_id=f"{series.series_code}:{utcnow():%Y-%m-%d}", raw=raw, category="macro",
            url=url, title=f"FRED {series.series_code}", entity_key=series.series_code, source_tier=1,
        )
        existing = {
            (o.obs_date, o.value)
            for o in session.scalars(
                select(MacroObservation).where(MacroObservation.series_id == series.id)
            )
        }
        inserted = 0
        latest_new: tuple[date, float] | None = None
        now = utcnow()
        for obs_date, value in observations:
            if (obs_date, value) in existing:
                continue
            session.add(
                MacroObservation(
                    series_id=series.id, obs_date=obs_date, value=value,
                    retrieved_at=now, source_document_id=doc.id,
                )
            )
            existing.add((obs_date, value))
            inserted += 1
            if latest_new is None or obs_date > latest_new[0]:
                latest_new = (obs_date, value)
        session.flush()
        summary["series"][series.series_code] = {"observations": len(observations), "inserted": inserted}
        if inserted and latest_new and existing and len(existing) > inserted:
            # new release for an already-tracked series -> event (linked investments raise severity)
            linked = list(
                session.scalars(
                    select(InvestmentMacroLink).where(InvestmentMacroLink.series_id == series.id)
                )
            )
            for link in linked or [None]:
                record_event(
                    session, "MACRO_RELEASE",
                    dedup_key=f"macro:{series.series_code}:{latest_new[0]}:{link.investment_id if link else 'global'}",
                    title=f"{series.name}: {latest_new[1]} ({latest_new[0]})",
                    occurred_at=datetime.combine(latest_new[0], datetime.min.time()),
                    investment_id=link.investment_id if link else None,
                    source_document_id=doc.id,
                    payload={"series": series.series_code, "date": latest_new[0], "value": latest_new[1]},
                    linked_investment=bool(link),
                )
    return summary


def latest_observations(session: Session, series: MacroSeries, n: int = 13) -> list[MacroObservation]:
    """Latest vintage per obs_date, most recent n dates."""
    rows = list(
        session.scalars(
            select(MacroObservation)
            .where(MacroObservation.series_id == series.id)
            .order_by(MacroObservation.obs_date.desc(), MacroObservation.retrieved_at.desc())
        )
    )
    seen: dict[date, MacroObservation] = {}
    for r in rows:
        seen.setdefault(r.obs_date, r)
        if len(seen) >= n:
            break
    return sorted(seen.values(), key=lambda r: r.obs_date)


def link_macro(
    session: Session, investment, series: MacroSeries, relationship: str | None = None,
    why_it_matters: str | None = None, expected_direction: str | None = None, importance: str = "MEDIUM",
) -> InvestmentMacroLink:
    existing = session.scalars(
        select(InvestmentMacroLink).where(
            InvestmentMacroLink.investment_id == investment.id, InvestmentMacroLink.series_id == series.id
        )
    ).first()
    if existing:
        return existing
    link = InvestmentMacroLink(
        investment_id=investment.id, series_id=series.id, relationship_=relationship,
        why_it_matters=why_it_matters, expected_direction=expected_direction, importance=importance,
    )
    session.add(link)
    session.flush()
    return link
