"""Row-Level Security: the isolation boundary, tested as the REAL runtime roles.

Every query here runs as rag_reader or app_rw (never the schema owner), exactly as
the application will at request time.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.orm import Session

from app.db.session import get_engine, owner_session, scoped_session

pytestmark = [pytest.mark.security, pytest.mark.integration]

# Tables each runtime role may read (rag_reader has no access to jobs or audit).
READER_TABLES = [
    "entities",
    "employees",
    "source_documents",
    "attendance_records",
    "document_chunks",
    "query_responses",
    "feedback_examples",
]
APP_TABLES = READER_TABLES + ["ingestion_jobs", "audit_events"]
ROLE_TABLES = [("reader", t) for t in READER_TABLES + ["v_attendance"]] + [
    ("app", t) for t in APP_TABLES + ["v_attendance"]
]


def _tenants(session, table) -> set[str]:
    return set(session.execute(text(f"SELECT DISTINCT tenant_id FROM {table}")).scalars())


def _count(session, sql, **params) -> int:
    return session.execute(text(sql), params).scalar_one()


# ------------------------------------------------------------------ tenant isolation


@pytest.mark.parametrize(("role", "table"), ROLE_TABLES)
def test_tenant_a_never_sees_tenant_b(corpus_db, scope_for, role, table):
    with scoped_session(scope_for("a_hr_admin"), role=role) as s:
        assert _tenants(s, table) == {"tenant_a"}
    with scoped_session(scope_for("b_manager"), role=role) as s:
        assert _tenants(s, table) == {"tenant_b"}


def test_tenant_b_exact_text_not_reachable_from_tenant_a(corpus_db, scope_for):
    """Even searching for tenant B's exact content returns nothing."""
    with scoped_session(scope_for("a_hr_admin")) as s:
        assert (
            _count(s, "SELECT count(*) FROM document_chunks WHERE text_masked LIKE '%E101%'") == 0
        )
        assert _count(s, "SELECT count(*) FROM v_attendance WHERE tenant_id = 'tenant_b'") == 0
        assert (
            _count(s, "SELECT count(*) FROM v_attendance WHERE employee_name = 'John Carter'") == 0
        )


def test_aggregates_cannot_include_other_tenant(corpus_db, scope_for):
    """A query that *asks* for every tenant still only aggregates the caller's rows."""
    sql = "SELECT count(DISTINCT tenant_id), count(*) FROM v_attendance"
    with scoped_session(scope_for("a_hr_admin")) as s:
        tenants, rows = s.execute(text(sql)).one()
    assert tenants == 1 and rows == 262 + 1  # 262 clean + Bob's conflict day


# ------------------------------------------------------------------ fail closed


@pytest.mark.parametrize(("role", "table"), ROLE_TABLES)
def test_no_context_means_no_rows(corpus_db, role, table):
    with Session(get_engine(role)) as s, s.begin():
        assert _count(s, f"SELECT count(*) FROM {table}") == 0, f"{role} leaked {table}"


def test_partial_context_means_no_rows(corpus_db):
    """Tenant set but product missing -> still nothing."""
    with Session(get_engine("reader")) as s, s.begin():
        s.execute(text("SELECT set_config('app.tenant_id', 'tenant_a', true)"))
        s.execute(text("SELECT set_config('app.entities', '*', true)"))
        s.execute(text("SELECT set_config('app.clearance', '3', true)"))
        assert _count(s, "SELECT count(*) FROM attendance_records") == 0


def test_context_does_not_leak_to_next_transaction(corpus_db, scope_for):
    """Pooled connection reuse: the scope is transaction-local."""
    engine = get_engine("reader")
    with engine.connect() as conn:
        with conn.begin():
            for k, v in scope_for("a_hr_admin").settings().items():
                conn.execute(text("SELECT set_config(:k, :v, true)"), {"k": f"app.{k}", "v": v})
            assert conn.execute(text("SELECT count(*) FROM attendance_records")).scalar_one() > 0
        with conn.begin():
            assert conn.execute(text("SELECT count(*) FROM attendance_records")).scalar_one() == 0


# ------------------------------------------------------------------ product / module


def test_product_isolation(corpus_db, scope_for):
    with scoped_session(scope_for("x_other_product")) as s:
        assert _count(s, "SELECT count(*) FROM attendance_records") == 4
        assert set(s.execute(text("SELECT DISTINCT product_id FROM v_attendance")).scalars()) == {
            "hrms_ai"
        }
    with scoped_session(scope_for("a_hr_admin")) as s:
        assert (
            _count(s, "SELECT count(*) FROM attendance_records WHERE product_id = 'hrms_ai'") == 0
        )


def test_module_isolation(corpus_db, scope_for):
    with scoped_session(scope_for("a_hr_admin", module="payroll")) as s:
        for table in ("attendance_records", "document_chunks", "source_documents", "v_attendance"):
            assert _count(s, f"SELECT count(*) FROM {table}") == 0


# ------------------------------------------------------------------ entity / RBAC / classification


def test_entity_isolation(corpus_db, scope_for):
    with scoped_session(scope_for("a_eng_manager")) as s:
        assert set(
            s.execute(text("SELECT DISTINCT entity_id FROM attendance_records")).scalars()
        ) == {"engineering"}
        assert (
            _count(s, "SELECT count(*) FROM v_attendance WHERE department = 'Human Resources'") == 0
        )
        assert _count(s, "SELECT count(*) FROM employees WHERE entity_id = 'hr'") == 0
    with scoped_session(scope_for("a_hr_admin")) as s:
        assert set(
            s.execute(text("SELECT DISTINCT entity_id FROM attendance_records")).scalars()
        ) == {
            "engineering",
            "hr",
            "sales",
        }


def test_multi_entity_documents_visible_only_to_all_entity_scope(corpus_db, scope_for):
    """A document spanning Engineering + HR (entity_id NULL) is hidden from single-entity scopes."""
    with scoped_session(scope_for("a_eng_manager")) as s:
        assert (
            _count(
                s, "SELECT count(*) FROM source_documents WHERE filename = 'tenant_a_sep_v2.csv'"
            )
            == 0
        )
        # ...but the Engineering rows extracted from it are visible (they carry entity_id).
        assert (
            _count(
                s,
                "SELECT count(*) FROM attendance_records WHERE source_file = 'tenant_a_sep_v2.csv'",
            )
            > 0
        )
    with scoped_session(scope_for("a_hr_admin")) as s:
        assert (
            _count(
                s, "SELECT count(*) FROM source_documents WHERE filename = 'tenant_a_sep_v2.csv'"
            )
            == 1
        )


def test_classification_filter(corpus_db, scope_for):
    remarks = "SELECT count(*) FROM document_chunks WHERE classification = 'confidential'"
    with scoped_session(scope_for("a_eng_manager")) as s:  # clearance internal
        assert _count(s, remarks) == 0
    with scoped_session(scope_for("a_eng_manager", clearance="confidential")) as s:
        assert _count(s, remarks) == 3


def test_employee_self_scope(corpus_db, scope_for):
    with scoped_session(scope_for("a_employee_e001")) as s:
        assert set(
            s.execute(text("SELECT DISTINCT employee_id FROM attendance_records")).scalars()
        ) == {"E001"}
        assert set(s.execute(text("SELECT DISTINCT employee_id FROM v_attendance")).scalars()) == {
            "E001"
        }
        assert set(s.execute(text("SELECT employee_id FROM employees")).scalars()) == {"E001"}
        # Narrative chunks (no employee) are not visible to a self-scoped employee.
        assert _count(s, "SELECT count(*) FROM document_chunks WHERE chunk_type = 'narrative'") == 0


# ------------------------------------------------------------------ writes (WITH CHECK)


def _insert_doc(s, tenant):
    s.execute(
        text(
            """INSERT INTO source_documents (document_id, product_id, tenant_id, module, entity_id,
               logical_name, filename, file_type, checksum_sha256, size_bytes, status,
               storage_path, uploaded_by)
               VALUES (:d, 'attendance_ai', :t, 'attendance', 'engineering', 'x', 'x.csv', 'csv',
                       repeat('a', 64), 1, 'received', 'x', 'test')"""
        ),
        {"d": uuid.uuid4(), "t": tenant},
    )


def test_cannot_write_into_another_tenant(corpus_db, scope_for):
    with pytest.raises(ProgrammingError, match="row-level security"):
        with scoped_session(scope_for("a_hr_admin"), role="app") as s:
            _insert_doc(s, "tenant_b")


def test_cannot_move_row_to_another_tenant(corpus_db, scope_for):
    with pytest.raises(ProgrammingError, match="row-level security"):
        with scoped_session(scope_for("a_hr_admin"), role="app") as s:
            s.execute(text("UPDATE ingestion_jobs SET tenant_id = 'tenant_b'"))


def test_cannot_write_outside_entity_scope(corpus_db, scope_for):
    with pytest.raises(ProgrammingError, match="row-level security"):
        with scoped_session(scope_for("a_hr_manager"), role="app") as s:
            _insert_doc(s, "tenant_a")  # entity 'engineering', caller is HR


def test_in_scope_write_allowed(corpus_db, scope_for):
    with scoped_session(scope_for("a_eng_manager"), role="app") as s:
        _insert_doc(s, "tenant_a")
        s.rollback()


# ------------------------------------------------------------------ privileges


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO attendance_records (record_id) VALUES (gen_random_uuid())",
        "UPDATE attendance_records SET status = 'present'",
        "DELETE FROM attendance_records",
        "TRUNCATE attendance_records",
        "DROP TABLE attendance_records",
        "CREATE TABLE evil (x int)",
        "SELECT phone_enc FROM employees",
        "SELECT national_id_enc FROM employees",
        "SELECT raw_values FROM attendance_records",
        "SELECT text FROM document_chunks",
        "SELECT * FROM audit_events",
        "SELECT * FROM ingestion_jobs",
        "SELECT * FROM employees",
    ],
)
def test_reader_privileges_denied(corpus_db, scope_for, sql):
    with pytest.raises(DBAPIError, match="permission denied|read-only transaction"):
        with scoped_session(scope_for("a_hr_admin")) as s:
            s.execute(text(sql))


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE audit_events SET outcome = 'tampered'",
        "DELETE FROM audit_events",
        "DELETE FROM attendance_records",
        "TRUNCATE audit_events",
        "DROP TABLE attendance_records",
        "ALTER TABLE attendance_records DISABLE ROW LEVEL SECURITY",
    ],
)
def test_app_role_privileges_denied(corpus_db, scope_for, sql):
    with pytest.raises(DBAPIError, match="permission denied|must be owner"):
        with scoped_session(scope_for("a_hr_admin"), role="app") as s:
            s.execute(text(sql))


def test_reader_can_read_allowed_columns(corpus_db, scope_for):
    with scoped_session(scope_for("a_hr_admin")) as s:
        assert (
            _count(s, "SELECT count(employee_name) FROM employees") == 12
        )  # tenant_a, this product
        assert _count(s, "SELECT count(text_masked) FROM document_chunks") > 0


def test_runtime_roles_cannot_bypass_rls(corpus_db):
    with owner_session() as s:
        rows = s.execute(
            text(
                "SELECT rolname, rolsuper, rolbypassrls FROM pg_roles WHERE rolname IN ('app_rw','rag_reader')"
            )
        ).all()
    assert sorted(rows) == [("app_rw", False, False), ("rag_reader", False, False)]


def test_audit_insert_allowed_and_reads_scoped(corpus_db, scope_for):
    with scoped_session(scope_for("a_hr_admin"), role="app") as s:
        s.execute(
            text(
                "INSERT INTO audit_events (request_id, tenant_id, product_id, event_type) VALUES ('r1','tenant_a','attendance_ai','test')"
            )
        )
        assert _tenants(s, "audit_events") == {"tenant_a"}
        s.rollback()


# ------------------------------------------------------------------ catalog guards


def test_every_tenant_table_has_forced_rls(corpus_db):
    """Guard for the future: any new table with tenant_id must have RLS enabled AND forced."""
    with owner_session() as s:
        rows = s.execute(
            text(
                """SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
                   FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                   JOIN information_schema.columns col
                     ON col.table_name = c.relname AND col.column_name = 'tenant_id'
                   WHERE n.nspname = 'public' AND c.relkind = 'r'"""
            )
        ).all()
    reference = {"tenants", "tenant_products"}  # global reference data, no tenant content
    unprotected = [r[0] for r in rows if not (r[1] and r[2]) and r[0] not in reference]
    assert rows and not unprotected, unprotected


def test_views_are_security_invoker(corpus_db):
    """A view without security_invoker runs as its owner and bypasses RLS."""
    with owner_session() as s:
        rows = s.execute(
            text(
                "SELECT relname, reloptions FROM pg_class WHERE relkind = 'v' AND relnamespace = 'public'::regnamespace"
            )
        ).all()
    assert rows
    for name, opts in rows:
        assert opts and "security_invoker=true" in opts, name
