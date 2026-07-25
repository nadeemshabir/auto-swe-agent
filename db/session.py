"""
db/session.py
Database engine, session factory, and initialization helpers.

Usage:
    from db.session import get_session

    with get_session() as session:
        run = session.get(Run, some_id)
        session.add(new_step)
        session.commit()

The engine is created lazily from DATABASE_URL. For tests, call
get_session(url="sqlite:///:memory:") to use an in-memory SQLite DB.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from db.models import Base

# ── Module-level state (lazy init) ───────────────────────────────────────────

_engine = None
_SessionFactory = None


def _get_engine(url: str | None = None):
    """Get or create the SQLAlchemy engine (singleton)."""
    global _engine
    if _engine is None or url is not None:
        db_url = url or os.getenv(
            "DATABASE_URL",
            "postgresql://agent:agent@localhost:5432/auto_swe_agent",
        )
        kwargs = {}
        if db_url.startswith("postgresql"):
            kwargs.update({
                "pool_pre_ping": True,     # detect stale connections
                "pool_size": 5,            # worker concurrency
                "max_overflow": 10,
            })
        elif db_url.startswith("sqlite"):
            from sqlalchemy.pool import StaticPool
            kwargs.update({
                "poolclass": StaticPool,
                "connect_args": {"check_same_thread": False},
            })
        _engine = create_engine(db_url, echo=False, **kwargs)
    return _engine


def _get_session_factory(url: str | None = None) -> sessionmaker:
    """Get or create the session factory (singleton)."""
    global _SessionFactory
    if _SessionFactory is None or url is not None:
        engine = _get_engine(url)
        _SessionFactory = sessionmaker(bind=engine, expire_on_commit=False)
    return _SessionFactory


# ── Public API ───────────────────────────────────────────────────────────────

@contextmanager
def get_session(url: str | None = None) -> Generator[Session, None, None]:
    """Context manager that yields a SQLAlchemy session.

    Commits on clean exit, rolls back on exception, and always closes.

    Parameters
    ----------
    url : str, optional
        Override the DATABASE_URL for this session. Primarily used in tests
        to point at an in-memory SQLite database.

    Example
    -------
        with get_session() as session:
            run = Run(repo="owner/repo", issue_number=1, status="queued")
            session.add(run)
            # auto-commits on exit
    """
    factory = _get_session_factory(url)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db(url: str | None = None) -> None:
    """Create all tables (idempotent — safe to call multiple times).

    For production, use Alembic migrations instead. This is for:
    - Tests (SQLite in-memory)
    - Quick local setup
    """
    engine = _get_engine(url)
    Base.metadata.create_all(engine)


def reset_singletons() -> None:
    """Reset module-level engine/session singletons (for tests)."""
    global _engine, _SessionFactory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionFactory = None
