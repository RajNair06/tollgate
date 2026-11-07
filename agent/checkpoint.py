"""PostgreSQL checkpointing for the graph + agent-database bootstrap.

LangGraph's PostgresSaver stores every super-step of the graph (state,
pending writes, interrupts) in PostgreSQL. This is what makes the approval
interrupt durable: the process can die mid-run — 'restart the container' —
and a fresh process resumes the exact paused state by thread_id.

The agent gets its OWN database (default: 'agent' on the same PostgreSQL
server as the gateway). Deliberate separation: the gateway's tables are the
system of record for money; the agent's tables are checkpoint plumbing and
run telemetry, with different lifecycles and no business being in each
other's migrations.
"""

from __future__ import annotations

import logging

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver
from sqlalchemy.engine import make_url

logger = logging.getLogger(__name__)


def ensure_database(database_url: str) -> None:
    """Create the agent database if it does not exist (idempotent)."""
    url = make_url(database_url)
    target = url.database
    # render_as_string(hide_password=False): str(url) would mask the password
    # as '***' and auth would fail with the literal asterisks.
    maintenance_url = url.set(database="postgres").render_as_string(hide_password=False)
    with psycopg.connect(maintenance_url, autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (target,)
        ).fetchone()
        if not exists:
            conn.execute(f'CREATE DATABASE "{target}"')
            logger.info("created agent database %r", target)


def open_checkpointer(database_url: str) -> tuple[psycopg.Connection, PostgresSaver]:
    """Connection + migrated PostgresSaver. Caller owns the connection.

    autocommit=True is what PostgresSaver expects for its own bookkeeping.
    """
    # prepare_threshold=0 mirrors PostgresSaver.from_conn_string: disables
    # server-side prepared statements, which otherwise collide across
    # connections/savers reusing statement names.
    conn = psycopg.connect(database_url, autocommit=True, prepare_threshold=0)
    saver = PostgresSaver(conn)
    saver.setup()  # creates checkpoint tables if missing; idempotent
    return conn, saver
