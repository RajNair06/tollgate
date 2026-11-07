"""Per-run metering: unique run ID, token budget, cost accounting.

Every run gets one RunMeter. Each LLM call charges its usage to the meter;
when the cumulative input+output tokens would exceed the budget, the call is
refused BEFORE being made (LLMBudgetExceeded) — a runaway agent loop can burn
at most one call past the point where the budget was still respected.

Costs use a static USD-per-million-token price table (input, output). Prices
are a config concern, not a live-quote concern: they change rarely, and an
agent should not call a pricing API to know what a run cost.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field


class LLMBudgetExceeded(Exception):
    """The run's token budget is exhausted."""


# USD per 1M tokens, (input, output). Extend as models are added.
# gemini-3.8-flash pricing per aistudio.google.com/docs/pricing as of
# 2026-11 (introductory rate valid through 2026-12-31).
PRICING_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "gemini-3.8-flash": (0.75, 3.75),
    "gemini-3.1-flash-lite": (0.25, 1.50),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-pro": (1.25, 10.00),
    "gemini-2.0-flash": (0.15, 0.60),
}


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class RunMeter:
    """One run: identity, budget, cumulative usage, derived cost."""

    model: str
    budget_tokens: int
    run_id: str = field(default_factory=lambda: f"run-{uuid.uuid4().hex[:16]}")
    started_at: float = field(default_factory=time.time)
    usage: Usage = field(default_factory=Usage)
    llm_calls: int = 0
    status: str = "running"  # running | completed | failed | budget_exceeded | ...

    def check_headroom(self, estimated_tokens: int = 0) -> None:
        """Refuse a call that would start over budget. Raises before any
        network call, so the budget is a hard ceiling, not a soft target."""
        if self.usage.total + estimated_tokens > self.budget_tokens:
            self.status = "budget_exceeded"
            raise LLMBudgetExceeded(
                f"run {self.run_id}: token budget {self.budget_tokens} exhausted "
                f"(used {self.usage.total})"
            )

    def charge(self, input_tokens: int, output_tokens: int) -> None:
        self.usage.input_tokens += input_tokens
        self.usage.output_tokens += output_tokens
        self.llm_calls += 1

    @property
    def cost_usd(self) -> float:
        pin, pout = PRICING_USD_PER_MTOK.get(self.model, (0.0, 0.0))
        return round(
            self.usage.input_tokens / 1_000_000 * pin
            + self.usage.output_tokens / 1_000_000 * pout,
            6,
        )

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "model": self.model,
            "status": self.status,
            "llm_calls": self.llm_calls,
            "input_tokens": self.usage.input_tokens,
            "output_tokens": self.usage.output_tokens,
            "total_tokens": self.usage.total,
            "budget_tokens": self.budget_tokens,
            "cost_usd": self.cost_usd,
            "elapsed_seconds": round(time.time() - self.started_at, 3),
        }
