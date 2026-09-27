"""/v1/health/deep and audit of provider calls/failures."""

import pytest
from sqlalchemy import text

from app.db.session import owner_session
from app.generation import router as router_mod
from app.generation.breaker import CircuitBreaker, MemoryBackend
from app.generation.providers.base import ProviderUnavailable
from app.generation.providers.mock import ChaosProvider, MockProvider
from app.generation.router import LLMRouter

pytestmark = pytest.mark.integration


def _events(rid):
    with owner_session() as s:
        return [
            dict(r)
            for r in s.execute(
                text("SELECT * FROM audit_events WHERE request_id = :r"), {"r": rid}
            ).mappings()
        ]


def test_health_deep_all_components(api):
    body = api.get("/v1/health/deep").json()
    c = body["components"]
    for k in ("service", "database", "search", "vector", "cache", "queue", "ocr"):
        assert c[k]["status"] == "ok", (k, c[k])
    assert c["database"]["pgvector"] and c["ocr"]["tesseract"].startswith("5")
    assert c["llm_providers"][0]["provider"] == "mock" and c["llm_fallback"]["enabled"] is True
    assert body["status"] == "ok"
    assert "password" not in str(body).lower() and "postgresql" not in str(body)


def test_health_deep_degraded_when_models_down(api, monkeypatch):
    down = LLMRouter(
        [ChaosProvider("unavailable", "ollama-primary")], CircuitBreaker(MemoryBackend())
    )
    monkeypatch.setattr(router_mod, "get_router", lambda: down)
    body = api.get("/v1/health/deep").json()
    assert body["status"] == "degraded"
    assert body["components"]["llm_providers"][0]["status"] == "down"
    assert body["components"]["llm_fallback"]["status"] == "ok"


def test_successful_fallback_is_audited(corpus_db):
    r = LLMRouter(
        [ChaosProvider("timeout", "ollama-primary"), MockProvider("ollama-fallback")],
        CircuitBreaker(MemoryBackend()),
    )
    r.complete(
        [{"role": "user", "content": "x"}],
        purpose="test",
        audit={"request_id": "llm-audit-1", "context": {"tenant_id": "tenant_a"}},
    )
    (ev,) = _events("llm-audit-1")
    assert ev["event_type"] == "llm_call" and ev["outcome"] == "ok"
    assert (
        ev["provider"] == "ollama-fallback"
        and ev["fallback_path"] == "ollama-primary>ollama-fallback"
    )
    assert ev["details"]["attempts"][0]["error"] == "timeout" and ev["tenant_id"] == "tenant_a"


def test_total_failure_is_audited(corpus_db):
    r = LLMRouter(
        [
            ChaosProvider("server_error", "ollama-primary"),
            ChaosProvider("unavailable", "ollama-fallback"),
        ],
        CircuitBreaker(MemoryBackend()),
    )
    with pytest.raises(ProviderUnavailable):
        r.complete(
            [{"role": "user", "content": "x"}], audit={"request_id": "llm-audit-2", "context": {}}
        )
    (ev,) = _events("llm-audit-2")
    assert ev["outcome"] == "all_providers_failed" and ev["error_code"] == "PROVIDER_UNAVAILABLE"
    assert ev["fallback_path"] == "ollama-primary>ollama-fallback>template"


def test_factory_builds_configured_chain(monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("LLM_CHAIN", "ollama-primary,ollama-fallback,template")
    get_settings.cache_clear()
    router_mod.reset_routers()
    try:
        r = router_mod.get_router()
        assert [(p.name, p.model) for p in r.providers] == [
            ("ollama-primary", "qwen2.5:7b-instruct"),
            ("ollama-fallback", "qwen2.5:3b-instruct"),
        ]
        assert r.template_fallback and not any(p.external for p in r.providers)
    finally:
        monkeypatch.setenv("LLM_CHAIN", "mock,template")
        get_settings.cache_clear()
        router_mod.reset_routers()
