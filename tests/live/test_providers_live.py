"""LIVE: real local Ollama models (excluded from the gate). Run: pytest -m live"""

import pytest
from pydantic import BaseModel

from app.generation.breaker import CircuitBreaker, MemoryBackend
from app.generation.prompts import PING_SYSTEM, PING_USER
from app.generation.router import LLMRouter, build_provider

pytestmark = pytest.mark.live


class Ping(BaseModel):
    ok: bool


@pytest.mark.parametrize("name", ["ollama-primary", "ollama-fallback"])
def test_text_model_answers_json(name):
    p = build_provider(name)
    if not p.ping().get("model_available"):
        pytest.skip(f"{p.model} not available in Ollama")
    r = LLMRouter([p], CircuitBreaker(MemoryBackend()), timeout_s=180)
    res = r.complete(
        [{"role": "system", "content": PING_SYSTEM}, {"role": "user", "content": PING_USER}],
        response_model=Ping,
    )
    assert res.parsed.ok is True
    print(f"\n{name} ({p.model}) latency {res.latency_ms} ms")


def test_vision_model_available():
    p = build_provider("ollama-vision")
    if not p.ping().get("reachable"):
        pytest.skip("Ollama not reachable")
    assert p.ping()["model_available"], "run: ollama pull qwen2.5vl:3b"
