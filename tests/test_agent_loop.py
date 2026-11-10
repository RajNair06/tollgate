"""Phase A tests: the hand-built agent loop, tool registry, Gemini client
retries, budget metering, and prompt loading. No network, no gateway, no
real Gemini — FakeLLM scripts the model, fake tools script the world.
"""

import json

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


def test_prompt_v2_fixes_refund_intent_wording():
    """v2 (2026-11-10, live rehearsal): the draft wrote "We are issuing a
    partial refund"; the critic read the intent as an unsupported factual
    claim and the run ended rejected_by_critic — no money moved, but the
    verdict was wrong. v2 tells the drafter the refund is strictly FUTURE,
    and tells the critic that intent statements are not evidence claims.
    v1 stays byte-identical so pinned runs remain reproducible."""
    for name in ("draft_response", "critic", "decide_refund", "loop_system"):
        assert load_prompt(name, "v2")
    draft = load_prompt("draft_response", "v2")
    assert "never as an action in progress or completed" in draft
    critic = load_prompt("critic", "v2")
    assert "statements of intent" in critic.lower()
    # v1 must NOT have picked up the clauses (immutability of old versions)
    assert "statements of intent" not in load_prompt("critic", "v1").lower()
    assert "in progress or completed" not in load_prompt("draft_response", "v1")
    assert "v2" in available_versions()


def test_prompt_v3_mechanical_compliance():
    """v3 (2026-11-10, second live rejection): v2's stylistic rule ("phrase
    as intent") was not enough for gemini-3.1-flash-lite — it wrote "We are
    issuing this refund" anyway, and the v2 critic (told to flag present-
    tense assertions) rejected it. v3 makes compliance mechanical: the
    drafter gets allowed/banned phrase lists and an inline MUST for the
    recommendation sentence; the critic gets an explicit chronology clause
    (intent — even present progressive — is never a violation) and two new
    violation classes: completed-refund claims and claimed investigations
    ("we have reviewed our delivery logs", seen in both live drafts)."""
    for name in ("draft_response", "critic", "decide_refund", "loop_system"):
        assert load_prompt(name, "v3")
    draft = load_prompt("draft_response", "v3")
    assert 'MUST begin with "We recommend" or "We propose"' in draft
    assert "Banned phrasings" in draft
    assert "Never claim investigations" in draft
    critic = load_prompt("critic", "v3")
    assert "present-progressive" in critic
    assert "ALREADY COMPLETED" in critic
    assert "Claimed investigations" in critic
    # v1/v2 untouched
    assert "present-progressive" not in load_prompt("critic", "v2")
    assert "Banned phrasings" not in load_prompt("draft_response", "v2")
    assert "v3" in available_versions()


def test_prompt_v4_internal_vs_external_jurisdiction():
    """v4 (2026-11-10, third live rejection): flash-lite complied with the
    v3 whitelist in the recommendation section but wrote "we have decided
    to accept the buyer's claim" in the body — and the v3 critic flagged
    the merchant's own decision as a completed action. Whack-a-mole ends
    with a principled line: statements of the merchant's decision/intent
    can never be evidence violations (the draft IS the decision); only
    external-world claims (money moved, goods delivered, investigations
    performed) must be evidence-backed. The drafter also gets a
    placeholder example — small models imitate examples more reliably
    than rules, and placeholders cannot trip the deterministic ID/
    timestamp scan."""
    for name in ("draft_response", "critic", "decide_refund", "loop_system"):
        assert load_prompt(name, "v4")
    critic = load_prompt("critic", "v4")
    assert "JURISDICTION" in critic
    assert "we have decided to accept the claim" in critic  # named as allowed
    assert "external world" in critic
    draft = load_prompt("draft_response", "v4")
    assert "EXAMPLE" in draft and "<PLACEHOLDER>" in draft
    assert "We propose to accept the buyer's claim" in draft
    # placeholders must not match the critic's deterministic scan patterns
    import re
    from agent.graph import _ID_RE, _TS_RE
    assert not _ID_RE.findall(draft)
    assert not _TS_RE.findall(draft)
    # v3 untouched
    assert "JURISDICTION" not in load_prompt("critic", "v3")
    assert "v4" in available_versions()


def test_prompt_v5_gives_the_schema_it_promises():
    """v5 (2026-11-10, first live decide_refund): the prompt said "matching
    the schema you are given" — but NO schema was ever given; flash-lite
    invented field names ({"decision": ...} for "proposed_action") and the
    run schema_failed. v5 injects the rendered RefundDecision JSON schema
    via {{DECISION_SCHEMA}} (replaced at build time, same pattern as
    {{MIN_CONFIDENCE}}), and the graph now also enforces it at the API
    layer via response_schema (constrained decoding)."""
    for name in ("draft_response", "critic", "decide_refund", "loop_system"):
        assert load_prompt(name, "v5")
    decide = load_prompt("decide_refund", "v5")
    assert "{{DECISION_SCHEMA}}" in decide
    assert "Field names are mandatory" in decide
    # older versions must not have the placeholder (their behavior is pinned;
    # the graph's replace() is a no-op on them)
    assert "{{DECISION_SCHEMA}}" not in load_prompt("decide_refund", "v4")
    assert "{{DECISION_SCHEMA}}" not in load_prompt("decide_refund", "v1")
    assert "v5" in available_versions()


def test_to_gemini_schema_strips_everything_the_api_rejects():
    """Live 400 (2026-11-10): handing the SDK the pydantic class made its
    converter emit snake_case additional_properties (extra="forbid") —
    INVALID_ARGUMENT. to_gemini_schema must produce only the supported
    OpenAPI subset: no $defs/$ref (inlined), Optional collapsed to
    nullable, pattern/minLength/title/additionalProperties gone, Literal
    mapped to enum."""
    from agent.gemini_client import to_gemini_schema
    from agent.state import RefundDecision

    schema = to_gemini_schema(RefundDecision)
    blob = json.dumps(schema)
    for banned in ("additionalProperties", "additional_properties", "$defs",
                   "$ref", "pattern", "minLength", "title"):
        assert banned not in blob
    assert schema["type"] == "object"
    assert "dispute_id" in schema["required"]
    assert "issue_refund" in schema["properties"]["proposed_action"]["enum"]
    # Optional[str] → nullable string, not anyOf
    assert schema["properties"]["transaction_id"]["type"] == "string"
    assert schema["properties"]["transaction_id"]["nullable"] is True
    # nested list of CitedEvidence: $ref inlined into items
    cited = schema["properties"]["cited_evidence"]
    assert cited["type"] == "array"
    assert "source" in cited["items"]["properties"]


def test_prompt_v6_bans_merchant_record_claims():
    """v6 (2026-11-10, live rejected_by_critic under the v4/v5 critic):
    flash-lite at default temperature re-phrased a packet fact as "we have
    no record of successful delivery" — a claim about merchant-internal
    records, which the critic (correctly) rejects. v6's drafter names the
    trap: missing evidence must be stated packet-relative only. Pairs with
    lower sampling temperatures in the graph and worker-level resampling
    of rejected_by_critic runs."""
    for name in ("draft_response", "critic", "decide_refund", "loop_system"):
        assert load_prompt(name, "v6")
    draft = load_prompt("draft_response", "v6")
    assert "We have no record of" in draft        # banned phrase listed
    assert "packet ONLY" in draft                  # packet-relative rule
    assert "We have no record of" not in load_prompt("draft_response", "v5")
    # v6 keeps the v5 schema placeholder in decide_refund
    assert "{{DECISION_SCHEMA}}" in load_prompt("decide_refund", "v6")
    assert "v6" in available_versions()


def test_refund_decision_accepts_real_paypal_dispute_ids():
    """Live schema_failed (2026-11-10): the gateway's DisputeId was relaxed
    for real Resolution-Center ids (61abff8), but the agent side still
    pinned ^PP-D-\\d{1,12}$ — the model echoed the correct id
    (PP-R-LUV-10198013) and our OWN schema rejected it. This test keeps
    both sides of the wire aligned."""
    import re

    from agent.state import DISPUTE_ID_PATTERN, RefundDecision
    from agent.testing import decision_json

    assert re.fullmatch(DISPUTE_ID_PATTERN, "PP-R-LUV-10198013")
    assert re.fullmatch(DISPUTE_ID_PATTERN, "PP-D-56244")     # demo ids too
    assert not re.fullmatch(DISPUTE_ID_PATTERN, "not-a-dispute")
    decision = RefundDecision.model_validate_json(
        decision_json(dispute_id="PP-R-LUV-10198013"),
        context={"min_confidence": 0.6})
    assert decision.dispute_id == "PP-R-LUV-10198013"


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
