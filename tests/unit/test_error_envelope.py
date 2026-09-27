import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.api.errors import AppError, register_error_handlers
from app.api.middleware import RequestIdMiddleware

pytestmark = pytest.mark.unit


class Body(BaseModel):
    n: int


@pytest.fixture
def app_client():
    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)
    register_error_handlers(app)

    @app.get("/boom")
    def boom():
        raise RuntimeError("secret internal detail")

    @app.get("/denied")
    def denied():
        raise AppError(403, "CONTEXT_MISMATCH", "Request context does not match token.")

    @app.post("/validate")
    def validate(body: Body):
        return body

    return TestClient(app, raise_server_exceptions=False)


def _assert_envelope(r, status, code):
    assert r.status_code == status
    err = r.json()["error"]
    assert err["code"] == code
    assert err["request_id"] == r.headers["X-Request-ID"]
    return err


def test_unknown_route(client):
    _assert_envelope(client.get("/nope"), 404, "NOT_FOUND")


def test_unhandled_error_hides_details(app_client):
    err = _assert_envelope(app_client.get("/boom"), 500, "INTERNAL_ERROR")
    assert "secret internal detail" not in err["message"]


def test_app_error_code(app_client):
    _assert_envelope(app_client.get("/denied"), 403, "CONTEXT_MISMATCH")


def test_validation_error(app_client):
    err = _assert_envelope(app_client.post("/validate", json={"n": "x"}), 422, "VALIDATION_ERROR")
    assert "n" in err["message"]
