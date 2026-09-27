"""Swap the vision model for a recorded output (or a failing provider) in tests."""

import json
from pathlib import Path

from app.generation.breaker import CircuitBreaker, MemoryBackend
from app.generation.providers.mock import ChaosProvider, MockProvider
from app.generation.router import LLMRouter
from app.ingestion.ocr import vision_engine

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "vision"


def use_recorded_vision(monkeypatch, fixture: str) -> MockProvider:
    data = json.loads((FIXTURES / f"{fixture}.json").read_text(encoding="utf-8"))
    provider = MockProvider("ollama-vision", default=data)
    router = LLMRouter([provider], CircuitBreaker(MemoryBackend()), template_fallback=False)
    monkeypatch.setattr(vision_engine, "get_vision_router", lambda: router)
    return provider


def vision_down(monkeypatch) -> None:
    router = LLMRouter(
        [ChaosProvider("unavailable", "ollama-vision")],
        CircuitBreaker(MemoryBackend()),
        template_fallback=False,
    )
    monkeypatch.setattr(vision_engine, "get_vision_router", lambda: router)
