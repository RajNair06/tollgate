"""The from-scratch agent loop: prompt → tool calls → tool outputs → LLM → ...

This is the mechanical heart of any tool-using agent, written out explicitly
(before LangGraph abstracts it into a graph):

    transcript = [task]
    repeat at most max_iterations times:
        response = llm.generate(transcript, tools=registry)
        transcript += [response.content]            # the model's turn, verbatim
        if response has no function calls:
            return response.text                    # done — final answer
        for each requested call:
            output = registry.execute(name, args)   # never raises
            transcript += [function_response(output)]  # feed results back
    give up with status "max_iterations"

Invariants worth naming:
- The model's raw content is appended BEFORE the function responses — Gemini
  requires each function_response to follow the function_call it answers.
- Tool failures are data, not exceptions: the LLM sees {"ok": false, "error":
  ...} and can correct itself (wrong ID format, unknown tool, ...).
- max_iterations bounds cost and prevents infinite call loops; the budget
  meter inside the LLM client bounds tokens. Both are hard stops.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Sequence

from agent.gemini_client import LLMClient
from agent.tools import ToolRegistry

logger = logging.getLogger(__name__)

DEFAULT_MAX_ITERATIONS = 8


@dataclass
class ToolCallRecord:
    iteration: int
    name: str
    args: dict
    output: dict


@dataclass
class LoopOutcome:
    status: str                    # completed | max_iterations
    final_text: str
    iterations: int
    tool_calls: list[ToolCallRecord] = field(default_factory=list)


def run_loop(
    llm: LLMClient,
    registry: ToolRegistry,
    task: str,
    *,
    system: str,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
) -> LoopOutcome:
    """Run one task to completion (or to the iteration cap)."""
    from google.genai import types

    transcript: list[Any] = [types.Content(
        role="user", parts=[types.Part(text=task)])]
    tool_calls: list[ToolCallRecord] = []
    tools = registry.gemini_tools()

    for iteration in range(1, max_iterations + 1):
        response = llm.generate(transcript, system=system, tools=tools)
        if response.content is not None:
            transcript.append(response.content)

        if not response.function_calls:
            logger.info("loop finished after %d iteration(s)", iteration)
            return LoopOutcome(status="completed", final_text=response.text,
                               iterations=iteration, tool_calls=tool_calls)

        # Execute every requested call, then return ALL outputs in one
        # function-response message (Gemini allows multiple parts per turn).
        parts = []
        for call in response.function_calls:
            output = registry.execute(call.name, call.args)
            tool_calls.append(ToolCallRecord(iteration=iteration, name=call.name,
                                             args=call.args, output=output))
            parts.append(types.Part.from_function_response(
                name=call.name, response=output))
        transcript.append(types.Content(role="user", parts=parts))

    logger.warning("loop hit the iteration cap (%d)", max_iterations)
    return LoopOutcome(status="max_iterations", final_text="",
                       iterations=max_iterations, tool_calls=tool_calls)
