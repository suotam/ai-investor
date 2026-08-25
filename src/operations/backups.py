"""Rotating SQLite backups. Local only, never committed (data/ is gitignored).

Retention (configurable): keep the newest N daily and M weekly backups. Restore = copy the
backup file over data/investor.db while nothing else runs (documented in README).
"""
from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from src.config import Settings
from src.logging_setup import get_logger

log = get_logger("operations.backups")

DAILY_KEEP = 7
WEEKLY_KEEP = 4


def backup_dir(settings: Settings) -> Path:
    return settings.db_path.parent / "backups"


def create_backup(settings: Settings, kind: str = "daily") -> Path | None:
    """Copy the SQLite file (safe while no writer is active). Returns the backup path."""
    if not settings.db_path.exists():
        return None
    folder = backup_dir(settings)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    dest = folder / f"investor-{kind}-{stamp}.db"
    n = 1
    while dest.exists():  # guarantee uniqueness even for sub-microsecond successive calls
        dest = folder / f"investor-{kind}-{stamp}-{n}.db"
        n += 1
    shutil.copy2(settings.db_path, dest)
    rotate(settings, kind)
    log.info("backup created: %s", dest)
    return dest


def rotate(settings: Settings, kind: str, keep: int | None = None) -> list[Path]:
    keep = keep if keep is not None else (DAILY_KEEP if kind == "daily" else WEEKLY_KEEP)
    folder = backup_dir(settings)
    if not folder.exists():
        return []
    files = sorted(folder.glob(f"investor-{kind}-*.db"), key=lambda p: p.name, reverse=True)
    removed = []
    for old in files[keep:]:
        old.unlink()
        removed.append(old)
        log.info("backup rotated out: %s", old.name)
    return removed


def latest_backup(settings: Settings) -> Path | None:
    folder = backup_dir(settings)
    if not folder.exists():
        return None
    files = sorted(folder.glob("investor-*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None
