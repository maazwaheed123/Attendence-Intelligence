"""Cached answers are invalidated by new data and feedback changes, and only
clean answers are cached."""

import pytest

from app import cache
from app.security import ratelimit
from tests.support.cache import cache_state, template_only
from tests.support.feedback import Q2, clean_feedback, submit  # noqa: F401
from tests.support.ingest import upload
from tests.support.llm import all_down, ask

pytestmark = pytest.mark.integration

Q1 = "Who was present on 1 September 2026?"


def test_new_ingest_invalidates(ingest_api, auth, monkeypatch):
    template_only(monkeypatch)
    before = ask(ingest_api, auth, "a_eng_manager", Q1)
    assert before["unavailable_reason"] == "no_data_in_scope"
    assert cache_state(ask(ingest_api, auth, "a_eng_manager", Q1)["request_id"]) == "hit"
    r = upload(ingest_api, auth("a_hr_admin"), "tenant_a_sep_v2.csv")
    assert r.status_code in (200, 202), r.text
    after = ask(ingest_api, auth, "a_eng_manager", Q1)
    assert cache_state(after["request_id"]) == "miss"
    assert after["status"] == "answered" and "E001" in after["answer"]


@pytest.mark.usefixtures("clean_feedback")
def test_feedback_changes_invalidate(api, auth, monkeypatch):
    template_only(monkeypatch)
    original = ask(api, auth, "a_reviewer", Q2)
    assert original["applied_feedback"] is None
    assert cache_state(ask(api, auth, "a_reviewer", Q2)["request_id"]) == "hit"
    fb = submit(api, auth, "a_reviewer", original["request_id"]).json()
    assert fb["status"] == "active", fb
    applied = ask(api, auth, "a_eng_manager", Q2)
    assert applied["applied_feedback"]["example_id"] == fb["example_id"]
    again = ask(api, auth, "a_eng_manager", Q2)
    assert cache_state(again["request_id"]) == "hit"
    assert again["applied_feedback"]["example_id"] == fb["example_id"]
    r = api.post(f"/v1/feedback/{fb['example_id']}/deactivate", headers=auth("a_reviewer"))
    assert r.status_code == 200, r.text
    after = ask(api, auth, "a_eng_manager", Q2)
    assert cache_state(after["request_id"]) == "miss" and after["applied_feedback"] is None


def test_degraded_answers_are_not_cached(api, auth, monkeypatch):
    all_down(monkeypatch)
    first = ask(api, auth, "a_eng_manager", Q1)
    assert ">" in first["fallback_path"]
    second = ask(api, auth, "a_eng_manager", Q1)
    assert cache_state(second["request_id"]) == "miss"


def test_default_mock_chain_is_not_cached(api, auth):
    ask(api, auth, "a_eng_manager", Q1)
    assert cache_state(ask(api, auth, "a_eng_manager", Q1)["request_id"]) == "miss"


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ({"status": "answered", "fallback_path": "template", "unavailable_reason": None}, True),
        (
            {
                "status": "needs_review",
                "fallback_path": "ollama-primary",
                "unavailable_reason": None,
            },
            True,
        ),  # fmt: skip
        (
            {
                "status": "unavailable",
                "fallback_path": "template",
                "unavailable_reason": "no_data_in_scope",
            },
            True,
        ),  # fmt: skip
        (
            {"status": "unavailable", "fallback_path": "template", "unavailable_reason": "blocked"},
            False,
        ),  # fmt: skip
        (
            {
                "status": "unavailable",
                "fallback_path": "template",
                "unavailable_reason": "out_of_scope",
            },
            False,
        ),  # fmt: skip
        (
            {
                "status": "answered",
                "fallback_path": "ollama-primary>ollama-fallback",
                "unavailable_reason": None,
            },
            False,
        ),  # fmt: skip
    ],
)
def test_cacheable(response, expected):
    assert cache.cacheable(response) is expected


def test_entries_expire(api, auth, monkeypatch):
    template_only(monkeypatch)
    ask(api, auth, "a_eng_manager", Q1)
    r = ratelimit.get_redis()
    ttls = [r.ttl(k) for k in r.scan_iter(f"{cache.PREFIX}[0-9a-f]*")]
    assert ttls and all(0 < t <= 600 for t in ttls)


def test_ttl_zero_disables_cache(api, auth, monkeypatch):
    template_only(monkeypatch)
    monkeypatch.setattr(cache, "get_settings", lambda: type("S", (), {"cache_ttl_s": 0})())
    r = ask(api, auth, "a_eng_manager", Q1)
    assert cache_state(r["request_id"]) == "off"


def test_redis_down_fails_open(api, auth, monkeypatch):
    import redis

    template_only(monkeypatch)

    def boom():
        raise redis.ConnectionError("down")

    monkeypatch.setattr(cache, "get_redis", boom)
    r = ask(api, auth, "a_eng_manager", Q1)
    assert r["status"] == "answered" and cache_state(r["request_id"]) == "off"
    cache.bump_data_version("tenant_a", "attendance_ai")
