"""Router: ordering, fallback, circuit breaker, external-provider policy, JSON repair."""

import pytest
from pydantic import BaseModel

from app.generation.breaker import CircuitBreaker, MemoryBackend
from app.generation.providers.base import ProviderUnavailable
from app.generation.providers.mock import ChaosProvider, MockProvider
from app.generation.router import LLMRouter, extract_json

pytestmark = pytest.mark.unit
MSG = [{"role": "user", "content": "hello"}]


class Answer(BaseModel):
    ok: bool


def router(*providers, template=True, fails=3, reset_s=60):
    return LLMRouter(
        list(providers),
        CircuitBreaker(MemoryBackend(), fails=fails, reset_s=reset_s),
        template_fallback=template,
    )


def test_first_healthy_provider_used():
    a, b = MockProvider("a", default="from-a"), MockProvider("b", default="from-b")
    r = router(a, b).complete(MSG)
    assert (r.text, r.provider, r.fallback_path) == ("from-a", "a", "a")
    assert len(b.calls) == 0


@pytest.mark.parametrize("mode", ["timeout", "rate_limited", "server_error", "unavailable"])
def test_falls_through_on_failure(mode):
    r = router(ChaosProvider(mode, "primary"), MockProvider("fallback", default="ok")).complete(MSG)
    assert r.provider == "fallback"
    assert r.fallback_path == "primary>fallback"
    assert r.attempts == [
        {
            "provider": "primary",
            "error": mode.replace("unavailable", "unavailable"),
            "detail": f"chaos {mode}",
        }
    ]


def test_all_fail_raises_with_attempts_and_template_flag():
    with pytest.raises(ProviderUnavailable) as exc:
        router(ChaosProvider("timeout", "p1"), ChaosProvider("server_error", "p2")).complete(MSG)
    assert [a["provider"] for a in exc.value.attempts] == ["p1", "p2"]
    assert exc.value.template_allowed is True
    with pytest.raises(ProviderUnavailable) as exc:
        router(ChaosProvider("timeout", "p1"), template=False).complete(MSG)
    assert exc.value.template_allowed is False


def test_circuit_opens_after_threshold_and_skips_provider():
    bad = ChaosProvider("timeout", "primary")
    good = MockProvider("fallback")
    r = router(bad, good, fails=3)
    for _ in range(3):
        r.complete(MSG)
    assert bad.calls == 3 and r.breaker.state("primary") == "open"
    res = r.complete(MSG)
    assert bad.calls == 3  # not called while open
    assert res.attempts[0] == {"provider": "primary", "error": "circuit_open"}


def test_half_open_probe_closes_on_success(monkeypatch):
    import app.generation.breaker as breaker_mod

    clock = {"t": 1000.0}
    monkeypatch.setattr(breaker_mod.time, "time", lambda: clock["t"])
    flaky = ChaosProvider("timeout", "primary", fail_times=1, then="recovered")
    r = router(flaky, MockProvider("fallback"), fails=1, reset_s=60)
    r.complete(MSG)  # fails -> open
    assert r.breaker.state("primary") == "open"
    clock["t"] += 61  # cool-down elapsed
    assert r.breaker.state("primary") == "half_open"
    res = r.complete(MSG)  # single probe allowed, succeeds
    assert res.provider == "primary" and res.text == "recovered"
    assert r.breaker.state("primary") == "closed"


def test_external_providers_skipped_unless_allowed():
    cloud = MockProvider("cloud", default="cloud")
    cloud.external = True
    local = MockProvider("local", default="local")
    r = router(cloud, local)
    res = r.complete(MSG)
    assert res.provider == "local" and cloud.calls == []
    assert res.attempts == [{"provider": "cloud", "error": "skipped_external"}]
    assert r.complete(MSG, allow_external=True).provider == "cloud"


def test_structured_output_validated():
    res = router(MockProvider(default={"ok": True})).complete(MSG, response_model=Answer)
    assert res.parsed == Answer(ok=True)


def test_json_repair_round():
    replies = iter(["Sure! Here you go: not json", '{"ok": true}'])
    p = MockProvider(default=lambda m: next(replies))
    res = router(p).complete(MSG, response_model=Answer)
    assert res.parsed.ok is True and len(p.calls) == 2
    assert "not valid JSON" in p.calls[1][-1]["content"]


def test_invalid_json_twice_falls_back():
    res = router(
        ChaosProvider("bad_json", "primary"), MockProvider("fallback", default={"ok": False})
    ).complete(MSG, response_model=Answer)
    assert res.provider == "fallback" and res.parsed.ok is False
    assert res.attempts[0]["error"] == "bad_response"


def test_schema_violation_is_bad_response():
    res = router(
        MockProvider("primary", default={"ok": "maybe", "x": 1}),
        MockProvider("fallback", default={"ok": True}),
    ).complete(MSG, response_model=Answer)
    assert res.provider == "fallback"


@pytest.mark.parametrize(
    "text",
    ['{"ok": true}', '```json\n{"ok": true}\n```', 'Here: {"ok": true} thanks'],
)
def test_extract_json_tolerates_wrapping(text):
    assert extract_json(text) == {"ok": True}


def test_breaker_falls_back_to_memory_when_redis_down():
    import redis

    class Down:
        def __getattr__(self, name):
            def boom(*a, **k):
                raise redis.ConnectionError("down")

            return boom

    b = CircuitBreaker(Down(), fails=1)
    b.failure("x")
    assert b.state("x") == "open" and not b.allow("x")
