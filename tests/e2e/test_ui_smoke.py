"""Streamlit UI smoke tests.

Pages run under streamlit.testing.v1.AppTest with a fake ApiClient (the UI's only
door to the service), so these check rendering and wiring. The real client is
then exercised against the real API (TestClient) to prove the wiring works.
"""

import ast
import sys
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parents[2] / "ui"
sys.path.insert(0, str(UI))

import client as ui_client  # noqa: E402
import jobs as ui_jobs  # noqa: E402

AppTest = pytest.importorskip("streamlit.testing.v1").AppTest

pytestmark = pytest.mark.e2e

PERSONAS = {
    "a_employee_e001": {"tenant": "tenant_a", "role": "employee"},
    "a_hr_admin": {"tenant": "tenant_a", "role": "hr_admin"},
    "a_reviewer": {"tenant": "tenant_a", "role": "reviewer"},
}
PERMS = {
    "a_hr_admin": ["export", "feedback_manage", "feedback_submit", "ingest", "query"],
    "a_reviewer": ["feedback_manage", "feedback_submit", "query"],
    "a_employee_e001": ["query"],
}
ANSWER = {
    "request_id": "req_ui_1",
    "status": "answered",
    "answer": "4 employees were present on 01/09/2026: Alice Johnson (E001) ...",
    "retrieval_mode": "structured",
    "context": {},
    "citations": [
        {
            "record_id": "r1",
            "source_file": "tenant_a_sep_v2.csv",
            "locator": "row=2",
            "excerpt": "2026-09-01 | E001 Alice Johnson | present",
        },  # fmt: skip
    ],
    "citation_total": 1,
    "confidence": 0.91,
    "confidence_band": "high",
    "confidence_explanation": "SQL validated; sources agree.",
    "unavailable_reason": None,
    "provider": "template",
    "model": "deterministic",
    "fallback_path": "template",
    "prompt_version": "p1.0",
    "retrieval_version": "r1.0-hybrid",
    "warnings": [],
    "applied_feedback": None,
}


class FakeClient:
    calls: list = []
    answer: dict = ANSWER
    fail_query: ui_client.ApiError | None = None
    job_status = "completed"

    def __init__(self, base_url=None, token=None):
        self.token = token

    def _log(self, *call):
        FakeClient.calls.append((self.token, *call))

    def personas(self):
        return PERSONAS

    def dev_token(self, persona):
        return f"tok-{persona}"

    def me(self):
        persona = self.token.removeprefix("tok-")
        role = PERSONAS[persona]["role"]
        return {
            "context": {
                "tenant_id": "tenant_a",
                "product_id": "attendance_ai",
                "role": role,
                "entity_scope": ["*"],
                "clearance": "internal",
            },  # fmt: skip
            "permissions": PERMS[persona],
            "visible_entities": ["engineering", "hr", "sales"],
        }

    def query(self, question, filters=None):
        self._log("query", question, filters)
        if FakeClient.fail_query:
            raise FakeClient.fail_query
        return {**FakeClient.answer}

    def ingest(self, filename, content, logical_name=None, entity_id=None):
        self._log("ingest", filename)
        return {"job_id": "j1", "status": "queued", "stage": "received"}

    def job(self, job_id):
        return {"job_id": job_id, "status": FakeClient.job_status, "stage": "completed",
                "counts": {"records": 12, "review_required": 1}, "warnings": [],
                "failures": [{"locator": "row=9", "reason": "unknown employee"}]}  # fmt: skip

    def jobs(self, status=None):
        return [{"job_id": "jf", "filename": "corrupt.xlsx", "stage": "parsing", "attempts": 3}]

    def retry(self, job_id):
        self._log("retry", job_id)
        return {"status": "queued"}

    def submit_feedback(self, body):
        self._log("feedback", body)
        return {"example_id": "ex123456789", "version": 1, "status": "active",
                "validation": {"ok": True}, "verification": {"matches_ideal": True}}  # fmt: skip

    def feedback(self, status=None):
        return [{"example_id": "ex123456789", "version": 1, "status": "active",
                 "intent_signature": "attendance_pct|entity", "question": "q",
                 "times_applied": 2, "reviewer_id": "user:a_reviewer",
                 "created_at": "2026-09-28"}]  # fmt: skip

    def deactivate(self, example_id):
        self._log("deactivate", example_id)
        return {"status": "inactive"}

    def rollback(self, example_id):
        self._log("rollback", example_id)
        return {"status": "active"}

    def export(self, body):
        self._log("export", body)
        return b"{}", {"content-type": "application/json", "x-export-checksum": "ab" * 32,
                       "x-export-record-count": "4",
                       "content-disposition": 'attachment; filename="x.json"'}  # fmt: skip

    def health_deep(self):
        return {"status": "degraded", "components": {
            "database": {"status": "ok"}, "cache": {"status": "down"},
            "llm_providers": [{"provider": "ollama-primary", "status": "ok", "circuit": "closed"}],
        }}  # fmt: skip


@pytest.fixture
def app(monkeypatch):
    FakeClient.calls = []
    FakeClient.answer, FakeClient.fail_query, FakeClient.job_status = ANSWER, None, "completed"
    monkeypatch.setattr(ui_client, "ApiClient", FakeClient)

    def make(persona="a_hr_admin", page="Ask"):
        at = AppTest.from_file(str(UI / "streamlit_app.py"), default_timeout=30)
        at.run()
        at.selectbox(key="persona").set_value(persona).run()
        at.radio(key="page").set_value(page).run()
        assert not at.exception, at.exception
        return at

    return make


def _text(at) -> str:
    parts = [e.value for kind in ("markdown", "caption", "success", "warning", "error", "info")
             for e in getattr(at, kind)]  # fmt: skip
    parts += [e.value for e in at.sidebar.markdown]
    return "\n".join(str(p) for p in parts)


def test_sidebar_shows_persona_context(app):
    at = app("a_hr_admin")
    text = _text(at)
    assert "tenant_a" in text and "hr_admin" in text and "internal" in text


def test_ask_renders_answer_citations_and_request_id(app):
    at = app("a_hr_admin")
    at.text_area(key="question").input("Who was present on 1 Sep?").run()
    at.button(key="ask").click().run()
    assert not at.exception
    assert "ANSWERED" in at.success[0].value
    assert at.dataframe[0].value["source_file"].tolist() == ["tenant_a_sep_v2.csv"]
    assert "req_ui_1" in _text(at)
    assert FakeClient.calls[-1] == ("tok-a_hr_admin", "query", "Who was present on 1 Sep?", {})


def test_filters_are_sent(app):
    at = app("a_hr_admin")
    at.text_area(key="question").input("Who was absent?").run()
    at.selectbox(key="ask_entity").set_value("sales").run()
    at.text_input(key="ask_emp").input("E008").run()
    at.button(key="ask").click().run()
    assert FakeClient.calls[-1][3] == {"entity_id": "sales", "employee_id": "E008"}


@pytest.mark.parametrize(("status", "kind"), [("unavailable", "error"),
                                              ("needs_review", "warning")])  # fmt: skip
def test_status_badges(app, status, kind):
    FakeClient.answer = {**ANSWER, "status": status, "citations": [], "citation_total": 0,
                         "unavailable_reason": "no_data_in_scope" if kind == "error" else None,
                         "warnings": ["1 record awaits review"]}  # fmt: skip
    at = app("a_hr_admin")
    at.text_area(key="question").input("q").run()
    at.button(key="ask").click().run()
    assert status.replace("_", " ").upper() in getattr(at, kind)[0].value
    assert "no_data_in_scope" in _text(at) or "awaits review" in _text(at)


def test_applied_feedback_badge(app):
    FakeClient.answer = {**ANSWER, "applied_feedback": {"example_id": "ex123456789",
                                                        "version": 2, "similarity": 0.97}}  # fmt: skip
    at = app("a_hr_admin")
    at.text_area(key="question").input("q").run()
    at.button(key="ask").click().run()
    assert "v2" in at.info[0].value


def test_api_error_is_shown(app):
    FakeClient.fail_query = ui_client.ApiError(403, "CONTEXT_MISMATCH", "entity_id is outside",
                                               "req_err")  # fmt: skip
    at = app("a_hr_admin")
    at.text_area(key="question").input("q").run()
    at.button(key="ask").click().run()
    assert "CONTEXT_MISMATCH" in at.error[0].value and "req_err" in at.error[0].value


def test_switching_persona_clears_last_answer(app):
    at = app("a_hr_admin")
    at.text_area(key="question").input("q").run()
    at.button(key="ask").click().run()
    assert at.session_state["last_answer"]
    at.selectbox(key="persona").set_value("a_employee_e001").run()
    assert "last_answer" not in at.session_state
    assert at.session_state["token"] == "tok-a_employee_e001"


def test_upload_hidden_for_employee(app):
    at = app("a_employee_e001", "Upload")
    assert "cannot ingest" in at.info[0].value


def test_upload_page_jobs_and_retry(app):
    at = app("a_hr_admin", "Upload")
    at.session_state["jobs"] = [{"filename": "a.csv", "job_id": "j1", "status": "queued"}]
    at.run()
    at.button(key="refresh").click().run()
    assert at.session_state["jobs"][0]["status"] == "completed"
    assert "a.csv" in at.expander[0].label and "completed" in at.expander[0].label
    at.button(key="retry_jf").click().run()
    assert ("tok-a_hr_admin", "retry", "jf") in FakeClient.calls
    assert "Retry queued" in at.success[0].value


def test_feedback_prefilled_and_submitted(app):
    at = app("a_reviewer", "Feedback")
    at.session_state["last_answer"] = ANSWER
    at.session_state["last_question"] = "Who was present on 1 Sep?"
    at.run()
    assert at.text_input(key="fb_rid").value == "req_ui_1"
    at.text_area(key="fb_text").input("State the date range.").run()
    next(b for b in at.button if b.label == "Submit feedback").click().run()
    body = next(c for c in FakeClient.calls if c[1] == "feedback")[2]
    assert body["original_request_id"] == "req_ui_1" and body["feedback"] == "State the date range."
    assert "active" in at.success[0].value
    at.button(key="fb_rollback").click().run()
    assert ("tok-a_reviewer", "rollback", "ex123456789") in FakeClient.calls


def test_feedback_prefill_follows_new_answers(app):
    at = app("a_reviewer", "Feedback")  # visited before any answer exists
    assert at.text_input(key="fb_rid").value == ""
    at.session_state["last_answer"] = ANSWER
    at.run()
    assert at.text_input(key="fb_rid").value == "req_ui_1"
    at.session_state["last_answer"] = {**ANSWER, "request_id": "req_ui_2"}
    at.run()
    assert at.text_input(key="fb_rid").value == "req_ui_2"


def test_feedback_hidden_for_employee(app):
    at = app("a_employee_e001", "Feedback")
    assert "reviewers" in at.info[0].value


def test_export_generates_download(app):
    at = app("a_hr_admin", "Export")
    at.radio(key="export_format").set_value("pdf").run()
    at.button(key="export").click().run()
    body = next(c for c in FakeClient.calls if c[1] == "export")[2]
    assert body["format"] == "pdf" and body["source"] == "records"
    assert "4 rows" in at.success[0].value


def test_export_hidden_for_reviewer(app):
    assert "Only HR admins" in app("a_reviewer", "Export").info[0].value


def test_health_tiles(app):
    at = app("a_hr_admin", "Health")
    assert "degraded" in at.warning[0].value
    labels = [m.label for m in at.metric]
    assert "database" in labels and "cache" in labels
    assert at.dataframe[0].value["provider"].tolist() == ["ollama-primary"]


# ------------------------------------------------------------------ helpers


class _File:
    def __init__(self, name, data=b"x"):
        self.name, self._data = name, data

    def getvalue(self):
        return self._data


class _JobClient:
    def __init__(self):
        self.polls = 0

    def ingest(self, name, content, logical, entity):
        if name == "bad.exe":
            raise ui_client.ApiError(415, "UNSUPPORTED_FILE", "not supported")
        return {"job_id": f"j-{name}", "status": "queued"}

    def job(self, job_id):
        self.polls += 1
        return {"job_id": job_id, "status": "completed" if self.polls > 2 else "running"}


def test_upload_files_reports_each_file():
    out = ui_jobs.upload_files(_JobClient(), [_File("a.csv"), _File("bad.exe")], "", None)
    assert out[0]["job_id"] == "j-a.csv"
    assert out[1]["status"] == "rejected" and out[1]["code"] == "UNSUPPORTED_FILE"


def test_refresh_waits_until_done(monkeypatch):
    monkeypatch.setattr(ui_jobs, "POLL_S", 0)
    c = _JobClient()
    jobs = ui_jobs.refresh_jobs(c, [{"filename": "a", "job_id": "j"}], wait=True)
    assert jobs[0]["status"] == "completed" and c.polls == 3


def test_ui_never_touches_backend_internals():
    for path in UI.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        mods |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        bad = {m for m in mods if m and m.split(".")[0] in ("app", "sqlalchemy", "redis",
                                                            "psycopg", "rq")}  # fmt: skip
        assert not bad, (path.name, bad)


# ------------------------------------------------------------------ real client vs real API


@pytest.fixture
def real(api, monkeypatch):
    def request(method, url, **kw):
        kw.pop("timeout", None)  # TestClient does not take one
        return api.request(method, url.removeprefix("http://api:8000"), **kw)

    monkeypatch.setattr(ui_client.httpx, "request", request)
    return ui_client.ApiClient("http://api:8000")


def test_real_client_end_to_end(real):
    assert "a_eng_manager" in real.personas()
    real.token = real.dev_token("a_eng_manager")
    me = real.me()
    assert me["context"]["entity_scope"] == ["engineering"] and "export" in me["permissions"]
    r = real.query("Who was present on 1 September 2026?")
    assert r["status"] == "answered" and len(r["citations"]) == 4
    content, headers = real.export({"source": "query", "request_id": r["request_id"],
                                    "format": "json"})  # fmt: skip
    assert headers["x-export-record-count"] == "4" and content.startswith(b"{")
    assert real.health_deep()["components"]["database"]["status"] == "ok"
    assert isinstance(real.jobs(), list)


def test_real_client_maps_errors(real):
    real.token = real.dev_token("a_eng_manager")
    with pytest.raises(ui_client.ApiError) as e:
        real.query("Who was present?", {"entity_id": "hr"})
    assert e.value.status == 403 and e.value.code == "CONTEXT_MISMATCH" and e.value.request_id
    real.token = real.dev_token("a_employee_e001")
    with pytest.raises(ui_client.ApiError) as e:
        real.export({"source": "records"})
    assert e.value.status == 403
    real.token = "garbage"
    with pytest.raises(ui_client.ApiError) as e:
        real.me()
    assert e.value.status == 401


def test_real_client_unreachable(monkeypatch):
    def boom(*a, **k):
        raise ui_client.httpx.ConnectError("no route")

    monkeypatch.setattr(ui_client.httpx, "request", boom)
    with pytest.raises(ui_client.ApiError) as e:
        ui_client.ApiClient("http://nowhere").me()
    assert e.value.code == "UNREACHABLE"
