"""Database URL normalization for hosted Postgres (Supabase, Render, ...).

Why this exists: hosted providers hand out connection strings that do not
match what our two database stacks expect.

- SQLAlchemy (gateway engine, alembic, worker queue engine) needs an
  explicit dialect: ``postgresql+psycopg://``. Bare ``postgres://`` or
  ``postgresql://`` strings either fail to parse or select the wrong
  driver — Render hands out ``postgres://`` and the Supabase dashboard
  hands out ``postgresql://``.
- psycopg (LangGraph checkpointer, ``agent_runs``, the dashboard's
  cross-database reads) is the opposite: it does NOT understand the
  ``+psycopg`` suffix and accepts only plain ``postgresql://`` /
  ``postgres://``.

Supabase additionally requires TLS on every external connection (pooler
and direct hosts alike), so URLs pointing at a ``.supabase.co`` host get
``sslmode=require`` appended unless they already pin an sslmode.

Both helpers are idempotent and leave other URLs untouched — local dev
strings pass through unchanged.
"""

from __future__ import annotations

_PLAIN_SCHEMES = ("postgres://", "postgresql://")
_SQLALCHEMY_SCHEME = "postgresql+psycopg://"


def _require_ssl_for_supabase(url: str) -> str:
    if ".supabase.co" in url and "sslmode=" not in url:
        separator = "&" if "?" in url else "?"
        return f"{url}{separator}sslmode=require"
    return url


def for_sqlalchemy(url: str) -> str:
    """Normalize a connection URL for SQLAlchemy's create_engine / alembic."""
    stripped = url.strip()
    for plain in _PLAIN_SCHEMES:
        if stripped.startswith(plain):
            stripped = _SQLALCHEMY_SCHEME + stripped[len(plain):]
            break
    return _require_ssl_for_supabase(stripped)


def for_psycopg(url: str) -> str:
    """Normalize a connection URL for raw psycopg connections.

    Deliberately does NOT add the SQLAlchemy dialect suffix — psycopg
    would reject ``postgresql+psycopg://``.
    """
    return _require_ssl_for_supabase(url.strip())
