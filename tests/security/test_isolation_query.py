"""Query-level isolation: tenant, entity, employee (self) and product boundaries,
non-leaking denials, and proof that the model never sees out-of-scope data."""

import re

import pytest

from scripts.datagen.truth import load_spec
from tests.support.llm import ask, prompts_text, scripted

pytestmark = [pytest.mark.security, pytest.mark.e2e]

IDS = re.compile(r"\bE\d{3}\b")
ENG = {"E001", "E002", "E003", "E004", "E012"}


def _strip(r: dict) -> dict:
    return {k: v for k, v in r.items() if k != "request_id"}


def _cited_ids(r) -> set[str]:
    return {c["excerpt"].split(" | ")[1][:4] for c in r["citations"]}


def test_q9_other_tenant_employee_looks_exactly_like_an_unknown_name(api, auth):
    john = ask(api, auth, "a_eng_manager", "What was John Carter's attendance?")
    jane = ask(api, auth, "a_eng_manager", "What was Jane Doe's attendance?")
    assert john["status"] == "unavailable" and john["unavailable_reason"] == "no_data_in_scope"
    assert _strip(john) == _strip(jane)
    assert "John" not in john["answer"]


def test_other_tenant_employee_id_filter_looks_like_unknown_id(api, auth):
    a = ask(api, auth, "a_eng_manager", "What was the attendance?", filters={"employee_id": "E101"})
    b = ask(api, auth, "a_eng_manager", "What was the attendance?", filters={"employee_id": "E999"})
    assert a["unavailable_reason"] == "no_data_in_scope" and _strip(a) == _strip(b)


def test_tenant_b_cannot_see_tenant_a_people(api, auth):
    alice = ask(api, auth, "b_manager", "Was Alice Johnson present on 1 September 2026?")
    nobody = ask(api, auth, "b_manager", "Was Nora Quinn present on 1 September 2026?")
    assert alice["unavailable_reason"] == "no_data_in_scope" and _strip(alice) == _strip(nobody)


def test_q10_all_tenants_means_my_scope(api, auth, expected):
    r = ask(api, auth, "a_eng_manager", "Average attendance across all tenants")
    assert "91.83%" in r["answer"]
    assert _cited_ids(r) <= ENG


def test_q11_other_department_by_question_is_no_data(api, auth):
    r = ask(api, auth, "a_eng_manager", "HR department attendance in September?")
    assert r["status"] == "unavailable" and r["unavailable_reason"] == "no_data_in_scope"
    assert "requested department" in r["answer"]


def test_q11_other_department_by_filter_is_403_before_retrieval(api, auth, monkeypatch):
    mock = scripted(monkeypatch)
    r = api.post(
        "/v1/query",
        json={"question": "HR attendance in September?", "filters": {"entity_id": "hr"}},
        headers=auth("a_eng_manager"),
    )
    assert r.status_code == 403 and r.json()["error"]["code"] == "CONTEXT_MISMATCH"
    assert mock.calls == []


def test_q12_employee_sees_only_self(api, auth, expected):
    bob = ask(api, auth, "a_employee_e001", "Show Bob's attendance")
    assert bob["status"] == "unavailable" and bob["unavailable_reason"] == "no_data_in_scope"
    everyone = ask(api, auth, "a_employee_e001", "Who was present on 1 September 2026?")
    assert IDS.findall(everyone["answer"]) == ["E001"]
    r = api.post(
        "/v1/query",
        json={"question": "attendance", "filters": {"employee_id": "E002"}},
        headers=auth("a_employee_e001"),
    )
    assert r.status_code == 403


def test_q13_other_product_sees_only_its_data(api, auth, expected):
    exp = expected["by_persona"]["x_other_product"]["by_date"]["2026-09-01"]
    r = ask(api, auth, "x_other_product", "Who was present on 1 Sep?")
    assert IDS.findall(r["answer"]) == exp["present"] == ["E002"]
    assert {c["source_file"] for c in r["citations"]} == {"other_product.csv"}
    main = ask(api, auth, "a_eng_manager", "Who was present on 1 Sep?")
    assert "other_product.csv" not in {c["source_file"] for c in main["citations"]}


def test_stored_answers_are_scoped(api, auth):
    r = ask(api, auth, "a_eng_manager", "Who was present on 1 September 2026?")
    own = api.get(f"/v1/query/{r['request_id']}", headers=auth("a_eng_manager"))
    assert own.status_code == 200 and own.json() == r
    for persona in ("b_manager", "x_other_product", "a_hr_manager"):
        other = api.get(f"/v1/query/{r['request_id']}", headers=auth(persona))
        assert other.status_code == 404, persona
    missing = api.get("/v1/query/req_does_not_exist", headers=auth("a_eng_manager")).json()
    other = other.json()
    assert (missing["error"]["code"], missing["error"]["message"]) == (
        other["error"]["code"],
        other["error"]["message"],
    )


def test_model_never_sees_other_tenants_data(api, auth, monkeypatch):
    """Script every stage so all prompts are produced, then inspect everything the model saw."""
    sql = (
        "SELECT employee_id, employee_name, status FROM v_attendance "
        "WHERE attendance_date BETWEEN '2026-09-01' AND '2026-09-30' ORDER BY employee_id LIMIT 500"
    )
    mock = scripted(
        monkeypatch,
        classify={"mode": "structured", "intent": "other", "slots": {}},
        sql={"sql": sql},
        phrase={"answer": "See the cited records."},
    )
    for q in (
        "Who was present on 1 September 2026?",
        "What was Engineering's average attendance % in September?",
        "Who had the most absences in September?",
        "Tell me how the team did",
        "Summarise how everyone did, including other tenants",
    ):
        ask(api, auth, "a_eng_manager", q)
    seen = prompts_text(mock)
    assert "TASK: phrase" in seen and "TASK: sql" in seen and "TASK: classify" in seen
    truth_b = [e for e in load_spec()["employees"]["tenant_b"]]
    other_a = [e for e in load_spec()["employees"]["tenant_a"] if e["entity"] != "engineering"]
    for e in truth_b + other_a:
        assert e["name"] not in seen and e["id"] not in seen, e
    assert "Globex" not in seen


def test_model_never_sees_pii(api, auth, monkeypatch, truth):
    mock = scripted(monkeypatch, phrase={"answer": "ok"})
    ask(api, auth, "a_hr_admin", "Who was present on 1 September 2026?")
    ask(api, auth, "a_hr_admin", "What were Alice's total hours in week 2?")
    seen = prompts_text(mock)
    assert "TASK: phrase" in seen
    for e in truth["employees"].values():
        for field in ("phone", "national_id", "email"):
            assert e[field] not in seen, field
