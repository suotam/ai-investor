"""Central logging. Never log secrets."""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_CONFIGURED = False


def setup_logging(log_path: Path, level: str = "INFO") -> logging.Logger:
    global _CONFIGURED
    root = logging.getLogger("investor")
    if _CONFIGURED:
        return root
    log_path.parent.mkdir(parents=True, exist_ok=True)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = RotatingFileHandler(log_path, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    sh.setLevel(logging.WARNING)
    root.addHandler(sh)
    _CONFIGURED = True
    return root


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"investor.{name}")
