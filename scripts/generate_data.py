"""Generate all synthetic sample files + ground truth + expected results.

Usage (inside the api container):
    python -m scripts.generate_data                 # writes data/generated + data/ground_truth
    python -m scripts.generate_data --out /tmp/x    # writes to another root (used by tests)

Deterministic: the same seed_spec.yaml always produces byte-identical files.
"""

import argparse
import json
from pathlib import Path

from scripts.datagen import expected as exp
from scripts.datagen.render_docs import (
    write_conflict_note,
    write_injection_memo,
    write_tenant_b_pdf,
    write_week2_docx,
)
from scripts.datagen.render_images import (
    write_handwritten_ambiguous,
    write_handwritten_sheet,
    write_printed_scan,
)
from scripts.datagen.render_tabular import (
    write_attendance_csv,
    write_invalid_files,
    write_other_product_csv,
    write_pii_xlsx,
    write_sales_xlsx,
)
from scripts.datagen.truth import build_truth, index_rows, load_spec
from scripts.datagen.util import sha256_file

ROOT = Path(__file__).resolve().parents[1]

# Order in which the demo corpus is ingested (matters for versioning: v1 then v2).
CORPUS_ORDER = [
    "tenant_a_sep.csv",
    "tenant_a_sep_v2.csv",
    "tenant_a_sales_sep.xlsx",
    "tenant_a_week2.docx",
    "tenant_a_hr_contacts.xlsx",
    "tenant_b_sep.pdf",
    "scan_printed.png",
    "scan_printed.pdf",
    "handwritten_sheet.png",
    "handwritten_ambiguous.png",
    "conflict_note.pdf",
    "injection_memo.docx",
    "other_product.csv",
    "corrupt.xlsx",
    "empty.csv",
]


def _dump(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def generate(root: Path = ROOT / "data") -> dict:
    spec = load_spec()
    gen, gt = root / "generated", root / "ground_truth"
    gen.mkdir(parents=True, exist_ok=True)
    gt.mkdir(parents=True, exist_ok=True)

    truth = build_truth(spec)
    rows, idx = truth["rows"], index_rows(truth["rows"])
    a = [r for r in rows if r["tenant_id"] == "tenant_a"]
    b = [r for r in rows if r["tenant_id"] == "tenant_b"]
    a_eng_hr = [r for r in a if r["entity_id"] in ("engineering", "hr")]
    a_sales = [r for r in a if r["entity_id"] == "sales"]
    errors = {(e["employee_id"], e["date"]) for e in spec["v1_errors"]}
    lucas_gap = {("E011", "2026-09-30")}  # missing from the xlsx; only on the messy scan

    manifests = [
        write_attendance_csv(
            gen / "tenant_a_sep.csv",
            a_eng_hr,
            logical_name="tenant_a_sep",
            version=1,
            errors=errors,
        ),
        write_attendance_csv(
            gen / "tenant_a_sep_v2.csv", a_eng_hr, logical_name="tenant_a_sep", version=2
        ),
        write_sales_xlsx(gen / "tenant_a_sales_sep.xlsx", a_sales, skip_keys=lucas_gap),
        write_week2_docx(gen / "tenant_a_week2.docx", idx, truth["employees"], spec),
        write_pii_xlsx(
            gen / "tenant_a_hr_contacts.xlsx",
            [r for r in a if r["entity_id"] == "hr" and r["attendance_date"] <= "2026-09-04"],
            truth["employees"],
        ),
        write_tenant_b_pdf(gen / "tenant_b_sep.pdf", b, spec),
        *write_printed_scan(
            gen / "scan_printed.png",
            gen / "scan_printed.pdf",
            [r for r in a_eng_hr if r["attendance_date"] == "2026-09-29"],
            day="2026-09-29",
            seed=spec["seed"] + 1,
        ),
        write_handwritten_sheet(
            gen / "handwritten_sheet.png",
            [r for r in a if r["entity_id"] == "hr" and r["attendance_date"] == "2026-09-30"],
            day="2026-09-30",
            seed=spec["seed"] + 2,
        ),
        write_handwritten_ambiguous(
            gen / "handwritten_ambiguous.png",
            [r for r in a_sales if r["attendance_date"] == "2026-09-30"],
            day="2026-09-30",
            seed=spec["seed"] + 3,
        ),
        write_conflict_note(gen / "conflict_note.pdf", idx, spec),
        write_injection_memo(gen / "injection_memo.docx"),
        write_other_product_csv(gen / "other_product.csv", truth["other_product_rows"]),
        *write_invalid_files(gen),
    ]
    # The CSV v1 manifest is flagged so tests know those rows get superseded.
    manifests[0].description += " (v1: contains 3 wrong rows, superseded by tenant_a_sep_v2.csv)"

    expected = exp.build_expected(truth, manifests, spec)
    exp.assert_unique_rankings(expected)

    files = {m.filename: m.to_dict() | {"sha256": sha256_file(gen / m.filename)} for m in manifests}
    assert sorted(files) == sorted(CORPUS_ORDER), "CORPUS_ORDER out of sync with generated files"

    _dump(
        gt / "ground_truth.json",
        {
            "employees": truth["employees"],
            "rows": rows,
            "other_product_rows": truth["other_product_rows"],
        },
    )
    _dump(gt / "manifests.json", {"corpus_order": CORPUS_ORDER, "files": files})
    _dump(gt / "expected_results.json", expected)
    return {"files": files, "expected": expected}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=ROOT / "data", help="data root to write into")
    args = ap.parse_args()
    result = generate(args.out)
    for name in CORPUS_ORDER:
        f = result["files"][name]
        print(f"{name:32} {f['format']:12} rows={len(f['rows']):4} sha256={f['sha256'][:12]}")
    print("evidence:", result["expected"]["evidence_summary"])


if __name__ == "__main__":
    main()
