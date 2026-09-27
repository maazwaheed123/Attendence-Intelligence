"""CSV + XLSX ingestion: canonical values, traceability, restricted columns."""

import pytest

from tests.support.ingest import records, upload

pytestmark = pytest.mark.integration


def _truth_idx(truth):
    return {
        (r["tenant_id"], r["product_id"], r["employee_id"], r["attendance_date"]): r
        for r in truth["rows"] + truth["other_product_rows"]
    }


def _assert_matches_manifest(recs, manifest, truth, product="attendance_ai", times=True):
    """Every manifest row became exactly one record with the right values and locator."""
    tidx = _truth_idx(truth)
    by_loc = {r["source_locator"]: r for r in recs}
    assert set(by_loc) == {row["locator"] for row in manifest["rows"]}
    for row in manifest["rows"]:
        rec = by_loc[row["locator"]]
        t = tidx[(row["tenant_id"], product, row["employee_id"], row["attendance_date"])]
        assert rec["employee_id"] == row["employee_id"]
        assert rec["attendance_date"].isoformat() == row["attendance_date"]
        assert rec["status"] == row["status"]
        assert rec["entity_id"] == t["entity_id"] and rec["department"] == t["department"]
        if times and row["matches_truth"]:
            assert (rec["check_in"].strftime("%H:%M") if rec["check_in"] else None) == t["check_in"]
            assert (rec["check_out"].strftime("%H:%M") if rec["check_out"] else None) == t[
                "check_out"
            ]
            assert (float(rec["total_hours"]) if rec["total_hours"] is not None else None) == t[
                "total_hours"
            ]
        assert rec["source_file"] == manifest["filename"]
        assert rec["tenant_id"] == manifest["tenant_id"] and rec["product_id"] == product
        assert not rec["review_required"]


def test_csv(ingest_api, auth, manifests, truth):
    r = upload(ingest_api, auth("a_hr_admin"), "tenant_a_sep_v2.csv")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "completed" and body["completed"]
    assert body["counts"]["records_created"] == 176 and body["counts"]["row_failures"] == 0
    assert body["file_type"] == "csv" and len(body["checksum_sha256"]) == 64
    assert body["logical_name"] == "tenant_a_sep" and body["version"] == 1
    assert [h["stage"] for h in body["stage_history"]] == [
        "received",
        "validated",
        "parsed",
        "normalized",
        "persisted",
        "completed",
    ]
    recs = records("source_file = 'tenant_a_sep_v2.csv'")
    _assert_matches_manifest(recs, manifests["files"]["tenant_a_sep_v2.csv"], truth)
    assert all(
        r["extraction_method"] == "csv" and float(r["extraction_confidence"]) == 0.99 for r in recs
    )


def test_xlsx_two_sheets_messy_layout(ingest_api, auth, manifests, truth):
    r = upload(ingest_api, auth("a_hr_admin"), "tenant_a_sales_sep.xlsx")
    body = r.json()
    assert body["status"] == "completed", body
    assert body["counts"]["records_created"] == 87
    assert body["counts"]["skipped_rows"] == 1  # "Total present days" row
    recs = records("source_file = 'tenant_a_sales_sep.xlsx'")
    _assert_matches_manifest(recs, manifests["files"]["tenant_a_sales_sep.xlsx"], truth)
    assert {r["source_locator"].split(";")[0] for r in recs} == {
        "sheet=Sep 1-15",
        "sheet=Sep 16-30",
    }


def test_restricted_columns_dropped(ingest_api, auth, manifests, truth):
    body = upload(ingest_api, auth("a_hr_admin"), "tenant_a_hr_contacts.xlsx").json()
    assert body["status"] == "completed"
    assert any("restricted columns dropped" in w and "Phone" in w for w in body["warnings"])
    recs = records("source_file = 'tenant_a_hr_contacts.xlsx'")
    _assert_matches_manifest(
        recs, manifests["files"]["tenant_a_hr_contacts.xlsx"], truth, times=False
    )
    for rec in recs:
        raw = str(rec["raw_values"])
        assert "555" not in raw and "NID-" not in raw and "@" not in raw
        assert set(rec["raw_values"]) == {
            "Employee ID",
            "Full Name",
            "Department",
            "Date",
            "Status",
        }


def test_other_product(ingest_api, auth, manifests, truth):
    body = upload(ingest_api, auth("x_other_product"), "other_product.csv").json()
    assert body["counts"]["records_created"] == 4
    recs = records("source_file = 'other_product.csv'")
    _assert_matches_manifest(
        recs, manifests["files"]["other_product.csv"], truth, product="hrms_ai"
    )


def test_raw_values_kept_for_traceability(ingest_api, auth):
    upload(ingest_api, auth("a_hr_admin"), "tenant_a_sales_sep.xlsx")
    rec = records("source_locator = 'sheet=Sep 16-30;row=3'")[0]
    assert rec["raw_values"]["Status"] in {"P", "A", "L", "WFH", "HD", "H"}
    assert rec["raw_values"]["Day"].endswith("-Sep-2026")


def test_traceability_endpoint(ingest_api, auth):
    body = upload(ingest_api, auth("a_hr_admin"), "tenant_a_sep_v2.csv").json()
    r = ingest_api.get(
        f"/v1/documents/{body['document_id']}/records",
        headers=auth("a_eng_manager"),
        params={"limit": 1000},
    )
    recs = r.json()["records"]
    # Engineering manager sees only engineering rows of the multi-department file.
    assert recs and {x["department"] for x in recs} == {"Engineering"}
    assert all(
        x["source_file"] == "tenant_a_sep_v2.csv" and x["source_locator"].startswith("row=")
        for x in recs
    )
