"""v_attendance: one resolved row per employee-day, with fixed metric columns.

- security_invoker = true: RLS of attendance_records applies to the CALLER.
  Without it a view runs with its owner's rights and would bypass RLS.
- Only active (not superseded) and non-review records are used.
- Several sources for the same employee-day: highest extraction confidence wins;
  if sources disagree, status = 'conflict' and the day is excluded from metrics.
- is_scheduled / present_value encode the business metric rules, so an
  LLM-written query cannot redefine "attendance %":
      attendance % = 100 * SUM(present_value) / SUM(is_scheduled::int)

Revision ID: 0003
Revises: 0002
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE VIEW v_attendance WITH (security_invoker = true, security_barrier = true) AS
        WITH active AS (
            -- explicit columns: the caller (security_invoker) has no right to raw_values
            SELECT record_id, product_id, tenant_id, module, entity_id, classification,
                   employee_id, employee_name, department, attendance_date, status,
                   check_in, check_out, total_hours, source_file, source_locator,
                   extraction_method, extraction_confidence
            FROM attendance_records
            WHERE is_active AND NOT review_required
        ),
        grouped AS (
            SELECT product_id, tenant_id, module, employee_id, attendance_date,
                   count(DISTINCT status) AS n_status,
                   array_agg(DISTINCT status ORDER BY status) AS reported_statuses,
                   array_agg(record_id ORDER BY extraction_confidence DESC, record_id) AS record_ids
            FROM active
            GROUP BY product_id, tenant_id, module, employee_id, attendance_date
        )
        SELECT DISTINCT ON (a.product_id, a.tenant_id, a.module, a.employee_id, a.attendance_date)
            a.record_id,
            a.product_id, a.tenant_id, a.module, a.entity_id, a.classification,
            a.employee_id, a.employee_name, a.department, a.attendance_date,
            CASE WHEN g.n_status > 1 THEN 'conflict' ELSE a.status END AS status,
            g.reported_statuses,
            a.check_in, a.check_out, a.total_hours,
            a.source_file, a.source_locator, a.extraction_method, a.extraction_confidence,
            g.n_status > 1 AS conflict,
            g.record_ids AS source_record_ids,
            cardinality(g.record_ids) AS source_count,
            (g.n_status = 1 AND a.status <> 'holiday') AS is_scheduled,
            CASE WHEN g.n_status > 1 THEN 0.0
                 WHEN a.status IN ('present', 'wfh') THEN 1.0
                 WHEN a.status = 'half_day' THEN 0.5
                 ELSE 0.0 END::numeric(2,1) AS present_value
        FROM active a
        JOIN grouped g USING (product_id, tenant_id, module, employee_id, attendance_date)
        ORDER BY a.product_id, a.tenant_id, a.module, a.employee_id, a.attendance_date,
                 a.extraction_confidence DESC, a.record_id;

        GRANT SELECT ON v_attendance TO rag_reader, app_rw;
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS v_attendance;")
