"""Core schema, least-privilege runtime roles, scope helper functions.

Revision ID: 0001
Revises:
"""

from alembic import op
from sqlalchemy.engine import make_url

from app.config import get_settings

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

def _password(url_secret) -> str:
    pw = make_url(url_secret.get_secret_value()).password or ""
    return pw.replace("'", "''")


def upgrade() -> None:
    s = get_settings()

    # ------------------------------------------------------------------ roles
    # Cluster-wide, so they may already exist (e.g. created by the test database).
    for role, url in (("app_rw", s.database_url_app), ("rag_reader", s.database_url_reader)):
        op.execute(
            f"""
            DO $$ BEGIN
              IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                CREATE ROLE {role} LOGIN;
              END IF;
            END $$;
            ALTER ROLE {role} WITH LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE
                PASSWORD '{_password(url)}';
            """
        )
    op.execute(
        """
        DO $$ BEGIN
          EXECUTE format('GRANT CONNECT ON DATABASE %I TO app_rw, rag_reader', current_database());
        END $$;
        GRANT USAGE ON SCHEMA public TO app_rw, rag_reader;
        """
    )

    # ------------------------------------------------------- scope functions
    # All RLS policies call these, so the isolation rule is defined ONCE.
    # current_setting(..., true) returns NULL when unset -> comparisons are NULL
    # -> policy is false -> zero rows (fail closed).
    op.execute(
        """
        CREATE FUNCTION class_rank(c text) RETURNS int
        LANGUAGE sql IMMUTABLE AS $$
          SELECT CASE c WHEN 'public' THEN 0 WHEN 'internal' THEN 1
                        WHEN 'confidential' THEN 2 WHEN 'restricted' THEN 3
                        ELSE 99 END
        $$;

        CREATE FUNCTION app_ctx(name text) RETURNS text
        LANGUAGE sql STABLE AS $$
          SELECT nullif(current_setting('app.' || name, true), '')
        $$;

        CREATE FUNCTION app_entity_ok(e text) RETURNS boolean
        LANGUAGE sql STABLE AS $$
          SELECT app_ctx('entities') = '*'
              OR e = ANY (string_to_array(app_ctx('entities'), ','))
        $$;

        CREATE FUNCTION app_scope_ok(p text, t text, m text, e text, c text) RETURNS boolean
        LANGUAGE sql STABLE AS $$
          SELECT p = app_ctx('product_id')
             AND t = app_ctx('tenant_id')
             AND m = app_ctx('module')
             AND app_entity_ok(e)
             AND class_rank(c) <= coalesce(app_ctx('clearance')::int, -1)
        $$;

        CREATE FUNCTION app_self_ok(emp text) RETURNS boolean
        LANGUAGE sql STABLE AS $$
          SELECT app_ctx('employee_id') IS NULL OR emp = app_ctx('employee_id')
        $$;
        """
    )

    # ------------------------------------------------------------------ tables
    op.execute(
        """
        CREATE TABLE products (
            product_id  text PRIMARY KEY,
            name        text NOT NULL
        );

        CREATE TABLE tenants (
            tenant_id          text PRIMARY KEY,
            name               text NOT NULL,
            allow_external_llm boolean NOT NULL DEFAULT false,
            date_format        text NOT NULL DEFAULT 'DD/MM/YYYY',
            created_at         timestamptz NOT NULL DEFAULT now()
        );

        CREATE TABLE tenant_products (
            tenant_id  text REFERENCES tenants,
            product_id text REFERENCES products,
            PRIMARY KEY (tenant_id, product_id)
        );

        CREATE TABLE entities (
            tenant_id   text NOT NULL REFERENCES tenants,
            entity_id   text NOT NULL,
            name        text NOT NULL,
            entity_type text NOT NULL DEFAULT 'department',
            PRIMARY KEY (tenant_id, entity_id)
        );

        CREATE TABLE employees (
            product_id      text NOT NULL REFERENCES products,
            tenant_id       text NOT NULL REFERENCES tenants,
            employee_id     text NOT NULL,
            employee_name   text NOT NULL,
            entity_id       text NOT NULL,
            phone_enc       text,          -- Fernet-encrypted, restricted
            national_id_enc text,          -- Fernet-encrypted, restricted
            email_enc       text,          -- Fernet-encrypted, confidential
            PRIMARY KEY (product_id, tenant_id, employee_id),
            FOREIGN KEY (tenant_id, entity_id) REFERENCES entities (tenant_id, entity_id)
        );

        CREATE TABLE source_documents (
            document_id            uuid PRIMARY KEY,
            product_id             text NOT NULL REFERENCES products,
            tenant_id              text NOT NULL REFERENCES tenants,
            module                 text NOT NULL,
            entity_id              text,  -- NULL = spans entities (visible to '*' scope only)
            logical_name           text NOT NULL,
            filename               text NOT NULL,
            file_type              text NOT NULL,
            checksum_sha256        char(64) NOT NULL,
            size_bytes             bigint NOT NULL,
            version                int NOT NULL DEFAULT 1,
            supersedes_document_id uuid REFERENCES source_documents,
            status                 text NOT NULL,
            classification         text NOT NULL DEFAULT 'internal',
            storage_path           text NOT NULL,
            uploaded_by            text NOT NULL,
            created_at             timestamptz NOT NULL DEFAULT now(),
            UNIQUE (product_id, tenant_id, module, checksum_sha256)
        );
        CREATE INDEX ix_docs_logical ON source_documents (product_id, tenant_id, module, logical_name);

        CREATE TABLE ingestion_jobs (
            job_id        uuid PRIMARY KEY,
            document_id   uuid REFERENCES source_documents,
            product_id    text NOT NULL,
            tenant_id     text NOT NULL,
            module        text NOT NULL,
            entity_id     text,
            stage         text NOT NULL,
            status        text NOT NULL,
            attempts      int NOT NULL DEFAULT 0,
            max_attempts  int NOT NULL DEFAULT 3,
            stage_history jsonb NOT NULL DEFAULT '[]',
            errors        jsonb NOT NULL DEFAULT '[]',
            warnings      jsonb NOT NULL DEFAULT '[]',
            counts        jsonb NOT NULL DEFAULT '{}',
            created_by    text NOT NULL,
            created_at    timestamptz NOT NULL DEFAULT now(),
            updated_at    timestamptz NOT NULL DEFAULT now()
        );

        CREATE TABLE attendance_records (
            record_id             uuid PRIMARY KEY,
            source_document_id    uuid NOT NULL REFERENCES source_documents,
            product_id            text NOT NULL,
            tenant_id             text NOT NULL,
            module                text NOT NULL,
            entity_id             text NOT NULL,
            classification        text NOT NULL DEFAULT 'internal',
            employee_id           text NOT NULL,
            employee_name         text NOT NULL,
            department            text NOT NULL,
            attendance_date       date NOT NULL,
            status                text NOT NULL CHECK (status IN
                ('present','absent','leave','holiday','wfh','half_day','unknown')),
            check_in              time,
            check_out             time,
            total_hours           numeric(5,2),
            source_file           text NOT NULL,
            source_locator        text NOT NULL,
            raw_values            jsonb NOT NULL DEFAULT '{}',
            extraction_method     text NOT NULL,
            extraction_confidence numeric(4,3) NOT NULL CHECK (extraction_confidence BETWEEN 0 AND 1),
            review_required       boolean NOT NULL DEFAULT false,
            review_reasons        text[] NOT NULL DEFAULT '{}',
            is_active             boolean NOT NULL DEFAULT true,
            created_at            timestamptz NOT NULL DEFAULT now(),
            UNIQUE (source_document_id, source_locator)
        );
        CREATE INDEX ix_att_scope_date ON attendance_records (tenant_id, product_id, attendance_date);
        CREATE INDEX ix_att_emp ON attendance_records (tenant_id, employee_id, attendance_date);

        CREATE TABLE document_chunks (
            chunk_id           uuid PRIMARY KEY,
            source_document_id uuid NOT NULL REFERENCES source_documents,
            record_id          uuid REFERENCES attendance_records,
            product_id         text NOT NULL,
            tenant_id          text NOT NULL,
            module             text NOT NULL,
            entity_id          text,
            employee_id        text,
            classification     text NOT NULL DEFAULT 'internal',
            chunk_type         text NOT NULL CHECK (chunk_type IN ('row_card','narrative')),
            text               text NOT NULL,         -- raw; never granted to rag_reader
            text_masked        text NOT NULL,         -- PII-masked; what retrieval sees
            locator            text NOT NULL,
            embedding          vector(384),
            tsv                tsvector GENERATED ALWAYS AS
                               (to_tsvector('english', coalesce(text_masked, ''))) STORED,
            suspicious         boolean NOT NULL DEFAULT false,
            is_active          boolean NOT NULL DEFAULT true,
            created_at         timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_chunks_embedding ON document_chunks USING hnsw (embedding vector_cosine_ops);
        CREATE INDEX ix_chunks_tsv ON document_chunks USING gin (tsv);
        CREATE INDEX ix_chunks_trgm ON document_chunks USING gin (text_masked gin_trgm_ops);

        CREATE TABLE query_responses (
            request_id        text PRIMARY KEY,
            product_id        text NOT NULL,
            tenant_id         text NOT NULL,
            module            text NOT NULL,
            entity_scope      text[] NOT NULL,
            role              text NOT NULL,
            sub               text NOT NULL,
            question_redacted text NOT NULL,
            question_hash     char(64) NOT NULL,
            mode              text,
            sql_executed      text,
            response          jsonb NOT NULL,
            provider          text,
            model             text,
            prompt_version    text,
            retrieval_version text,
            created_at        timestamptz NOT NULL DEFAULT now()
        );

        CREATE TABLE feedback_examples (
            example_id              uuid PRIMARY KEY,
            lineage_id              uuid NOT NULL,
            version                 int NOT NULL,
            status                  text NOT NULL CHECK (status IN ('active','inactive','rejected')),
            question                text NOT NULL,
            question_embedding      vector(384),
            intent_signature        text NOT NULL,
            original_request_id     text,
            original_response       jsonb,
            feedback                text NOT NULL,
            ideal_final_output      text NOT NULL,
            sql_template            text,
            answer_template         text,
            style_notes             text,
            product_id              text NOT NULL,
            tenant_id               text NOT NULL,
            module                  text NOT NULL,
            entity_id               text,
            role_scope              text[] NOT NULL DEFAULT '{}',
            classification          text NOT NULL DEFAULT 'internal',
            reviewer_id             text NOT NULL,
            approved_by             text,
            approved_at             timestamptz,
            model                   text,
            prompt_version          text,
            retrieval_version       text,
            validation              jsonb NOT NULL DEFAULT '{}',
            times_applied           int NOT NULL DEFAULT 0,
            last_applied_request_id text,
            created_at              timestamptz NOT NULL DEFAULT now(),
            updated_at              timestamptz NOT NULL DEFAULT now(),
            UNIQUE (lineage_id, version)
        );

        CREATE TABLE audit_events (
            event_id             bigserial PRIMARY KEY,
            request_id           text NOT NULL,
            ts                   timestamptz NOT NULL DEFAULT now(),
            sub                  text,
            product_id           text,
            tenant_id            text,
            module               text,
            entity_scope         text[],
            role                 text,
            event_type           text NOT NULL,
            query_mode           text,
            retrieved_source_ids text[],
            provider             text,
            model                text,
            fallback_path        text,
            confidence           numeric(4,3),
            outcome              text,
            error_code           text,
            latency_ms           int,
            details              jsonb NOT NULL DEFAULT '{}'
        );
        CREATE INDEX ix_audit_request ON audit_events (request_id);
        """
    )

    # ------------------------------------------------------------------ grants
    # app_rw: ingestion + application writes. No DELETE anywhere (soft-delete via
    # is_active/status), no UPDATE/DELETE on audit_events (append-only).
    # rag_reader: SELECT only, and only on non-PII columns.
    op.execute(
        """
        GRANT SELECT ON products, tenants, tenant_products TO app_rw, rag_reader;
        GRANT SELECT ON entities TO app_rw, rag_reader;
        GRANT SELECT, INSERT, UPDATE ON employees, source_documents, ingestion_jobs,
              attendance_records, document_chunks, query_responses, feedback_examples TO app_rw;
        GRANT SELECT, INSERT ON audit_events TO app_rw;
        GRANT USAGE ON SEQUENCE audit_events_event_id_seq TO app_rw;

        GRANT SELECT (product_id, tenant_id, employee_id, employee_name, entity_id)
              ON employees TO rag_reader;
        GRANT SELECT (document_id, product_id, tenant_id, module, entity_id, logical_name,
                      filename, file_type, version, status, classification, created_at)
              ON source_documents TO rag_reader;
        GRANT SELECT (record_id, source_document_id, product_id, tenant_id, module, entity_id,
                      classification, employee_id, employee_name, department, attendance_date,
                      status, check_in, check_out, total_hours, source_file, source_locator,
                      extraction_method, extraction_confidence, review_required, review_reasons,
                      is_active, created_at)
              ON attendance_records TO rag_reader;
        GRANT SELECT (chunk_id, source_document_id, record_id, product_id, tenant_id, module,
                      entity_id, employee_id, classification, chunk_type, text_masked, locator,
                      embedding, tsv, suspicious, is_active, created_at)
              ON document_chunks TO rag_reader;
        GRANT SELECT ON query_responses, feedback_examples TO rag_reader;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS audit_events, feedback_examples, query_responses, document_chunks,
            attendance_records, ingestion_jobs, source_documents, employees, entities,
            tenant_products, tenants, products CASCADE;
        DROP FUNCTION IF EXISTS app_self_ok(text), app_scope_ok(text, text, text, text, text),
            app_entity_ok(text), app_ctx(text), class_rank(text);
        """
    )
    # Roles are cluster-wide (shared with the test database) and are kept.
