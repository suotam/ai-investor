"""Engine/session factory and migration helpers."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from src.config import PROJECT_ROOT

_ENGINE_CACHE: dict[str, Engine] = {}


def get_engine(db_url: str) -> Engine:
    eng = _ENGINE_CACHE.get(db_url)
    if eng is None:
        if db_url.startswith("sqlite:///") and not db_url.endswith(":memory:"):
            Path(db_url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        eng = create_engine(db_url, future=True)

        @event.listens_for(eng, "connect")
        def _fk_on(dbapi_conn, _rec):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        _ENGINE_CACHE[db_url] = eng
    return eng


def get_session_factory(db_url: str) -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(db_url), future=True, expire_on_commit=False)


@contextmanager
def session_scope(db_url: str) -> Iterator[Session]:
    factory = get_session_factory(db_url)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def alembic_config(db_url: str) -> AlembicConfig:
    cfg = AlembicConfig(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "src" / "db" / "migrations"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


def run_migrations(db_url: str) -> None:
    """Upgrade the database to head. Idempotent."""
    command.upgrade(alembic_config(db_url), "head")


def current_revision(db_url: str) -> str | None:
    eng = get_engine(db_url)
    with eng.connect() as conn:
        try:
            row = conn.execute(text("SELECT version_num FROM alembic_version")).fetchone()
        except Exception:
            return None
    return row[0] if row else None


def dispose_engine(db_url: str) -> None:
    eng = _ENGINE_CACHE.pop(db_url, None)
    if eng is not None:
        eng.dispose()
