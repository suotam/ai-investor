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
    # v3 intelligence
    sec_user_agent: str = "InvestorOS/3.0 (personal research; contact not-set@example.com)"
    sec_rate_limit_seconds: float = 0.15
    sec_material_forms: list[str] = field(default_factory=lambda: [
        "10-K", "10-Q", "8-K", "20-F", "6-K", "S-1", "DEF 14A", "424B4", "F-1"
    ])
    ai_enabled: bool = False
    ai_provider: str = "llama_cpp"
    ai_base_url: str = "http://127.0.0.1:8080/v1"
    ai_model: str = "local"
    ai_timeout_seconds: float = 120.0
    ai_temperature: float = 0.2
    macro_default_series: list[dict] = field(default_factory=list)
    # v4 briefing thresholds (delta materiality)
    brief_price_move_pct: float = 5.0
    brief_weight_change_pp: float = 1.0
    brief_portfolio_move_pct: float = 2.0
    brief_insider_min_value_usd: float = 100000.0
    brief_macro_change_pct: float = 2.0
    brief_valuation_band_pct: float = 10.0
    brief_daily_max_items: int = 10
    brief_weekly_max_items: int = 25
    brief_output_dir: Path = PROJECT_ROOT / "output" / "briefs"
    # v5 operations & mentor
    ai_server_executable: str = r"C:\llama-cuda\bin\llama-server.exe"
    ai_server_model: str = ""
    ai_server_context: int = 16384
    tts_enabled: bool = False
    tts_provider: str = "windows_sapi"
    tts_rate: int = 0
    mentor_daily_target_minutes: int = 7
    mentor_weekly_target_minutes: int = 15
    mentor_show_technicals: bool = True
    mentor_show_macro: bool = True
    mentor_show_discovery: bool = True
    mentor_teach_me: bool = True
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


def _bday(raw: dict, key: str, default):
    return ((raw.get("briefing") or {}).get("daily") or {}).get(key, default)


def load_settings(config_path: str | Path | None = None) -> Settings:
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    cfg_path = _resolve(config_path or os.environ.get("INVESTOR_CONFIG_PATH", "config/settings.yaml"))
    raw: dict[str, Any] = {}
    if cfg_path.exists():
        with open(cfg_path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}

    pf = raw.get("portfolio", {}) or {}
    sec = raw.get("sec", {}) or {}
    ai = raw.get("ai", {}) or {}
    intel = raw.get("intelligence", {}) or {}
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
        sec_user_agent=os.environ.get("SEC_USER_AGENT") or sec.get("user_agent", Settings.sec_user_agent),
        sec_rate_limit_seconds=float(sec.get("rate_limit_seconds", 0.15)),
        sec_material_forms=list(sec.get("material_forms") or Settings().sec_material_forms),
        ai_enabled=bool(ai.get("enabled", False)),
        ai_provider=os.environ.get("AI_PROVIDER") or ai.get("provider", "llama_cpp"),
        ai_base_url=os.environ.get("AI_BASE_URL") or ai.get("base_url", "http://127.0.0.1:8080/v1"),
        ai_model=os.environ.get("AI_MODEL") or ai.get("model", "local"),
        ai_timeout_seconds=float(ai.get("timeout_seconds", 120)),
        ai_temperature=float(ai.get("temperature", 0.2)),
        macro_default_series=list(intel.get("macro_series") or []),
        brief_price_move_pct=float(_bday(raw, "price_move_pct", 5.0)),
        brief_weight_change_pp=float(_bday(raw, "portfolio_weight_change_pp", 1.0)),
        brief_portfolio_move_pct=float(_bday(raw, "portfolio_move_pct", 2.0)),
        brief_insider_min_value_usd=float(_bday(raw, "insider_min_value_usd", 100000)),
        brief_macro_change_pct=float(_bday(raw, "macro_change_pct", 2.0)),
        brief_valuation_band_pct=float(_bday(raw, "valuation_band_pct", 10.0)),
        brief_daily_max_items=int(_bday(raw, "max_items", 10)),
        brief_weekly_max_items=int(((raw.get("briefing") or {}).get("weekly") or {}).get("max_items", 25)),
        brief_output_dir=_resolve(((raw.get("briefing") or {}).get("output_dir")) or "output/briefs"),
        ai_server_executable=(raw.get("ai", {}) or {}).get("server_executable", Settings.ai_server_executable),
        ai_server_model=(raw.get("ai", {}) or {}).get("server_model", ""),
        ai_server_context=int((raw.get("ai", {}) or {}).get("server_context", 16384)),
        tts_enabled=bool((raw.get("tts", {}) or {}).get("enabled", False)),
        tts_provider=(raw.get("tts", {}) or {}).get("provider", "windows_sapi"),
        tts_rate=int((raw.get("tts", {}) or {}).get("rate", 0)),
        mentor_daily_target_minutes=int((raw.get("mentor", {}) or {}).get("daily_target_minutes", 7)),
        mentor_weekly_target_minutes=int((raw.get("mentor", {}) or {}).get("weekly_target_minutes", 15)),
        mentor_show_technicals=bool((raw.get("mentor", {}) or {}).get("show_technicals", True)),
        mentor_show_macro=bool((raw.get("mentor", {}) or {}).get("show_macro", True)),
        mentor_show_discovery=bool((raw.get("mentor", {}) or {}).get("show_discovery", True)),
        mentor_teach_me=bool((raw.get("mentor", {}) or {}).get("teach_me", True)),
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
