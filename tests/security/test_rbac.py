"""RBAC matrix and endpoint permission enforcement."""

import pytest

from app.security.rbac import ROLE_MAX_CLEARANCE, Permission, clearance_allowed, has_permission

pytestmark = pytest.mark.security

P = Permission
EXPECTED = {
    "hr_admin": {P.INGEST, P.QUERY, P.EXPORT, P.FEEDBACK_SUBMIT, P.FEEDBACK_MANAGE, P.AUDIT_READ},
    "manager": {P.INGEST, P.QUERY, P.EXPORT},
    "reviewer": {P.QUERY, P.FEEDBACK_SUBMIT, P.FEEDBACK_MANAGE},
    "employee": {P.QUERY},
    "auditor": {P.AUDIT_READ},
}


@pytest.mark.unit
@pytest.mark.parametrize("role", sorted(EXPECTED))
@pytest.mark.parametrize("perm", list(Permission))
def test_permission_matrix(role, perm):
    assert has_permission(role, perm) == (perm in EXPECTED[role])


@pytest.mark.unit
def test_unknown_role_has_nothing():
    assert not any(has_permission("root", p) for p in Permission)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("role", "clearance", "ok"),
    [
        ("hr_admin", "restricted", True),
        ("manager", "confidential", True),
        ("manager", "restricted", False),
        ("employee", "confidential", False),
        ("reviewer", "internal", True),
        ("employee", "bogus", False),
    ],
)
def test_clearance_ceiling(role, clearance, ok):
    assert clearance_allowed(role, clearance) is ok
    assert set(ROLE_MAX_CLEARANCE) == set(EXPECTED)


@pytest.mark.integration
@pytest.mark.parametrize(
    ("persona", "status"),
    [("a_auditor", 200), ("a_hr_admin", 200), ("a_eng_manager", 403), ("a_employee_e001", 403)],
)
def test_audit_endpoint_requires_permission(api, auth, persona, status):
    r = api.get("/v1/audit", headers=auth(persona))
    assert r.status_code == status, r.text
    if status == 403:
        assert r.json()["error"]["code"] == "FORBIDDEN"
