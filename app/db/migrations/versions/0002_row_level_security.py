"""Row-Level Security: the isolation boundary.

Every tenant-bearing table gets ENABLE + FORCE RLS and a policy built from the
scope functions in 0001 (USING for reads, WITH CHECK for writes). Nothing in the
application or the LLM can widen these filters: they run inside Postgres.

Revision ID: 0002
Revises: 0001
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

SCOPE = "app_scope_ok(product_id, tenant_id, module, entity_id, classification)"

POLICIES = {
    "entities": "tenant_id = app_ctx('tenant_id') AND app_entity_ok(entity_id)",
    "employees": (
        "product_id = app_ctx('product_id') AND tenant_id = app_ctx('tenant_id')"
        " AND app_entity_ok(entity_id) AND app_self_ok(employee_id)"
    ),
    "source_documents": SCOPE,
    "ingestion_jobs": "app_scope_ok(product_id, tenant_id, module, entity_id, 'internal')",
    "attendance_records": f"{SCOPE} AND app_self_ok(employee_id)",
    "document_chunks": f"{SCOPE} AND app_self_ok(employee_id)",
    "query_responses": (
        "product_id = app_ctx('product_id') AND tenant_id = app_ctx('tenant_id')"
        " AND module = app_ctx('module')"
        " AND (app_ctx('entities') = '*'"
        "      OR entity_scope <@ string_to_array(app_ctx('entities'), ','))"
        " AND (app_ctx('employee_id') IS NULL OR sub = app_ctx('sub'))"
    ),
    "feedback_examples": SCOPE,
}


def upgrade() -> None:
    for table, expr in POLICIES.items():
        op.execute(
            f"""
            ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
            ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
            CREATE POLICY p_scope ON {table} USING ({expr}) WITH CHECK ({expr});
            """
        )
    # Audit log: anyone in the application may append; reads are tenant-scoped.
    op.execute(
        """
        ALTER TABLE audit_events ENABLE ROW LEVEL SECURITY;
        ALTER TABLE audit_events FORCE ROW LEVEL SECURITY;
        CREATE POLICY p_audit_insert ON audit_events FOR INSERT WITH CHECK (true);
        CREATE POLICY p_audit_read ON audit_events FOR SELECT
            USING (tenant_id = app_ctx('tenant_id') AND product_id = app_ctx('product_id'));
        """
    )


def downgrade() -> None:
    for table in POLICIES:
        op.execute(
            f"""
            DROP POLICY IF EXISTS p_scope ON {table};
            ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;
            ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;
            """
        )
    op.execute(
        """
        DROP POLICY IF EXISTS p_audit_insert ON audit_events;
        DROP POLICY IF EXISTS p_audit_read ON audit_events;
        ALTER TABLE audit_events NO FORCE ROW LEVEL SECURITY;
        ALTER TABLE audit_events DISABLE ROW LEVEL SECURITY;
        """
    )
