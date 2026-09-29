"""ORM models mirroring the Alembic schema (migrations are the source of truth;
tests/integration/test_migrations.py fails if the two drift apart)."""

import uuid
from datetime import date, datetime, time
from decimal import Decimal

from pgvector.sqlalchemy import Vector
from sqlalchemy import ARRAY, BigInteger, Computed, ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

EMBED_DIM = 768


class Base(DeclarativeBase):
    pass


class Product(Base):
    __tablename__ = "products"
    product_id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text)


class Tenant(Base):
    __tablename__ = "tenants"
    tenant_id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    allow_external_llm: Mapped[bool] = mapped_column(default=False)
    date_format: Mapped[str] = mapped_column(Text, default="DD/MM/YYYY")
    created_at: Mapped[datetime] = mapped_column(server_default="now()")


class TenantProduct(Base):
    __tablename__ = "tenant_products"
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.tenant_id"), primary_key=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.product_id"), primary_key=True)


class Entity(Base):
    __tablename__ = "entities"
    tenant_id: Mapped[str] = mapped_column(Text, primary_key=True)
    entity_id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    entity_type: Mapped[str] = mapped_column(Text, default="department")


class Employee(Base):
    __tablename__ = "employees"
    product_id: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(Text, primary_key=True)
    employee_id: Mapped[str] = mapped_column(Text, primary_key=True)
    employee_name: Mapped[str] = mapped_column(Text)
    entity_id: Mapped[str] = mapped_column(Text)
    phone_enc: Mapped[str | None] = mapped_column(Text)
    national_id_enc: Mapped[str | None] = mapped_column(Text)
    email_enc: Mapped[str | None] = mapped_column(Text)


class SourceDocument(Base):
    __tablename__ = "source_documents"
    document_id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True)
    product_id: Mapped[str] = mapped_column(Text)
    tenant_id: Mapped[str] = mapped_column(Text)
    module: Mapped[str] = mapped_column(Text)
    entity_id: Mapped[str | None] = mapped_column(Text)
    logical_name: Mapped[str] = mapped_column(Text)
    filename: Mapped[str] = mapped_column(Text)
    file_type: Mapped[str] = mapped_column(Text)
    checksum_sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    version: Mapped[int] = mapped_column(default=1)
    supersedes_document_id: Mapped[uuid.UUID | None] = mapped_column(UUID)
    status: Mapped[str] = mapped_column(Text)
    classification: Mapped[str] = mapped_column(Text, default="internal")
    storage_path: Mapped[str] = mapped_column(Text)
    uploaded_by: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default="now()")


class IngestionJob(Base):
    __tablename__ = "ingestion_jobs"
    job_id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True)
    document_id: Mapped[uuid.UUID | None] = mapped_column(UUID)
    product_id: Mapped[str] = mapped_column(Text)
    tenant_id: Mapped[str] = mapped_column(Text)
    module: Mapped[str] = mapped_column(Text)
    entity_id: Mapped[str | None] = mapped_column(Text)
    stage: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(default=0)
    max_attempts: Mapped[int] = mapped_column(default=3)
    stage_history: Mapped[list] = mapped_column(JSONB, default=list)
    errors: Mapped[list] = mapped_column(JSONB, default=list)
    warnings: Mapped[list] = mapped_column(JSONB, default=list)
    counts: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_by: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default="now()")
    updated_at: Mapped[datetime] = mapped_column(server_default="now()")


class AttendanceRecord(Base):
    __tablename__ = "attendance_records"
    record_id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True)
    source_document_id: Mapped[uuid.UUID] = mapped_column(UUID)
    product_id: Mapped[str] = mapped_column(Text)
    tenant_id: Mapped[str] = mapped_column(Text)
    module: Mapped[str] = mapped_column(Text)
    entity_id: Mapped[str] = mapped_column(Text)
    classification: Mapped[str] = mapped_column(Text, default="internal")
    employee_id: Mapped[str] = mapped_column(Text)
    employee_name: Mapped[str] = mapped_column(Text)
    department: Mapped[str] = mapped_column(Text)
    attendance_date: Mapped[date]
    status: Mapped[str] = mapped_column(Text)
    check_in: Mapped[time | None]
    check_out: Mapped[time | None]
    total_hours: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    source_file: Mapped[str] = mapped_column(Text)
    source_locator: Mapped[str] = mapped_column(Text)
    raw_values: Mapped[dict] = mapped_column(JSONB, default=dict)
    extraction_method: Mapped[str] = mapped_column(Text)
    extraction_confidence: Mapped[Decimal] = mapped_column(Numeric(4, 3))
    review_required: Mapped[bool] = mapped_column(default=False)
    review_reasons: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(server_default="now()")


class DocumentChunk(Base):
    __tablename__ = "document_chunks"
    chunk_id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True)
    source_document_id: Mapped[uuid.UUID] = mapped_column(UUID)
    record_id: Mapped[uuid.UUID | None] = mapped_column(UUID)
    product_id: Mapped[str] = mapped_column(Text)
    tenant_id: Mapped[str] = mapped_column(Text)
    module: Mapped[str] = mapped_column(Text)
    entity_id: Mapped[str | None] = mapped_column(Text)
    employee_id: Mapped[str | None] = mapped_column(Text)
    classification: Mapped[str] = mapped_column(Text, default="internal")
    chunk_type: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    text_masked: Mapped[str] = mapped_column(Text)
    locator: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBED_DIM))
    tsv: Mapped[str | None] = mapped_column(
        TSVECTOR, Computed("to_tsvector('english', coalesce(text_masked, ''))", persisted=True)
    )
    suspicious: Mapped[bool] = mapped_column(default=False)
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(server_default="now()")


class QueryResponse(Base):
    __tablename__ = "query_responses"
    request_id: Mapped[str] = mapped_column(Text, primary_key=True)
    product_id: Mapped[str] = mapped_column(Text)
    tenant_id: Mapped[str] = mapped_column(Text)
    module: Mapped[str] = mapped_column(Text)
    entity_scope: Mapped[list[str]] = mapped_column(ARRAY(Text))
    role: Mapped[str] = mapped_column(Text)
    sub: Mapped[str] = mapped_column(Text)
    question_redacted: Mapped[str] = mapped_column(Text)
    question_hash: Mapped[str] = mapped_column(String(64))
    mode: Mapped[str | None] = mapped_column(Text)
    sql_executed: Mapped[str | None] = mapped_column(Text)
    response: Mapped[dict] = mapped_column(JSONB)
    provider: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(Text)
    retrieval_version: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default="now()")


class FeedbackExample(Base):
    __tablename__ = "feedback_examples"
    example_id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True)
    lineage_id: Mapped[uuid.UUID] = mapped_column(UUID)
    version: Mapped[int]
    status: Mapped[str] = mapped_column(Text)
    question: Mapped[str] = mapped_column(Text)
    question_embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBED_DIM))
    intent_signature: Mapped[str] = mapped_column(Text)
    original_request_id: Mapped[str | None] = mapped_column(Text)
    original_response: Mapped[dict | None] = mapped_column(JSONB)
    feedback: Mapped[str] = mapped_column(Text)
    ideal_final_output: Mapped[str] = mapped_column(Text)
    sql_template: Mapped[str | None] = mapped_column(Text)
    answer_template: Mapped[str | None] = mapped_column(Text)
    style_notes: Mapped[str | None] = mapped_column(Text)
    product_id: Mapped[str] = mapped_column(Text)
    tenant_id: Mapped[str] = mapped_column(Text)
    module: Mapped[str] = mapped_column(Text)
    entity_id: Mapped[str | None] = mapped_column(Text)
    role_scope: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    classification: Mapped[str] = mapped_column(Text, default="internal")
    reviewer_id: Mapped[str] = mapped_column(Text)
    approved_by: Mapped[str | None] = mapped_column(Text)
    approved_at: Mapped[datetime | None]
    model: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(Text)
    retrieval_version: Mapped[str | None] = mapped_column(Text)
    validation: Mapped[dict] = mapped_column(JSONB, default=dict)
    times_applied: Mapped[int] = mapped_column(default=0)
    last_applied_request_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default="now()")
    updated_at: Mapped[datetime] = mapped_column(server_default="now()")


class AuditEvent(Base):
    __tablename__ = "audit_events"
    event_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(Text)
    ts: Mapped[datetime] = mapped_column(server_default="now()")
    sub: Mapped[str | None] = mapped_column(Text)
    product_id: Mapped[str | None] = mapped_column(Text)
    tenant_id: Mapped[str | None] = mapped_column(Text)
    module: Mapped[str | None] = mapped_column(Text)
    entity_scope: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    role: Mapped[str | None] = mapped_column(Text)
    event_type: Mapped[str] = mapped_column(Text)
    query_mode: Mapped[str | None] = mapped_column(Text)
    retrieved_source_ids: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    provider: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    fallback_path: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    outcome: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[int | None]
    details: Mapped[dict] = mapped_column(JSONB, default=dict)
