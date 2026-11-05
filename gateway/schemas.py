"""Request/response schemas — strict validation at the edge.

Strictness rules (deliberate):
- extra="forbid": unknown fields are a 422, not silently ignored. A typo
  like "ammount" must fail loudly, not authorize a request missing its amount.
- strict=True: no type coercion. amount must arrive as a JSON STRING
  ("10.99") — a JSON float is rejected, because floats are exactly what the
  Decimal discipline exists to keep out of the system. Same wire convention
  PayPal itself uses.
- Patterns and bounds mirror the DB CHECK constraints, so bad data is
  rejected at the edge with a helpful 422 instead of an opaque DB error.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field


def _strict_decimal(v: Any) -> Decimal:
    """Parse a decimal STRING directly into Decimal — never via float.

    Why a BeforeValidator: strict=True makes Pydantic demand a real Decimal
    instance, but JSON has no decimal type. This bridges the gap while keeping
    the strictness contract: str in, Decimal out; int/float/bool rejected with
    a clear message (a JSON number would smuggle float imprecision in).
    """
    if isinstance(v, Decimal):
        return v
    if isinstance(v, str):
        try:
            return Decimal(v)
        except InvalidOperation:
            raise ValueError(f"not a valid decimal string: {v!r}") from None
    raise ValueError('amount must be a decimal string, e.g. "10.99"')


StrictDecimal = Annotated[Decimal, BeforeValidator(_strict_decimal)]


class ActionRequest(BaseModel):
    """POST /v1/agent-actions body."""
    model_config = ConfigDict(extra="forbid", strict=True)

    action_type: str = Field(
        min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$",
        description="Lowercase snake_case verb, e.g. 'refund'. Must be in the agent's policy allowlist.",
    )
    amount: StrictDecimal = Field(
        gt=0, max_digits=12, decimal_places=2,
        description="Positive decimal STRING, e.g. \"10.99\". JSON numbers are rejected (strict mode).",
    )
    currency: str = Field(default="USD", min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    idempotency_key: str = Field(
        min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$",
        description="Caller-generated unique key. Replays return the original decision.",
    )
    reference_id: str | None = Field(
        default=None, max_length=128,
        description="External resource this action targets, e.g. a PayPal capture ID.",
    )


class DecisionResponse(BaseModel):
    """The policy engine verdict. HTTP 200 for ALL outcomes — the decision is
    the payload; 4xx is reserved for auth/validation/rate-limit failures."""
    outcome: Literal["allowed", "denied", "pending_approval", "deduplicated"]
    reason_code: str
    action_id: int
    approval_id: int | None = None
    duplicate_of_action_id: int | None = None
    original_outcome: str | None = None
    rolling_24h_used: Decimal
    rolling_24h_cap: Decimal


class ApprovalDecisionRequest(BaseModel):
    """POST /v1/approvals/{id}/decision body."""
    model_config = ConfigDict(extra="forbid", strict=True)

    decision: Literal["approved", "rejected"]
    decided_by: str = Field(min_length=1, max_length=128)


class ExecutionResultRequest(BaseModel):
    """POST /v1/approvals/{id}/execute body — outcome of the PayPal call."""
    model_config = ConfigDict(extra="forbid", strict=True)

    executed: bool
    reason_code: str | None = Field(default=None, max_length=64)


class ActionView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    action_type: str
    amount: Decimal
    currency: str
    outcome: str
    reason_code: str
    reference_id: str | None
    created_at: str
