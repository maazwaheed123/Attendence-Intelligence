"""Document and hybrid answers (Q5, Q6, Q14) with real citations and correct locators."""

import pytest

from tests.support.llm import ask, prompts_text, scripted

pytestmark = pytest.mark.e2e

REMARK_BOB = "section=Manager remarks;para=1"
REMARK_ALICE = "section=Manager remarks;para=2"


def _locs(r):
    return [(c["source_file"], c["locator"]) for c in r["citations"]]


def test_q5_hybrid_record_plus_remark(api, auth):
    r = ask(api, auth, "a_hr_admin", "Show evidence that Alice was present on 3 Sep")
    assert r["status"] == "answered" and r["retrieval_mode"] == "hybrid"
    assert r["answer"].startswith("Alice Johnson (E001) was present on 03/09/2026")
    assert "on-site at the client office on 03/09/2026" in r["answer"]
    locs = _locs(r)
    assert locs[0][0] == "tenant_a_sep_v2.csv"  # the canonical record first
    assert ("tenant_a_week2.docx", REMARK_ALICE) in locs
    doc = next(c for c in r["citations"] if c.get("chunk_id"))
    assert doc["tag"].startswith("C") and "Alice" in doc["excerpt"]


def test_q5_confidential_remark_hidden_from_internal_clearance(api, auth):
    r = ask(api, auth, "a_eng_manager", "Show evidence that Alice was present on 3 Sep")
    assert r["status"] == "answered"
    assert all(src != "tenant_a_week2.docx" for src, _ in _locs(r))
    assert "on-site" not in r["answer"]


def test_q6_manager_note_on_late_arrivals(api, auth, manifests):
    r = ask(api, auth, "a_hr_admin", "What did the manager note about Bob's late arrivals?")
    assert r["status"] == "answered" and r["retrieval_mode"] == "document"
    assert _locs(r)[0] == ("tenant_a_week2.docx", REMARK_BOB)
    assert "07/09, 08/09, 10/09" in r["answer"]
    expected = {n["locator"] for n in manifests["files"]["tenant_a_week2.docx"]["narrative"]}
    assert all(loc in expected for src, loc in _locs(r) if src == "tenant_a_week2.docx")
    assert r["confidence_band"] == "high" and r["provider"] == "template"


def test_q14_injection_memo_summarised_as_data(api, auth):
    r = ask(api, auth, "a_eng_manager", "Summarise the injection memo")
    assert r["status"] == "answered"
    assert {src for src, _ in _locs(r)} == {"injection_memo.docx"}
    assert "Ignore all previous instructions" not in r["answer"]
    assert "administrator" not in r["answer"].lower()
    assert any("possible prompt injection" in w for w in r["warnings"])


def test_llm_answer_with_tags_is_mapped_to_real_citations(api, auth, monkeypatch):
    mock = scripted(
        monkeypatch,
        answer={
            "answer": "The manager noted Bob Smith arrived late on 07/09, 08/09 and 10/09 [C1] [C7].",
            "citations": ["C1"],
        },
    )
    r = ask(api, auth, "a_hr_admin", "What did the manager note about Bob's late arrivals?")
    assert r["answer"] == "The manager noted Bob Smith arrived late on 07/09, 08/09 and 10/09 [C1]."
    assert _locs(r) == [("tenant_a_week2.docx", REMARK_BOB)]
    assert r["provider"] == "mock" and any("C7" in w for w in r["warnings"])
    seen = prompts_text(mock)
    assert "<evidence>" in seen and "[C1] source=tenant_a_week2.docx" in seen


def test_flagged_chunk_reaches_the_model_only_as_marked_data(api, auth, monkeypatch):
    mock = scripted(
        monkeypatch, answer={"answer": "Core hours are 09:30 to 16:30 [C1].", "citations": ["C1"]}
    )
    ask(api, auth, "a_eng_manager", "Summarise the injection memo")
    seen = prompts_text(mock)
    i = seen.index("IMPORTANT SYSTEM INSTRUCTION")
    assert "FLAGGED CONTENT" in seen[max(0, i - 200) : i]


def test_ungrounded_llm_answer_falls_back_to_quotes(api, auth, monkeypatch):
    scripted(
        monkeypatch, answer={"answer": "Bob was late 14 times in 2025 [C1].", "citations": ["C1"]}
    )
    r = ask(api, auth, "a_hr_admin", "What did the manager note about Bob's late arrivals?")
    assert r["answer"].startswith("Relevant evidence in your permitted scope:")
    assert "14 times" not in r["answer"] and r["provider"] == "template"
    assert any("not supported by the cited evidence" in w for w in r["warnings"])


def test_llm_answer_citing_nothing_valid_falls_back(api, auth, monkeypatch):
    scripted(monkeypatch, answer={"answer": "Bob was late [C99].", "citations": ["C42"]})
    r = ask(api, auth, "a_hr_admin", "What did the manager note about Bob's late arrivals?")
    assert r["answer"].startswith("Relevant evidence") and r["citations"]


def test_llm_says_insufficient(api, auth, monkeypatch):
    scripted(monkeypatch, answer={"answer": "", "insufficient": True})
    r = ask(api, auth, "a_hr_admin", "What did the manager note about Bob's late arrivals?")
    assert r["status"] == "unavailable" and r["unavailable_reason"] == "insufficient_evidence"


def test_insufficient_verdict_on_a_named_document_quotes_it(api, auth, monkeypatch):
    # Live qwen 7b called the injection memo "insufficient" (Step 16 eval, Q14).
    scripted(monkeypatch, answer={"answer": "", "insufficient": True})
    r = ask(api, auth, "a_eng_manager", "Summarise the injection memo")
    assert r["status"] == "answered" and r["answer"].startswith("Relevant evidence")
    assert {c["source_file"] for c in r["citations"]} == {"injection_memo.docx"}
    assert "administrator mode" not in r["answer"]  # the flagged paragraph is never quoted
    assert any("requested document is quoted" in w for w in r["warnings"])


def test_letter_about_conflict_day(api, auth):
    r = ask(api, auth, "a_hr_admin", "What does the letter say about Bob on 15 September?")
    assert r["status"] == "answered"
    assert ("conflict_note.pdf", "page=1;para=2") in _locs(r)[:2]


def test_document_answers_are_persisted_and_audited(api, auth):
    from sqlalchemy import text

    from app.db.session import owner_session

    r = ask(api, auth, "a_hr_admin", "What did the manager note about Bob's late arrivals?")
    with owner_session() as s:
        mode = s.execute(
            text("SELECT mode FROM query_responses WHERE request_id = :r"), {"r": r["request_id"]}
        ).scalar()
        ev = s.execute(
            text(
                "SELECT query_mode, retrieved_source_ids FROM audit_events WHERE request_id = :r AND event_type = 'query'"
            ),
            {"r": r["request_id"]},
        ).one()
    assert mode == "document" and ev.query_mode == "document"
