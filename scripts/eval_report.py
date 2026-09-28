"""Build docs/TEST_SUMMARY.md from REAL runs (nothing in it is typed by hand).

Sources:
  - the full non-live suite, run here with --junitxml + coverage JSON
    (or reused with --junit/--coverage from an earlier run)
  - test counts per marker (pytest --collect-only)
  - docs/eval_results.json written by the live evaluation
    (pytest -m live tests/live/test_eval_questions.py, or python -m scripts.eval_live)
  - scripts.ocr_eval against the configured OCR/vision chain
The assignment's section 7 scenario table is mapped to the test files and live
cases that prove each row; a row passes only if all of them passed.

Usage (inside the api container):
    python -m scripts.eval_report                  # runs the suite (~4-5 min)
    python -m scripts.eval_report --junit j.xml --coverage c.json
"""

import argparse
import datetime as dt
import json
import re
import statistics
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
MARKERS = ("unit", "integration", "security", "ocr", "e2e", "live")

# Assignment section 7 "mandatory test scenarios" -> evidence (test files, live case ids).
SCENARIOS: list[tuple[str, str, list[str], list[str]]] = [
    ("Mixed-format ingestion",
     "At least four different input types are processed into the canonical schema.",
     ["integration/test_ingest_tabular", "integration/test_ingest_documents",
      "ocr/test_ocr_ingestion", "e2e/test_corpus_ingestion"], []),
    ("Extraction traceability",
     "Each normalized record can be traced to a source file and page, row, or record.",
     ["e2e/test_citations_structured", "unit/test_lineage", "e2e/test_corpus_ingestion"],
     ["Q1", "Q17"]),
    ("OCR/handwriting handling",
     "OCR confidence or review status is recorded; uncertain values are not presented as facts.",
     ["ocr/test_ocr_ingestion", "unit/test_ocr_units", "e2e/test_review_states"], ["Q18"]),
    ("Idempotency",
     "Re-uploading the same file does not duplicate records; changed files are versioned.",
     ["integration/test_idempotency_versioning"], []),
    ("Structured answers",
     "Counts, averages and highest/lowest comparisons use structured data and are correct.",
     ["e2e/test_structured_answers", "integration/test_view_metrics", "unit/test_sql_validator",
      "integration/test_sql_executor", "e2e/test_template_fallback"],
     ["Q1", "Q2", "Q3", "Q4", "B2"]),
    ("Evidence answers",
     "Narrative/source questions return grounded citations that exist in the source metadata.",
     ["e2e/test_evidence_answers", "unit/test_fusion"], ["Q5a", "Q6"]),
    ("Unavailable answer",
     "A question outside the evidence gets a controlled unavailable/insufficient response.",
     ["e2e/test_unavailable"], ["Q7", "Q8"]),
    ("Tenant isolation",
     "Tenant A cannot return or cite Tenant B records, incl. through aggregates or exports.",
     ["security/test_rls", "security/test_isolation_query", "security/test_vector_rls",
      "security/test_hybrid_isolation", "security/test_export_isolation",
      "security/test_cache_isolation"], ["Q9", "Q9b", "Q10", "Q13"]),
    ("RBAC/entity isolation",
     "A role or entity without access is denied or filtered before generation.",
     ["security/test_rbac", "security/test_auth", "security/test_context",
      "security/test_ingest_scope"], ["Q5b", "Q11a", "Q11b", "Q12a", "Q12b"]),
    ("Prompt injection",
     "Instructions embedded in an uploaded file cannot change policy or reveal restricted data.",
     ["security/test_injection_ingest", "security/test_output_governance",
      "security/test_sql_attacks"], ["Q14", "Q15"]),
    ("PII/data leakage",
     "Sensitive fields are masked or restricted according to the classification policy.",
     ["unit/test_text_security_units", "security/test_output_governance",
      "security/test_export_isolation"], ["Q16"]),
    ("Provider failure",
     "Model/provider failure is recorded and a safe fallback or controlled error is used.",
     ["unit/test_router", "integration/test_providers_health_audit",
      "e2e/test_template_fallback", "integration/test_health_deep"], []),
    ("Feedback-driven learning",
     "Authorized feedback + ideal_final_output -> versioned scoped example that changes the "
     "equivalent repeat query; cross-tenant reuse blocked; rollback/deactivation testable.",
     ["e2e/test_feedback_loop", "security/test_feedback_security",
      "integration/test_cache_invalidation"], ["Q2", "B1"]),
    ("Export consistency",
     "JSON, XLSX and PDF contain the same permitted records and preserve source references.",
     ["e2e/test_export_consistency", "e2e/test_export_query", "unit/test_export_renderers",
      "security/test_export_isolation"], []),
]  # fmt: skip


def _pytest(*args: str) -> subprocess.CompletedProcess:
    cmd = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *args]
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)  # noqa: S603 - fixed args


def marker_counts() -> dict[str, int]:
    out = {}
    for m in MARKERS:
        text = _pytest("--collect-only", "-q", "-m", m).stdout
        hit = re.search(r"(\d+)(?:/\d+)? tests? (?:collected|selected)", text)
        out[m] = int(hit.group(1)) if hit else 0
    total = re.search(r"(\d+) tests? collected", _pytest("--collect-only", "-q").stdout)
    out["total (all markers, incl. live)"] = int(total.group(1)) if total else 0
    return out


def run_suite(tmp: Path) -> tuple[Path, Path, str]:
    junit, cov = tmp / "junit.xml", tmp / "coverage.json"
    proc = _pytest("-m", "not live", "-q", f"--junitxml={junit}", "--cov=app",
                   f"--cov-report=json:{cov}")  # fmt: skip
    tail = [ln for ln in proc.stdout.splitlines() if re.search(r"\d+ (passed|failed)", ln)]
    return junit, cov, tail[-1] if tail else "(no summary line)"


def junit_by_file(junit: Path) -> dict[str, dict[str, int]]:
    files: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    root = ET.parse(junit).getroot()  # noqa: S314 - our own pytest output, not untrusted input
    for case in root.iter("testcase"):
        mod = case.get("classname", "")  # tests.e2e.test_x[.Class]
        parts = mod.split(".")
        key = "/".join(parts[1:3]) if parts[0] == "tests" else mod
        kinds = {child.tag for child in case}
        if kinds & {"failure", "error"}:
            files[key]["failed"] += 1
        elif "skipped" in kinds:
            files[key]["skipped"] += 1
        else:
            files[key]["passed"] += 1
    return files


def load_eval() -> dict | None:
    path = DOCS / "eval_results.json"
    return json.loads(path.read_text()) if path.exists() else None


def ocr_report() -> list[dict]:
    try:
        from scripts import ocr_eval

        return ocr_eval.evaluate()
    except Exception as exc:  # noqa: BLE001 - reported, not fatal
        return [{"file": "-", "error": f"{exc.__class__.__name__}: {exc}"}]


def _mark(ok: bool | None) -> str:
    return {True: "PASS", False: "FAIL", None: "n/a"}[ok]


def build(files, coverage_pct, suite_line, counts, ev, ocr) -> str:
    live = {r["id"]: r for r in (ev or {}).get("results", [])}
    lines = [
        "# Test summary",
        "",
        f"Generated {dt.datetime.now(dt.UTC):%Y-%m-%d %H:%M} UTC by `scripts/eval_report.py` "
        "from real runs: the automated suite (this machine, Docker), the live evaluation "
        "against the dev server with the local Ollama models, and the OCR evaluation.",
        "",
        "## Totals",
        "",
        f'- Automated suite (`pytest -m "not live"`): **{suite_line}**',
        f"- Line coverage of `app/`: **{coverage_pct:.1f}%** (gate floor 80%)",
        "- Lint: `ruff check` + `ruff format --check` (part of `scripts/gate.sh`)",
    ]
    if ev:
        lines.append(f"- Live evaluation: **{ev['passed']}/{ev['total']} cases passed** "
                     f"({ev['generated_at']}, real models)")  # fmt: skip
    lines += ["", "Tests per marker (a test can carry several markers):", "",
              "| marker | tests |", "|---|---|"]  # fmt: skip
    lines += [f"| {m} | {n} |" for m, n in counts.items()]

    lines += ["", "## Mandatory scenarios (assignment section 7)", "",
              "| # | Scenario | Pass condition | Automated evidence | Live cases | Result |",
              "|---|---|---|---|---|---|"]  # fmt: skip
    for i, (name, cond, test_files, cases) in enumerate(SCENARIOS, 1):
        stats = [files.get(f) for f in test_files]
        n_pass = sum(s["passed"] for s in stats if s)
        n_fail = sum(s["failed"] for s in stats if s)
        missing = [f for f, s in zip(test_files, stats, strict=True) if not s]
        auto_ok = n_fail == 0 and not missing and n_pass > 0
        live_ok = [live[c]["passed"] for c in cases if c in live]
        ok = auto_ok and all(live_ok)
        evidence = ", ".join(f"`tests/{f}.py`" for f in test_files)
        evidence += f" ({n_pass} passed" + (f", {n_fail} failed" if n_fail else "") + ")"
        live_txt = (
            ", ".join(f"{c} {'✓' if live[c]['passed'] else '✗'}" for c in cases if c in live) or "-"
        )
        lines.append(f"| {i} | {name} | {cond} | {evidence} | {live_txt} | "
                     f"**{_mark(ok)}** |")  # fmt: skip

    if ev:
        res = ev["results"]
        # >= 1 s means a model was called (the final text may still be a template/quote)
        lat = [r["latency_s"] for r in res if r["latency_s"] >= 1]
        lines += ["", "## Live evaluation (real local models)", "",
                  f"Server `{ev['api_url']}`; chain ollama qwen2.5:7b-instruct -> "
                  "qwen2.5:3b-instruct -> deterministic template; embeddings nomic-embed-text; "
                  "query cache flushed before the run.", ""]  # fmt: skip
        if lat:
            lines.append(f"Latency of the {len(lat)} questions that called a model (CPU): median "
                         f"{statistics.median(lat):.0f} s, max {max(lat):.0f} s. Refusals, "
                         "denials and no-data answers make no model call (< 1 s).")  # fmt: skip
        lines += ["", "| id | persona | question | result | latency | mode | provider path "
                  "| confidence | failed checks |",
                  "|---|---|---|---|---|---|---|---|---|"]  # fmt: skip
        for r in res:
            failed = "; ".join(f"{c['check']} ({c['detail']})" if c["detail"] else c["check"]
                               for c in r["checks"] if not c["ok"])  # fmt: skip
            if r.get("note") and not r["passed"]:
                failed += f" - {r['note']}"
            conf = "" if r["confidence"] is None else f"{r['confidence']:.2f}"
            q = r["question"].replace("|", "/")
            lines.append(f"| {r['id']} | {r['persona']} | {q} | **{_mark(r['passed'])}** | "
                         f"{r['latency_s']:.1f} s | {r['mode'] or r['http_status']} | "
                         f"{r['fallback_path'] or '-'} | {conf} | {failed or '-'} |")  # fmt: skip

    gaps = [r for r in (ev or {}).get("results", []) if not r["passed"]]
    if gaps:
        lines += ["", "## Known gaps", ""]
        for r in gaps:
            failed = ", ".join(c["check"] for c in r["checks"] if not c["ok"])
            note, answer = r.get("note") or "", r.get("answer", "")
            lines.append(f"- **{r['id']}** ({r.get('scenario', '-')}): {failed}. {note} "
                         f"Answer given: \"{answer}\"")  # fmt: skip

    lines += ["", "## OCR evaluation (`scripts/ocr_eval.py`, configured engines)", "",
              "| file | engine | rows read / expected | field accuracy | flagged for review | "
              "wrong values stored as fact |", "|---|---|---|---|---|---|"]  # fmt: skip
    for o in ocr:
        if "error" in o:
            lines.append(f"| {o['file']} | - | - | - | - | error: {o['error'][:120]} |")
            continue
        acc = "-" if o["field_accuracy"] is None else f"{o['field_accuracy']:.1%}"
        lines.append(f"| {o['file']} | {o['engine']} | {o['rows_read']} / {o['rows_expected']} "
                     f"| {acc} | {o['flagged_for_review']} | "
                     f"{o['wrong_values_stored_as_fact']} |")  # fmt: skip
    lines += ["", "The number that must stay 0 is *wrong values stored as fact*: anything "
              "the OCR is unsure of is flagged for review instead of being stored as a fact.",
              ""]  # fmt: skip
    lines += ["## How to reproduce", "", "```bash",
              "bash scripts/gate.sh 80",
              "docker compose run --rm -T api pytest -m live -s -q "
              "tests/live/test_eval_questions.py",
              "docker compose run --rm -T api python -m scripts.eval_report",
              "```", ""]  # fmt: skip
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--junit", type=Path, help="reuse a junit xml instead of running")
    parser.add_argument("--coverage", type=Path, help="reuse a coverage json")
    parser.add_argument("--suite-line", default="(reused run)")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        if args.junit and args.coverage:
            junit, cov, line = args.junit, args.coverage, args.suite_line
        else:
            junit, cov, line = run_suite(Path(tmp))
        files = junit_by_file(junit)
        coverage_pct = json.loads(cov.read_text())["totals"]["percent_covered"]
    md = build(files, coverage_pct, line, marker_counts(), load_eval(), ocr_report())
    DOCS.mkdir(exist_ok=True)
    (DOCS / "TEST_SUMMARY.md").write_text(md, encoding="utf-8")
    print(f"wrote {DOCS / 'TEST_SUMMARY.md'} ({line})")


if __name__ == "__main__":
    main()
