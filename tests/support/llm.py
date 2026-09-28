"""Script the query pipeline's model router in tests.

Every prompt the pipeline sends starts with "TASK: classify|sql|phrase", so a
MockProvider can answer each stage separately. The provider records every call,
which lets tests assert exactly what the model saw.
"""

from app import orchestrator
from app.generation.breaker import CircuitBreaker, MemoryBackend
from app.generation.providers.mock import ChaosProvider, MockProvider
from app.generation.router import LLMRouter


def use_router(monkeypatch, providers, *, template: bool = True) -> LLMRouter:
    router = LLMRouter(providers, CircuitBreaker(MemoryBackend()), template_fallback=template)
    monkeypatch.setattr(orchestrator, "get_router", lambda: router)
    return router


def scripted(monkeypatch, *, classify=None, sql=None, phrase=None, default="ok") -> MockProvider:
    """A mock model. Stages without a script answer `default` (invalid JSON -> fails)."""
    rules = []
    for task, response in (("classify", classify), ("sql", sql), ("phrase", phrase)):
        if response is not None:
            rules.append((rf"^TASK: {task}\b", response))
    provider = MockProvider("mock", rules=rules, default=default)
    use_router(monkeypatch, [provider])
    return provider


def all_down(monkeypatch) -> list[ChaosProvider]:
    """Primary times out, fallback is unavailable -> the template engine answers."""
    providers = [
        ChaosProvider("timeout", "ollama-primary"),
        ChaosProvider("unavailable", "ollama-fallback"),
    ]
    use_router(monkeypatch, providers)
    return providers


def prompts_text(provider: MockProvider) -> str:
    """Everything the model received, as one string."""
    parts = []
    for messages in provider.calls:
        for m in messages:
            c = m["content"]
            parts.append(c if isinstance(c, str) else str(c))
    return "\n".join(parts)


def ask(api, auth, persona: str, question: str, **body) -> dict:
    r = api.post("/v1/query", json={"question": question, **body}, headers=auth(persona))
    assert r.status_code == 200, r.text
    return r.json()
