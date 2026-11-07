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

import re
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


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
    # Dedup only: the original was 'allowed' but its PayPal execution
    # definitely failed — replay will not re-execute; use a NEW key to retry.
    original_execution_failed: bool = False
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


# ---------------------------------------------------------------------------
# Admin provisioning (POST /v1/admin/...) — guarded by X-Admin-Key
# ---------------------------------------------------------------------------

class CreateAgentRequest(BaseModel):
    """Admin: provision an agent with its policy and first API key."""
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    allowed_actions: list[str] = Field(min_length=1, max_length=32)
    per_txn_cap: StrictDecimal = Field(gt=0, max_digits=12, decimal_places=2)
    rolling_24h_cap: StrictDecimal = Field(gt=0, max_digits=12, decimal_places=2)
    approval_threshold: StrictDecimal = Field(gt=0, max_digits=12, decimal_places=2)
    approval_ttl_minutes: int = Field(default=60, gt=0, le=43200)
    key_label: str | None = Field(default="primary", max_length=64)

    @field_validator("allowed_actions")
    @classmethod
    def _check_actions(cls, v: list[str]) -> list[str]:
        for item in v:
            if not re.fullmatch(r"[a-z][a-z0-9_]*", item):
                raise ValueError(
                    f"action {item!r} invalid: lowercase snake_case only, "
                    "e.g. 'refund'"
                )
        if len(set(v)) != len(v):
            raise ValueError("allowed_actions contains duplicates")
        return v

    @model_validator(mode="after")
    def _threshold_within_cap(self) -> "CreateAgentRequest":
        # Mirrors ck_policies_threshold_within_cap: reject at the edge with a
        # clear 422 instead of an opaque DB IntegrityError.
        if self.approval_threshold > self.per_txn_cap:
            raise ValueError(
                "approval_threshold must be <= per_txn_cap, otherwise the "
                "approval path is unreachable (cap denies first)"
            )
        return self


class CreateAgentResponse(BaseModel):
    agent_id: int
    name: str
    key_id: int
    key_prefix: str
    # Shown exactly once. The system stores only the SHA-256 hash; no one —
    # not even an admin — can ever retrieve this plaintext again.
    api_key: str


class IssueKeyResponse(BaseModel):
    agent_id: int
    key_id: int
    key_prefix: str
    api_key: str  # shown exactly once


class RevokeKeyResponse(BaseModel):
    key_id: int
    agent_id: int
    key_prefix: str
    revoked_at: str
    already_revoked: bool


class AgentKeyView(BaseModel):
    id: int
    key_prefix: str
    label: str | None
    created_at: str
    revoked_at: str | None


class AgentAdminView(BaseModel):
    id: int
    name: str
    is_active: bool
    created_at: str
    policy: dict
    keys: list[AgentKeyView]
