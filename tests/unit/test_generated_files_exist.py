import json
import zipfile

import pytest
from PIL import Image

from scripts.generate_data import CORPUS_ORDER

pytestmark = pytest.mark.unit

REQUIRED_CATEGORIES = {
    "flat file": {"csv"},
    "xlsx": {"xlsx"},
    "docx or text pdf": {"docx", "pdf"},
    "scanned / handwritten": {"image", "pdf_scanned"},
}


def test_every_corpus_file_exists(data_dir):
    for name in CORPUS_ORDER:
        path = data_dir / "generated" / name
        assert path.exists(), name


def test_all_four_required_input_categories_present(manifests):
    formats = {m["format"] for m in manifests["files"].values() if m["kind"] == "attendance"}
    for category, accepted in REQUIRED_CATEGORIES.items():
        assert formats & accepted, f"missing input category: {category}"


def test_images_are_real_scans(data_dir):
    for name in ("scan_printed.png", "handwritten_sheet.png", "handwritten_ambiguous.png"):
        img = Image.open(data_dir / "generated" / name)
        assert img.size[0] > 1000 and img.size[1] > 1500
        lo, hi = img.convert("L").getextrema()
        assert hi - lo > 150, f"{name} looks blank"


def test_scanned_pdf_has_no_text_layer(data_dir):
    import pdfplumber

    with pdfplumber.open(data_dir / "generated" / "scan_printed.pdf") as pdf:
        assert (pdf.pages[0].extract_text() or "").strip() == ""
        assert pdf.pages[0].images, "scanned PDF should contain an image"


def test_invalid_files_are_invalid(data_dir):
    assert (data_dir / "generated" / "empty.csv").stat().st_size == 0
    assert not zipfile.is_zipfile(data_dir / "generated" / "corrupt.xlsx")


def test_canonical_schema_covers_truth_fields(data_dir, truth):
    schema = json.loads((data_dir / "schema" / "canonical_attendance.schema.json").read_text())
    props = schema["properties"]
    business = {k for k in truth["rows"][0] if k != "evidence_status"}
    assert business <= set(props), business - set(props)
    statuses = {r["status"] for r in truth["rows"]}
    assert statuses <= set(props["status"]["enum"])


def test_ambiguity_and_conflict_fixtures_exist(truth, manifests):
    by_status = {}
    for r in truth["rows"]:
        by_status.setdefault(r["evidence_status"], []).append(
            (r["employee_id"], r["attendance_date"])
        )
    assert by_status["review_only"] == [("E011", "2026-09-30")]
    assert by_status["conflict"] == [("E002", "2026-09-15")]
    amb = manifests["files"]["handwritten_ambiguous.png"]["rows"]
    assert {r["employee_id"] for r in amb if r["expected_review"]} == {"E009", "E011"}
