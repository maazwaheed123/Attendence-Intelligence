"""End-to-end demo over the public API (the assignment's submission checklist).

  1. health            6. export the answer as JSON, XLSX and PDF (same rows, same checksum)
  2. ingest (--full)   7. feedback: reviewer correction + ideal output -> new version,
  3. answer + sources     repeat query applies it, tenant B does not get it
  4. unavailable       8. rollback to the previous version
  5. blocked cross-tenant / cross-entity

Every step is checked; the script exits non-zero if any check fails.

Usage (dev stack running; models make each answer take ~30-90 s on CPU):
    docker compose run --rm -T api python -m scripts.demo          # corpus already ingested
    docker compose run --rm -T api python -m scripts.demo --full   # + (re)ingest all samples
From the host use DEMO_API_URL=http://localhost:8000 python -m scripts.demo.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx

API_URL = os.getenv("DEMO_API_URL", "http://api:8000")
ROOT = Path(__file__).resolve().parents[1]
EXPORT_DIR = ROOT / "data" / "exports"  # git-ignored
Q1 = "Who was present on 1 September 2026?"
Q2 = "What was Engineering's average attendance % in September?"
IDEAL = (
    "Engineering attendance from 01/09/2026 to 30/09/2026 was 91.83%, calculated from 95.5 "
    "present employee-days out of 104 scheduled employee-days."
)

RESULTS: list[tuple[str, bool]] = []


def check(name: str, ok: bool) -> bool:
    RESULTS.append((name, bool(ok)))
    print(f"   [{'ok' if ok else 'FAIL'}] {name}")
    return ok


def title(text: str) -> None:
    print(f"\n== {text}")


class Api:
    def __init__(self, url: str):
        self.http = httpx.Client(base_url=url, timeout=600)
        self.tokens: dict[str, str] = {}

    def h(self, persona: str) -> dict:
        if persona not in self.tokens:
            r = self.http.post("/v1/auth/dev-token", json={"persona": persona})
            r.raise_for_status()
            self.tokens[persona] = r.json()["access_token"]
        return {"Authorization": f"Bearer {self.tokens[persona]}"}

    def ask(self, persona: str, question: str, **body) -> httpx.Response:
        start = time.perf_counter()
        r = self.http.post("/v1/query", json={"question": question, **body},
                           headers=self.h(persona))  # fmt: skip
        print(f"   {persona}: {question!r} -> HTTP {r.status_code} "
              f"({time.perf_counter() - start:.0f} s)")  # fmt: skip
        return r


def show(r: dict, n_cites: int = 4) -> None:
    print(f"   status={r['status']} mode={r['retrieval_mode']} confidence={r['confidence']:.2f} "
          f"({r['confidence_band']}) provider={r['fallback_path']}")  # fmt: skip
    print(f"   answer: {r['answer']}")
    for c in r["citations"][:n_cites]:
        print(f"     - {c['source_file']} [{c['locator']}] {c['excerpt'][:80]}")
    if r["citation_total"] > n_cites:
        print(f"     ... {r['citation_total']} sources in total")
    for w in r["warnings"]:
        print(f"   warning: {w}")


def step_health(api: Api) -> None:
    title("1. Health")
    h = api.http.get("/v1/health/deep").json()
    comps = {k: v.get("status") for k, v in h["components"].items() if isinstance(v, dict)}
    print(f"   overall={h['status']} {comps}")
    check("service reachable and database ok", comps.get("database") == "ok")


def _persona_for(meta: dict) -> str:
    if meta.get("product_id") == "hrms_ai":
        return "x_other_product"
    return "b_manager" if meta.get("tenant_id") == "tenant_b" else "a_hr_admin"


def step_ingest(api: Api) -> None:
    title("2. Ingest every sample file (idempotent: re-uploads are reported as duplicates)")
    manifests = json.loads((ROOT / "data/ground_truth/manifests.json").read_text())["files"]
    jobs = []
    for name in sorted(manifests):
        meta = manifests[name]
        persona = _persona_for(meta)
        data = {"logical_name": meta["logical_name"]} if meta.get("logical_name") else {}
        content = (ROOT / "data/generated" / name).read_bytes()
        r = api.http.post("/v1/ingest", files={"file": (name, content)}, data=data,
                          headers=api.h(persona))  # fmt: skip
        body = r.json()
        jobs.append((name, persona, meta.get("expected_failure"), r.status_code, body))
    deadline = time.monotonic() + 900
    final = {}
    for name, persona, expected_failure, _status, body in jobs:
        job = body
        while job.get("job_id") and job.get("status") in ("queued", "running", "retrying"):
            if time.monotonic() > deadline:
                break
            time.sleep(3)
            job = api.http.get(f"/v1/jobs/{job['job_id']}", headers=api.h(persona)).json()
        final[name] = job
        counts = job.get("counts") or {}
        state = job.get("status") or job.get("error", {}).get("code")
        print(f"   {name:28} {state:10} v{job.get('version')} records={counts.get('records')} "
              f"review={counts.get('review_required')}")  # fmt: skip
        if expected_failure:
            check(f"{name} rejected with a reason", state in ("failed", "UNSUPPORTED_FILE",
                                                              "VALIDATION_ERROR"))  # fmt: skip
    done = [n for n, j in final.items() if j.get("status") in ("completed", "duplicate")]
    check(f"{len(done)} files completed or recognised as duplicates", len(done) >= 10)


def step_answer(api: Api) -> str:
    title("3. Answer with citations and confidence")
    r = api.ask("a_eng_manager", Q1).json()
    show(r)
    ok = r["status"] == "answered" and all(e in r["answer"] for e in ("E001", "E002", "E003"))
    check("Q1 answered from permitted data with 4 citations", ok and len(r["citations"]) == 4)
    return r["request_id"]


def step_unavailable(api: Api) -> None:
    title("4. Controlled unavailable answers")
    for q, reason in (("What was Alice's attendance in December 2027?", "no_data_in_scope"),
                      ("What is the company revenue?", "out_of_scope"),
                      ("Give me Alice's phone number", "not_permitted")):  # fmt: skip
        r = api.ask("a_eng_manager", q).json()
        print(f"   -> {r['answer']}")
        check(f"unavailable ({reason})", r["status"] == "unavailable"
              and r["unavailable_reason"] == reason)  # fmt: skip


def step_blocked(api: Api, q1_request_id: str) -> None:
    title("5. Blocked cross-tenant / cross-entity requests")
    other = api.ask("a_eng_manager", "What was John Carter's attendance?").json()
    unknown = api.ask("a_eng_manager", "What was Jane Doe's attendance?").json()
    print(f"   tenant B employee -> {other['answer']}")
    same = {k: other[k] for k in ("status", "answer", "citations")} == {
        k: unknown[k] for k in ("status", "answer", "citations")
    }
    check("another tenant's employee looks exactly like a non-existent one", same)
    r = api.ask("a_eng_manager", "HR attendance in September?", filters={"entity_id": "hr"})
    print(f"   entity filter outside the token -> {r.status_code} {r.json()['error']['code']}")
    check("entity outside the token -> 403 before retrieval", r.status_code == 403)
    r = api.http.get(f"/v1/query/{q1_request_id}", headers=api.h("b_manager"))
    check("tenant B cannot read tenant A's stored answer (404)", r.status_code == 404)
    r = api.http.post("/v1/export", json={"source": "query", "request_id": q1_request_id},
                      headers=api.h("b_manager"))  # fmt: skip
    check("tenant B cannot export tenant A's answer (404)", r.status_code == 404)


def step_export(api: Api, q1_request_id: str) -> None:
    title("6. Export the answer's evidence as JSON, XLSX and PDF")
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    sums = set()
    for fmt in ("json", "xlsx", "pdf"):
        r = api.http.post("/v1/export", json={"source": "query", "request_id": q1_request_id,
                                              "format": fmt},
                          headers=api.h("a_eng_manager"))  # fmt: skip
        path = EXPORT_DIR / f"demo_q1.{fmt}"
        path.write_bytes(r.content)
        sums.add((r.headers.get("x-export-checksum"), r.headers.get("x-export-record-count")))
        print(f"   {fmt:4} HTTP {r.status_code} rows={r.headers.get('x-export-record-count')} "
              f"checksum={str(r.headers.get('x-export-checksum'))[:16]}... -> {path}")  # fmt: skip
    check(
        "three formats: same rows, same checksum", len(sums) == 1 and None not in next(iter(sums))
    )


def step_feedback(api: Api) -> None:
    title("7. Feedback / training loop")
    original = api.ask("a_reviewer", Q2).json()
    print(f"   before: {original['answer']}")
    r = api.http.post("/v1/feedback", headers=api.h("a_reviewer"), json={
        "original_request_id": original["request_id"],
        "feedback": "State the date range and how the percentage was calculated.",
        "ideal_final_output": IDEAL, "approve": True})  # fmt: skip
    fb = r.json()
    print(f"   submitted -> HTTP {r.status_code} status={fb.get('status')} "
          f"version={fb.get('version')} verification={fb.get('verification')}")  # fmt: skip
    check("feedback stored as an active, versioned example", fb.get("status") == "active")
    repeat = api.ask("a_eng_manager", Q2).json()
    print(f"   repeat (manager): {repeat['answer']}  applied={repeat['applied_feedback']}")
    applied = repeat["applied_feedback"] or {}
    check("equivalent repeat query uses the new version",
          applied.get("example_id") == fb.get("example_id"))  # fmt: skip
    b = api.ask("b_manager", Q2).json()
    print(f"   tenant B: {b['answer']}  applied={b['applied_feedback']}")
    check("tenant B is not affected", b["applied_feedback"] is None and "89.68" in b["answer"])

    title("8. Roll back to the previous version")
    r = api.http.post(f"/v1/feedback/{fb.get('example_id')}/rollback",
                      headers=api.h("a_reviewer"))  # fmt: skip
    if r.status_code == 409:  # first ever example: nothing to roll back to -> deactivate
        r = api.http.post(f"/v1/feedback/{fb.get('example_id')}/deactivate",
                          headers=api.h("a_reviewer"))  # fmt: skip
        print(f"   no earlier version; deactivated -> HTTP {r.status_code}")
        after = api.ask("a_eng_manager", Q2).json()
        check("after deactivation the example is no longer applied",
              after["applied_feedback"] is None)  # fmt: skip
        return
    body = r.json()
    active = body.get("active") or {}
    print(
        f"   rolled back {str(body.get('rolled_back'))[:8]}... -> active v{active.get('version')}"
    )
    after = api.ask("a_eng_manager", Q2).json()
    now = (after["applied_feedback"] or {}).get("example_id")
    check("after rollback the previous version is applied", now == active.get("example_id"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--full", action="store_true", help="(re)ingest every sample file")
    args = parser.parse_args()
    api = Api(API_URL)
    step_health(api)
    if args.full:
        step_ingest(api)
    rid = step_answer(api)
    step_unavailable(api)
    step_blocked(api, rid)
    step_export(api, rid)
    step_feedback(api)
    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    for n in failed:
        print(f"   FAILED: {n}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
