"""Agent configuration — environment-driven, with safe local defaults.

The agent is a separate process from the gateway; it authenticates to the
gateway's MCP endpoint with a regular agent API key (tg_...) and is subject
to the same policies, caps, and audit trail as any other agent.
"""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class AgentSettings:
    # Toll gate MCP endpoint + the agent's own API key (issued via admin API).
    mcp_url: str
    gateway_api_key: str
    # LLM
    gemini_api_key: str
    model: str
    # Per-run token budget (input+output). Exceeding it aborts the run.
    token_budget: int
    # Agent's own PostgreSQL (checkpointing + run log). Separate database
    # from the gateway's, same server by default.
    database_url: str
    # Prompt version directory under agent/prompts/.
    prompt_version: str

    # Transient-failure retry policy for LLM calls.
    max_retries: int = 5
    backoff_base_seconds: float = 0.5
    backoff_max_seconds: float = 30.0


def get_agent_settings() -> AgentSettings:
    return AgentSettings(
        mcp_url=os.environ.get("AGENT_MCP_URL", "http://localhost:8000/mcp").strip(),
        gateway_api_key=os.environ.get("AGENT_API_KEY", "").strip(),
        gemini_api_key=os.environ.get("GEMINI_API_KEY", "").strip(),
        model=os.environ.get("AGENT_MODEL", "gemini-2.5-flash").strip(),
        token_budget=int(os.environ.get("AGENT_TOKEN_BUDGET", "100000")),
        database_url=os.environ.get(
            "AGENT_DATABASE_URL",
            "postgresql://tollgate:tollgate@localhost:5433/agent",
        ).strip(),
        prompt_version=os.environ.get("AGENT_PROMPT_VERSION", "v1").strip(),
    )
