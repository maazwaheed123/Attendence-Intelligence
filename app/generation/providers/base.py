"""Provider interface. Generation never decides access: providers only ever receive
context that retrieval has already filtered through RLS."""

from dataclasses import dataclass, field
from typing import Any, Protocol


class ProviderError(Exception):
    """A provider failed for this call; the router tries the next one."""

    kind = "error"


class ProviderTimeout(ProviderError):
    kind = "timeout"


class ProviderRateLimited(ProviderError):
    kind = "rate_limited"


class ProviderServerError(ProviderError):
    kind = "server_error"


class ProviderBadResponse(ProviderError):
    """Unparseable / schema-invalid output (after the repair retry)."""

    kind = "bad_response"


class ProviderNotConfigured(ProviderError):
    """Model missing, connection refused, provider not set up."""

    kind = "unavailable"


class ProviderUnavailable(Exception):
    """Every provider in the chain failed. Callers fall back (template engine / OCR)."""

    def __init__(self, attempts: list[dict], template_allowed: bool):
        super().__init__(
            "all providers failed: " + "; ".join(f"{a['provider']}={a['error']}" for a in attempts)
        )
        self.attempts = attempts
        self.template_allowed = template_allowed


@dataclass
class LLMResult:
    text: str
    provider: str
    model: str
    latency_ms: int
    parsed: Any = None  # validated pydantic object when a response_model was given
    fallback_path: str = ""
    attempts: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=dict)


class LLMProvider(Protocol):
    name: str
    model: str
    external: bool  # True = data leaves this machine (blocked unless tenant allows)
    supports_vision: bool

    def chat(
        self, messages: list[dict], *, json_mode: bool, timeout: float, max_tokens: int
    ) -> LLMResult: ...

    def ping(self, timeout: float = 2.0) -> dict: ...
