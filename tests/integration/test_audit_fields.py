"""Audit completeness: every query outcome records the required fields."""

import pytest
from sqlalchemy import text

from app.db.session import owner_session
from tests.support.llm import ask

pytestmark = pytest.mark.integration

REQUIRED = (
    "request_id", "sub", "product_id", "tenant_id", "module", "entity_scope", "role",
    "query_mode", "provider", "fallback_path", "confidence", "outcome", "latency_ms",
    "retrieved_source_ids",
)  # fmt: skip

CASES = [
    ("a_eng_manager", "Who was present on 1 September 2026?", "answered"),
    ("a_hr_admin", "What did the manager note about Bob's late arrivals?", "answered"),
    ("a_hr_admin", "Show evidence that Alice was present on 3 Sep", "answered"),
    ("a_eng_manager", "What was John Carter's attendance?", "filtered_or_absent"),
    ("a_eng_manager", "What is the company revenue?", "unavailable"),
    ("a_hr_admin", "Was Bob present on 15 September?", "needs_review"),
    ("a_hr_admin", "Was Lucas Martin present on 30 September?", "needs_review"),
    ("a_employee_e001", "What is my attendance?", "answered"),
]


@pytest.mark.parametrize(("persona", "question", "outcome"), CASES)
def test_query_event_has_every_required_field(api, auth, persona, question, outcome):
    r = ask(api, auth, persona, question)
    with owner_session() as s:
        ev = (
            s.execute(
                text("SELECT * FROM audit_events WHERE request_id = :r AND event_type = 'query'"),
                {"r": r["request_id"]},
            )
            .mappings()
            .one()
        )
    missing = [f for f in REQUIRED if ev[f] is None]
    assert missing == [], missing
    assert ev["outcome"] == outcome
    assert sorted(ev["retrieved_source_ids"]) == sorted(
        c.get("chunk_id") or c["record_id"] for c in r["citations"]
    )
    assert len(ev["details"]["question_hash"]) == 64 and "status" in ev["details"]
    assert question not in str(dict(ev))  # the raw question is never audited
