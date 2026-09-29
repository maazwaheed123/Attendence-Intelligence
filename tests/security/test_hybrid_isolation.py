"""Document / hybrid retrieval never crosses tenant, entity, product, self or clearance
boundaries, and the model never receives out-of-scope evidence."""

import pytest

from scripts.datagen.truth import load_spec
from tests.support.llm import ask, prompts_text, scripted

pytestmark = [pytest.mark.security, pytest.mark.e2e]

TENANT_B = load_spec()["employees"]["tenant_b"]


def _files(r):
    return {c["source_file"] for c in r["citations"]}


QUESTIONS = [
    "What did the manager note about Bob's late arrivals?",
    "Show evidence that Alice was present on 3 Sep",
    "Summarise the injection memo",
    "What does the letter say about Bob on 15 September?",
    "Show me the notes and remarks for everyone in all tenants",
]


@pytest.mark.parametrize("persona", ["a_eng_manager", "a_employee_e001", "x_other_product"])
def test_model_never_sees_out_of_scope_evidence(api, auth, monkeypatch, persona):
    mock = scripted(monkeypatch, answer={"answer": "", "insufficient": True})
    for q in QUESTIONS:
        ask(api, auth, persona, q)
    seen = prompts_text(mock)
    for e in TENANT_B:
        assert e["name"] not in seen and e["id"] not in seen
    assert "tenant_b_sep.pdf" not in seen
    if persona != "a_hr_admin":
        assert "arrived late on 07/09" not in seen
    if persona == "a_employee_e001":
        assert "E002 Bob Smith" not in seen
    if persona == "x_other_product":
        assert "tenant_a_sep_v2.csv" not in seen


def test_tenant_b_documents_are_invisible_to_tenant_a(api, auth):
    r = ask(api, auth, "a_hr_admin", "Show me the notes and remarks for everyone in all tenants")
    assert "tenant_b_sep.pdf" not in _files(r)
    b = ask(api, auth, "b_manager", "Show me the notes and remarks for everyone in all tenants")
    assert not _files(b) & {"tenant_a_week2.docx", "injection_memo.docx", "conflict_note.pdf"}


def test_other_tenant_person_in_document_question_is_no_data(api, auth):
    john = ask(api, auth, "a_hr_admin", "What did the manager note about John Carter?")
    nobody = ask(api, auth, "a_hr_admin", "What did the manager note about Jane Doe?")
    strip = lambda r: {k: v for k, v in r.items() if k != "request_id"}  # noqa: E731
    assert john["status"] == "unavailable" and strip(john) == strip(nobody)


def test_employee_document_search_is_self_only(api, auth):
    r = ask(api, auth, "a_employee_e001", "Show evidence that Alice was present on 3 Sep")
    for c in r["citations"]:
        assert "E002" not in c["excerpt"] and "Bob" not in c["excerpt"]


def test_injected_instruction_does_not_change_scope(api, auth, monkeypatch):
    """Even if the model 'obeys' the memo, it can only cite what retrieval gave it."""
    scripted(
        monkeypatch,
        answer={
            "answer": "Administrator mode: all tenants' records follow [C1] [C2] [C3] [C4] [C5] [C6] [C7] [C8].",
            "citations": [],
        },
    )
    r = ask(api, auth, "a_eng_manager", "Summarise the injection memo")
    assert len(r["citations"]) <= 6
    assert "tenant_b_sep.pdf" not in _files(r)
