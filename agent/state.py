"""Graph state + the structured refund-decision schema.

AgentState is the LangGraph state: a TypedDict every node reads from and
returns partial updates to. LangGraph merges each node's returned dict into
the state and the checkpointer persists every version — which is what makes
interrupt/resume across process restarts work.

RefundDecision is the contract the decide_refund node must produce. It is
deliberately paranoid:
- extra="forbid": a field the model invented is a validation failure;
- every ID/timestamp field carries the SAME regex the gateway enforces, so a
  hallucinated transaction ID cannot even be represented in a valid decision;
- cross-field validators encode policy ("refund amount only with
  issue_refund", "low confidence must escalate") — the model is told these
  rules in the prompt, and the schema mechanically enforces them.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, model_validator

# Same vocabularies as the gateway's MCP layer — one wire language end to end.
CURRENCIES = ("USD", "EUR", "GBP", "CAD", "AUD", "INR")
TXN_ID_PATTERN = r"^[A-Z0-9]{17}$"
DISPUTE_ID_PATTERN = r"^PP-D-\d{1,12}$"
TIMESTAMP_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$"

# Below this confidence the schema refuses any automatic action.
# This is a POLICY DEFAULT, not a measured value: LLM self-reported
# confidence is uncalibrated until agent_runs accumulates labeled outcomes
# (every human approval/escalation decision is a label). Override per
# deployment via AGENT_MIN_CONFIDENCE (agent/config.py) → build_graph(
# min_confidence=...) → validation context below.
MIN_CONFIDENCE_FOR_ACTION = 0.6


class AgentState(TypedDict, total=False):
    # identity
    run_id: str
    dispute_id: str
    # gather_evidence
    evidence: dict[str, Any]      # {"dispute": ..., "transactions": {...}, "retrieved_at": ...}
    # draft_response
    draft: str
    # critic
    critic_report: dict[str, Any]  # {"supported": bool, "violations": [...], "id_violations": [...]}
    # decide_refund
    decision: dict[str, Any] | None       # validated RefundDecision as dict
    decision_error: str | None
    # request_refund / execute_refund
    refund_request: dict[str, Any] | None  # interpret_refund_result(...) envelope
    refund_result: dict[str, Any] | None   # post-approval execution result
    # lifecycle
    status: str                    # running | <terminal status>
    summary: str
    usage: dict[str, Any]          # RunMeter.as_dict() snapshot
    error: str | None              # failure detail for terminal error states


class CitedEvidence(BaseModel):
    """One fact the decision relies on, with its source and exact value."""
    model_config = ConfigDict(extra="forbid")

    source: Literal["dispute", "transaction", "draft", "gateway"]
    claim: str = Field(min_length=1, max_length=500)
    # The exact value cited — an amount, a status, an ID. Quoted verbatim so
    # the deterministic post-check can verify it appears in the evidence.
    value: str = Field(min_length=1, max_length=200)
    transaction_id: str | None = Field(default=None, pattern=TXN_ID_PATTERN)
    observed_at: str | None = Field(default=None, pattern=TIMESTAMP_PATTERN)


class RefundDecision(BaseModel):
    """The structured decision — validated before any money can move."""
    model_config = ConfigDict(extra="forbid")

    dispute_id: str = Field(pattern=DISPUTE_ID_PATTERN)
    proposed_action: Literal["issue_refund", "deny_refund", "escalate_human"]
    # Decimal STRING with exactly 2 places — same wire discipline as the
    # gateway ("0.00" for non-refund actions).
    refund_amount: str = Field(pattern=r"^\d+\.\d{2}$")
    currency: Literal[CURRENCIES] = "USD"
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(min_length=1, max_length=2000)
    # The transaction the refund would act on (required for issue_refund).
    transaction_id: str | None = Field(default=None, pattern=TXN_ID_PATTERN)
    cited_evidence: list[CitedEvidence] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def _cross_field_rules(self, info: ValidationInfo) -> "RefundDecision":
        try:
            amount = Decimal(self.refund_amount)
        except InvalidOperation as exc:  # pragma: no cover — pattern guards this
            raise ValueError(f"refund_amount is not a decimal: {self.refund_amount!r}") from exc

        if self.proposed_action == "issue_refund":
            if amount <= 0:
                raise ValueError("issue_refund requires refund_amount > 0")
            if self.transaction_id is None:
                raise ValueError("issue_refund requires the transaction_id it acts on")
        else:
            if amount != 0:
                raise ValueError(
                    f"{self.proposed_action} must carry refund_amount '0.00', "
                    f"got {self.refund_amount!r}")

        min_confidence = float((info.context or {}).get(
            "min_confidence", MIN_CONFIDENCE_FOR_ACTION))
        if self.confidence < min_confidence and self.proposed_action != "escalate_human":
            raise ValueError(
                f"confidence {self.confidence} < {min_confidence} "
                f"must escalate_human, not {self.proposed_action}")
        return self
