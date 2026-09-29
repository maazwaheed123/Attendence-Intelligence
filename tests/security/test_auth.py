"""Authentication at the gateway."""

import pytest

from app.config import get_settings
from app.main import create_app
from tests.support.tokens import forge, unsigned

pytestmark = [pytest.mark.security, pytest.mark.integration]


def _get_me(api, token=None, raw_header=None):
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if raw_header:
        headers["Authorization"] = raw_header
    return api.get("/v1/me", headers=headers)


def _assert_401(r):
    assert r.status_code == 401, r.text
    assert r.json()["error"]["code"] == "UNAUTHENTICATED"
    assert r.headers["WWW-Authenticate"] == "Bearer"


def test_valid_token_accepted(api, auth):
    r = api.get("/v1/me", headers=auth("a_eng_manager"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["context"]["tenant_id"] == "tenant_a"
    assert body["context"]["entity_scope"] == ["engineering"]
    assert body["visible_entities"] == ["engineering"]
    assert "query" in body["permissions"] and "feedback_submit" not in body["permissions"]


def test_me_never_echoes_token(api, token_for):
    tok = token_for("a_eng_manager")
    r = _get_me(api, tok)
    assert tok not in r.text and "token_id" not in r.text


def test_missing_token(api):
    _assert_401(_get_me(api))


@pytest.mark.parametrize(
    "header", ["Basic abc", "Bearer", "Bearer ", "bearer not-a-jwt", "Token x.y.z"]
)
def test_malformed_header(api, header):
    _assert_401(_get_me(api, raw_header=header))


def test_wrong_signature(api):
    _assert_401(_get_me(api, forge("a_eng_manager", secret="attacker-secret-" + "x" * 32)))


def test_alg_none_rejected(api):
    _assert_401(_get_me(api, unsigned("a_hr_admin")))


def test_expired(api):
    r = _get_me(api, forge("a_eng_manager", exp=1_000_000))
    _assert_401(r)
    assert "expired" in r.json()["error"]["message"].lower()


def test_wrong_audience_or_issuer(api):
    _assert_401(_get_me(api, forge("a_eng_manager", aud="some-other-api")))
    _assert_401(_get_me(api, forge("a_eng_manager", iss="evil-issuer")))


@pytest.mark.parametrize(
    "claim", ["exp", "jti", "sub", "tenant_id", "product_id", "role", "entities", "clearance"]
)
def test_missing_claim(api, claim):
    _assert_401(_get_me(api, forge("a_eng_manager", drop=(claim,))))


@pytest.mark.parametrize(
    "override",
    [
        {"role": "superuser"},
        {"clearance": "restricted"},
        {"entities": "engineering"},
        {"entities": []},
        {"tenant_id": "tenant_a' OR '1'='1"},
        {"entities": ["engineering,hr"]},
    ],
)
def test_invalid_claims(api, override):
    _assert_401(_get_me(api, forge("a_eng_manager", **override)))


def test_employee_without_employee_id(api):
    _assert_401(_get_me(api, forge("a_employee_e001", employee_id=None)))


def test_dev_token_endpoint(api):
    r = api.post("/v1/auth/dev-token", json={"persona": "a_eng_manager"})
    assert r.status_code == 200
    tok = r.json()["access_token"]
    assert _get_me(api, tok).status_code == 200
    assert api.post("/v1/auth/dev-token", json={"persona": "root"}).status_code == 404


def test_dev_token_not_mounted_in_prod(monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("APP_ENV", "prod")
    get_settings.cache_clear()
    try:
        prod = TestClient(create_app(), raise_server_exceptions=False)
        assert prod.post("/v1/auth/dev-token", json={"persona": "a_hr_admin"}).status_code == 404
        assert "/v1/auth/dev-token" not in prod.get("/openapi.json").json()["paths"]
    finally:
        monkeypatch.setenv("APP_ENV", "test")
        get_settings.cache_clear()
