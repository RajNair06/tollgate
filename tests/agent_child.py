"""Child process for the cross-process resume test.

Run by tests/test_agent_graph.py::test_resume_survives_a_process_restart as
a plain script (not under pytest):

    python tests/agent_child.py <db_url> run    <thread_id> <script.json>
    python tests/agent_child.py <db_url> resume <thread_id> <script.json>

The "run" phase invokes the graph until it pauses at the approval interrupt
and prints the interrupt payload as JSON. The process then EXITS — all state
lives in PostgreSQL. The "resume" phase is a brand-new process: it rebuilds
the graph, reads the approval_id back from the checkpoint's pending
interrupt, resumes with {"approved": true}, and prints the final status.

script.json shape: {"llm": [<text or null>...], "responses": {tool: envelope}}
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langgraph.types import Command  # noqa: E402

from agent.checkpoint import open_checkpointer  # noqa: E402
from agent.graph import build_graph  # noqa: E402
from agent.testing import FakeRegistry, ScriptedLLM  # noqa: E402


def _interrupt_payload(state):
    for task in getattr(state, "tasks", ()):
        for intr in getattr(task, "interrupts", ()):
            return dict(intr.value)
    return None


def main() -> int:
    db_url, phase, thread_id, script_path = sys.argv[1:5]
    script = json.loads(Path(script_path).read_text())

    conn, saver = open_checkpointer(db_url)
    try:
        graph = build_graph(ScriptedLLM(script["llm"]),
                            FakeRegistry(script["responses"]),
                            prompt_version="v1", checkpointer=saver)
        config = {"configurable": {"thread_id": thread_id}}

        if phase == "run":
            state = {"run_id": thread_id, "dispute_id": "PP-D-56244",
                     "status": "running"}
            result = graph.invoke(state, config)
            if "__interrupt__" not in result:
                print(json.dumps({"paused": False,
                                  "status": result.get("status")}))
                return 1
            print(json.dumps({"paused": True,
                              "interrupt": result["__interrupt__"][0].value}))
            return 0

        if phase == "resume":
            snap = graph.get_state(config)
            payload = _interrupt_payload(snap)
            if payload is None:
                print(json.dumps({"error": "thread is not paused"}))
                return 1
            result = graph.invoke(Command(resume={
                "approved": True,
                "approval_id": payload["approval_id"],
                "decided_by": "child-process-test",
            }), config)
            print(json.dumps({"status": result.get("status"),
                              "summary": result.get("summary")}))
            return 0

        print(json.dumps({"error": f"unknown phase {phase!r}"}))
        return 2
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
