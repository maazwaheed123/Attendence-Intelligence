"""Attendance Intelligence console (Streamlit).

Talks ONLY to the public API through client.ApiClient with the selected
persona's bearer token, so every page sees exactly what that caller may see:
the UI adds no access logic of its own (it only hides buttons a role cannot
use; the API still enforces everything).

Pages: Ask, Upload, Feedback, Export, Health.
"""

import datetime as dt
import os
import sys

import pandas as pd
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import client as api_client  # noqa: E402
from jobs import refresh_jobs, upload_files  # noqa: E402

PAGES = ("Ask", "Upload", "Feedback", "Export", "Health")
STATUS_BADGE = {"answered": "success", "needs_review": "warning", "unavailable": "error"}
BAND_ICON = {"high": "🟢", "medium": "🟡", "low": "🔴"}

ss = st.session_state


def api() -> "api_client.ApiClient":
    return api_client.ApiClient(token=ss.get("token"))


def show_error(exc: Exception) -> None:
    if isinstance(exc, api_client.ApiError):
        rid = f" (request {exc.request_id})" if exc.request_id else ""
        st.error(f"{exc.status} {exc.code}: {exc.message}{rid}")
    else:
        st.error(f"Unexpected error: {exc}")


def can(permission: str) -> bool:
    return permission in (ss.get("me") or {}).get("permissions", [])


def sidebar() -> str:
    st.sidebar.title("Attendance Intelligence")
    try:
        personas = ss.get("personas") or api().personas()
        ss["personas"] = personas
    except Exception as exc:  # noqa: BLE001 - shown to the user
        st.sidebar.error("Personas unavailable (dev tokens are disabled outside dev/test).")
        show_error(exc)
        return st.sidebar.radio("Page", PAGES, key="page")
    names = sorted(personas)
    default = names.index("a_hr_admin") if "a_hr_admin" in names else 0
    persona = st.sidebar.selectbox("Persona", names, index=default, key="persona")
    if persona != ss.get("token_persona"):
        try:
            ss["token"] = api_client.ApiClient().dev_token(persona)
            ss["token_persona"] = persona
            ss["me"] = api().me()
            ss.pop("last_answer", None)
            ss.pop("export_file", None)
        except Exception as exc:  # noqa: BLE001
            show_error(exc)
    me = ss.get("me") or {}
    ctx = me.get("context", {})
    if ctx:
        st.sidebar.markdown(
            f"**Tenant:** {ctx.get('tenant_id')}  \n**Product:** {ctx.get('product_id')}  \n"
            f"**Role:** {ctx.get('role')}  \n**Entities:** {', '.join(ctx.get('entity_scope', []))}"
            f"  \n**Clearance:** {ctx.get('clearance')}"
            + (f"  \n**Employee:** {ctx['employee_id']}" if ctx.get("employee_id") else "")
        )
        st.sidebar.caption("Permissions: " + ", ".join(me.get("permissions", [])))
    return st.sidebar.radio("Page", PAGES, key="page")


def filters_form(prefix: str, with_status: bool = False) -> dict:
    out: dict = {}
    with st.expander("Filters (optional)"):
        c1, c2, c3, c4 = st.columns(4)
        if c1.checkbox("From date", key=f"{prefix}_use_from"):
            out["date_from"] = c1.date_input(
                "date_from", dt.date(2026, 9, 1), key=f"{prefix}_from"
            ).isoformat()
        if c2.checkbox("To date", key=f"{prefix}_use_to"):
            out["date_to"] = c2.date_input(
                "date_to", dt.date(2026, 9, 30), key=f"{prefix}_to"
            ).isoformat()
        entities = ["(any)", *(ss.get("me") or {}).get("visible_entities", [])]
        entity = c3.selectbox("Entity", entities, key=f"{prefix}_entity")
        if entity != "(any)":
            out["entity_id"] = entity
        emp = c4.text_input("Employee id", key=f"{prefix}_emp").strip()
        if emp:
            out["employee_id"] = emp
        if with_status:
            status = st.selectbox(
                "Status",
                [
                    "(any)",
                    "present",
                    "absent",
                    "leave",
                    "holiday",
                    "wfh",
                    "half_day",
                    "unknown",
                    "conflict",
                ],  # fmt: skip
                key=f"{prefix}_status",
            )
            if status != "(any)":
                out["status"] = status
    return out


def render_answer(r: dict) -> None:
    badge = getattr(st, STATUS_BADGE.get(r["status"], "info"))
    badge(f"**{r['status'].replace('_', ' ').upper()}** — {r['answer']}")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Confidence", f"{BAND_ICON.get(r['confidence_band'], '')} {r['confidence']:.2f}",
              r["confidence_band"])  # fmt: skip
    c2.metric("Retrieval mode", r["retrieval_mode"])
    c3.metric("Provider", r.get("provider") or "-", r.get("model") or None, delta_color="off")
    c4.metric("Citations", r["citation_total"])
    st.caption(r["confidence_explanation"])
    if r.get("unavailable_reason"):
        st.caption(f"Unavailable reason: `{r['unavailable_reason']}`")
    for w in r.get("warnings") or []:
        st.warning(w)
    if r.get("applied_feedback"):
        af = r["applied_feedback"]
        st.info(
            f"Reviewer-approved answer style applied (example {af['example_id'][:8]}…, "
            f"v{af['version']}, similarity {af['similarity']:.2f})"
        )
    if r["citations"]:
        rows = [
            {
                "tag": c.get("tag") or "",
                "source_file": c["source_file"],
                "locator": c["locator"],
                "excerpt": c["excerpt"],
            }  # fmt: skip
            for c in r["citations"]
        ]
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
        if r["citation_total"] > len(r["citations"]):
            st.caption(f"Showing {len(r['citations'])} of {r['citation_total']} sources.")
    st.caption(
        f"request `{r['request_id']}` · fallback `{r['fallback_path']}` · prompt "
        f"{r['prompt_version']} · retrieval {r['retrieval_version']}"
    )


def page_ask() -> None:
    st.header("Ask a question")
    st.caption("Answers come only from data your persona may see, with sources and confidence.")
    question = st.text_area("Question", key="question", placeholder="Who was present on 1 Sep?")
    filters = filters_form("ask")
    if st.button("Ask", key="ask", type="primary", disabled=not question.strip()):
        with st.spinner("Thinking… local CPU models can take a minute or two."):
            try:
                ss["last_answer"] = api().query(question.strip(), filters)
                ss["last_question"] = question.strip()
            except Exception as exc:  # noqa: BLE001
                show_error(exc)
    if ss.get("last_answer"):
        render_answer(ss["last_answer"])


def render_job(j: dict) -> None:
    status = j.get("status", "?")
    icon = {"completed": "✅", "duplicate": "♻️", "failed": "❌", "rejected": "⛔"}.get(status, "⏳")
    with st.expander(f"{icon} {j['filename']} — {status} ({j.get('stage') or '-'})",
                     expanded=status in ("failed", "rejected")):  # fmt: skip
        if j.get("error"):
            st.error(f"{j.get('code')}: {j['error']}")
        if j.get("version"):
            st.caption(f"logical name `{j.get('logical_name')}` · version {j['version']}"
                       + (" (supersedes an earlier version)" if j.get("supersedes_document_id")
                          else ""))  # fmt: skip
        counts = j.get("counts") or {}
        if counts:
            cols = st.columns(min(len(counts), 6))
            for col, (k, v) in zip(cols, list(counts.items())[:6], strict=False):
                col.metric(k.replace("_", " "), v)
        for w in j.get("warnings") or []:
            st.warning(w)
        if j.get("failures"):
            st.markdown("**Row failures / review items**")
            st.dataframe(pd.DataFrame(j["failures"]), hide_index=True, use_container_width=True)


def page_upload() -> None:
    st.header("Upload attendance evidence")
    if not can("ingest"):
        st.info("Your role cannot ingest files (hr_admin and manager can).")
        return
    files = st.file_uploader(
        "CSV, XLSX, DOCX, PDF (text or scanned), PNG/JPG scans or handwriting",
        accept_multiple_files=True,
        key="files",
    )
    c1, c2 = st.columns(2)
    logical = c1.text_input("Logical name (groups versions of one file)", key="logical_name")
    entities = ["(from file)", *(ss.get("me") or {}).get("visible_entities", [])]
    entity = c2.selectbox("Entity", entities, key="upload_entity")
    if st.button("Upload", key="upload", type="primary", disabled=not files):
        with st.spinner("Uploading…"):
            ss["jobs"] = upload_files(
                api(), files, logical.strip(), None if entity == "(from file)" else entity
            ) + ss.get("jobs", [])
    if ss.get("jobs"):
        c1, c2 = st.columns(2)
        try:
            if c1.button("Refresh status", key="refresh"):
                ss["jobs"] = refresh_jobs(api(), ss["jobs"])
            if c2.button("Wait for completion", key="wait"):
                with st.spinner("Processing (OCR and embeddings can take a while)…"):
                    ss["jobs"] = refresh_jobs(api(), ss["jobs"], wait=True)
        except Exception as exc:  # noqa: BLE001
            show_error(exc)
        for j in ss["jobs"]:
            render_job(j)
    st.subheader("Failed-file queue")
    try:
        failed = api().jobs("failed")
    except Exception as exc:  # noqa: BLE001
        show_error(exc)
        return
    if not failed:
        st.caption("No failed jobs in your scope.")
    for j in failed:
        c1, c2 = st.columns([4, 1])
        c1.write(f"❌ {j.get('filename') or j['job_id']} — stage {j.get('stage')}, "
                 f"attempts {j.get('attempts')}")  # fmt: skip
        if c2.button("Retry", key=f"retry_{j['job_id']}"):
            try:
                res = api().retry(j["job_id"])
                st.success(f"Retry queued: {res.get('status')}")
            except Exception as exc:  # noqa: BLE001
                show_error(exc)


def page_feedback() -> None:
    st.header("Feedback / training loop")
    if not can("feedback_submit"):
        st.info("Only reviewers and HR admins can submit feedback.")
        return
    last = ss.get("last_answer") or {}
    if ss.get("fb_seeded_for") != last.get("request_id"):
        ss["fb_seeded_for"] = last.get("request_id")
        ss["fb_rid"] = last.get("request_id", "")
        ss["fb_q"] = ss.get("last_question", "") if last else ""
        ss["fb_resp"] = ss["fb_ideal"] = last.get("answer", "")
    with st.form("feedback_form"):
        rid = st.text_input("Original request id", key="fb_rid")
        question = st.text_input("Question", key="fb_q")
        st.text_area("Agent response", key="fb_resp", disabled=True)
        feedback = st.text_area("Correction / feedback", key="fb_text")
        ideal = st.text_area("Ideal final output", key="fb_ideal")
        submitted = st.form_submit_button("Submit feedback", type="primary")
    if submitted:
        body = {
            "original_request_id": rid.strip(),
            "question": question.strip() or None,
            "agent_response": last.get("answer"),
            "feedback": feedback.strip(),
            "ideal_final_output": ideal.strip(),
            "approve": True,
        }
        with st.spinner("Validating against live data and re-running the question…"):
            try:
                ss["feedback_result"] = api().submit_feedback(body)
            except Exception as exc:  # noqa: BLE001
                show_error(exc)
    res = ss.get("feedback_result")
    if res:
        (st.success if res.get("status") == "active" else st.error)(
            f"Example {res.get('example_id', '')[:8]}… v{res.get('version')}: {res.get('status')}"
        )
        c1, c2 = st.columns(2)
        c1.markdown("**Validation**")
        c1.json(res.get("validation") or {})
        c2.markdown("**Verification (re-run)**")
        c2.json(res.get("verification") or {})

    st.subheader("Examples and versions")
    try:
        examples = api().feedback()
    except Exception as exc:  # noqa: BLE001
        show_error(exc)
        return
    if not examples:
        st.caption("No feedback examples in your scope.")
        return
    cols = ["example_id", "version", "status", "intent_signature", "question", "times_applied",
            "reviewer_id", "created_at"]  # fmt: skip
    st.dataframe(pd.DataFrame(examples)[[c for c in cols if c in examples[0]]], hide_index=True,
                 use_container_width=True)  # fmt: skip
    if not can("feedback_manage"):
        return
    ids = [f"{e['example_id']} (v{e['version']}, {e['status']})" for e in examples]
    choice = st.selectbox("Manage example", ids, key="fb_manage")
    example_id = choice.split(" ")[0]
    c1, c2 = st.columns(2)
    for col, label, fn in ((c1, "Deactivate", "deactivate"), (c2, "Roll back", "rollback")):
        if col.button(label, key=f"fb_{fn}"):
            try:
                res = getattr(api(), fn)(example_id)
                st.success(f"{label}: {res.get('status', 'done')}")
            except Exception as exc:  # noqa: BLE001
                show_error(exc)


def page_export() -> None:
    st.header("Export permitted results")
    if not can("export"):
        st.info("Only HR admins and managers can export.")
        return
    source = st.radio("Source", ["records", "query"], horizontal=True, key="export_source",
                      format_func={"records": "Attendance records", "query": "A stored answer's "
                                   "evidence"}.get)  # fmt: skip
    body: dict = {"source": source}
    if source == "query":
        default = (ss.get("last_answer") or {}).get("request_id", "")
        body["request_id"] = st.text_input("Request id", default, key="export_rid").strip()
    else:
        body["filters"] = filters_form("export", with_status=True)
    fmt = st.radio("Format", ["json", "xlsx", "pdf"], horizontal=True, key="export_format")
    if st.button("Generate export", key="export", type="primary"):
        try:
            content, headers = api().export({**body, "format": fmt})
            ss["export_file"] = {"content": content, "headers": headers, "format": fmt}
        except Exception as exc:  # noqa: BLE001
            show_error(exc)
    f = ss.get("export_file")
    if f:
        h = f["headers"]
        st.success(f"{h.get('x-export-record-count')} rows · checksum "
                   f"`{(h.get('x-export-checksum') or '')[:16]}…`")  # fmt: skip
        name = (h.get("content-disposition") or "").split("filename=")[-1].strip('"')
        st.download_button(f"Download {f['format'].upper()}", f["content"],
                           file_name=name or f"export.{f['format']}",
                           mime=h.get("content-type"), key="download")  # fmt: skip


def page_health() -> None:
    st.header("Service health")
    try:
        h = api().health_deep()
    except Exception as exc:  # noqa: BLE001
        show_error(exc)
        return
    overall = h["status"]
    {"ok": st.success, "degraded": st.warning}.get(overall, st.error)(f"Overall: {overall}")
    comps = h["components"]
    tiles = [(k, v) for k, v in comps.items() if isinstance(v, dict)]
    for i in range(0, len(tiles), 4):
        for col, (name, c) in zip(st.columns(4), tiles[i : i + 4], strict=False):
            icon = "🟢" if c.get("status") == "ok" else "🔴"
            col.metric(name.replace("_", " "), f"{icon} {c.get('status')}")
            extra = {k: v for k, v in c.items() if k not in ("status",)}
            if extra:
                col.caption(", ".join(f"{k}: {v}" for k, v in extra.items()))
    for key in ("llm_providers", "vision_providers"):
        if comps.get(key):
            st.markdown(f"**{key.replace('_', ' ')}**")
            st.dataframe(pd.DataFrame(comps[key]), hide_index=True, use_container_width=True)


def main() -> None:
    st.set_page_config(page_title="Attendance Intelligence", layout="wide")
    page = sidebar()
    {
        "Ask": page_ask,
        "Upload": page_upload,
        "Feedback": page_feedback,
        "Export": page_export,
        "Health": page_health,
    }[page]()


main()
