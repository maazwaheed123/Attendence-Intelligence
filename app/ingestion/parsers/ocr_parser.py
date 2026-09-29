"""Images and scanned PDFs -> RawRecords via preprocess -> Tesseract (+ vision) -> reconcile.

Printed vs handwritten is decided from Tesseract's mean word confidence: printed
registers are read by Tesseract (fast, deterministic); handwriting goes to the
vision model, cross-checked against Tesseract. Scanned PDFs are rendered at 200 dpi
and processed page by page.
"""

import pymupdf as fitz

from app.config import get_settings
from app.ingestion.ocr import preprocess, tesseract_engine, vision_engine
from app.ingestion.ocr.reconcile import reconcile
from app.ingestion.types import ParseResult, PermanentError, TransientError

PRINTED_MEAN_CONF = 0.80


class _State:
    vision_unavailable = False


def _page(content: bytes, page: int, result: ParseResult, state: _State) -> str | None:
    s = get_settings()
    gray, prep = preprocess.prepare(content)
    table = tesseract_engine.read(gray)
    handwriting = table is None or table.mean_conf < PRINTED_MEAN_CONF
    vision, vinfo = None, {"vision": "not_needed"}
    if handwriting:
        vision, vinfo = vision_engine.read(preprocess.to_png(gray))
        if vision is None:
            state.vision_unavailable = True
            tried = ", ".join(f"{a['provider']}={a['error']}" for a in vinfo.get("attempts", []))
            result.warnings.append(
                f"page {page}: vision OCR unavailable ({tried}); "
                "handwriting read by Tesseract only, all rows flagged for review"
            )
    records, info = reconcile(
        table, vision, handwriting=handwriting, threshold=s.ocr_review_threshold, page=page
    )
    result.records.extend(records)
    kind = "handwritten" if handwriting else "printed"
    if table:
        result.warnings.append(
            f"page {page}: {kind} sheet, engine={info['engine']}, "
            f"tesseract_mean_conf={table.mean_conf:.2f}, deskew={prep['deskew_deg']}deg"
        )
    else:
        result.warnings.append(f"page {page}: no table found by Tesseract, engine={info['engine']}")
    if not info["sheet_date"] and records:
        result.warnings.append(f"page {page}: no sheet date found; rows need a date to be stored")
    return info["engine"]


def _method(engines: set) -> str:
    engines.discard(None)
    if not engines:
        return "ocr_tesseract"
    return engines.pop() if len(engines) == 1 else "ocr_reconciled"


def _no_records(state: _State, what: str):
    if state.vision_unavailable:
        raise TransientError(
            f"no attendance table could be read from the {what} (vision OCR unavailable)"
        )
    raise PermanentError(f"no attendance table could be read from the {what}")


def parse(content: bytes, filename: str) -> ParseResult:
    result, state = ParseResult(method="ocr_tesseract"), _State()
    engines = {_page(content, 1, result, state)}
    result.method = _method(engines)
    if not result.records:
        _no_records(state, "image")
    return result


def parse_scanned_pdf(content: bytes, filename: str) -> ParseResult:
    result, state = ParseResult(method="ocr_tesseract"), _State()
    engines = set()
    with fitz.open(stream=content, filetype="pdf") as doc:
        for p_no, page in enumerate(doc, start=1):
            png = page.get_pixmap(dpi=200).tobytes("png")
            engines.add(_page(png, p_no, result, state))
    result.method = _method(engines)
    if not result.records:
        _no_records(state, "scanned PDF")
    return result
