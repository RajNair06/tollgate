"""Shared fixtures: a real PostgreSQL test database, migrated by Alembic.

Why a real database (not SQLite/mocks): the correctness claims under test —
SELECT FOR UPDATE serialization, partial unique indexes, CHECK constraints,
NUMERIC money — are PostgreSQL behavior. Mocking them would test the mocks.

Isolation:
- Session scope: create tollgate_test if absent, run `alembic upgrade head`
  (so migrations themselves are exercised on every run), repoint gateway.db.
- Function scope: TRUNCATE ... RESTART IDENTITY CASCADE + flush Redis
  rate-limit keys (agent IDs restart at 1, so stale rl:agent:1:* counters
  from a previous test would leak into the next one).

Seeding commits immediately (its own session_scope) so worker threads in the
concurrency test — which use their own sessions — can see the fixtures.
"""

import os
from decimal import Decimal
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from sqlalchemy import select, text

TEST_DB_URL = "postgresql+psycopg://tollgate:tollgate@localhost:5433/tollgate_test"
ADMIN_DSN = "postgresql://tollgate:tollgate@localhost:5433/postgres"


@pytest.fixture(scope="session", autouse=True)
def _database():
    """Create + migrate the test database once per session."""
    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = 'tollgate_test'"
        ).fetchone()
        if not exists:
            conn.execute(sql.SQL("CREATE DATABASE tollgate_test"))

    os.environ["DATABASE_URL"] = TEST_DB_URL

    from alembic import command
    from alembic.config import Config

    command.upgrade(Config("alembic.ini"), "head")

    from gateway.db import reset_engine_for_tests

    reset_engine_for_tests(TEST_DB_URL)
    yield


@pytest.fixture(autouse=True)
def _clean(_database):
    """Empty every table and the Redis rate-limit keyspace before each test."""
    from gateway.db import session_scope
    from gateway.redis_client import get_redis

    with session_scope() as s:
        # CASCADE reaches policies/actions/approvals/audit_log via FKs;
        # events/outbox/webhook_* have no FKs and are truncated explicitly.
        s.execute(text(
            "TRUNCATE agents, events, outbox, webhook_events, webhook_outbox "
            "RESTART IDENTITY CASCADE"))
    try:
        r = get_redis()
        stale = list(r.scan_iter(match="rl:*", count=500))
        if stale:
            r.delete(*stale)
    except Exception:
        pass  # Redis down: rate-limit tests will be skipped/fail loudly instead
    yield


@pytest.fixture()
def make_agent():
    """Factory: provision agent + policy, return (agent_id, plaintext_key).

    Defaults: per_txn=100, rolling=500, threshold=50, ttl=60min, refund only.
    """
    from gateway.api_keys import generate_api_key
    from gateway.db import session_scope
    from gateway.models import Agent, AgentKey, Policy

    def _make(
        *,
        per_txn: str = "100.00",
        rolling: str = "500.00",
        threshold: str = "50.00",
        ttl_minutes: int = 60,
        allowed: tuple[str, ...] = ("refund",),
        active: bool = True,
    ) -> tuple[int, str]:
        plaintext, key_hash, prefix = generate_api_key()
        with session_scope() as s:  # commits on exit → visible to other sessions
            agent = Agent(name=f"agent-{uuid4().hex[:10]}", is_active=active)
            s.add(agent)
            s.flush()
            s.add(
                Policy(
                    agent_id=agent.id,
                    allowed_actions=list(allowed),
                    per_txn_cap=Decimal(per_txn),
                    rolling_24h_cap=Decimal(rolling),
                    approval_threshold=Decimal(threshold),
                    approval_ttl_minutes=ttl_minutes,
                )
            )
            s.add(
                AgentKey(
                    agent_id=agent.id, key_hash=key_hash,
                    key_prefix=prefix, label="primary",
                )
            )
            s.flush()
            agent_id = agent.id
        return agent_id, plaintext

    return _make


@pytest.fixture()
def decide(make_agent):
    """Run one authorization in its own committed transaction."""
    from gateway.db import session_scope
    from gateway.models import Agent, Policy
    from gateway.policy_engine import ActionRequest, authorize

    def _decide(agent_id: int, req: ActionRequest):
        with session_scope() as s:
            agent = s.get(Agent, agent_id)
            policy = s.execute(
                select(Policy).where(Policy.agent_id == agent_id)
            ).scalar_one()
            return authorize(s, agent, policy, req)

    return _decide
