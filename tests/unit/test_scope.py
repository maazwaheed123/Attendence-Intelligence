import pytest

from app.security.scope import DbScope, ScopeError

pytestmark = [pytest.mark.unit, pytest.mark.security]

BASE = dict(
    product_id="attendance_ai",
    tenant_id="tenant_a",
    module="attendance",
    entities=("engineering",),
    clearance="internal",
)


def test_valid_scope_settings():
    s = DbScope(**BASE, sub="user:alice")
    assert s.settings() == {
        "product_id": "attendance_ai",
        "tenant_id": "tenant_a",
        "module": "attendance",
        "entities": "engineering",
        "clearance": "1",
        "employee_id": "",
        "sub": "user:alice",
    }


def test_all_entities():
    s = DbScope(**{**BASE, "entities": ("*",)})
    assert s.all_entities and s.settings()["entities"] == "*"


@pytest.mark.parametrize(
    "override",
    [
        {"tenant_id": "tenant_a' OR '1'='1"},
        {"tenant_id": ""},
        {"tenant_id": "Tenant_A"},
        {"product_id": "attendance_ai;DROP"},
        {"module": "*"},
        {"entities": ("engineering,hr",)},  # comma would widen the entity list
        {"entities": ("engineering", "*")},  # wildcard mixed in
        {"entities": ()},
        {"clearance": "top-secret"},
        {"employee_id": "E001' --"},
    ],
)
def test_invalid_scopes_rejected(override):
    with pytest.raises(ScopeError):
        DbScope(**{**BASE, **override})


def test_scope_is_immutable():
    s = DbScope(**BASE)
    with pytest.raises(AttributeError):
        s.tenant_id = "tenant_b"  # type: ignore[misc]
