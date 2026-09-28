"""Redis query cache for /v1/query.

The key is a sha256 over EVERYTHING that can change an answer:
  - the caller's full isolation scope (DbScope.settings(): product, tenant,
    module, entities, clearance, employee id, sub) + role, so two callers with
    different scopes can never share an entry
  - the normalized question and the request filters
  - today's date (relative dates such as "last week" move with the calendar)
  - data_version(tenant, product): bumped when an ingestion job completes
  - feedback_version(tenant, product): bumped on every feedback change
  - PROMPT_VERSION, RETRIEVAL_VERSION and the configured model chain
Only clean answers are stored: never blocked or provider-degraded responses
(a failed provider in fallback_path), so they are retried when models recover.
A hit is not a shortcut around governance: the orchestrator re-runs
postprocess.finalize with a freshly loaded scoped directory, persists and
audits it under a new request id.
Every Redis failure fails OPEN (no cache), and /v1/health/deep shows it.
"""

import datetime as dt
import hashlib
import json
import logging

import redis

from app.config import get_settings
from app.generation.prompts import PROMPT_VERSION
from app.retrieval import RETRIEVAL_VERSION
from app.security.context import SecurityContext
from app.security.ratelimit import get_redis

log = logging.getLogger(__name__)

PREFIX = "qc:"
_STATS = {"hit": "qc:stats:hit", "miss": "qc:stats:miss"}


def _data_key(tenant: str, product: str) -> str:
    return f"dv:{tenant}:{product}"


def _feedback_key(tenant: str, product: str) -> str:
    return f"fv:{tenant}:{product}"


def _bump(key: str) -> None:
    try:
        get_redis().incr(key)
    except redis.RedisError:
        log.warning("cache version bump failed for %s (entries still expire by TTL)", key)


def bump_data_version(tenant: str, product: str) -> None:
    _bump(_data_key(tenant, product))


def bump_feedback_version(tenant: str, product: str) -> None:
    _bump(_feedback_key(tenant, product))


def normalize(question: str) -> str:
    return " ".join(question.lower().split())


def key_for(ctx: SecurityContext, question: str, filters: dict) -> str | None:
    """None when the cache is disabled or Redis is unavailable."""
    s = get_settings()
    if s.cache_ttl_s <= 0:
        return None
    try:
        dv, fv = get_redis().mget(
            _data_key(ctx.tenant_id, ctx.product_id), _feedback_key(ctx.tenant_id, ctx.product_id)
        )
    except redis.RedisError:
        log.warning("query cache unavailable; answering without cache")
        return None
    material = {
        "scope": ctx.to_scope().settings(),
        "role": ctx.role,
        "q": normalize(question),
        "filters": {k: str(v) for k, v in sorted(filters.items())},
        "today": dt.date.today().isoformat(),
        "dv": (dv or b"0").decode(),
        "fv": (fv or b"0").decode(),
        "pv": PROMPT_VERSION,
        "rv": RETRIEVAL_VERSION,
        "chain": s.llm_chain,
    }
    digest = hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()
    return PREFIX + digest


def cacheable(response: dict) -> bool:
    if ">" in (response.get("fallback_path") or ""):
        return False  # a provider failed: retry once models recover
    if response["status"] in ("answered", "needs_review"):
        return True
    return response["unavailable_reason"] == "no_data_in_scope"


def get(key: str | None) -> dict | None:
    if key is None:
        return None
    try:
        r = get_redis()
        raw = r.get(key)
        r.incr(_STATS["hit" if raw else "miss"])
    except redis.RedisError:
        log.warning("query cache read failed; answering without cache")
        return None
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def put(key: str | None, entry: dict) -> None:
    if key is None:
        return
    try:
        get_redis().set(key, json.dumps(entry, default=str), ex=get_settings().cache_ttl_s)
    except redis.RedisError:
        log.warning("query cache write failed")


def stats() -> dict:
    """For /v1/health/deep; raises on Redis errors (the caller reports 'down')."""
    r = get_redis()
    hit, miss = (int(v or 0) for v in r.mget(_STATS["hit"], _STATS["miss"]))
    total = hit + miss
    return {
        "hits": hit,
        "misses": miss,
        "hit_ratio": round(hit / total, 3) if total else None,
        "ttl_s": get_settings().cache_ttl_s,
    }
