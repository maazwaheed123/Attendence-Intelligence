"""Tesseract word-level OCR + table reconstruction from the header's column positions."""

import re
from dataclasses import dataclass, field

import numpy as np
import pytesseract
from rapidfuzz import process

from app.ingestion.normalize.mapping import SYNONYMS, map_header

_DATE = re.compile(r"\b(\d{1,2}[/.\-]\d{1,2}[/.\-]\d{4})\b")
_FOOTER = re.compile(r"^(supervisor|signature|signed|approved|total)", re.I)
COLUMN_TOLERANCE_PX = 40


@dataclass
class OcrCell:
    text: str
    conf: float


@dataclass
class OcrRow:
    row: int
    cells: dict[str, OcrCell]


@dataclass
class OcrTable:
    header: dict[str, str]
    rows: list[OcrRow]
    sheet_date: OcrCell | None
    mean_conf: float
    lines: list[str] = field(default_factory=list)


def _lines(gray: np.ndarray) -> list[list[dict]]:
    d = pytesseract.image_to_data(gray, config="--psm 6", output_type=pytesseract.Output.DICT)
    lines: dict[tuple, list[dict]] = {}
    for i, text in enumerate(d["text"]):
        if not text.strip():
            continue
        key = (d["block_num"][i], d["par_num"][i], d["line_num"][i])
        conf = max(0.0, float(d["conf"][i])) / 100
        lines.setdefault(key, []).append({"text": text.strip(), "conf": conf, "left": d["left"][i]})
    return [sorted(ws, key=lambda w: w["left"]) for _, ws in sorted(lines.items())]


_SINGLE_WORD_SYNONYMS = {
    syn: canon for canon, syns in SYNONYMS.items() for syn in syns if " " not in syn
}


def _map_ocr_header(word: str) -> str | None:
    """Exact synonym first; otherwise fuzzy (OCR reads 'Status' as 'Ctatus')."""
    exact = map_header(word)
    if exact or len(word) < 4:
        return exact
    match = process.extractOne(word.lower(), list(_SINGLE_WORD_SYNONYMS), score_cutoff=75)
    return _SINGLE_WORD_SYNONYMS[match[0]] if match else None


def _header(words: list[dict], fuzzy: bool = False) -> dict[str, int] | None:
    cols = {}
    mapper = _map_ocr_header if fuzzy else map_header
    for w in words:
        canon = mapper(w["text"].strip(":|"))
        if canon and canon != "restricted" and canon not in cols:
            cols[canon] = w["left"]
    has_identity = "employee_id" in cols or "employee_name" in cols
    return cols if has_identity and "status" in cols and len(cols) >= 3 else None


def read(gray: np.ndarray) -> OcrTable | None:
    lines = _lines(gray)
    text_lines = [" ".join(w["text"] for w in ws) for ws in lines]
    header_idx, cols = None, None
    for fuzzy in (False, True):
        for i, ws in enumerate(lines):
            if found := _header(ws, fuzzy=fuzzy):
                header_idx, cols = i, found
                break
        if cols:
            break
    if cols is None:
        return None

    sheet_date = None
    for ws in lines[:header_idx]:
        for w in ws:
            if m := _DATE.search(w["text"]):
                sheet_date = OcrCell(m.group(1), w["conf"])
                break

    ordered = sorted(cols.items(), key=lambda kv: kv[1])
    header_text = {
        c: next(w["text"] for w in lines[header_idx] if w["left"] == x) for c, x in ordered
    }
    rows, confs = [], []
    for ws in lines[header_idx + 1 :]:
        if _FOOTER.match(ws[0]["text"]):
            break
        cells: dict[str, list[dict]] = {}
        for w in ws:
            col = None
            for canon, x in ordered:
                if w["left"] + COLUMN_TOLERANCE_PX >= x:
                    col = canon
            if col:
                cells.setdefault(col, []).append(w)
        if len(cells) < 2:
            continue
        row = OcrRow(
            row=len(rows) + 2,
            cells={
                c: OcrCell(" ".join(w["text"] for w in words), min(w["conf"] for w in words))
                for c, words in cells.items()
            },
        )
        rows.append(row)
        confs += [w["conf"] for w in ws]
    return OcrTable(
        header=header_text,
        rows=rows,
        sheet_date=sheet_date,
        mean_conf=float(np.mean(confs)) if confs else 0.0,
        lines=text_lines,
    )
