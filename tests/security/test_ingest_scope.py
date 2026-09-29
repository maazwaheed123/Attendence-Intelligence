"""Ingestion can only create data inside the uploader's scope."""

import pytest

from tests.support.ingest import records, upload

pytestmark = [pytest.mark.security, pytest.mark.integration]


def test_rows_outside_entity_scope_become_failures(ingest_api, auth):
    """Engineering manager uploads the Engineering+HR file: HR rows are refused."""
    body = upload(ingest_api, auth("a_eng_manager"), "tenant_a_sep_v2.csv").json()
    assert body["status"] == "completed"
    assert body["counts"]["records_created"] == 110
    assert body["counts"]["row_failures"] == 66
    assert all("not found in your permitted scope" in f["reason"] for f in body["failures"])
    assert {r["entity_id"] for r in records()} == {"engineering"}


def test_records_always_get_token_tenant(ingest_api, auth):
    upload(ingest_api, auth("b_manager"), "tenant_b_sep.pdf")
    upload(ingest_api, auth("a_hr_admin"), "tenant_a_sales_sep.xlsx")
    by_file = {(r["source_file"], r["tenant_id"], r["product_id"]) for r in records()}
    assert by_file == {
        ("tenant_b_sep.pdf", "tenant_b", "attendance_ai"),
        ("tenant_a_sales_sep.xlsx", "tenant_a", "attendance_ai"),
    }


def test_tenant_b_cannot_ingest_tenant_a_employees(ingest_api, auth):
    body = upload(ingest_api, auth("b_manager"), "tenant_a_sep_v2.csv").json()
    assert body["status"] == "failed"
    assert records() == []


@pytest.mark.parametrize("persona", ["a_employee_e001", "a_reviewer", "a_auditor"])
def test_roles_without_ingest_permission(ingest_api, auth, persona):
    r = upload(ingest_api, auth(persona), "tenant_a_sep_v2.csv")
    assert r.status_code == 403 and r.json()["error"]["code"] == "FORBIDDEN"


@pytest.mark.parametrize(
    "form", [{"tenant_id": "tenant_b"}, {"product_id": "hrms_ai"}, {"entity_id": "hr"}]
)
def test_form_context_cannot_redirect_ingestion(ingest_api, auth, form):
    r = upload(ingest_api, auth("a_eng_manager"), "tenant_a_sep_v2.csv", **form)
    assert r.status_code == 403 and r.json()["error"]["code"] == "CONTEXT_MISMATCH"


def test_jobs_are_tenant_scoped(ingest_api, auth):
    body = upload(ingest_api, auth("a_hr_admin"), "tenant_a_sep_v2.csv").json()
    assert (
        ingest_api.get(f"/v1/jobs/{body['job_id']}", headers=auth("b_manager")).status_code == 404
    )
    assert ingest_api.get("/v1/jobs", headers=auth("b_manager")).json()["jobs"] == []
    assert ingest_api.get("/v1/documents", headers=auth("b_manager")).json()["documents"] == []
    assert (
        ingest_api.get(f"/v1/jobs/{body['job_id']}", headers=auth("a_hr_admin")).status_code == 200
    )


def test_same_file_in_two_tenants_is_independent(ingest_api, auth):
    """Checksums are unique per tenant, so tenant B's upload is not 'a duplicate'."""
    a = upload(ingest_api, auth("a_hr_admin"), "tenant_a_sep_v2.csv").json()
    b = upload(ingest_api, auth("b_manager"), "tenant_a_sep_v2.csv").json()
    assert a["status"] == "completed" and b["status"] != "duplicate"
