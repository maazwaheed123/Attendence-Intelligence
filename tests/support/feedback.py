"""Feedback test helpers: Q2 + the reviewer's ideal output (the example in docs/api_examples.http)."""

import pytest
from sqlalchemy import text

from app.db.session import owner_session

Q2 = "What was Engineering's average attendance % in September?"
IDEAL = (
    "Engineering attendance from 01/09/2026 to 30/09/2026 was 91.83%, calculated from 95.5 "
    "present employee-days out of 104 scheduled employee-days."
)
FEEDBACK = "Always state the date range and how the percentage was calculated."


@pytest.fixture
def clean_feedback(corpus_db):
    """Examples created by a test must not change answers in other tests."""
    yield
    with owner_session() as s:
        s.execute(text("DELETE FROM feedback_examples WHERE intent_signature <> 'sig'"))


def submit(api, auth, persona, request_id, ideal=IDEAL, feedback=FEEDBACK, **extra):
    body = {
        "original_request_id": request_id,
        "feedback": feedback,
        "ideal_final_output": ideal,
        **extra,
    }
    return api.post("/v1/feedback", json=body, headers=auth(persona))


def ask_id(api, auth, persona, question=Q2) -> dict:
    r = api.post("/v1/query", json={"question": question}, headers=auth(persona))
    assert r.status_code == 200, r.text
    return r.json()
