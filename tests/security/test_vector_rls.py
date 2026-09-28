"""Vector, keyword and trigram search run under RLS: another scope's chunks are
unreachable even with their EXACT text / vector (a perfect nearest neighbour)."""

import pytest
from sqlalchemy import text

from app.db.session import owner_session, scoped_session
from app.retrieval.embeddings import FakeEmbedder
from app.retrieval.stores import PgFtsStore, PgTrigramStore, PgVectorStore, UnscopedSession

pytestmark = [pytest.mark.security, pytest.mark.integration]

E = FakeEmbedder(768)
VEC, FTS, TRGM = PgVectorStore(), PgFtsStore(), PgTrigramStore()


def _chunks(where, **params):
    with owner_session() as s:
        return [
            dict(r)
            for r in s.execute(
                text(f"SELECT * FROM document_chunks WHERE {where}"), params
            ).mappings()
        ]


def _vector(scope, query_text, k=20, **kw):
    with scoped_session(scope, role="reader") as s:
        return VEC.search(s, E.embed_query(query_text), k=k, **kw)


def test_other_tenants_exact_vector_is_unreachable(corpus_db, scope_for):
    target = _chunks("tenant_id = 'tenant_b' AND chunk_type = 'row_card' AND is_active LIMIT 1")[0]
    b_ids = {str(c["chunk_id"]) for c in _chunks("tenant_id = 'tenant_b'")}

    own = _vector(scope_for("b_manager"), target["text_masked"], k=1)
    assert own[0].chunk_id == str(target["chunk_id"]) and own[0].score == pytest.approx(1.0)

    for persona in ("a_hr_admin", "a_eng_manager", "a_employee_e001", "x_other_product"):
        hits = _vector(scope_for(persona), target["text_masked"], k=50)
        assert hits, persona  # the search works, it just never sees tenant_b
        assert not {h.chunk_id for h in hits} & b_ids, persona


@pytest.mark.parametrize("store", [FTS, TRGM], ids=["fts", "trigram"])
def test_keyword_search_is_scoped(corpus_db, scope_for, store):
    with scoped_session(scope_for("b_manager")) as s:
        assert store.search(s, "John Carter")
    for persona in ("a_hr_admin", "a_eng_manager"):
        with scoped_session(scope_for(persona)) as s:
            hits = store.search(s, "John Carter", k=50)
        assert all("John Carter" not in h.text for h in hits), persona


def test_entity_scope(corpus_db, scope_for):
    hits = _vector(scope_for("a_eng_manager"), "E005 Priya Nair Human Resources Present", k=100)
    assert hits and {h.entity_id for h in hits} <= {"engineering", None}
    with scoped_session(scope_for("a_eng_manager")) as s:
        assert FTS.search(s, "Priya Nair") == []


def test_employee_sees_only_own_chunks(corpus_db, scope_for):
    hits = _vector(scope_for("a_employee_e001"), "E002 Bob Smith Absent", k=100)
    assert hits and {h.employee_id for h in hits} == {"E001"}


def test_product_scope(corpus_db, scope_for):
    hits = _vector(scope_for("x_other_product"), "Alice Johnson Present", k=100)
    doc_ids = {str(c["source_document_id"]) for c in _chunks("product_id = 'hrms_ai'")}
    assert hits and {h.source_document_id for h in hits} <= doc_ids


def test_confidential_remarks_need_clearance(corpus_db, scope_for):
    remark = _chunks("classification = 'confidential' AND tenant_id = 'tenant_a' LIMIT 1")[0]
    admin = _vector(scope_for("a_hr_admin"), remark["text_masked"], k=1)  # restricted clearance
    assert admin[0].chunk_id == str(remark["chunk_id"])
    manager = _vector(scope_for("a_eng_manager"), remark["text_masked"], k=50)  # internal
    assert str(remark["chunk_id"]) not in {h.chunk_id for h in manager}
    assert all(h.classification == "internal" for h in manager)


def test_superseded_chunks_are_never_returned(corpus_db, scope_for):
    old = _chunks("NOT is_active AND chunk_type = 'row_card' LIMIT 1")[0]
    with scoped_session(scope_for("a_hr_admin")) as s:
        hits = FTS.search(s, old["text_masked"], k=50)
    assert str(old["chunk_id"]) not in {h.chunk_id for h in hits}


def test_search_without_scope_is_refused(corpus_db):
    with owner_session() as s:  # bypasses RLS: exactly why stores refuse it
        for call in (
            lambda: VEC.search(s, E.embed_query("x")),
            lambda: FTS.search(s, "x"),
            lambda: TRGM.search(s, "x"),
        ):
            with pytest.raises(UnscopedSession):
                call()


def test_hnsw_settings_applied_per_transaction(corpus_db, scope_for):
    with scoped_session(scope_for("a_eng_manager")) as s:
        VEC.search(s, E.embed_query("present"), k=5)
        assert s.execute(text("SHOW hnsw.iterative_scan")).scalar() == "relaxed_order"
        assert s.execute(text("SHOW hnsw.ef_search")).scalar() == "100"
    with scoped_session(scope_for("a_eng_manager")) as s:  # SET LOCAL: gone next transaction
        assert s.execute(text("SHOW hnsw.ef_search")).scalar() == "40"


def test_hits_expose_only_masked_text_and_chunk_type_filter(corpus_db, scope_for):
    with scoped_session(scope_for("a_eng_manager")) as s:
        hits = VEC.search(
            s, E.embed_query("manager remarks late"), k=10, chunk_types=("narrative",)
        )
    assert hits and {h.chunk_type for h in hits} == {"narrative"}
    assert all(h.source == "vector" and -1 <= h.score <= 1 for h in hits)


def test_small_scope_is_not_starved_by_the_index(corpus_db, scope_for):
    """x_other_product sees 4 row cards among ~800; iterative scan must still return them."""
    hits = _vector(scope_for("x_other_product"), "Bob Smith Present", k=10)
    assert len(hits) == len(
        _chunks("product_id = 'hrms_ai' AND is_active AND embedding IS NOT NULL")
    )
