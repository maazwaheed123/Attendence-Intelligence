from pathlib import Path

import numpy as np
import pytest

from app.ingestion.ocr import preprocess
from app.ingestion.ocr.reconcile import reconcile
from app.ingestion.ocr.tesseract_engine import OcrCell, OcrRow, OcrTable
from app.ingestion.ocr.vision_engine import VisionRow, VisionSheet

pytestmark = [pytest.mark.unit, pytest.mark.ocr]
GEN = Path(__file__).resolve().parents[2] / "data" / "generated"


def test_deskew_detects_scan_rotation():
    gray = preprocess.load_gray((GEN / "scan_printed.png").read_bytes())
    assert abs(abs(preprocess.estimate_skew(gray)) - 1.8) < 0.3  # generator rotated by 1.8 deg


def test_grid_lines_removed():
    img = np.full((400, 800), 255, np.uint8)
    img[200, 20:780] = 0  # long horizontal rule
    img[190:210, 100:104] = 0  # short glyph-like stroke
    out = preprocess.remove_lines(img)
    assert out[200, 400] == 255 and out[195, 101] == 0


def _table(status="Present", conf=0.9):
    return OcrTable(
        header={"employee_id": "ID", "status": "Status"},
        rows=[
            OcrRow(
                2,
                {
                    "employee_id": OcrCell("E001", conf),
                    "employee_name": OcrCell("Alice Johnson", conf),
                    "status": OcrCell(status, conf),
                },
            )
        ],
        sheet_date=OcrCell("30/09/2026", 0.9),
        mean_conf=conf,
    )


def test_engines_agree_raises_confidence():
    v = VisionSheet(
        sheet_date="30/09/2026",
        rows=[
            VisionRow(
                employee_id="E001", employee_name="Alice Johnson", status="Present", confidence=0.85
            )
        ],
    )
    (rec,), info = reconcile(_table(), v, handwriting=True, threshold=0.75)
    assert (
        info["engine"] == "ocr_reconciled" and rec.confidence == 0.85
    )  # min(id 0.85, status 0.90)


def test_engines_disagree_lowers_confidence():
    v = VisionSheet(
        sheet_date="30/09/2026",
        rows=[VisionRow(employee_id="E001", status="Absent", confidence=0.9)],
    )
    (rec,), _ = reconcile(_table("Present", 0.9), v, handwriting=True, threshold=0.75)
    assert rec.confidence <= 0.5 and any("disagree on status" in n for n in rec.notes)


def test_uncertain_fields_cap_confidence():
    v = VisionSheet(
        sheet_date="30/09/2026",
        rows=[
            VisionRow(employee_id="E01?", status="P?", confidence=0.9, uncertain_fields=["status"])
        ],
    )
    (rec,), _ = reconcile(None, v, handwriting=True, threshold=0.75)
    assert rec.confidence == 0.4 and rec.values["status"] == "P?"


def test_low_confidence_time_dropped_not_stored():
    t = _table()
    t.rows[0].cells["check_in"] = OcrCell("O9:1?", 0.2)
    (rec,), _ = reconcile(t, None, handwriting=False, threshold=0.75)
    assert rec.values["check_in"] is None and any("check_in unreadable" in n for n in rec.notes)
    assert rec.raw["check_in"] == "O9:1?"  # raw OCR text kept for review


def test_handwriting_without_vision_capped():
    (rec,), info = reconcile(_table(conf=0.95), None, handwriting=True, threshold=0.75)
    assert info["engine"] == "ocr_tesseract" and rec.confidence <= 0.6


def test_sheet_date_disagreement_noted():
    v = VisionSheet(
        sheet_date="29/09/2026",
        rows=[VisionRow(employee_id="E001", status="Present", confidence=0.9)],
    )
    (rec,), info = reconcile(_table(), v, handwriting=True, threshold=0.75)
    assert info["sheet_date"] == "29/09/2026" and any("sheet date" in n for n in rec.notes)
