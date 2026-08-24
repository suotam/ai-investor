"""IBKR sync orchestration: fetch (or load file) -> archive raw -> parse -> idempotent import."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from src.config import Settings, get_secret
from src.connectors.ibkr.flex_client import FlexClient
from src.connectors.ibkr.flex_parser import parse_flex_statement
from src.core import sha256_bytes
from src.logging_setup import get_logger
from src.portfolio.importer import ImportResult, finish_import_run, import_statement, start_import_run

log = get_logger("ibkr.sync")
SOURCE = "ibkr"


def archive_raw(settings: Settings, xml_text: str, label: str = "flex") -> tuple[Path, str]:
    """Store the raw statement for audit under data/raw/ibkr/YYYY-MM-DD/. Contains no secrets."""
    now = datetime.now()
    folder = settings.raw_dir / "ibkr" / now.strftime("%Y-%m-%d")
    folder.mkdir(parents=True, exist_ok=True)
    data = xml_text.encode("utf-8")
    digest = sha256_bytes(data)
    path = folder / f"{label}_{now.strftime('%H%M%S')}_{digest[:12]}.xml"
    path.write_bytes(data)
    return path, digest


def sync_ibkr(session: Session, settings: Settings, xml_file: str | Path | None = None) -> ImportResult:
    """Run one IBKR sync. If xml_file is given, it is used instead of the Flex Web Service
    (useful for offline testing and for manually downloaded Flex reports)."""
    run = start_import_run(session, job="sync-ibkr", source=SOURCE)
    log.info("sync-ibkr start (run id=%s, file=%s)", run.id, xml_file or "<flex web service>")
    try:
        if xml_file:
            xml_text = Path(xml_file).read_text(encoding="utf-8")
            label = "file"
        else:
            client = FlexClient(
                token=get_secret("IBKR_FLEX_TOKEN") or "",
                query_id=get_secret("IBKR_FLEX_QUERY_ID") or "",
                base_url=settings.ibkr_base_url,
                version=settings.ibkr_flex_version,
                poll_interval_seconds=settings.ibkr_poll_interval_seconds,
                max_poll_attempts=settings.ibkr_max_poll_attempts,
            )
            xml_text = client.fetch()
            label = "flex"
        raw_path, digest = archive_raw(settings, xml_text, label)
        statement = parse_flex_statement(xml_text)
        result = import_statement(session, statement, source=SOURCE, source_file=str(raw_path))
        finish_import_run(
            session,
            run,
            result,
            raw_path=str(raw_path),
            raw_sha256=digest,
            details={
                "period_from": statement.period_from.isoformat() if statement.period_from else None,
                "period_to": statement.period_to.isoformat() if statement.period_to else None,
            },
        )
        log.info("sync-ibkr end: inserted=%d duplicates=%d", result.inserted, result.duplicates)
        return result
    except Exception as exc:
        finish_import_run(session, run, error=f"{type(exc).__name__}: {exc}")
        session.flush()
        log.error("sync-ibkr failed: %s", exc)
        raise
