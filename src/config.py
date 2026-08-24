"""Configuration loading: YAML settings (non-secret) + .env (secrets)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class BenchmarkConfig:
    symbol: str
    name: str
    currency: str


@dataclass
class Settings:
    base_currency: str = "CZK"
    default_benchmark: str = "SPY"
    benchmarks: list[BenchmarkConfig] = field(default_factory=list)
    market_data_provider: str = "yahoo"
    default_history_start: str = "2015-01-01"
    ibkr_flex_version: int = 3
    ibkr_base_url: str = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService"
    ibkr_poll_interval_seconds: float = 5.0
    ibkr_max_poll_attempts: int = 12
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8501
    log_path: Path = PROJECT_ROOT / "logs" / "investor.log"
    log_level: str = "INFO"
    db_path: Path = PROJECT_ROOT / "data" / "investor.db"
    raw_dir: Path = PROJECT_ROOT / "data" / "raw"
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.db_path.as_posix()}"


def _resolve(p: str | Path) -> Path:
    path = Path(p)
    return path if path.is_absolute() else PROJECT_ROOT / path


def load_settings(config_path: str | Path | None = None) -> Settings:
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    cfg_path = _resolve(config_path or os.environ.get("INVESTOR_CONFIG_PATH", "config/settings.yaml"))
    raw: dict[str, Any] = {}
    if cfg_path.exists():
        with open(cfg_path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}

    pf = raw.get("portfolio", {}) or {}
    md = raw.get("market_data", {}) or {}
    ib = raw.get("ibkr", {}) or {}
    dash = raw.get("dashboard", {}) or {}
    lg = raw.get("logging", {}) or {}

    benchmarks = [
        BenchmarkConfig(symbol=b["symbol"], name=b.get("name", b["symbol"]), currency=b.get("currency", "USD"))
        for b in (pf.get("benchmarks") or [])
    ]

    s = Settings(
        base_currency=str(raw.get("base_currency", "CZK")).upper(),
        default_benchmark=pf.get("default_benchmark", "SPY"),
        benchmarks=benchmarks,
        market_data_provider=md.get("provider", "yahoo"),
        default_history_start=str(md.get("default_history_start", "2015-01-01")),
        ibkr_flex_version=int(ib.get("flex_version", 3)),
        ibkr_base_url=ib.get("base_url", Settings.ibkr_base_url),
        ibkr_poll_interval_seconds=float(ib.get("poll_interval_seconds", 5)),
        ibkr_max_poll_attempts=int(ib.get("max_poll_attempts", 12)),
        dashboard_host=dash.get("host", "127.0.0.1"),
        dashboard_port=int(dash.get("port", 8501)),
        log_path=_resolve(lg.get("path", "logs/investor.log")),
        log_level=lg.get("level", "INFO"),
        db_path=_resolve(os.environ.get("INVESTOR_DB_PATH", "data/investor.db")),
        raw=raw,
    )
    return s


def get_secret(name: str) -> str | None:
    """Read a secret from the environment (.env is loaded by load_settings)."""
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    val = os.environ.get(name)
    return val if val else None


def mask_secret(value: str | None) -> str:
    """Return a safe representation for logs: never the full value."""
    if not value:
        return "<unset>"
    if len(value) <= 6:
        return "***"
    return f"{value[:3]}...{value[-2:]} (len={len(value)})"
