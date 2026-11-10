"""Shared fakes for agent tests — importable by pytest AND by the
cross-process child script (tests/agent_child.py), so both exercise the real
graph against scripted worlds.

ScriptedLLM: pops canned texts (or raises canned exceptions) per generate().
FakeRegistry: canned tool envelopes per tool name, records every call.
"""

from __future__ import annotations

import json
from typing import Any, Sequence

from agent.budget import RunMeter
from agent.gemini_client import LLMResult

CAPTURE_ID = "05Y454980K6595141"   # 17 chars, real sandbox shape
DISPUTE_ID = "PP-D-56244"

DISPUTE = {
    "dispute_id": DISPUTE_ID,
    "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
    "status": "WAITING_FOR_SELLER_RESPONSE",
    "dispute_amount": {"value": "12.99", "currency_code": "USD"},
    "create_time": "2026-11-01T10:00:00Z",
    "disputed_transactions": [{"seller_transaction_id": CAPTURE_ID}],
}

TRANSACTION = {
    "id": CAPTURE_ID,
    "status": "COMPLETED",
    "amount": {"value": "12.99", "currency": "USD"},
    "final_capture": True,
    "created_time": "2026-10-28T09:00:00Z",
    "updated_time": "2026-10-28T09:05:00Z",
}

# A draft that cites ONLY facts present in the evidence above.
CLEAN_DRAFT = (
    "The buyer disputes capture 05Y454980K6595141 for 12.99 USD, created "
    "2026-10-28T09:00:00Z with status COMPLETED. The dispute "
    "(PP-D-56244, reason MERCHANDISE_OR_SERVICE_NOT_RECEIVED) was opened "
    "2026-11-01T10:00:00Z. The evidence contains no delivery confirmation.\n\n"
    "REFUND RECOMMENDATION: FULL REFUND of 12.99 USD — item-not-received "
    "claims without delivery evidence should be refunded."
)

CRITIC_OK = json.dumps({"supported": True, "violations": []})


def decision_json(*, action: str = "issue_refund", amount: str = "12.99",
                  confidence: float = 0.9, dispute_id: str = DISPUTE_ID,
                  transaction_id: str | None = CAPTURE_ID) -> str:
    """A schema-valid RefundDecision payload (as the LLM would emit it)."""
    return json.dumps({
        "dispute_id": dispute_id,
        "proposed_action": action,
        "refund_amount": amount if action == "issue_refund" else "0.00",
        "currency": "USD",
        "confidence": confidence,
        "reasoning": "Item-not-received dispute with no delivery evidence in the packet.",
        "transaction_id": transaction_id if action == "issue_refund" else None,
        "cited_evidence": [{
            "source": "transaction",
            "claim": "capture completed for 12.99 USD",
            "value": "12.99",
            "transaction_id": transaction_id,
            "observed_at": "2026-10-28T09:00:00Z",
        }],
    })


class ScriptedLLM:
    """Canned LLM. Script entries: str → returned as text; Exception → raised."""

    def __init__(self, script: Sequence[str | Exception], meter: RunMeter | None = None):
        self.script = list(script)
        self.meter = meter
        self.prompts_seen: list[str | None] = []
        self.inputs_seen: list[str] = []
        self.response_schemas_seen: list = []

    def generate(self, contents, *, system=None, tools=None, temperature=None,
                 response_schema=None) -> LLMResult:
        self.prompts_seen.append(system)
        self.response_schemas_seen.append(response_schema)
        self.inputs_seen.append(str(contents))
        if not self.script:
            raise AssertionError("ScriptedLLM exhausted — graph made an extra call")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        if self.meter is not None:
            self.meter.check_headroom()
            self.meter.charge(100, 50)  # mirror GeminiClient's metering
        return LLMResult(text=item, function_calls=(), content=None,
                         input_tokens=100, output_tokens=50)


class FakeRegistry:
    """Canned tool envelopes. responses: {tool_name: dict | list[dict] | Exception}.
    A list pops per call; a dict repeats. Records (name, args) in .calls."""

    def __init__(self, responses: dict[str, Any]):
        self.responses = responses
        self.calls: list[tuple[str, dict]] = []

    def execute(self, name: str, args: dict | None) -> dict:
        args = args or {}
        self.calls.append((name, args))
        item = self.responses.get(name)
        if item is None:
            return {"ok": False, "error": f"UNKNOWN_TOOL: {name}"}
        if isinstance(item, list):
            item = item.pop(0) if len(item) > 1 else item[0]
        if isinstance(item, Exception):
            return {"ok": False, "error": str(item)}
        return {"ok": True, "result": item}

    def names_called(self) -> list[str]:
        return [n for n, _ in self.calls]


# --- canned gateway envelopes (the shapes the real MCP server returns) -------

def envelope_dispute(dispute: dict = DISPUTE) -> dict:
    return {"ok": True, "summary": f"Dispute {dispute['dispute_id']}",
            "dispute": dispute}


def envelope_transaction(txn: dict = TRANSACTION) -> dict:
    return {"ok": True, "summary": f"Transaction {txn['id']}",
            "transaction": txn}


def envelope_refund_pending(approval_id: int = 4, action_id: int = 65) -> dict:
    return {"ok": True,
            "summary": f"Refund awaits human approval (approval #{approval_id})",
            "decision": {"outcome": "pending_approval", "reason_code": "APPROVAL_REQUIRED",
                          "action_id": action_id, "approval_id": approval_id,
                          "duplicate_of_action_id": None, "original_outcome": None,
                          "rolling_24h_used": "22.00", "rolling_24h_cap": "100.00"}}


def envelope_refund_executed(action_id: int = 66, refund_id: str = "92F13256LW3371821") -> dict:
    return {"ok": True, "summary": f"Refund {refund_id} executed",
            "decision": {"outcome": "allowed", "reason_code": "OK",
                          "action_id": action_id, "approval_id": None,
                          "duplicate_of_action_id": None, "original_outcome": None,
                          "rolling_24h_used": "12.99", "rolling_24h_cap": "100.00"},
            "refund": {"id": refund_id, "status": "COMPLETED",
                        "amount": {"value": "12.99", "currency": "USD"},
                        "paypal_request_id": "req-xyz"}}


def envelope_refund_denied(action_id: int = 67,
                            reason: str = "PER_TXN_CAP_EXCEEDED") -> dict:
    return {"ok": True, "summary": f"Refund DENIED ({reason})",
            "decision": {"outcome": "denied", "reason_code": reason,
                          "action_id": action_id, "approval_id": None,
                          "duplicate_of_action_id": None, "original_outcome": None,
                          "rolling_24h_used": "0.00", "rolling_24h_cap": "100.00"}}


def envelope_execute_approved(refund_id: str = "2VF20713Y24658739") -> dict:
    return {"ok": True, "summary": f"Approved refund executed: {refund_id}",
            "approval": {"id": 4, "status": "executed",
                          "reason_code": "PAYPAL_REFUND_COMPLETED"},
            "refund": {"id": refund_id, "status": "COMPLETED",
                        "amount": {"value": "12.99", "currency": "USD"},
                        "paypal_request_id": "req-abc"}}
