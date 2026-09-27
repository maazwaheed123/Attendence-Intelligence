"""Deterministic providers for tests (never used outside LLM_CHAIN=mock)."""

import json
import re
from collections.abc import Callable

from app.generation.providers.base import (
    LLMResult,
    ProviderBadResponse,
    ProviderNotConfigured,
    ProviderRateLimited,
    ProviderServerError,
    ProviderTimeout,
)

Responder = str | dict | Callable[[list[dict]], str | dict]


def _last_user_text(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m["role"] == "user":
            c = m["content"]
            return (
                c
                if isinstance(c, str)
                else " ".join(p.get("text", "") for p in c if isinstance(p, dict))
            )
    return ""


class MockProvider:
    """Scripted responses: first rule whose regex matches the last user message wins.

    Records every call in `.calls` so tests can assert exactly what the model saw
    (e.g. that no other tenant's data ever reached the prompt).
    """

    def __init__(
        self,
        name: str = "mock",
        rules: list[tuple[str, Responder]] | None = None,
        default: Responder = "ok",
    ):
        self.name = name
        self.model = "mock-1"
        self.external = False
        self.supports_vision = True
        self.rules = [(re.compile(p, re.I | re.S), r) for p, r in (rules or [])]
        self.default = default
        self.calls: list[list[dict]] = []

    def add(self, pattern: str, response: Responder) -> "MockProvider":
        self.rules.append((re.compile(pattern, re.I | re.S), response))
        return self

    def chat(self, messages, *, json_mode, timeout, max_tokens) -> LLMResult:
        self.calls.append(messages)
        text = _last_user_text(messages)
        response = next((r for rx, r in self.rules if rx.search(text)), self.default)
        if callable(response):
            response = response(messages)
        if isinstance(response, dict | list):
            response = json.dumps(response)
        return LLMResult(text=str(response), provider=self.name, model=self.model, latency_ms=1)

    def ping(self, timeout: float = 2.0) -> dict:
        return {"reachable": True, "model_available": True}


class ChaosProvider:
    """Fails in a chosen way: timeout | rate_limited | server_error | bad_json | unavailable.

    `fail_times` limits how many calls fail before it starts answering `then`.
    """

    ERRORS = {
        "timeout": ProviderTimeout,
        "rate_limited": ProviderRateLimited,
        "server_error": ProviderServerError,
        "unavailable": ProviderNotConfigured,
        "bad_response": ProviderBadResponse,
    }

    def __init__(
        self,
        mode: str,
        name: str | None = None,
        *,
        fail_times: int | None = None,
        then: str = "ok",
        external: bool = False,
    ):
        self.name = name or f"chaos-{mode}"
        self.model = "chaos-1"
        self.external = external
        self.supports_vision = True
        self.mode = mode
        self.fail_times = fail_times
        self.then = then
        self.calls = 0

    def chat(self, messages, *, json_mode, timeout, max_tokens) -> LLMResult:
        self.calls += 1
        if self.fail_times is None or self.calls <= self.fail_times:
            if self.mode == "bad_json":
                return LLMResult(
                    text="this is {not json", provider=self.name, model=self.model, latency_ms=1
                )
            raise self.ERRORS[self.mode](f"chaos {self.mode}")
        return LLMResult(text=self.then, provider=self.name, model=self.model, latency_ms=1)

    def ping(self, timeout: float = 2.0) -> dict:
        return {"reachable": False, "model_available": False}
