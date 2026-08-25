"""SEC/EDGAR connector (Tier 1 primary source). Read-only, rate-limited, idempotent.

Endpoints (official):
  * https://www.sec.gov/files/company_tickers.json          ticker -> CIK
  * https://data.sec.gov/submissions/CIK##########.json     filing index per issuer
  * https://data.sec.gov/api/xbrl/companyfacts/CIK#####.json  structured facts (see xbrl.py)

Foreign private issuers (e.g. NU / Nu Holdings) do NOT file 10-K/10-Q - they file 20-F and
6-K. The connector therefore never assumes domestic forms: it stores whatever the issuer
actually files and filters by the configurable `sec.material_forms` list.

SEC fair-access rules: descriptive User-Agent (set SEC_USER_AGENT in .env) and max ~10 req/s;
we default to one request per `sec_rate_limit_seconds` (0.15 s) with retries and timeouts.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable

import requests

from src.config import Settings
from src.db.intelligence import SourceDocument
from src.intelligence.entities import normalize_cik, remember_cik, resolve_investment
from src.intelligence.events import record_event
from src.intelligence.provenance import register_source
from src.logging_setup import get_logger
from src.research.investments import ResearchError

log = get_logger("intelligence.sec")

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"
COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10}.json"
ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession_nodash}/{filename}"

FetchFn = Callable[[str], bytes]


class SecError(ResearchError):
    pass


class SecClient:
    """Thin HTTP layer: UA, rate limit, retries, timeouts. `fetch` is injectable for tests."""

    def __init__(self, settings: Settings, fetch: FetchFn | None = None):
        self.settings = settings
        self._last_request = 0.0
        if fetch is not None:
            self._fetch = fetch
        else:
            sess = requests.Session()
            sess.headers.update({"User-Agent": settings.sec_user_agent, "Accept-Encoding": "gzip, deflate"})

            def _http_fetch(url: str) -> bytes:
                last_exc: Exception | None = None
                for attempt in range(3):
                    self._throttle()
                    try:
                        resp = sess.get(url, timeout=30)
                        if resp.status_code == 429 or resp.status_code >= 500:
                            last_exc = SecError(f"HTTP {resp.status_code} for {url}")
                            time.sleep(1.0 + attempt)
                            continue
                        resp.raise_for_status()
                        return resp.content
                    except requests.RequestException as exc:
                        last_exc = exc
                        time.sleep(0.5 + attempt)
                raise SecError(f"SEC request failed after retries: {url}: {last_exc}")

            self._fetch = _http_fetch

    def _throttle(self) -> None:
        wait = self.settings.sec_rate_limit_seconds - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def get(self, url: str) -> bytes:
        return self._fetch(url)

    def get_json(self, url: str) -> dict:
        try:
            return json.loads(self.get(url).decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise SecError(f"invalid JSON from {url}: {exc}") from exc


# --- company resolution ------------------------------------------------------


def resolve_cik(client: SecClient, ticker: str) -> tuple[str, str]:
    """ticker -> (cik, official title) via the official mapping file."""
    data = client.get_json(TICKERS_URL)
    want = ticker.strip().upper()
    for row in data.values():
        if str(row.get("ticker", "")).upper() == want:
            return normalize_cik(row["cik_str"]), row.get("title", "")
    raise SecError(f"ticker {want} not found in SEC company_tickers.json")


@dataclass
class FilingRecord:
    accession: str
    form: str
    filing_date: date | None
    report_date: date | None
    primary_document: str
    primary_doc_description: str | None
    is_xbrl: bool


def parse_submissions(payload: dict) -> tuple[dict, list[FilingRecord]]:
    """Parse the submissions JSON into issuer info + recent filing records."""
    info = {
        "cik": normalize_cik(payload.get("cik", "0")),
        "name": payload.get("name"),
        "sic": payload.get("sicDescription"),
        "fiscal_year_end": payload.get("fiscalYearEnd"),
        "exchanges": payload.get("exchanges") or [],
        "tickers": payload.get("tickers") or [],
    }
    recent = (payload.get("filings") or {}).get("recent") or {}
    out: list[FilingRecord] = []
    n = len(recent.get("accessionNumber") or [])
    for i in range(n):
        out.append(
            FilingRecord(
                accession=recent["accessionNumber"][i],
                form=recent["form"][i],
                filing_date=_d(recent.get("filingDate", [None] * n)[i]),
                report_date=_d(recent.get("reportDate", [None] * n)[i]),
                primary_document=(recent.get("primaryDocument") or [""] * n)[i],
                primary_doc_description=(recent.get("primaryDocDescription") or [None] * n)[i],
                is_xbrl=bool((recent.get("isXBRL") or [0] * n)[i]),
            )
        )
    return info, out


def _d(v) -> date | None:
    if not v:
        return None
    try:
        return date.fromisoformat(str(v))
    except ValueError:
        return None


def filing_url(cik: str, accession: str, filename: str) -> str:
    return ARCHIVES_URL.format(cik=normalize_cik(cik), accession_nodash=accession.replace("-", ""), filename=filename)


# --- sync --------------------------------------------------------------------


def sync_filings(
    session,
    settings: Settings,
    ticker: str,
    client: SecClient | None = None,
    forms: list[str] | None = None,
    limit: int = 40,
    download_documents: bool = False,
) -> dict:
    """Sync the filing index for one issuer: archive the submissions JSON, register each
    material filing as a source document and create NEW_FILING events. Idempotent.
    Primary documents are downloaded only on demand (download_documents=True) to respect
    SEC fair use; the index itself is enough for the intelligence inbox."""
    client = client or SecClient(settings)
    forms = forms or settings.sec_material_forms
    cik, title = resolve_cik(client, ticker)
    sub_raw = client.get(SUBMISSIONS_URL.format(cik10=cik.zfill(10)))
    info, filings = parse_submissions(json.loads(sub_raw.decode("utf-8")))

    # remember CIK on the linked instrument + investment
    inv = resolve_investment(session, ticker=ticker, cik=cik)
    if inv is not None and inv.instrument_id is not None:
        from src.db.models import Instrument

        inst = session.get(Instrument, inv.instrument_id)
        if inst is not None:
            remember_cik(session, inst, cik)

    register_source(
        session, settings, provider="sec_edgar", source_type="submissions", external_id=f"CIK{cik}",
        raw=sub_raw, category="sec", url=SUBMISSIONS_URL.format(cik10=cik.zfill(10)),
        title=f"EDGAR submissions {info.get('name') or title}", issuer=info.get("name") or title,
        entity_key=cik, source_tier=1,
        metadata={"tickers": info.get("tickers"), "exchanges": info.get("exchanges"),
                  "forms_seen": sorted({f.form for f in filings})},
    )

    inserted = duplicates = 0
    events_created = 0
    material = [f for f in filings if f.form in forms][:limit]
    for f in material:
        url = filing_url(cik, f.accession, f.primary_document) if f.primary_document else None
        raw_doc = None
        if download_documents and f.primary_document:
            try:
                raw_doc = client.get(url)
            except SecError as exc:
                log.warning("could not download %s %s: %s", f.form, f.accession, exc)
        doc, created = register_source(
            session, settings, provider="sec_edgar", source_type="filing", external_id=f.accession,
            raw=raw_doc, category="sec", url=url,
            title=f"{f.form} {f.primary_doc_description or ''}".strip(),
            issuer=info.get("name") or title, entity_key=cik,
            published_at=datetime.combine(f.filing_date, datetime.min.time()) if f.filing_date else None,
            period_end=f.report_date, source_tier=1,
            metadata={"form": f.form, "is_xbrl": f.is_xbrl, "primary_document": f.primary_document},
        )
        if created:
            inserted += 1
            _, ev_created = record_event(
                session,
                "NEW_FILING",
                dedup_key=f"sec:filing:{f.accession}",
                title=f"{ticker.upper()}: new {f.form} filed {f.filing_date}",
                occurred_at=datetime.combine(f.filing_date or date.today(), datetime.min.time()),
                investment_id=inv.id if inv else None,
                instrument_id=inv.instrument_id if inv else None,
                summary=f"{info.get('name') or title} filed {f.form} (period {f.report_date or 'n/a'})",
                source_document_id=doc.id,
                payload={"form": f.form, "accession": f.accession, "report_date": f.report_date},
                form=f.form,
            )
            events_created += int(ev_created)
        else:
            duplicates += 1

    summary = {
        "ticker": ticker.upper(), "cik": cik, "issuer": info.get("name") or title,
        "forms_seen": sorted({f.form for f in filings}), "material_considered": len(material),
        "filings_inserted": inserted, "filings_duplicate": duplicates, "events_created": events_created,
    }
    log.info("sec sync %s: %s", ticker, summary)
    return summary


def list_filings(session, cik: str | None = None, ticker_entity: str | None = None) -> list[SourceDocument]:
    from sqlalchemy import select

    stmt = select(SourceDocument).where(
        SourceDocument.provider == "sec_edgar", SourceDocument.source_type == "filing"
    )
    if cik:
        stmt = stmt.where(SourceDocument.entity_key == normalize_cik(cik))
    return list(session.scalars(stmt.order_by(SourceDocument.published_at.desc())))

def list_filing_files(client: SecClient, cik: str, accession: str) -> list[str]:
    """Filenames inside one filing via the archive index.json (official endpoint)."""
    url = ARCHIVES_URL.format(cik=normalize_cik(cik), accession_nodash=accession.replace("-", ""), filename="index.json")
    data = client.get_json(url)
    items = ((data.get("directory") or {}).get("item")) or []
    return [i.get("name") for i in items if i.get("name")]
