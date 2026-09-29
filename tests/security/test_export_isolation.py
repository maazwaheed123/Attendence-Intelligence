"""Exports never contain data outside the caller's product/tenant/entity/role scope."""

import pytest
from sqlalchemy import text

from app.db.session import owner_session
from app.export import dataset
from app.security import pii
from tests.support.export import UUID, export, json_rows, pdf_text, xlsx_rows
from tests.support.llm import ask

pytestmark = [pytest.mark.security, pytest.mark.e2e]


def _owner_rows(ids: list[str]) -> list:
    with owner_session() as s:
        return s.execute(
            text(
                "SELECT product_id, tenant_id, entity_id, employee_id FROM attendance_records "
                "WHERE record_id = ANY(CAST(:ids AS uuid[]))"
            ),
            {"ids": ids},
        ).all()


def _ids(api, auth, persona, **body):
    r = export(api, auth, persona, source="records", **body)
    assert r.status_code == 200, r.text
    return [x["record_id"] for x in json_rows(r.content)[1]]


def test_tenant_a_export_has_no_tenant_b_rows(api, auth):
    ids = _ids(api, auth, "a_hr_admin")
    rows = _owner_rows(ids)
    assert len(rows) == len(ids) > 0
    assert {r.tenant_id for r in rows} == {"tenant_a"}
    assert len({r.product_id for r in rows}) == 1
    assert not {r.employee_id for r in rows} & {"E101", "E102", "E103", "E104", "E105", "E106"}


def test_tenant_b_export_only_tenant_b(api, auth):
    rows = _owner_rows(_ids(api, auth, "b_manager"))
    assert rows and {r.tenant_id for r in rows} == {"tenant_b"}


def test_eng_manager_export_only_engineering(api, auth):
    rows = _owner_rows(_ids(api, auth, "a_eng_manager"))
    assert rows and {r.entity_id for r in rows} == {"engineering"}


def test_other_product_export_only_its_product(api, auth):
    a = _owner_rows(_ids(api, auth, "a_hr_admin"))
    x = _owner_rows(_ids(api, auth, "x_other_product"))
    assert x and len({r.product_id for r in x}) == 1
    assert {r.product_id for r in x}.isdisjoint({r.product_id for r in a})


def test_employee_filter_outside_entity_is_empty(api, auth):
    assert _ids(api, auth, "a_eng_manager", filters={"employee_id": "E005"}) == []
    assert _ids(api, auth, "a_eng_manager", filters={"employee_id": "E999"}) == []


@pytest.mark.parametrize("persona", ["a_employee_e001", "a_reviewer", "a_auditor"])
def test_roles_without_export_permission(api, auth, persona):
    assert export(api, auth, persona, source="records").status_code == 403


def test_unauthenticated(api):
    assert api.post("/v1/export", json={"source": "records"}).status_code == 401


def test_entity_filter_outside_scope_is_403(api, auth):
    r = export(api, auth, "a_eng_manager", source="records", filters={"entity_id": "hr"})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "CONTEXT_MISMATCH"


def test_other_tenants_request_id_is_404_like_unknown(api, auth):
    b_rid = ask(api, auth, "b_manager", "Who was absent on 2 September 2026?")["request_id"]
    r = export(api, auth, "a_hr_admin", source="query", request_id=b_rid)
    unknown = export(api, auth, "a_hr_admin", source="query", request_id="req_does_not_exist")
    assert r.status_code == unknown.status_code == 404
    strip = lambda j: {k: v for k, v in j["error"].items() if k != "request_id"}  # noqa: E731
    assert strip(r.json()) == strip(unknown.json())


def test_other_entity_request_id_is_404(api, auth):
    hr_rid = ask(api, auth, "a_hr_admin", "Who was present on 1 September 2026?")["request_id"]
    r = export(api, auth, "a_eng_manager", source="query", request_id=hr_rid)
    assert r.status_code == 404


def test_invalid_status_filter_rejected(api, auth):
    r = export(api, auth, "a_hr_admin", source="records", filters={"status": "x' OR 1=1"})
    assert r.status_code == 422


def test_no_pii_in_any_format(api, auth):
    for fmt in ("json", "xlsx", "pdf"):
        r = export(api, auth, "a_hr_admin", fmt, source="records")
        if fmt == "pdf":
            words = UUID.sub("", pdf_text(r.content)).split()
            assert not [w for w in words if pii.contains_pii(w)], fmt
            continue
        else:
            rows = (json_rows if fmt == "json" else xlsx_rows)(r.content)[1]
            body = " ".join(str(v) for row in rows for k, v in row.items() if not k.endswith("_id"))
        assert not pii.contains_pii(body), fmt


def test_text_fields_are_masked():
    raw = {
        "record_id": "r1",
        "employee_name": "Alice",
        "excerpt": "call +1 555 010 0100 or alice@example.com, NID-12345678",
    }
    internal = dataset._row(raw, "record", keep_tail=False)
    assert internal["excerpt"] == "call [PHONE] or [EMAIL], [NATIONAL_ID]"
    restricted = dataset._row(raw, "record", keep_tail=True)
    assert "[PHONE ******0100]" in restricted["excerpt"] and "[EMAIL]" in restricted["excerpt"]


def test_xlsx_metadata_scope_is_callers(api, auth):
    meta, _ = xlsx_rows(export(api, auth, "a_eng_manager", "xlsx", source="records").content)
    assert '"tenant_id": "tenant_a"' in meta["scope"] and '"engineering"' in meta["scope"]
