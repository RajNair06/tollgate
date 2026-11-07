"""Phase A tests: the hand-built agent loop, tool registry, Gemini client
retries, budget metering, and prompt loading. No network, no gateway, no
real Gemini — FakeLLM scripts the model, fake tools script the world.
"""

import pytest
from google.genai import types

from agent.budget import LLMBudgetExceeded, RunMeter
from agent.gemini_client import (
    FunctionCall,
    GeminiClient,
    LLMError,
    LLMResult,
)
from agent.loop_agent import run_loop
from agent.prompts import available_versions, load_prompt
from agent.tools import (
    ToolCallError,
    ToolRegistry,
    ToolSpec,
    interpret_refund_result,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeLLM:
    """Scripted LLM: pops one canned LLMResult per generate() call and
    records every transcript it was shown (so tests can assert tool outputs
    were fed back)."""

    def __init__(self, responses: list[LLMResult]):
        self.responses = list(responses)
        self.transcripts: list[list] = []
        self.calls = 0

    def generate(self, contents, *, system=None, tools=None, temperature=None):
        self.transcripts.append(list(contents))
        self.systems = system
        if not self.responses:
            raise AssertionError("FakeLLM exhausted — loop did not stop")
        self.calls += 1
        return self.responses.pop(0)


def text_result(text: str, in_tok=10, out_tok=5) -> LLMResult:
    content = types.Content(role="model", parts=[types.Part(text=text)])
    return LLMResult(text=text, function_calls=(), content=content,
                     input_tokens=in_tok, output_tokens=out_tok)


def call_result(name: str, args: dict) -> LLMResult:
    content = types.Content(role="model", parts=[
        types.Part(function_call=types.FunctionCall(name=name, args=args))])
    return LLMResult(text="", function_calls=(FunctionCall(name, args),),
                     content=content, input_tokens=10, output_tokens=5)


def echo_registry(value: dict | None = None, boom: bool = False) -> ToolRegistry:
    reg = ToolRegistry()

    def fn(args):
        if boom:
            raise RuntimeError("kaboom")
        return value if value is not None else {"echo": args}

    reg.register(ToolSpec(name="echo", description="echoes",
                          parameters={"type": "object", "properties": {}}, fn=fn))
    return reg


SYSTEM = "test system prompt"


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------

def test_loop_completes_with_final_text():
    llm = FakeLLM([text_result("the answer is 42")])
    out = run_loop(llm, echo_registry(), "task", system=SYSTEM)
    assert out.status == "completed"
    assert out.final_text == "the answer is 42"
    assert out.iterations == 1
    assert out.tool_calls == []


def test_tool_output_is_fed_back_to_the_llm():
    """The core contract: after a tool call, the NEXT transcript must contain
    a function_response part carrying the tool's output dict."""
    llm = FakeLLM([
        call_result("echo", {"x": 1}),
        text_result("done"),
    ])
    out = run_loop(llm, echo_registry(value={"echo": {"x": 1}}),
                   "task", system=SYSTEM)
    assert out.status == "completed"
    assert len(out.tool_calls) == 1
    assert out.tool_calls[0].output == {"ok": True, "result": {"echo": {"x": 1}}}

    second_transcript = llm.transcripts[1]
    # user task, model function_call turn, function_response turn
    assert len(second_transcript) == 3
    fr_parts = second_transcript[2].parts
    assert any(getattr(p, "function_response", None) is not None for p in fr_parts)
    payload = fr_parts[0].function_response.response
    assert payload == {"ok": True, "result": {"echo": {"x": 1}}}


def test_max_iteration_cap_stops_a_looping_model():
    llm = FakeLLM([call_result("echo", {"i": i}) for i in range(10)])
    out = run_loop(llm, echo_registry(), "task", system=SYSTEM, max_iterations=3)
    assert out.status == "max_iterations"
    assert out.iterations == 3
    assert len(out.tool_calls) == 3
    assert out.final_text == ""


def test_unknown_tool_error_is_data_not_a_crash():
    llm = FakeLLM([
        call_result("nonexistent", {}),
        text_result("recovered"),
    ])
    out = run_loop(llm, echo_registry(), "task", system=SYSTEM)
    assert out.status == "completed"
    record = out.tool_calls[0]
    assert record.output["ok"] is False
    assert "UNKNOWN_TOOL" in record.output["error"]
    assert record.output["available_tools"] == ["echo"]


def test_tool_exception_is_returned_as_error():
    llm = FakeLLM([
        call_result("echo", {}),
        text_result("recovered"),
    ])
    out = run_loop(llm, echo_registry(boom=True), "task", system=SYSTEM)
    payload = out.tool_calls[0].output
    assert payload["ok"] is False
    assert "TOOL_CRASHED" in payload["error"] and "kaboom" in payload["error"]


def test_multiple_calls_in_one_turn_all_get_responses():
    llm = FakeLLM([
        LLMResult(text="", function_calls=(FunctionCall("echo", {"a": 1}),
                                           FunctionCall("echo", {"b": 2})),
                  content=types.Content(role="model", parts=[
                      types.Part(function_call=types.FunctionCall(name="echo", args={"a": 1})),
                      types.Part(function_call=types.FunctionCall(name="echo", args={"b": 2})),
                  ]), input_tokens=10, output_tokens=5),
        text_result("both done"),
    ])
    out = run_loop(llm, echo_registry(), "task", system=SYSTEM)
    assert len(out.tool_calls) == 2
    fr_parts = llm.transcripts[1][2].parts
    assert len(fr_parts) == 2  # one function_response per call, same turn


# ---------------------------------------------------------------------------
# Gemini client: retries, backoff, error classes, metering
# ---------------------------------------------------------------------------

class FakeSDKResponse:
    def __init__(self, text="ok"):
        self.text = text
        self.function_calls = []
        self.candidates = [types.Candidate(
            content=types.Content(role="model", parts=[types.Part(text=text)]),
            finish_reason="STOP")]
        self.usage_metadata = types.UsageMetadata(
            prompt_token_count=11, response_token_count=7, total_token_count=18)


class FakeSDK:
    """Stands in for genai.Client: .models.generate_content with a scripted
    sequence of exceptions/results."""

    def __init__(self, script: list):
        self.script = list(script)
        self.models = self
        self.attempts = 0

    def generate_content(self, model, contents, config):
        self.attempts += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_client(script, sleeps, meter=None, rng=lambda: 0.5):
    return GeminiClient(api_key="fake", meter=meter,
                        sleep=sleeps.append, rng=rng, client=FakeSDK(script))


def _server_error():
    from google.genai import errors
    return errors.ServerError(503, {"error": {"message": "unavailable"}})


def _client_error(code=400):
    from google.genai import errors
    return errors.ClientError(code, {"error": {"message": "bad request"}})


def test_transient_errors_retry_with_exponential_backoff():
    sleeps: list[float] = []
    # rng()=0.5 → jitter factor = 0.5+0.5 = 1.0 → delays are exact powers.
    client = make_client([_server_error(), _server_error(), FakeSDKResponse()],
                         sleeps)
    result = client.generate(["hi"])
    assert result.text == "ok"
    assert sleeps == [0.5, 1.0]  # base*2^0, base*2^1 (jitter factor 1.0)


def test_backoff_is_capped():
    sleeps: list[float] = []
    client = GeminiClient(api_key="fake", sleep=sleeps.append, rng=lambda: 0.5,
                          backoff_base=1.0, backoff_cap=4.0, max_retries=5,
                          client=FakeSDK([_server_error()] * 5 + [FakeSDKResponse()]))
    client.generate(["hi"])
    assert sleeps == [1.0, 2.0, 4.0, 4.0, 4.0]  # capped at 4s


def test_jitter_scales_delays():
    sleeps: list[float] = []
    # rng()=0.0 → factor 0.5; rng()=0.999... → factor ~1.5
    rngs = iter([0.0, 0.5])
    client = GeminiClient(api_key="fake", sleep=sleeps.append, rng=lambda: next(rngs),
                          backoff_base=2.0, max_retries=5,
                          client=FakeSDK([_server_error(), _server_error(), FakeSDKResponse()]))
    client.generate(["hi"])
    assert sleeps == [1.0, 4.0]  # 2*2^0*0.5, 2*2^1*1.0


def test_429_is_transient_but_400_is_not():
    from google.genai import errors
    sleeps: list[float] = []
    client = make_client([_client_error(429), FakeSDKResponse()], sleeps)
    client.generate(["hi"])           # 429 → retried
    assert len(sleeps) == 1

    client2 = make_client([_client_error(400)], [])
    with pytest.raises(LLMError):     # 400 → raised immediately, no retry
        client2.generate(["hi"])
    assert client2._client.attempts == 1


def test_retries_exhausted_raises_llm_error():
    sleeps: list[float] = []
    client = GeminiClient(api_key="fake", sleep=sleeps.append, rng=lambda: 0.5,
                          max_retries=2, client=FakeSDK([_server_error()] * 3))
    with pytest.raises(LLMError):
        client.generate(["hi"])
    assert len(sleeps) == 2  # slept between attempts, gave up after the last


def test_successful_call_charges_the_meter():
    meter = RunMeter(model="gemini-2.5-flash", budget_tokens=1000)
    client = make_client([FakeSDKResponse(), FakeSDKResponse()], [], meter=meter)
    client.generate(["a"])
    client.generate(["b"])
    assert meter.usage.input_tokens == 22
    assert meter.usage.output_tokens == 14
    assert meter.llm_calls == 2
    assert meter.cost_usd > 0


def test_exhausted_budget_blocks_before_any_call():
    meter = RunMeter(model="gemini-2.5-flash", budget_tokens=10)
    client = make_client([FakeSDKResponse()], [], meter=meter)
    client.generate(["a"])                       # spends 18 tokens > 10 budget
    with pytest.raises(LLMBudgetExceeded):
        client.generate(["b"])                   # refused BEFORE the wire
    assert meter.status == "budget_exceeded"     # flagged by the headroom check
    assert client._client.attempts == 1


def test_missing_api_key_fails_fast():
    with pytest.raises(LLMError, match="GEMINI_API_KEY"):
        GeminiClient(api_key="")


# ---------------------------------------------------------------------------
# Registry, envelopes, prompts, budget math
# ---------------------------------------------------------------------------

def test_registry_rejects_duplicate_names():
    reg = echo_registry()
    with pytest.raises(ValueError, match="already registered"):
        reg.register(ToolSpec(name="echo", description="x", parameters={}, fn=lambda a: a))


def test_gemini_tools_shape():
    tools = echo_registry().gemini_tools()
    assert len(tools) == 1
    decl = tools[0].function_declarations[0]
    assert decl.name == "echo"


def test_interpret_refund_result_kinds():
    executed = interpret_refund_result({
        "ok": True, "summary": "s",
        "decision": {"outcome": "allowed", "action_id": 7},
        "refund": {"id": "R1", "status": "COMPLETED"}})
    assert executed["kind"] == "executed" and executed["refund"]["id"] == "R1"

    pending = interpret_refund_result({
        "ok": True, "summary": "s",
        "decision": {"outcome": "pending_approval", "approval_id": 4, "action_id": 8}})
    assert pending == {"kind": "pending_approval", "approval_id": 4,
                       "action_id": 8, "summary": "s"}

    denied = interpret_refund_result({
        "ok": True, "summary": "s",
        "decision": {"outcome": "denied", "reason_code": "PER_TXN_CAP_EXCEEDED",
                     "action_id": 9}})
    assert denied["kind"] == "denied"
    assert denied["reason_code"] == "PER_TXN_CAP_EXCEEDED"

    dupe = interpret_refund_result({
        "ok": True, "summary": "s",
        "decision": {"outcome": "deduplicated", "original_outcome": "allowed",
                     "duplicate_of_action_id": 3}})
    assert dupe["kind"] == "deduplicated" and dupe["original_outcome"] == "allowed"

    err = interpret_refund_result({"ok": False, "error": "PAYPAL_ERROR: x"})
    assert err["kind"] == "error"


def test_prompt_loader_reads_v1_and_fails_loudly():
    text = load_prompt("loop_system", "v1")
    assert "Toll Gate" in text and "idempotency_key" in text
    for name in ("draft_response", "critic", "decide_refund"):
        assert load_prompt(name, "v1")
    with pytest.raises(FileNotFoundError):
        load_prompt("loop_system", "v99")
    with pytest.raises(FileNotFoundError):
        load_prompt("nonexistent_prompt", "v1")
    assert "v1" in available_versions()


def test_min_confidence_env_parsing(monkeypatch):
    from agent.config import get_agent_settings
    from agent.state import MIN_CONFIDENCE_FOR_ACTION

    monkeypatch.delenv("AGENT_MIN_CONFIDENCE", raising=False)
    assert get_agent_settings().min_confidence == MIN_CONFIDENCE_FOR_ACTION

    monkeypatch.setenv("AGENT_MIN_CONFIDENCE", "0.8")
    assert get_agent_settings().min_confidence == 0.8

    # Fail loudly at startup, never silently clamp: out-of-range or garbage
    # values are configuration errors.
    for bad in ("1.5", "-0.1", "high"):
        monkeypatch.setenv("AGENT_MIN_CONFIDENCE", bad)
        with pytest.raises(ValueError):
            get_agent_settings()


def test_meter_cost_math():
    meter = RunMeter(model="gemini-2.5-flash", budget_tokens=10**9)
    meter.charge(1_000_000, 1_000_000)   # 1M in + 1M out
    # gemini-2.5-flash: $0.30/M in, $2.50/M out
    assert meter.cost_usd == 2.80
    assert meter.as_dict()["total_tokens"] == 2_000_000


def test_unknown_model_costs_zero_without_crashing():
    meter = RunMeter(model="future-model-9", budget_tokens=100)
    meter.charge(50, 10)
    assert meter.cost_usd == 0.0


def test_default_model_has_a_pricing_entry():
    """Model catalogs drift (a live 404 taught us this). If the default
    model has no price, every run would silently record cost 0."""
    from agent.budget import PRICING_USD_PER_MTOK
    from agent.gemini_client import DEFAULT_MODEL
    assert DEFAULT_MODEL in PRICING_USD_PER_MTOK
