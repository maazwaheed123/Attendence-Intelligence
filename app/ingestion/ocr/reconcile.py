"""Combine Tesseract and vision-LLM readings into RawRecords with honest confidence.

Confidence of a record = its weakest KEY field (identity, status). Per field:
  - engine confidence (Tesseract word confidence; vision self-report capped at 0.90)
  - fields the vision model marked uncertain            -> 0.40
  - engines read known-but-different statuses/IDs       -> <= 0.50 + reason
  - handwriting without the vision model                -> <= 0.60 (always review)
  - printed text exactly matching the status vocabulary / ID pattern -> >= 0.85
Secondary fields (times, department) below the threshold are DROPPED (set to None)
with a note rather than stored as facts. The normalizer then validates IDs/names
against the roster and statuses against the vocabulary (e.g. "P?" -> unknown).
"""

import re

from rapidfuzz import fuzz

from app.ingestion.normalize.status import map_status
from app.ingestion.ocr.tesseract_engine import OcrTable
from app.ingestion.ocr.vision_engine import VisionSheet
from app.ingestion.types import RawRecord

KEY_FIELDS = ("employee_id", "status")
SECONDARY = ("check_in", "check_out", "department")
FIELDS = ("employee_id", "employee_name", "department", "status", "check_in", "check_out")
PRINTED_CAP = 0.95
LEXICON_FLOOR = 0.85
_ID_PATTERN = re.compile(r"^[A-Z]\d{3,}$")
VISION_CAP = 0.90
NO_VISION_HANDWRITING_CAP = 0.60


def _norm_id(s):
    return (s or "").upper().replace("O", "0").replace(" ", "").replace("€", "E").replace("£", "E")


def _match_tesseract_row(vrow, table: OcrTable | None):
    if not table:
        return None
    best, best_score = None, 0
    for r in table.rows:
        score = 0
        if vrow.employee_id and "employee_id" in r.cells:
            score = max(
                score, fuzz.ratio(_norm_id(vrow.employee_id), _norm_id(r.cells["employee_id"].text))
            )
        if vrow.employee_name and "employee_name" in r.cells:
            score = max(
                score, fuzz.token_sort_ratio(vrow.employee_name, r.cells["employee_name"].text)
            )
        if score > best_score:
            best, best_score = r, score
    return best if best_score >= 70 else None


def reconcile(
    table: OcrTable | None,
    vision: VisionSheet | None,
    *,
    handwriting: bool,
    threshold: float,
    page: int = 1,
) -> tuple[list[RawRecord], dict]:
    info = {"engine": None, "sheet_date": None}
    t_date = (
        table.sheet_date.text
        if table and table.sheet_date and table.sheet_date.conf >= 0.6
        else None
    )
    v_date = vision.sheet_date if vision else None
    sheet_date = (v_date or t_date) if handwriting else (t_date or v_date)
    date_reasons = []
    if t_date and v_date and t_date.replace(".", "/") != v_date.replace(".", "/"):
        date_reasons.append(f"OCR engines disagree on sheet date ('{t_date}' vs '{v_date}')")
    info["sheet_date"] = sheet_date

    records: list[RawRecord] = []
    if vision and (handwriting or not table):
        info["engine"] = "ocr_reconciled" if table else "ocr_vision"
        for i, v in enumerate(vision.rows, start=2):
            t_row = _match_tesseract_row(v, table)
            conf = {f: min(v.confidence, VISION_CAP) for f in FIELDS}
            reasons = list(date_reasons)
            for f in v.uncertain_fields:
                if f in conf:
                    conf[f] = 0.4
                    reasons.append(f"{f} unclear on the sheet")
            if v.notes:
                reasons.append(f"transcriber note: {v.notes}")
            if t_row and "status" in t_row.cells and v.status:
                t_status, t_known = map_status(t_row.cells["status"].text)
                v_status, v_known = map_status(v.status)
                if (
                    t_known
                    and v_known
                    and t_status != v_status
                    and t_row.cells["status"].conf >= 0.5
                ):
                    conf["status"] = min(conf["status"], 0.5)
                    reasons.append(
                        "OCR engines disagree on status "
                        f"('{t_row.cells['status'].text}' vs '{v.status}')"
                    )
                elif t_known and v_known:
                    conf["status"] = min(0.95, conf["status"] + 0.05)
            values = {f: getattr(v, f, None) for f in FIELDS}
            records.append(
                _record(page, i, values, conf, reasons, sheet_date, threshold, raw_engine="vision")
            )
    elif table:
        info["engine"] = "ocr_tesseract"
        cap = PRINTED_CAP if not handwriting else NO_VISION_HANDWRITING_CAP
        for r in table.rows:
            values = {f: (r.cells[f].text if f in r.cells else None) for f in FIELDS}
            conf = {f: min(r.cells[f].conf, cap) if f in r.cells else 1.0 for f in FIELDS}
            reasons = list(date_reasons)
            if handwriting:
                reasons.append("handwriting read by Tesseract only (vision OCR unavailable)")
            if not handwriting:
                if (
                    values["status"]
                    and map_status(values["status"])[1]
                    and values["status"].isalpha()
                ):
                    conf["status"] = max(conf["status"], LEXICON_FLOOR)
                if values["employee_id"] and _ID_PATTERN.match(values["employee_id"]):
                    conf["employee_id"] = max(conf["employee_id"], LEXICON_FLOOR)
            ident = max(
                conf["employee_id"] if values["employee_id"] else 0,
                conf["employee_name"] if values["employee_name"] else 0,
            )
            conf["employee_id"] = ident
            records.append(
                _record(
                    page,
                    r.row,
                    values,
                    conf,
                    reasons,
                    sheet_date,
                    threshold,
                    raw_engine="tesseract",
                )
            )
    return records, info


def _record(page, row, values, conf, reasons, sheet_date, threshold, raw_engine) -> RawRecord:
    raw = {k: v for k, v in values.items() if v is not None}
    raw["_engine"] = raw_engine
    raw["_field_confidence"] = {
        k: round(c, 2) for k, c in conf.items() if values.get(k) is not None
    }
    for f in SECONDARY:
        if values.get(f) is not None and conf[f] < threshold:
            reasons.append(f"info: {f} unreadable ('{values[f]}'), not stored")
            values[f] = None
    key_conf = min(conf[f] for f in KEY_FIELDS)
    return RawRecord(
        locator=f"page={page};row={row}",
        values={**values, "attendance_date": sheet_date},
        raw=raw,
        confidence=round(key_conf, 3),
        notes=reasons,
    )
