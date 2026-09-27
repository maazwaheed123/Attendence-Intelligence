"""Fixed-window rate limit per subject, backed by Redis.

Fails OPEN if Redis is unavailable (availability over throttling for an MVP);
the failure is logged and visible in /v1/health/deep (Step 14).
"""

import logging
import time
from functools import lru_cache

import redis

from app.config import get_settings

log = logging.getLogger(__name__)


@lru_cache
def get_redis() -> redis.Redis:
    return redis.Redis.from_url(
        get_settings().redis_url, socket_timeout=1, socket_connect_timeout=1
    )


def allow(subject: str, limit: int | None = None) -> bool:
    limit = limit or get_settings().rate_limit_per_min
    key = f"rl:{subject}:{int(time.time() // 60)}"
    try:
        r = get_redis()
        count = r.incr(key)
        if count == 1:
            r.expire(key, 61)
        return count <= limit
    except redis.RedisError:
        log.warning("rate limiter unavailable; allowing request")
        return True
