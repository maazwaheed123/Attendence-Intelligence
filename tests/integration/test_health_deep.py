"""/v1/health/deep diagnostics (cache, queue, breakers) and structured logs."""

import json
import logging

import pytest
import redis

from app import cache
from app.api.middleware import request_id_var
from app.ingestion import worker
from app.logging_setup import JsonFormatter
from app.security import ratelimit
from tests.support.cache import template_only
from tests.support.llm import ask

pytestmark = pytest.mark.integration


class _DeadRedis:
    def __getattr__(self, name):
        def fail(*a, **k):
            raise redis.ConnectionError("down")

        return fail


def test_cache_stats_reported(api, auth, monkeypatch):
    template_only(monkeypatch)
    q = "Who was present on 1 September 2026?"
    ask(api, auth, "a_eng_manager", q)
    ask(api, auth, "a_eng_manager", q)
    c = api.get("/v1/health/deep").json()["components"]["cache"]
    assert c["status"] == "ok"
    qc = c["query_cache"]
    assert qc["hits"] >= 1 and qc["misses"] >= 1 and 0 < qc["hit_ratio"] < 1
    assert qc["ttl_s"] == 600


def test_breaker_state_per_provider(api):
    providers = api.get("/v1/health/deep").json()["components"]["llm_providers"]
    assert providers and all(p["circuit"] in ("closed", "open", "half_open") for p in providers)


def test_redis_down_is_degraded_not_crashed(api, auth, monkeypatch):
    dead = _DeadRedis()
    monkeypatch.setattr(ratelimit, "get_redis", lambda: dead)
    monkeypatch.setattr(cache, "get_redis", lambda: dead)
    monkeypatch.setattr(worker, "get_queue_connection", lambda: dead)
    r = api.get("/v1/health/deep")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "degraded"
    assert body["components"]["cache"]["status"] == "down"
    assert body["components"]["queue"]["status"] == "down"
    assert body["components"]["database"]["status"] == "ok"
    assert ask(api, auth, "a_eng_manager", "Who was present on 1 September 2026?")["answer"]


def test_json_log_lines_carry_request_id():
    fmt = JsonFormatter()
    token = request_id_var.set("req_log_test")
    try:
        rec = logging.makeLogRecord(
            {"name": "app.x", "levelname": "INFO", "msg": "hello %s", "args": ("w",)}
        )
        rec.cache = "hit"
        line = json.loads(fmt.format(rec))
    finally:
        request_id_var.reset(token)
    assert line["request_id"] == "req_log_test" and line["message"] == "hello w"
    assert line["level"] == "INFO" and line["cache"] == "hit"
    outside = json.loads(fmt.format(logging.makeLogRecord({"msg": "x"})))
    assert outside["request_id"] == "-"


def test_json_log_includes_exception():
    try:
        raise ValueError("bad")
    except ValueError:
        import sys

        rec = logging.makeLogRecord({"msg": "failed", "exc_info": sys.exc_info()})
    assert "ValueError: bad" in json.loads(JsonFormatter().format(rec))["exc"]
