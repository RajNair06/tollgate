"""Concurrency test: 50 simultaneous refund requests vs one rolling cap.

The race being proven impossible: without serialization, all 50 threads
could read "consumed = 0" at the same time, each conclude it fits under the
cap, and together approve 5x the daily limit (lost update).

The mechanism under test: authorize() takes SELECT ... FOR UPDATE on the
agent row first, so all 50 transactions queue on that one row lock. Each
thread sees the sum INCLUDING every previously committed decision.

Two variants:
- allowed-path:  exactly floor(cap/amount) actions allowed, rest denied,
                 total == cap, never above.
- pending-path:  every request >= approval_threshold routes to pending;
                 reservations must obey the same cap.
"""

import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import func, select

from gateway.db import session_scope
from gateway.models import Action, Agent, Policy
from gateway.policy_engine import ActionRequest, authorize

N_THREADS = 50


def _fire_concurrent(agent_id: int, amount: str) -> list:
    """Run N_THREADS authorizations simultaneously (barrier-aligned)."""
    barrier = threading.Barrier(N_THREADS)
    results = []
    results_lock = threading.Lock()

    def worker():
        r = ActionRequest(
            action_type="refund",
            amount=Decimal(amount),
            currency="USD",
            idempotency_key=uuid4().hex,  # unique: no dedup interference
        )
        barrier.wait()  # release all threads at once for maximum contention
        with session_scope() as s:
            agent = s.get(Agent, agent_id)
            policy = s.execute(
                select(Policy).where(Policy.agent_id == agent_id)
            ).scalar_one()
            d = authorize(s, agent, policy, r)
        with results_lock:
            results.append(d)

    with ThreadPoolExecutor(max_workers=N_THREADS) as pool:
        futures = [pool.submit(worker) for _ in range(N_THREADS)]
        for f in futures:
            f.result()  # re-raise any worker exception
    return results


def _db_totals(agent_id: int) -> tuple[Decimal, int]:
    """(sum of committed cap-reserving amounts, row count) straight from SQL."""
    with session_scope() as s:
        total = s.execute(
            select(func.coalesce(func.sum(Action.amount), 0)).where(
                Action.agent_id == agent_id,
                Action.outcome.in_(("allowed", "pending_approval")),
            )
        ).scalar_one()
        count = s.execute(
            select(func.count()).select_from(Action).where(
                Action.agent_id == agent_id,
                Action.outcome.in_(("allowed", "pending_approval")),
            )
        ).scalar_one()
    return Decimal(total), count


def test_50_concurrent_refunds_never_exceed_daily_cap(make_agent):
    # per_txn=1000 and threshold=1000 keep $10 requests on the allowed path;
    # rolling=100 → at most 10 can ever be approved.
    agent_id, _ = make_agent(per_txn="1000.00", rolling="100.00", threshold="1000.00")

    results = _fire_concurrent(agent_id, "10.00")

    allowed = [d for d in results if d.outcome == "allowed"]
    denied = [d for d in results if d.outcome == "denied"]
    assert len(results) == N_THREADS
    assert len(allowed) == 10  # exactly floor(100/10)
    assert len(denied) == 40
    assert all(d.reason_code == "ROLLING_24H_CAP_EXCEEDED" for d in denied)

    # The invariant the whole system exists for:
    total, count = _db_totals(agent_id)
    assert total == Decimal("100.00")
    assert total <= Decimal("100.00")
    assert count == 10

    # Every single decision — allowed AND denied — is in the audit log.
    with session_scope() as s:
        from gateway.models import AuditLog

        audited = s.execute(
            select(func.count()).select_from(AuditLog).where(AuditLog.agent_id == agent_id)
        ).scalar_one()
    assert audited == N_THREADS


def test_50_concurrent_pending_reservations_respect_cap(make_agent):
    # threshold == per_txn == 10 → every $10 request routes to pending_approval;
    # reservations must serialize exactly like allowances.
    agent_id, _ = make_agent(per_txn="10.00", rolling="100.00", threshold="10.00")

    results = _fire_concurrent(agent_id, "10.00")

    pending = [d for d in results if d.outcome == "pending_approval"]
    denied = [d for d in results if d.outcome == "denied"]
    assert len(pending) == 10
    assert len(denied) == 40
    total, _ = _db_totals(agent_id)
    assert total == Decimal("100.00")  # reserved == cap, never above
