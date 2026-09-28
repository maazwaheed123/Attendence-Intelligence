"""Renderers only format: same rows in, same rows out, for any content."""

import json

import pytest

from app.export import dataset, renderers
from tests.support.export import pdf_ids, pdf_text, xlsx_rows

pytestmark = pytest.mark.unit

RID = "00000000-0000-4000-8000-00000000000{}"


def _ds(rows: list[dict]) -> dataset.ExportDataset:
    rows = [{c: r.get(c) for c in dataset.COLUMNS} for r in rows]
    ids = [dataset.row_key(r) for r in rows]
    meta = {
        "request_id": "req_test",
        "source": {"type": "records"},
        "generated_at": "2026-09-28T00:00:00+00:00",
        "scope": {"tenant_id": "t"},
        "record_count": len(rows),
        "truncated": False,
        "checksum": dataset.checksum(ids),
        "classification": "confidential",
        "columns": list(dataset.COLUMNS),
    }
    return dataset.ExportDataset(metadata=meta, rows=rows)


def test_empty_dataset_renders_everywhere():
    ds = _ds([])
    assert json.loads(renderers.to_json(ds))["records"] == []
    assert xlsx_rows(renderers.to_xlsx(ds))[1] == []
    assert pdf_ids(renderers.to_pdf(ds)) == []


def test_markup_is_escaped_and_order_kept():
    rows = [
        {"row_type": "record", "record_id": RID.format(2), "employee_name": "<b>x</b> & y"},
        {"row_type": "document", "chunk_id": RID.format(1), "excerpt": "note <script>"},
    ]
    ds = _ds(rows)
    pdf = renderers.to_pdf(ds)
    assert pdf_ids(pdf) == [RID.format(2), RID.format(1)]
    text = pdf_text(pdf)
    assert "<b>x</b> & y" in text and "CONFIDENTIAL | request req_test" in text
    assert [dataset.row_key(r) for r in xlsx_rows(renderers.to_xlsx(ds))[1]] == ds.row_ids


def test_checksum_is_order_sensitive():
    assert dataset.checksum(["a", "b"]) != dataset.checksum(["b", "a"])
