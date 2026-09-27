"""Every API request is audited with context; secrets never are."""

import pytest
from sqlalchemy import text

from app.db.session import owner_session
from app.security import ratelimit

pytestmark = pytest.mark.integration


def _events(request_id):
    with owner_session() as s:
        return [
            dict(r)
            for r in s.execute(
                text("SELECT * FROM audit_events WHERE request_id = :r ORDER BY event_id"),
                {"r": request_id},
            ).mappings()
        ]


def test_authenticated_request_audited(api, auth):
    r = api.get("/v1/me", headers={**auth("a_eng_manager"), "X-Request-ID": "audit-ok-1"})
    assert r.status_code == 200
    (ev,) = _events("audit-ok-1")
    assert ev["event_type"] == "http_request"
    assert ev["sub"] == "user:a_eng_manager"
    assert (ev["tenant_id"], ev["product_id"], ev["module"]) == (
        "tenant_a",
        "attendance_ai",
        "attendance",
    )
    assert ev["entity_scope"] == ["engineering"] and ev["role"] == "manager"
    assert ev["outcome"] == "ok" and ev["latency_ms"] is not None
    assert ev["details"]["path"] == "/v1/me" and ev["details"]["status"] == 200


def test_denied_request_audited_without_identity(api):
    api.get("/v1/me", headers={"Authorization": "Bearer garbage", "X-Request-ID": "audit-denied-1"})
    (ev,) = _events("audit-denied-1")
    assert ev["outcome"] == "denied" and ev["error_code"] == "UNAUTHENTICATED"
    assert ev["sub"] is None and ev["tenant_id"] is None


def test_forbidden_request_records_error_code(api, auth):
    api.get("/v1/audit", headers={**auth("a_eng_manager"), "X-Request-ID": "audit-forbidden-1"})
    (ev,) = _events("audit-forbidden-1")
    assert ev["outcome"] == "denied" and ev["error_code"] == "FORBIDDEN"
    assert ev["sub"] == "user:a_eng_manager"


def test_tokens_never_written_to_audit(api, token_for):
    tok = token_for("a_hr_admin")
    api.get("/v1/me", headers={"Authorization": f"Bearer {tok}", "X-Request-ID": "audit-secret-1"})
    with owner_session() as s:
        dump = s.execute(
            text("SELECT string_agg(audit_events::text, ' ') FROM audit_events")
        ).scalar()
    assert tok not in dump
    assert tok.split(".")[2] not in dump  # not even the signature


def test_health_not_audited(api):
    api.get("/v1/health", headers={"X-Request-ID": "audit-health-1"})
    assert _events("audit-health-1") == []


def test_audit_read_is_tenant_scoped(api, auth):
    api.get("/v1/me", headers={**auth("b_manager"), "X-Request-ID": "audit-tenant-b-1"})
    r = api.get("/v1/audit", headers=auth("a_auditor"), params={"request_id": "audit-tenant-b-1"})
    assert r.status_code == 200 and r.json()["events"] == []
    r = api.get("/v1/audit", headers=auth("a_auditor"), params={"request_id": "audit-ok-1"})
    assert r.status_code == 200


@pytest.mark.security
def test_rate_limit(api, token_for, monkeypatch):
    monkeypatch.setattr(
        ratelimit, "allow", lambda sub, limit=None, _orig=ratelimit.allow: _orig(sub, 3)
    )
    headers = {"Authorization": f"Bearer {token_for('a_eng_manager', sub='user:rate-test')}"}
    ratelimit.get_redis().delete(*ratelimit.get_redis().keys("rl:user:rate-test:*") or ["x"])
    codes = [api.get("/v1/me", headers=headers).status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]


@pytest.mark.security
def test_rate_limit_fails_open_when_redis_down(monkeypatch):
    import redis as redis_lib

    class Down:
        def incr(self, *a):
            raise redis_lib.ConnectionError("down")

    monkeypatch.setattr(ratelimit, "get_redis", lambda: Down())
    assert ratelimit.allow("anyone", 1) is True
