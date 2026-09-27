import pytest

pytestmark = pytest.mark.integration


def test_health_ok(client):
    r = client.get("/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["service"] == "attendance-intelligence"


def test_request_id_generated(client):
    r = client.get("/v1/health")
    assert r.headers["X-Request-ID"].startswith("req_")


def test_safe_request_id_echoed(client):
    r = client.get("/v1/health", headers={"X-Request-ID": "demo-123"})
    assert r.headers["X-Request-ID"] == "demo-123"


def test_unsafe_request_id_replaced(client):
    r = client.get("/v1/health", headers={"X-Request-ID": "bad id\r\nX-Injected: 1"})
    assert r.headers["X-Request-ID"].startswith("req_")
    assert "X-Injected" not in r.headers


def test_openapi_available(client):
    r = client.get("/openapi.json")
    assert r.status_code == 200
    assert "/v1/health" in r.json()["paths"]
