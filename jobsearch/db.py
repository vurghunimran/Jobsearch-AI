"""SQLite engine and session helpers."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from jobsearch import models  # noqa: F401  (import registers the tables)
from jobsearch.config import Settings, get_settings

_engine: Engine | None = None


def _configure_sqlite(dbapi_connection, _record) -> None:
    """WAL keeps the dashboard readable while the scheduler writes."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def get_engine(settings: Settings | None = None) -> Engine:
    global _engine
    if _engine is None:
        settings = settings or get_settings()
        settings.ensure_dirs()
        url = settings.resolved_database_url
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        _engine = create_engine(url, connect_args=connect_args, pool_pre_ping=True)
        if url.startswith("sqlite"):
            event.listen(_engine, "connect", _configure_sqlite)
    return _engine


def init_db(settings: Settings | None = None) -> None:
    SQLModel.metadata.create_all(get_engine(settings))


def reset_engine() -> None:
    """Drop the cached engine. Used by tests that point at a temp database."""
    global _engine
    if _engine is not None:
        _engine.dispose()
    _engine = None


@contextmanager
def session_scope(settings: Settings | None = None) -> Iterator[Session]:
    """Transactional session: commits on success, rolls back on exception."""
    # expire_on_commit=False keeps loaded values usable after commit, which
    # matters because the pipeline hands ORM objects to worker threads.
    session = Session(get_engine(settings), expire_on_commit=False)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
