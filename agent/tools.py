"""Tool registry: the agent's ONLY doorway to the outside world.

A tool is a ToolSpec: name, description, JSON-schema parameters, and a Python
callable. The registry does three jobs:

1. Translate specs into Gemini function declarations (schema → the model).
2. Execute calls by name, validating that the tool exists.
3. NEVER raise into the agent loop: a failed tool becomes a structured
   {"ok": False, "error": ...} result that is fed back to the LLM, which can
   read the error and adapt. The loop only dies on budget/LLM failures.

All toll-gate tools are thin MCP wrappers: each call opens a fresh fastmcp
Client session (async, bridged with asyncio.run), sends the agent's API key,
and returns the gateway's JSON envelope. Fresh session per call keeps the
agent stateless across process restarts — the gateway is the only source of
truth, and it re-authenticates every request anyway.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict  # JSON schema object
    fn: Callable[[dict], Any]


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"tool {spec.name!r} already registered")
        self._tools[spec.name] = spec

    def tool(self, name: str, description: str, parameters: dict):
        """Decorator form of register()."""
        def wrap(fn: Callable[[dict], Any]) -> Callable[[dict], Any]:
            self.register(ToolSpec(name=name, description=description,
                                   parameters=parameters, fn=fn))
            return fn
        return wrap

    def names(self) -> list[str]:
        return list(self._tools)

    def gemini_tools(self) -> list[Any]:
        """Function declarations for the Gemini API (schema passed through —
        the MCP server enforces the real validation)."""
        from google.genai import types
        return [types.Tool(function_declarations=[
            types.FunctionDeclaration(
                name=s.name, description=s.description, parameters=s.parameters)
            for s in self._tools.values()
        ])]

    def execute(self, name: str, args: dict | None) -> dict:
        """Run one tool call. Returns a JSON-able dict, never raises."""
        spec = self._tools.get(name)
        if spec is None:
            return {"ok": False, "error": f"UNKNOWN_TOOL: no tool named {name!r}",
                    "available_tools": self.names()}
        try:
            result = spec.fn(args or {})
        except ToolCallError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 — feed ANY failure back to the LLM
            logger.exception("tool %s crashed", name)
            return {"ok": False, "error": f"TOOL_CRASHED: {type(exc).__name__}: {exc}"}
        return {"ok": True, "result": result}


class ToolCallError(Exception):
    """Tool-level failure with a message meant for the LLM to read."""


# ---------------------------------------------------------------------------
# MCP bridge: toll-gate tools
# ---------------------------------------------------------------------------

def _call_mcp(url: str, api_key: str, tool_name: str, arguments: dict) -> Any:
    """One MCP tools/call over streamable HTTP. Sync wrapper around the async
    fastmcp Client. Raises ToolCallError with the gateway's reason code."""
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport
    from fastmcp.exceptions import ToolError

    async def _run():
        transport = StreamableHttpTransport(
            url=url, headers={"Authorization": f"Bearer {api_key}"})
        async with Client(transport) as client:
            return await client.call_tool(tool_name, arguments)

    try:
        result = asyncio.run(_run())
    except ToolError as exc:
        # Gateway errors arrive as "REASON_CODE: message" — pass through intact.
        raise ToolCallError(str(exc)) from exc
    except Exception as exc:  # connection refused, DNS, protocol errors
        raise ToolCallError(f"GATEWAY_UNREACHABLE: {type(exc).__name__}: {exc}") from exc

    data = result.data
    if data is None:  # non-structured error result
        raise ToolCallError(result.content[0].text if result.content
                            else "tool returned no data")
    return data


# Parameter schemas — deliberately mirror the gateway's own tool schemas
# (the MCP server enforces them; these tell the LLM what to produce).
_TXN_ID = {"type": "string", "pattern": "^[A-Z0-9]{17}$",
           "description": "PayPal transaction/capture ID (17 uppercase alphanumeric)."}
_DISPUTE_ID = {"type": "string", "pattern": "^PP-D-\\d{1,12}$",
               "description": "PayPal dispute ID, e.g. PP-D-56244."}
_IDEM_KEY = {"type": "string", "minLength": 8, "maxLength": 64,
             "pattern": "^[A-Za-z0-9_-]+$",
             "description": "Idempotency key; replays return the original decision."}
_AMOUNT = {"type": "string", "pattern": "^\\d+\\.\\d{2}$",
           "description": 'Positive decimal string, e.g. "10.99".'}
_CURRENCY = {"type": "string", "enum": ["USD", "EUR", "GBP", "CAD", "AUD", "INR"]}


def build_tollgate_registry(url: str, api_key: str) -> ToolRegistry:
    """Registry with the five gateway tools, each an MCP wrapper."""
    reg = ToolRegistry()

    def spec(name: str, description: str, parameters: dict) -> None:
        reg.register(ToolSpec(
            name=name, description=description, parameters=parameters,
            fn=lambda args, _n=name: _call_mcp(url, api_key, _n, args)))

    spec("list_disputes",
         "List PayPal disputes visible to this account (newest first).",
         {"type": "object",
          "properties": {"page_size": {"type": "integer", "minimum": 1, "maximum": 50},
                          "start_time": {"type": "string",
                                          "description": "RFC 3339 UTC filter, e.g. 2026-10-01T00:00:00Z."}},
          "required": []})

    spec("get_dispute",
         "Fetch one dispute in full (reason, status, amounts, disputed transactions).",
         {"type": "object", "properties": {"dispute_id": _DISPUTE_ID},
          "required": ["dispute_id"]})

    spec("get_transaction",
         "Fetch a PayPal capture/transaction by ID (status, amount, timestamps).",
         {"type": "object", "properties": {"transaction_id": _TXN_ID},
          "required": ["transaction_id"]})

    spec("issue_refund",
         "Refund a capture, gated by this agent's policy. Outcomes: allowed "
         "(executed on PayPal immediately), pending_approval (a human must "
         "decide; no money moves), denied, deduplicated.",
         {"type": "object",
          "properties": {"capture_id": _TXN_ID, "amount": _AMOUNT,
                          "currency": _CURRENCY, "idempotency_key": _IDEM_KEY,
                          "note_to_payer": {"type": "string", "maxLength": 256}},
          "required": ["capture_id", "amount", "idempotency_key"]})

    spec("execute_approved_refund",
         "Execute a refund whose approval a human has granted "
         "(approval status must be 'approved'). Idempotent via approval id.",
         {"type": "object", "properties": {"approval_id": {"type": "integer"}},
          "required": ["approval_id"]})

    return reg


def interpret_refund_result(data: dict) -> dict:
    """Normalize an issue_refund envelope into what the graph branches on:

    kind ∈ executed | pending_approval | denied | deduplicated | error
    """
    if not data.get("ok"):
        return {"kind": "error", "detail": data}
    decision = data.get("decision", {})
    outcome = decision.get("outcome")
    if outcome == "allowed" and data.get("refund"):
        return {"kind": "executed", "refund": data["refund"],
                "action_id": decision.get("action_id"), "summary": data.get("summary")}
    if outcome == "pending_approval":
        return {"kind": "pending_approval",
                "approval_id": decision.get("approval_id"),
                "action_id": decision.get("action_id"),
                "summary": data.get("summary")}
    if outcome == "denied":
        return {"kind": "denied", "reason_code": decision.get("reason_code"),
                "action_id": decision.get("action_id"), "summary": data.get("summary")}
    if outcome == "deduplicated":
        return {"kind": "deduplicated",
                "original_outcome": decision.get("original_outcome"),
                "duplicate_of_action_id": decision.get("duplicate_of_action_id"),
                "summary": data.get("summary")}
    return {"kind": "error", "detail": data}
