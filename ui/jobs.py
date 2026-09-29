"""Upload + job polling helpers (plain functions, testable without Streamlit)."""

import time

from client import ApiError

POLL_S, POLL_MAX_S = 2.0, 180.0
RUNNING = ("queued", "running", "retrying")


def upload_files(client, files, logical_name: str | None, entity_id: str | None) -> list[dict]:
    """Submit each file; returns one result per file (job view or error)."""
    out = []
    for f in files:
        try:
            job = client.ingest(f.name, f.getvalue(), logical_name or None, entity_id)
            out.append({"filename": f.name, **job})
        except ApiError as exc:
            out.append({"filename": f.name, "status": "rejected", "error": exc.message,
                        "code": exc.code})  # fmt: skip
    return out


FINAL = ("completed", "duplicate", "failed", "rejected")


def _refresh(client, job: dict) -> dict:
    """Re-read one job; finished jobs are not polled, a rate-limited read keeps the old view."""
    if not job.get("job_id") or job.get("status") in FINAL:
        return job
    try:
        return {"filename": job["filename"], **client.job(job["job_id"])}
    except ApiError as exc:
        if exc.status == 429:
            return job
        raise


def refresh_jobs(client, jobs: list[dict], wait: bool = False) -> list[dict]:
    deadline = time.monotonic() + (POLL_MAX_S if wait else 0)
    while True:
        jobs = [_refresh(client, j) for j in jobs]
        running = [j for j in jobs if j.get("status") in RUNNING]
        if not running or time.monotonic() >= deadline:
            return jobs
        # One read per running job per round: pacing rounds by the number of running jobs
        # keeps polling at ~30 requests/min, under the API's per-user rate limit.
        time.sleep(POLL_S * len(running))
