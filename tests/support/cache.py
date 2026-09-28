"""Query-cache test helpers.

With the default mock chain every answer has fallback_path "mock>template" (a
failed provider) and is therefore never cached. template_only() gives a clean
chain (fallback_path "template"), so answers become cacheable.
"""

from sqlalchemy import text

from app.db.session import owner_session
from tests.support.llm import use_router


def template_only(monkeypatch):
    return use_router(monkeypatch, [], template=True)


def cache_state(request_id: str) -> str | None:
    """'hit' | 'miss' | 'off' from the request's query audit event."""
    with owner_session() as s:
        return s.execute(
            text(
                "SELECT details->>'cache' FROM audit_events "
                "WHERE request_id = :r AND event_type = 'query'"
            ),
            {"r": request_id},
        ).scalar()


def same_except_request_id(a: dict, b: dict) -> bool:
    strip = lambda r: {k: v for k, v in r.items() if k != "request_id"}  # noqa: E731
    return strip(a) == strip(b)
