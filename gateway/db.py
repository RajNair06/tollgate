"""Database engine, session, and transaction management.

One engine per process (it owns the connection pool). Sessions are
short-lived: one per unit of work, via session_scope().

Transaction contract of session_scope():
- Work inside the context manager shares ONE transaction.
- Normal exit  -> COMMIT.
- Any exception -> ROLLBACK, then the exception propagates.
This makes multi-table writes (action + approval + audit + events + outbox)
atomic: all land, or none do.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from config import get_settings

_engine = None
_SessionFactory = None


def get_engine():
    """Lazily create the process-wide engine."""
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_engine(
            settings.database_url,
            pool_size=10,          # concurrent workers (threads) each need a connection
            max_overflow=20,       # burst headroom for the concurrency test
            pool_pre_ping=True,    # transparently replace dead connections
        )
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _SessionFactory


@contextmanager
def session_scope() -> Iterator[Session]:
    """One session, one transaction. Commit on success, rollback on error."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_engine_for_tests(url: str) -> None:
    """Point the module at a different database (used by the test suite)."""
    global _engine, _SessionFactory
    if _engine is not None:
        _engine.dispose()
    _engine = create_engine(url, pool_size=10, max_overflow=40, pool_pre_ping=True)
    _SessionFactory = sessionmaker(bind=_engine, expire_on_commit=False)
