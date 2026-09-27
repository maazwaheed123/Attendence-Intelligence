"""OCR accuracy report against ground truth (uses the REAL configured vision model).

Usage (inside the api container):  python -m scripts.ocr_eval
Prints, per scanned file: engine used, rows read, field accuracy on
employee_id / status / date, how many rows were flagged for review, and whether
any wrong value was stored as a fact (the number that must stay 0).
"""

import json
from pathlib import Path

from app.ingestion.normalize.datetimes import parse_date
from app.ingestion.normalize.status import map_status
from app.ingestion.parsers import ocr_parser, pdf_parser

ROOT = Path(__file__).resolve().parents[1]
FILES = [
    "scan_printed.png",
    "scan_printed.pdf",
    "handwritten_sheet.png",
    "handwritten_ambiguous.png",
]


def evaluate() -> list[dict]:
    manifests = json.loads((ROOT / "data/ground_truth/manifests.json").read_text())["files"]
    report = []
    for name in FILES:
        content = (ROOT / "data/generated" / name).read_bytes()
        parse = pdf_parser.parse if name.endswith(".pdf") else ocr_parser.parse
        try:
            result = parse(content, name)
        except Exception as exc:  # noqa: BLE001
            report.append({"file": name, "error": str(exc)})
            continue
        truth = {r["locator"]: r for r in manifests[name]["rows"]}
        fields = correct = wrong_facts = review = 0
        for rec in result.records:
            t = truth.get(rec.locator)
            if not t:
                continue
            status, known = map_status(rec.values.get("status"))
            d, _ = parse_date(rec.values.get("attendance_date"))
            got = {
                "employee_id": (rec.values.get("employee_id") or "").upper(),
                "status": status,
                "date": d.isoformat() if d else None,
            }
            want = {
                "employee_id": t["employee_id"],
                "status": t["status"],
                "date": t["attendance_date"],
            }
            is_review = rec.confidence < 0.75 or not known
            review += is_review
            for k in got:
                fields += 1
                correct += got[k] == want[k]
                wrong_facts += (got[k] != want[k]) and not is_review
        report.append(
            {
                "file": name,
                "engine": result.method,
                "rows_read": len(result.records),
                "rows_expected": len(truth),
                "field_accuracy": round(correct / fields, 3) if fields else None,
                "flagged_for_review": review,
                "wrong_values_stored_as_fact": wrong_facts,
            }
        )
    return report


if __name__ == "__main__":
    for row in evaluate():
        print(json.dumps(row))
