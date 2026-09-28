"""The UI's only way to reach the service: a thin HTTP client for the public API.

The UI never touches the database, Redis or the models. Every call carries the
persona's bearer token, so the API enforces exactly the same isolation as for
any other client. All HTTP lives here so UI tests can replace this class.
"""

import os

import httpx

API_URL = os.getenv("API_URL", "http://api:8000")
TIMEOUT_S = float(os.getenv("UI_HTTP_TIMEOUT_S", "300"))  # CPU inference is slow


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, request_id: str | None = None):
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message, self.request_id = status, code, message, request_id


class ApiClient:
    def __init__(self, base_url: str = API_URL, token: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.token = token

    # ------------------------------------------------------------------ plumbing
    def _request(self, method: str, path: str, **kw) -> httpx.Response:
        headers = kw.pop("headers", {})
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            r = httpx.request(
                method, f"{self.base_url}{path}", headers=headers, timeout=TIMEOUT_S, **kw
            )
        except httpx.HTTPError as exc:
            raise ApiError(
                0, "UNREACHABLE", f"API not reachable: {exc.__class__.__name__}"
            ) from exc
        if r.status_code >= 400:
            try:
                err = r.json()["error"]
                raise ApiError(r.status_code, err["code"], err["message"], err.get("request_id"))
            except (ValueError, KeyError, TypeError):
                raise ApiError(r.status_code, "HTTP_ERROR", r.text[:200]) from None
        return r

    def _json(self, method: str, path: str, **kw) -> dict:
        return self._request(method, path, **kw).json()

    # ------------------------------------------------------------------ auth / context
    def personas(self) -> dict:
        return self._json("GET", "/v1/auth/personas")

    def dev_token(self, persona: str) -> str:
        return self._json("POST", "/v1/auth/dev-token", json={"persona": persona})["access_token"]

    def me(self) -> dict:
        return self._json("GET", "/v1/me")

    # ------------------------------------------------------------------ ingestion
    def ingest(self, filename: str, content: bytes, logical_name: str | None = None,
               entity_id: str | None = None) -> dict:  # fmt: skip
        data = {k: v for k, v in (("logical_name", logical_name), ("entity_id", entity_id)) if v}
        return self._json("POST", "/v1/ingest", files={"file": (filename, content)}, data=data)

    def job(self, job_id: str) -> dict:
        return self._json("GET", f"/v1/jobs/{job_id}")

    def jobs(self, status: str | None = None) -> list[dict]:
        params = {"status": status} if status else {}
        return self._json("GET", "/v1/jobs", params=params)["jobs"]

    def retry(self, job_id: str) -> dict:
        return self._json("POST", f"/v1/jobs/{job_id}/retry")

    # ------------------------------------------------------------------ query
    def query(self, question: str, filters: dict | None = None) -> dict:
        body = {"question": question}
        if filters:
            body["filters"] = filters
        return self._json("POST", "/v1/query", json=body)

    # ------------------------------------------------------------------ feedback
    def submit_feedback(self, body: dict) -> dict:
        return self._json("POST", "/v1/feedback", json=body)

    def feedback(self, status: str | None = None) -> list[dict]:
        params = {"status": status} if status else {}
        return self._json("GET", "/v1/feedback", params=params)["examples"]

    def deactivate(self, example_id: str) -> dict:
        return self._json("POST", f"/v1/feedback/{example_id}/deactivate")

    def rollback(self, example_id: str) -> dict:
        return self._json("POST", f"/v1/feedback/{example_id}/rollback")

    # ------------------------------------------------------------------ export / health
    def export(self, body: dict) -> tuple[bytes, dict]:
        r = self._request("POST", "/v1/export", json=body)
        keep = ("content-type", "content-disposition", "x-export-checksum", "x-export-record-count")
        return r.content, {k: r.headers.get(k) for k in keep}

    def health_deep(self) -> dict:
        return self._json("GET", "/v1/health/deep")
