"""Text-based PDF: tables per page -> records; text outside tables -> narrative.

Repeated page headers/footers and page numbers are removed. A PDF without a text
layer is a scan and is routed to OCR instead.
"""

import io

import pdfplumber
from pdfminer.pdfparser import PDFSyntaxError

from app.ingestion.cleaning import remove_repeated_lines
from app.ingestion.parsers.tabular import extract_rows
from app.ingestion.types import NarrativeBlock, ParseResult, PermanentError

MIN_TEXT_CHARS_PER_PAGE = 20


def is_scanned(content: bytes) -> bool:
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        chars = sum(len(p.chars) for p in pdf.pages)
        return chars < MIN_TEXT_CHARS_PER_PAGE * max(1, len(pdf.pages))


def parse(content: bytes, filename: str) -> ParseResult:
    try:
        pdf = pdfplumber.open(io.BytesIO(content))
    except (PDFSyntaxError, ValueError) as exc:
        raise PermanentError(f"could not open PDF: {exc.__class__.__name__}") from exc

    result = ParseResult(method="pdf_text")
    with pdf:
        if sum(len(p.chars) for p in pdf.pages) < MIN_TEXT_CHARS_PER_PAGE * max(1, len(pdf.pages)):
            from app.ingestion.parsers.ocr_parser import parse_scanned_pdf

            return parse_scanned_pdf(content, filename)

        page_lines: list[list[str]] = []
        for p_no, page in enumerate(pdf.pages, start=1):
            tables = page.find_tables()
            for t_no, table in enumerate(tables, start=1):
                rows = [[(c or "").strip() for c in row] for row in table.extract()]
                extract_rows(
                    rows,
                    result,
                    lambda i, p=p_no, t=t_no: f"page={p};table={t};row={i + 1}",
                    source_label=f"page {p_no} table {t_no}",
                )
            outside = page
            for table in tables:
                outside = outside.outside_bbox(table.bbox)
            page_lines.append((outside.extract_text() or "").splitlines())

    cleaned, removed = remove_repeated_lines(page_lines)
    if removed:
        result.warnings.append(f"removed {len(removed)} repeated header/footer/page-number line(s)")
    for p_no, lines in enumerate(cleaned, start=1):
        for para_no, text in enumerate(_paragraphs(lines), start=1):
            result.narrative.append(
                NarrativeBlock(
                    locator=f"page={p_no};para={para_no}", section=f"Page {p_no}", text=text
                )
            )
    if not result.records and not result.narrative:
        raise PermanentError("PDF contains no attendance table and no text")
    return result


def _paragraphs(lines: list[str]) -> list[str]:
    """Join wrapped lines into paragraphs (a line ending with '.' closes one)."""
    paras, cur = [], []
    for line in lines:
        cur.append(line)
        if line.endswith((".", ":", "!", "?")) or len(line) < 40:
            paras.append(" ".join(cur))
            cur = []
    if cur:
        paras.append(" ".join(cur))
    return [p for p in paras if p.strip()]
