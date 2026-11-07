"""Autonomous dispute-handling agent for the toll gate.

Two generations live here:
- loop_agent.py   a from-scratch prompt/tool loop (registry, max iterations,
                  error handling) — the mechanics, hand-built and visible.
- graph.py        the same job as a LangGraph StateGraph with typed nodes,
                  critic validation, PostgreSQL checkpointing, and
                  interrupt/resume across human approvals.

Both talk to the toll gate exclusively through its MCP server (/mcp) — the
agent never touches PayPal or the database directly.
"""
