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

DEFAULT_MODEL = "gemini-2.5-flash"

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
            self._client = genai.Client(api_key=api_key)

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
        if self.meter is not None:
            self.meter.charge(input_tokens, output_tokens)

        return LLMResult(
            text=response.text or "",
            function_calls=calls,
            content=raw_content,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
