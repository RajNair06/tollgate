"""Run telemetry: one row per agent run in the agent database.

Recorded per run: run_id, dispute, terminal status, input/output tokens,
computed USD cost, LLM call count, and the full record as JSONB. Written by
the graph's finalize node through the run_log hook — upsert, because a
resumed run finalizes again and must update (not duplicate) its row.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import (JSON, BigInteger, Column, DateTime, MetaData,
                        Numeric, String, Table, create_engine, text)

logger = logging.getLogger(__name__)

_metadata = MetaData()

agent_runs = Table(
    "agent_runs", _metadata,
    Column("run_id", String(64), primary_key=True),
    Column("dispute_id", String(32), nullable=False),
    Column("status", String(48), nullable=False),
    Column("summary", String(2000), nullable=False, default=""),
    Column("input_tokens", BigInteger, nullable=False, default=0),
    Column("output_tokens", BigInteger, nullable=False, default=0),
    Column("total_tokens", BigInteger, nullable=False, default=0),
    Column("cost_usd", Numeric(12, 6), nullable=False, default=0),
    Column("llm_calls", BigInteger, nullable=False, default=0),
    Column("detail", JSON, nullable=False, default=dict),
    Column("created_at", DateTime(timezone=True), nullable=False,
           default=lambda: datetime.now(timezone.utc)),
    Column("updated_at", DateTime(timezone=True), nullable=False,
           default=lambda: datetime.now(timezone.utc)),
)

_UPSERT = text("""
INSERT INTO agent_runs
    (run_id, dispute_id, status, summary, input_tokens, output_tokens,
     total_tokens, cost_usd, llm_calls, detail, created_at, updated_at)
VALUES
    (:run_id, :dispute_id, :status, :summary, :input_tokens, :output_tokens,
     :total_tokens, :cost_usd, :llm_calls, CAST(:detail AS JSON), now(), now())
ON CONFLICT (run_id) DO UPDATE SET
    status = EXCLUDED.status,
    summary = EXCLUDED.summary,
    input_tokens = EXCLUDED.input_tokens,
    output_tokens = EXCLUDED.output_tokens,
    total_tokens = EXCLUDED.total_tokens,
    cost_usd = EXCLUDED.cost_usd,
    llm_calls = EXCLUDED.llm_calls,
    detail = EXCLUDED.detail,
    updated_at = now()
""")


class RunLog:
    """Bound to one agent database; init() creates the table if missing."""

    def __init__(self, database_url: str):
        self._engine = create_engine(database_url.replace("postgresql://", "postgresql+psycopg://", 1)
                                     if database_url.startswith("postgresql://") else database_url)

    def init(self) -> None:
        _metadata.create_all(self._engine)

    def write(self, record: dict) -> None:
        usage = record.get("usage") or {}
        with self._engine.begin() as conn:
            conn.execute(_UPSERT, {
                "run_id": record["run_id"],
                "dispute_id": record.get("dispute_id", ""),
                "status": record.get("status", "unknown"),
                "summary": (record.get("summary") or "")[:2000],
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
                "cost_usd": usage.get("cost_usd", 0),
                "llm_calls": usage.get("llm_calls", 0),
                "detail": json.dumps(record, default=str),
            })

    def dispose(self) -> None:
        self._engine.dispose()
