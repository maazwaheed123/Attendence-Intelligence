"""Uploaded content with embedded instructions is stored and flagged as DATA."""

import pytest
from sqlalchemy import text

from app.db.session import owner_session, scoped_session
from tests.support.ingest import upload

pytestmark = [pytest.mark.security, pytest.mark.integration]


def test_injection_memo_flagged_not_dropped(ingest_api, auth, scope_for):
    body = upload(ingest_api, auth("a_eng_manager"), "injection_memo.docx").json()
    assert body["status"] == "completed"
    assert body["counts"]["records_created"] == 0
    assert body["counts"]["narrative_chunks"] == 4 and body["counts"]["suspicious_chunks"] == 1
    assert any("possible prompt injection" in w for w in body["warnings"])
    with owner_session() as s:
        rows = s.execute(
            text("SELECT text, suspicious FROM document_chunks ORDER BY locator")
        ).all()
    flagged = [t for t, sus in rows if sus]
    assert len(flagged) == 1 and "Ignore all previous instructions" in flagged[0]
    with scoped_session(scope_for("a_eng_manager")) as s:
        assert (
            s.execute(text("SELECT count(*) FROM document_chunks WHERE suspicious")).scalar_one()
            == 1
        )
    with scoped_session(scope_for("b_manager")) as s:
        assert s.execute(text("SELECT count(*) FROM document_chunks")).scalar_one() == 0


def test_injection_cannot_change_ingestion_scope(ingest_api, auth):
    """A document asking for other tenants' data still produces only in-scope rows."""
    upload(ingest_api, auth("a_eng_manager"), "injection_memo.docx")
    with owner_session() as s:
        tenants = set(s.execute(text("SELECT DISTINCT tenant_id FROM document_chunks")).scalars())
    assert tenants == {"tenant_a"}
