"""JSON, XLSX and PDF renderers over one ExportDataset.

The renderers only format: they never add, drop or reorder rows, so the three
files carry identical record lists and the same checksum. The PDF footer
repeats the classification and request id on every page.
"""

import io
import json

from openpyxl import Workbook
from openpyxl.styles import Font
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import LongTable, Paragraph, SimpleDocTemplate, Spacer, TableStyle

from app.export.dataset import COLUMNS, ExportDataset

MEDIA_TYPES = {
    "json": "application/json",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pdf": "application/pdf",
}


def to_json(ds: ExportDataset) -> bytes:
    body = {"metadata": ds.metadata, "records": ds.rows}
    return json.dumps(body, indent=2, ensure_ascii=False).encode()


def _meta_pairs(ds: ExportDataset) -> list[tuple[str, str]]:
    m = ds.metadata
    pairs = [
        ("request_id", m["request_id"]),
        ("source", json.dumps(m["source"], ensure_ascii=False)),
        ("generated_at", m["generated_at"]),
        ("scope", json.dumps(m["scope"], ensure_ascii=False)),
        ("record_count", str(m["record_count"])),
        ("truncated", str(m["truncated"]).lower()),
        ("checksum", m["checksum"]),
        ("classification", m["classification"].upper()),
    ]
    return pairs


def to_xlsx(ds: ExportDataset) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Records"
    ws.append(list(COLUMNS))
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for r in ds.rows:
        ws.append([r[c] for c in COLUMNS])
    ws.freeze_panes = "A2"
    meta = wb.create_sheet("Metadata")
    for k, v in _meta_pairs(ds):
        meta.append([k, v])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# PDF: the columns that fit a landscape A4 page; "id" = record_id or chunk_id.
_PDF_COLS = (  # (column, header label, width)
    ("id", "record / chunk id", 58 * mm),
    ("attendance_date", "date", 18 * mm),
    ("employee_id", "emp", 11 * mm),
    ("employee_name", "name", 28 * mm),
    ("department", "department", 25 * mm),
    ("status", "status", 18 * mm),
    ("check_in", "in", 11 * mm),
    ("check_out", "out", 11 * mm),
    ("total_hours", "hours", 11 * mm),
    ("source_file", "source file", 38 * mm),
    ("source_locator", "locator", 38 * mm),
    ("extraction_confidence", "conf", 10 * mm),
)
_CELL = ParagraphStyle("cell", fontName="Helvetica", fontSize=6, leading=7)
_HEAD = ParagraphStyle("head", parent=_CELL, fontName="Helvetica-Bold")
_META = ParagraphStyle("meta", fontName="Helvetica", fontSize=7, leading=9)
_TITLE = ParagraphStyle("title", fontName="Helvetica-Bold", fontSize=12, leading=15)


def _esc(v) -> str:
    s = "" if v is None else str(v)
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _pdf_cell(r: dict, col: str) -> str:
    if col == "id":
        return r["record_id"] or r["chunk_id"]
    v = r[col]
    if col == "status" and r["conflict"] and v != "conflict":
        v = f"{v} (conflict)"
    if col in ("check_in", "check_out") and isinstance(v, str):
        v = v[:5]
    return _esc(v)


def to_pdf(ds: ExportDataset) -> bytes:
    m = ds.metadata
    footer = f"{m['classification'].upper()} | request {m['request_id']}"
    buf = io.BytesIO()

    def _page(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.drawString(10 * mm, 6 * mm, footer)
        canvas.drawRightString(doc.pagesize[0] - 10 * mm, 6 * mm, f"page {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(
        buf,
        pagesize=landscape(A4),
        leftMargin=8 * mm,
        rightMargin=8 * mm,
        topMargin=10 * mm,
        bottomMargin=12 * mm,
        title=f"Attendance export {m['request_id']}",
        subject=f"checksum {m['checksum']}",
    )
    story = [Paragraph("Attendance export", _TITLE)]
    story += [Paragraph(f"<b>{k}</b>: {_esc(v)}", _META) for k, v in _meta_pairs(ds)]
    story.append(Spacer(1, 4 * mm))
    data = [[Paragraph(label, _HEAD) for _, label, _ in _PDF_COLS]]
    data += [[Paragraph(_pdf_cell(r, c), _CELL) for c, _, _ in _PDF_COLS] for r in ds.rows]
    table = LongTable(data, colWidths=[w for _, _, w in _PDF_COLS], repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    story.append(table)
    excerpts = [(r, r["excerpt"]) for r in ds.rows if r["excerpt"]]
    if excerpts:
        story += [Spacer(1, 4 * mm), Paragraph("Document excerpts", _TITLE)]
        for r, ex in excerpts:
            story.append(Paragraph(f"<b>{_esc(r['source_locator'])}</b>: {_esc(ex)}", _META))
    doc.build(story, onFirstPage=_page, onLaterPages=_page)
    return buf.getvalue()


RENDERERS = {"json": to_json, "xlsx": to_xlsx, "pdf": to_pdf}
