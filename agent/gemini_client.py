"""Gemini API client: generate_content with tools, retries, and metering.

Design notes:
- One method, generate(), wraps google-genai's generate_content. It returns
  an LLMResult carrying: the text, any function calls the model wants made,
  the RAW content object (callers append it to the conversation verbatim so
  function responses can follow it), and token usage.
- Transient failures (429, 5xx, network) retry with EXPONENTIAL BACKOFF +
  JITTER: delay = min(cap, base * 2^attempt) * uniform(0.5, 1.5). The jitter
  matters when many runs fail at once (e.g. a rate-limit burst): identical
  delays would make every client retry in lockstep and re-trigger the limit.
- 4xx (except 429) are NOT retried — a bad request does not improve by
  waiting.
- Every successful call charges the RunMeter (when one is passed), and the
  budget is checked BEFORE the call so an exhausted run never hits the wire.
- google-genai is imported lazily inside __init__ so the rest of the agent
  package (and its tests, which use FakeLLM) never needs the SDK configured.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, Sequence

from agent.budget import LLMBudgetExceeded, RunMeter

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-3.8-flash"

# HTTP statuses worth retrying: rate limit + server-side transients.
_TRANSIENT_STATUS = {408, 425, 429, 500, 502, 503, 504}


class LLMError(Exception):
    """Non-transient LLM failure (bad request, auth, unsupported input)."""


@dataclass(frozen=True)
class FunctionCall:
    name: str
    args: dict


@dataclass(frozen=True)
class LLMResult:
    text: str
    function_calls: tuple[FunctionCall, ...]
    content: Any            # raw types.Content — append to the transcript as-is
    input_tokens: int
    output_tokens: int


# Gemini structured output accepts only an OpenAPI 3.0 subset. Keys outside
# it are dropped; passing the pydantic class to the SDK instead emits
# snake_case "additional_properties" (from extra="forbid"), which the proto
# rejects with 400 INVALID_ARGUMENT (field-verified 2026-11-10).
_GEMINI_KEEP = {"type", "format", "description", "enum", "items",
                "required", "properties", "nullable", "propertyOrdering"}


def _gemini_clean(node: Any, defs: dict) -> Any:
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        return _gemini_clean(defs[node["$ref"].rsplit("/", 1)[-1]], defs)
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key == "anyOf":
            # pydantic renders Optional[X] as anyOf=[X-schema, {"type":"null"}];
            # Gemini wants the sub-schema plus nullable=true.
            real = [s for s in value if s != {"type": "null"}]
            if len(real) == 1:
                sub = _gemini_clean(real[0], defs)
                if isinstance(sub, dict):
                    out.update(sub)
                    out["nullable"] = True
        elif key == "allOf" and len(value) == 1:
            out.update(_gemini_clean(value[0], defs))
        elif key == "properties":
            out["properties"] = {k: _gemini_clean(v, defs)
                                 for k, v in value.items()}
        elif key == "items":
            out["items"] = _gemini_clean(value, defs)
        elif key in _GEMINI_KEEP:
            out[key] = value
        # Everything else (title, default, pattern, minLength, $defs,
        # additionalProperties, ...) is silently dropped: unsupported or
        # actively rejected by the API.
    return out


def to_gemini_schema(model: Any) -> dict:
    """pydantic model -> OpenAPI dict Gemini structured output accepts."""
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})
    return _gemini_clean(schema, defs)


class LLMClient(Protocol):
    """What the loop agent and the graph nodes depend on. GeminiClient
    implements it for real; tests implement it with scripted responses."""

    def generate(
        self,
        contents: Sequence[Any],
        *,
        system: str | None = None,
        tools: Sequence[Any] | None = None,
        temperature: float | None = None,
        response_schema: Any | None = None,
    ) -> LLMResult: ...


class GeminiClient:
    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        *,
        meter: RunMeter | None = None,
        max_retries: int = 5,
        backoff_base: float = 0.5,
        backoff_cap: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
        rng: Callable[[], float] = random.random,
        client: Any | None = None,
    ):
        if not api_key and client is None:
            raise LLMError("GEMINI_API_KEY is not set")
        self.model = model
        self.meter = meter
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        # Injectable for tests: sleep (no real waiting) and rng (fixed jitter).
        self._sleep = sleep
        self._rng = rng
        if client is not None:
            self._client = client
        else:
            from google import genai  # lazy: tests never need the SDK
            from google.genai import types
            # The SDK default per-request timeout is 600 s: a provider that
            # HANGS (instead of 503ing) blocks the single-threaded worker for
            # up to an hour across the retry ladder — seen live 2026-11-10.
            # 90 s is ample for these payloads (AFC round-trips included) and
            # fails fast into the ladder instead.
            self._client = genai.Client(
                api_key=api_key,
                http_options=types.HttpOptions(timeout=90_000),  # milliseconds
            )

    # -- retry policy ---------------------------------------------------------

    def _is_transient(self, exc: Exception) -> bool:
        from google.genai import errors  # lazy import
        if isinstance(exc, errors.ServerError):
            return True
        if isinstance(exc, errors.ClientError):
            return getattr(exc, "code", None) in _TRANSIENT_STATUS
        # Network-level failures (DNS, connection reset, timeouts).
        return isinstance(exc, (ConnectionError, TimeoutError, OSError))

    def _backoff_delay(self, attempt: int) -> float:
        """Exponential with jitter: base * 2^attempt, capped, ±50% jitter."""
        exp = min(self.backoff_cap, self.backoff_base * (2 ** attempt))
        return exp * (0.5 + self._rng())  # rng() ∈ [0,1) → factor ∈ [0.5, 1.5)

    # -- the one public call ---------------------------------------------------

    def generate(
        self,
        contents: Sequence[Any],
        *,
        system: str | None = None,
        tools: Sequence[Any] | None = None,
        temperature: float | None = None,
        response_schema: Any | None = None,
    ) -> LLMResult:
        if self.meter is not None:
            self.meter.check_headroom()  # raises LLMBudgetExceeded before any I/O

        from google.genai import types  # lazy import

        config_kwargs: dict[str, Any] = {}
        if system is not None:
            config_kwargs["system_instruction"] = system
        if tools:
            config_kwargs["tools"] = list(tools)
        if temperature is not None:
            config_kwargs["temperature"] = temperature
        if response_schema is not None:
            # Constrained decoding: the API guarantees schema-shaped JSON.
            # Field names were a GUESS until 2026-11-10 — flash-lite guessed
            # {"decision": ...} instead of {"proposed_action": ...} and
            # schema_failed the first live run to reach decide_refund.
            config_kwargs["response_mime_type"] = "application/json"
            # Never hand the SDK the pydantic class: its converter emits
            # snake_case additional_properties → 400. Send the cleaned
            # OpenAPI-subset dict instead.
            config_kwargs["response_schema"] = to_gemini_schema(response_schema)
        config = types.GenerateContentConfig(**config_kwargs)

        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self._client.models.generate_content(
                    model=self.model, contents=list(contents), config=config)
                break
            except Exception as exc:  # noqa: BLE001 — classified below
                if not self._is_transient(exc) or attempt == self.max_retries:
                    raise LLMError(f"Gemini call failed: {exc}") from exc
                last_exc = exc
                delay = self._backoff_delay(attempt)
                logger.warning(
                    "Gemini transient failure (attempt %d/%d): %s — retrying in %.2fs",
                    attempt + 1, self.max_retries + 1, exc, delay)
                self._sleep(delay)
        else:  # pragma: no cover — the loop always breaks or raises
            raise LLMError(f"Gemini call failed after retries: {last_exc}")

        usage = response.usage_metadata
        input_tokens = getattr(usage, "prompt_token_count", 0) or 0
        output_tokens = (getattr(usage, "response_token_count", None)
                         or getattr(usage, "candidates_token_count", 0) or 0)

        raw_content = None
        if response.candidates:
            raw_content = response.candidates[0].content

        calls = tuple(
            FunctionCall(name=fc.name, args=dict(fc.args or {}))
            for fc in (response.function_calls or [])
        )
        # Accessing response.text when the model returned function_call parts
        # makes the SDK emit a warning; the text is empty-ish anyway. Only
        # read it for text responses.
        text = "" if calls else (response.text or "")
        if self.meter is not None:
            self.meter.charge(input_tokens, output_tokens)

        return LLMResult(
            text=text,
            function_calls=calls,
            content=raw_content,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
