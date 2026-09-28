"""The query cache can never serve one scope's answer to another scope."""

import pytest

from app import cache
from app.security.context import SecurityContext
from tests.support.cache import cache_state, same_except_request_id, template_only
from tests.support.llm import ask

pytestmark = [pytest.mark.security, pytest.mark.e2e]

Q1 = "Who was present on 1 September 2026?"


@pytest.fixture(autouse=True)
def _clean_chain(monkeypatch):
    template_only(monkeypatch)


def test_repeat_question_is_a_hit_with_new_request_id(api, auth):
    first = ask(api, auth, "a_eng_manager", Q1)
    second = ask(api, auth, "a_eng_manager", Q1)
    assert cache_state(first["request_id"]) == "miss"
    assert cache_state(second["request_id"]) == "hit"
    assert first["request_id"] != second["request_id"]
    assert same_except_request_id(first, second)
    # the hit is persisted under its own id and readable back
    stored = api.get(f"/v1/query/{second['request_id']}", headers=auth("a_eng_manager"))
    assert stored.status_code == 200 and stored.json()["request_id"] == second["request_id"]


def test_whitespace_and_case_normalized(api, auth):
    ask(api, auth, "a_eng_manager", Q1)
    r = ask(api, auth, "a_eng_manager", "  who WAS present on 1   September 2026? ")
    assert cache_state(r["request_id"]) == "hit"


@pytest.mark.parametrize(
    "other", ["b_manager", "a_hr_manager", "x_other_product", "a_hr_admin", "a_reviewer"]
)
def test_other_scope_never_hits(api, auth, other):
    a = ask(api, auth, "a_eng_manager", Q1)
    b = ask(api, auth, other, Q1)
    assert cache_state(b["request_id"]) == "miss"
    assert b["context"]["role"] == {"a_reviewer": "reviewer"}.get(other, b["context"]["role"])
    if other in ("b_manager", "a_hr_manager", "x_other_product"):
        assert {c["record_id"] for c in a["citations"]}.isdisjoint(
            {c["record_id"] for c in b["citations"]}
        )


def test_other_employee_never_hits(api, auth, token_for):
    mine = ask(api, auth, "a_employee_e001", "What is my attendance?")
    assert "E001" in mine["answer"]
    bob = {"Authorization": "Bearer " + token_for("a_employee_e001", sub="user:bob",
                                                  employee_id="E002")}  # fmt: skip
    r = api.post("/v1/query", json={"question": "What is my attendance?"}, headers=bob)
    assert r.status_code == 200, r.text
    assert cache_state(r.json()["request_id"]) == "miss"
    assert "E001" not in r.json()["answer"] and "E002" in r.json()["answer"]


def test_filters_are_part_of_the_key(api, auth):
    ask(api, auth, "a_hr_admin", Q1)
    r = ask(api, auth, "a_hr_admin", Q1, filters={"entity_id": "sales"})
    assert cache_state(r["request_id"]) == "miss"


def _ctx(**over) -> SecurityContext:
    base = dict(
        sub="u1",
        product_id="attendance_ai",
        tenant_id="tenant_a",
        module="attendance",
        role="manager",
        entities=("engineering",),
        clearance="internal",
        employee_id=None,
        token_id="t",
    )
    return SecurityContext(**{**base, **over})


@pytest.mark.parametrize(
    "change",
    [
        {"tenant_id": "tenant_b"},
        {"product_id": "hrms_ai"},
        {"entities": ("hr",)},
        {"entities": ("*",)},
        {"clearance": "confidential"},
        {"role": "hr_admin"},
        {"sub": "u2"},
    ],
)
def test_key_covers_every_scope_field(corpus_db, change):
    assert cache.key_for(_ctx(), Q1, {}) != cache.key_for(_ctx(**change), Q1, {})
    assert cache.key_for(_ctx(), Q1, {}) == cache.key_for(_ctx(), Q1, {})


def test_poisoned_entry_is_still_governed(api, auth, monkeypatch):
    """A hit is re-finalized in the caller's scope: a tampered entry cannot leak."""
    stored = {}
    real_put = cache.put
    monkeypatch.setattr(cache, "put", lambda k, e: (stored.update(e), real_put(k, e)))
    assert ask(api, auth, "a_eng_manager", Q1)["status"] == "answered"
    entry = {**stored, "response": {**stored["response"]}}
    entry["response"]["answer"] = "John Carter (E101) of tenant_b was present on 01/09/2026."
    monkeypatch.setattr(cache, "get", lambda k: entry)
    r = ask(api, auth, "a_eng_manager", Q1)
    assert cache_state(r["request_id"]) == "hit"
    assert "E101" not in r["answer"] and "John Carter" not in r["answer"]
    assert r["unavailable_reason"] == "blocked"


def test_key_differs_per_employee_self_scope(corpus_db):
    e1 = _ctx(role="employee", employee_id="E001")
    e2 = _ctx(role="employee", employee_id="E002")
    assert cache.key_for(e1, "What is my attendance?", {}) != cache.key_for(
        e2, "What is my attendance?", {}
    )
