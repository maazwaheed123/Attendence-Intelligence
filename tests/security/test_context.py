"""Gateway context validation: registry checks and body/token consistency."""

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.api.deps import enforce_request_context, get_security_context
from app.api.errors import register_error_handlers
from app.api.middleware import RequestIdMiddleware
from app.security.context import SecurityContext
from tests.support.tokens import forge

pytestmark = [pytest.mark.security, pytest.mark.integration]


class Body(BaseModel):
    product_id: str | None = None
    tenant_id: str | None = None
    module: str | None = None
    entity_id: str | None = None


@pytest.fixture
def ctx_client(corpus_db):
    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)
    register_error_handlers(app)

    @app.post("/echo")
    def echo(body: Body, ctx: SecurityContext = Depends(get_security_context)):
        enforce_request_context(ctx, **body.model_dump())
        return {"ok": True}

    return TestClient(app, raise_server_exceptions=False)


def _code(r):
    return r.status_code, r.json().get("error", {}).get("code")


def test_matching_body_context_ok(ctx_client, auth):
    r = ctx_client.post(
        "/echo",
        headers=auth("a_eng_manager"),
        json={"tenant_id": "tenant_a", "product_id": "attendance_ai", "entity_id": "engineering"},
    )
    assert r.status_code == 200


@pytest.mark.parametrize(
    "body",
    [
        {"tenant_id": "tenant_b"},
        {"product_id": "hrms_ai"},
        {"module": "payroll"},
        {"entity_id": "hr"},
    ],
)
def test_body_cannot_change_or_widen_context(ctx_client, auth, body):
    assert _code(ctx_client.post("/echo", headers=auth("a_eng_manager"), json=body)) == (
        403,
        "CONTEXT_MISMATCH",
    )


def test_all_entity_scope_may_name_any_entity(ctx_client, auth):
    assert (
        ctx_client.post("/echo", headers=auth("a_hr_admin"), json={"entity_id": "hr"}).status_code
        == 200
    )


@pytest.mark.parametrize(
    "override",
    [
        {"product_id": "unknown_product"},
        {"tenant_id": "tenant_b", "product_id": "hrms_ai"},
        {"tenant_id": "tenant_zzz"},
        {"module": "payroll"},
        {"entities": ["engineering", "nonexistent_dept"]},
        {"tenant_id": "tenant_b", "entities": ["sales"]},
    ],
)
def test_token_context_must_exist_in_registry(ctx_client, override):
    r = ctx_client.post(
        "/echo", headers={"Authorization": f"Bearer {forge('a_eng_manager', **override)}"}, json={}
    )
    assert _code(r) == (403, "FORBIDDEN")
