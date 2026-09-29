"""OCR accuracy report against ground truth (uses the REAL configured vision model).

Usage (inside the api container):  python -m scripts.ocr_eval
Prints, per scanned file: engine used, rows read, field accuracy on
employee_id / status / date / times after normalization, how many rows the pipeline
flags for review, and every wrong value that would be stored as a fact (must stay 0).
"""

import json
from pathlib import Path

from sqlalchemy import text

from app.config import get_settings
from app.db.session import owner_session
from app.ingestion.normalize.normalizer import Employee, Roster, normalize
from app.ingestion.parsers import ocr_parser, pdf_parser

ROOT = Path(__file__).resolve().parents[1]
FILES = [
    "scan_printed.png",
    "scan_printed.pdf",
    "handwritten_sheet.png",
    "handwritten_ambiguous.png",
]


def _roster(tenant_id: str) -> tuple[Roster, str]:
    with owner_session() as s:
        rows = s.execute(
            text(
                "SELECT e.employee_id, e.employee_name, e.entity_id, en.name FROM employees e "
                "JOIN entities en ON en.tenant_id = e.tenant_id AND en.entity_id = e.entity_id "
                "WHERE e.tenant_id = :t"
            ),
            {"t": tenant_id},
        ).all()
        fmt = s.execute(
            text("SELECT date_format FROM tenants WHERE tenant_id = :t"), {"t": tenant_id}
        ).scalar_one()
    return Roster([Employee(*r) for r in rows]), fmt


def _hhmm(value) -> str | None:
    if value in (None, "", "-"):
        return None
    if hasattr(value, "strftime"):
        return value.strftime("%H:%M")
    h, _, m = str(value).partition(":")
    return f"{int(h):02d}:{m}"


def evaluate() -> list[dict]:
    """Scores what would be STORED: normalized values and the pipeline's own review flag."""
    manifests = json.loads((ROOT / "data/ground_truth/manifests.json").read_text())["files"]
    threshold = get_settings().ocr_review_threshold
    report = []
    for name in FILES:
        meta = manifests[name]
        content = (ROOT / "data/generated" / name).read_bytes()
        parse = pdf_parser.parse if name.endswith(".pdf") else ocr_parser.parse
        try:
            result = parse(content, name)
            roster, fmt = _roster(meta["tenant_id"])
            drafts, failures = normalize(
                result, roster, date_format=fmt, review_threshold=threshold
            )
        except Exception as exc:  # noqa: BLE001
            report.append({"file": name, "error": str(exc)})
            continue
        truth = {r["locator"]: r for r in meta["rows"]}
        fields = correct = review = 0
        wrong_facts, missed = [], []
        for d in drafts:
            t = truth.get(d["source_locator"])
            if not t:
                continue
            rendered = t.get("rendered") or {}
            got = {
                "employee_id": d["employee_id"],
                "status": d["status"],
                "date": d["attendance_date"].isoformat(),
                "check_in": _hhmm(d["check_in"]),
                "check_out": _hhmm(d["check_out"]),
            }
            want = {
                "employee_id": t["employee_id"],
                "status": t["status"],
                "date": t["attendance_date"],
                "check_in": _hhmm(rendered.get("In")),
                "check_out": _hhmm(rendered.get("Out")),
            }
            review += d["review_required"]
            if t.get("expected_review") and not d["review_required"]:
                missed.append(d["source_locator"])
            for k in got:
                if k.startswith("check_") and got[k] is None:
                    continue
                fields += 1
                correct += got[k] == want[k]
                if got[k] != want[k] and not d["review_required"]:
                    wrong_facts.append(f"{d['source_locator']} {k}={got[k]} (truth {want[k]})")
        report.append(
            {
                "file": name,
                "engine": result.method,
                "rows_read": len(result.records),
                "rows_expected": len(truth),
                "row_failures": len(failures),
                "field_accuracy": round(correct / fields, 3) if fields else None,
                "flagged_for_review": review,
                "expected_review_missed": missed,
                "wrong_values_stored_as_fact": len(wrong_facts),
                "wrong_facts": wrong_facts,
            }
        )
    return report


if __name__ == "__main__":
    for row in evaluate():
        print(json.dumps(row))
