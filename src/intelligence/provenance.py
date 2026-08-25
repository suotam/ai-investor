"""Provenance: local raw archive + source_documents registry. Idempotent, append-only."""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.config import Settings
from src.core import sha256_bytes, utcnow
from src.db.intelligence import SourceDocument
from src.logging_setup import get_logger

log = get_logger("intelligence.provenance")

PARSER_VERSION = "v3.0"


def archive_raw(settings: Settings, category: str, name: str, data: bytes) -> tuple[Path, str]:
    """Store raw payload under data/raw/<category>/. Never overwrites a different payload:
    the digest is part of the filename, so re-downloading identical content is a no-op and
    changed content lands in a new file."""
    digest = sha256_bytes(data)
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in name)[:120]
    folder = settings.raw_dir / category
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{safe}_{digest[:12]}{_ext(safe)}"
    if not path.exists():
        path.write_bytes(data)
    return path, digest


def _ext(name: str) -> str:
    return "" if "." in name.rsplit("_", 1)[-1] or "." in name else ""


def register_source(
    session: Session,
    settings: Settings,
    provider: str,
    source_type: str,
    external_id: str,
    raw: bytes | None = None,
    category: str | None = None,
    url: str | None = None,
    title: str | None = None,
    issuer: str | None = None,
    entity_key: str | None = None,
    published_at: datetime | None = None,
    period_start: date | None = None,
    period_end: date | None = None,
    source_tier: int = 2,
    metadata: dict | None = None,
    status: str = "archived",
) -> tuple[SourceDocument, bool]:
    """Find-or-create a source document (idempotent by provider+source_type+external_id).
    Returns (doc, created). Existing rows are never overwritten; a changed payload for the
    same external id is archived as a new raw file and noted in metadata."""
    doc = session.scalars(
        select(SourceDocument).where(
            SourceDocument.provider == provider,
            SourceDocument.source_type == source_type,
            SourceDocument.external_id == external_id,
        )
    ).first()
    raw_path = digest = None
    if raw is not None:
        raw_path, digest = archive_raw(settings, category or provider, f"{source_type}_{external_id}", raw)
    if doc is not None:
        if raw_path is not None and doc.raw_path is None:
            # backfill: the document was registered from an index without its payload
            doc.raw_path, doc.sha256 = str(raw_path), digest
            session.flush()
        elif digest and doc.sha256 and digest != doc.sha256:
            meta = json.loads(doc.metadata_json) if doc.metadata_json else {}
            revs = meta.setdefault("revised_payloads", [])
            entry = {"sha256": digest, "path": str(raw_path), "retrieved_at": utcnow().isoformat()}
            if entry["sha256"] not in [r.get("sha256") for r in revs]:
                revs.append(entry)
                doc.metadata_json = json.dumps(meta)
                log.warning("source %s/%s changed content; new payload archived, original kept", provider, external_id)
        return doc, False
    doc = SourceDocument(
        provider=provider,
        source_type=source_type,
        external_id=external_id,
        url=url,
        title=title,
        issuer=issuer,
        entity_key=entity_key,
        published_at=published_at,
        period_start=period_start,
        period_end=period_end,
        raw_path=str(raw_path) if raw_path else None,
        sha256=digest,
        parser_version=PARSER_VERSION,
        source_tier=source_tier,
        metadata_json=json.dumps(metadata) if metadata else None,
        status=status,
    )
    session.add(doc)
    session.flush()
    return doc, True
