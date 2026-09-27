"""RQ worker entrypoint. Run with:  rq worker --url $REDIS_URL ingestion

A transient failure re-raises so RQ's Retry policy re-enqueues it (10 s, 30 s);
after the last attempt the job is marked failed and appears in the failed-file
queue (GET /v1/jobs?status=failed), where it can be retried manually.
"""

from functools import lru_cache

import redis

from app.config import get_settings


@lru_cache
def get_queue_connection() -> redis.Redis:
    return redis.Redis.from_url(get_settings().redis_url)


def run_job(job_id: str, scope_dict: dict) -> str:
    from app.ingestion.service import process

    return process(job_id, scope_dict, raise_transient=True)
