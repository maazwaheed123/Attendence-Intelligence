"""Live-eval scoring and the TEST_SUMMARY builder (no server, no models)."""

import pytest

from scripts import eval_live, eval_report

pytestmark = pytest.mark.unit


def _body(**over):
    body = {
        "status": "answered",
        "retrieval_mode": "structured",
        "unavailable_reason": None,
        "answer": "4 employees: Alice Johnson (E001), Bob Smith (E002)",
        "citations": [{"source_file": "a.csv", "locator": "row=2", "excerpt": "x"}],
        "warnings": [],
        "applied_feedback": None,
    }
    return {**body, **over}


def test_checks_pass_and_fail():
    case = dict(status="answered", mode={"structured"}, contains=["E001"], absent=["E101"],
                cites={"a.csv"}, n_citations=1)  # fmt: skip
    assert all(c["ok"] for c in eval_live._checks(case, 200, _body(), {}))
    bad = eval_live._checks(case, 200, _body(answer="John Carter (E101)"), {})
    failed = {c["check"] for c in bad if not c["ok"]}
    assert failed == {"contains 'E001'", "absent 'E101'"}


def test_expected_http_status_and_identical_denials():
    assert eval_live._checks({"http": 403}, 403, {}, {})[0]["ok"]
    ref = {"Q9": {"response": _body(status="unavailable")}}
    same = eval_live._checks({"same_as": "Q9"}, 200, _body(status="unavailable"), ref)
    assert all(c["ok"] for c in same)
    diff = eval_live._checks({"same_as": "Q9"}, 200, _body(answer="other"), ref)
    assert not all(c["ok"] for c in diff)


def test_case_ids_unique_and_scenarios_known():
    ids = [c["id"] for c in eval_live.CASES]
    assert len(ids) == len(set(ids))
    names = {s[0] for s in eval_report.SCENARIOS}
    assert {c["scenario"] for c in eval_live.CASES} <= names
    referenced = {c for s in eval_report.SCENARIOS for c in s[3]}
    assert referenced <= set(ids)


def test_every_scenario_points_at_existing_tests():
    root = eval_report.ROOT / "tests"
    for name, _, files, _ in eval_report.SCENARIOS:
        for f in files:
            assert (root / f"{f}.py").exists(), (name, f)


def test_build_marks_failures():
    files = {f: {"passed": 3, "failed": 0} for s in eval_report.SCENARIOS for f in s[2]}
    files["e2e/test_unavailable"] = {"passed": 2, "failed": 1}
    ev = {"passed": 1, "total": 2, "generated_at": "t", "api_url": "http://api:8000",
          "results": [
              {"id": "Q1", "persona": "p", "question": "q", "passed": True, "latency_s": 50.0,
               "mode": "structured", "http_status": 200, "fallback_path": "ollama-primary",
               "confidence": 0.9, "checks": [], "note": None},
              {"id": "Q18", "persona": "p", "question": "q", "passed": False, "latency_s": 1.0,
               "mode": "structured", "http_status": 200, "fallback_path": "template",
               "confidence": 0.5, "checks": [{"check": "status", "ok": False,
                                              "detail": "unavailable"}], "note": "needs vision"},
          ]}  # fmt: skip
    md = eval_report.build(files, 96.2, "900 passed", {"unit": 1}, ev, [])
    rows = {ln.split(" | ")[1]: ln for ln in md.splitlines() if ln.startswith("| ") and " | " in ln}
    assert rows["Unavailable answer"].endswith("**FAIL** |")
    assert rows["OCR/handwriting handling"].endswith("**FAIL** |")
    assert rows["Idempotency"].endswith("**PASS** |")
    assert "96.2%" in md and "needs vision" in md and "median 26 s" in md
    assert "## Known gaps" in md and "**Q18**" in md
