"""Ingestion orchestration.

submit()  - validate, checksum, idempotency, versioning, store file, create
            document + job, then run synchronously (INGEST_SYNC) or enqueue (RQ).
process() - stages: parsed -> normalized -> persisted -> completed. Each stage is
            recorded on the job; errors are permanent (fail now) or transient (retry).

Every database write runs under the uploader's DbScope with the app_rw role, so
RLS WITH CHECK guarantees ingestion can only create data inside that scope.
"""

import hashlib
import logging
import re
import uuid
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path, PurePath

from sqlalchemy import text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.errors import AppError
from app.config import get_settings
from app.db.models import AttendanceRecord, DocumentChunk, IngestionJob, SourceDocument
from app.db.session import scoped_session
from app.ingestion.detect import detect
from app.ingestion.normalize.normalizer import Roster, normalize
from app.ingestion.parsers import csv_parser, docx_parser, pdf_parser, xlsx_parser
from app.ingestion.types import IngestionError, ParseResult, PermanentError
from app.security import injection, pii
from app.security.context import SecurityContext
from app.security.scope import DbScope

log = logging.getLogger(__name__)

NS_RECORD = uuid.UUID("0b3c2f0e-6c3e-4c8e-9a51-2a8f6b1d7c11")
PARSERS = {
    "csv": csv_parser.parse,
    "xlsx": xlsx_parser.parse,
    "docx": docx_parser.parse,
    "pdf": pdf_parser.parse,
}
CONFIDENTIAL_SECTIONS = ("remark", "comment", "confidential", "disciplinary")
_VERSION_SUFFIX = re.compile(r"[_\- ]v\d+$", re.I)
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]")


def register_parser(file_type: str, fn) -> None:
    PARSERS[file_type] = fn


def logical_name_for(filename: str) -> str:
    return _VERSION_SUFFIX.sub("", PurePath(filename).stem).lower()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def ingestion_scope(ctx: SecurityContext) -> DbScope:
    """Write scope for ingestion: the uploader's tenant/product/module/entities, but
    SYSTEM clearance. Classification is assigned by the pipeline from content (e.g.
    manager remarks -> confidential), not capped by who uploaded the file; RLS WITH
    CHECK still pins every row to the uploader's tenant, product, module and entities.
    The uploader can read back only what their own clearance allows."""
    return replace(ctx.to_scope(), clearance="restricted")


def scope_to_dict(scope: DbScope) -> dict:
    d = asdict(scope)
    d["entities"] = list(scope.entities)
    return d


def scope_from_dict(d: dict) -> DbScope:
    return DbScope(**{**d, "entities": tuple(d["entities"])})


# --------------------------------------------------------------------------- submit


def submit(
    ctx: SecurityContext,
    filename: str,
    content: bytes,
    *,
    logical_name: str | None = None,
    entity_id: str | None = None,
) -> dict:
    s = get_settings()
    filename = _SAFE_NAME.sub("_", PurePath(filename or "upload").name)[:200]
    if not content:
        raise AppError(422, "VALIDATION_ERROR", "File is empty.")
    if len(content) > s.max_upload_mb * 1024 * 1024:
        raise AppError(413, "PAYLOAD_TOO_LARGE", f"File exceeds {s.max_upload_mb} MB.")
    file_type, problem = detect(filename, content)
    if file_type is None:
        raise AppError(415, "UNSUPPORTED_FILE", problem)
    if file_type not in PARSERS:
        raise AppError(415, "UNSUPPORTED_FILE", f"'{file_type}' ingestion is not enabled yet.")

    checksum = hashlib.sha256(content).hexdigest()
    logical = (logical_name or logical_name_for(filename)).strip().lower()[:120]
    if entity_id is None and not ctx.all_entities and len(ctx.entities) == 1:
        entity_id = ctx.entities[0]
    scope = ingestion_scope(ctx)
    job_id = uuid.uuid4()

    with scoped_session(scope, role="app") as db:
        existing = db.execute(
            text(
                "SELECT document_id FROM source_documents WHERE checksum_sha256 = :c "
                "AND product_id = :p AND tenant_id = :t AND module = :m"
            ),
            {"c": checksum, "p": ctx.product_id, "t": ctx.tenant_id, "m": ctx.module},
        ).scalar()
        if existing:
            db.add(_job(job_id, existing, ctx, entity_id, "completed", "duplicate"))
            db.flush()
            return job_view(db, job_id, duplicate_of=existing, checksum=checksum)

        prev = db.execute(
            text(
                "SELECT document_id, version FROM source_documents "
                "WHERE logical_name = :ln AND status IN ('completed', 'superseded') "
                "ORDER BY version DESC LIMIT 1"
            ),
            {"ln": logical},
        ).first()
        doc_id = uuid.uuid4()
        path = (
            Path(s.upload_dir)
            / ctx.tenant_id
            / ctx.product_id
            / f"{checksum}{PurePath(filename).suffix.lower()}"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(content)
        db.add(
            SourceDocument(
                document_id=doc_id,
                product_id=ctx.product_id,
                tenant_id=ctx.tenant_id,
                module=ctx.module,
                entity_id=entity_id,
                logical_name=logical,
                filename=filename,
                file_type=file_type,
                checksum_sha256=checksum,
                size_bytes=len(content),
                version=(prev.version + 1) if prev else 1,
                supersedes_document_id=prev.document_id if prev else None,
                status="received",
                classification="internal",
                storage_path=str(path),
                uploaded_by=ctx.sub,
            )
        )
        try:
            db.flush()  # document first: the job row references it
        except IntegrityError as exc:
            if "checksum_sha256" not in str(exc.orig):
                raise
            # Same file already ingested in this tenant, outside the caller's entity scope.
            raise AppError(
                409, "CONFLICT", "This file was already ingested for this tenant."
            ) from exc
        db.add(_job(job_id, doc_id, ctx, entity_id, "received", "queued"))

    scope_dict = scope_to_dict(scope)
    if s.ingest_sync:
        run_with_retries(job_id, scope_dict)
    else:
        enqueue(job_id, scope_dict)
    with scoped_session(scope, role="app") as db:
        return job_view(db, job_id, checksum=checksum)


def _job(job_id, doc_id, ctx, entity_id, stage, status) -> IngestionJob:
    return IngestionJob(
        job_id=job_id,
        document_id=doc_id,
        product_id=ctx.product_id,
        tenant_id=ctx.tenant_id,
        module=ctx.module,
        entity_id=entity_id,
        stage=stage,
        status=status,
        attempts=0,
        max_attempts=3,
        stage_history=[{"stage": stage, "status": status, "at": _now()}],
        errors=[],
        warnings=[],
        counts={},
        created_by=ctx.sub,
    )


def enqueue(job_id, scope_dict: dict) -> None:
    from rq import Queue, Retry

    from app.ingestion.worker import get_queue_connection, run_job

    Queue("ingestion", connection=get_queue_connection()).enqueue(
        run_job, str(job_id), scope_dict, retry=Retry(max=2, interval=[10, 30]), job_timeout=900
    )


def run_with_retries(job_id, scope_dict: dict) -> str:
    """Synchronous mode: retry transient failures in-process."""
    outcome = "retry"
    for _ in range(3):
        outcome = process(job_id, scope_dict)
        if outcome != "retry":
            break
    return outcome


def retry_job(ctx: SecurityContext, job_id: uuid.UUID) -> dict:
    scope = ingestion_scope(ctx)
    with scoped_session(scope, role="app") as db:
        job = db.get(IngestionJob, job_id)
        if job is None:
            raise AppError(404, "NOT_FOUND", "Job not found.")
        if job.status != "failed":
            raise AppError(
                409, "CONFLICT", f"Only failed jobs can be retried (status={job.status})."
            )
        job.status, job.attempts = "queued", 0
        job.stage_history = [
            *job.stage_history,
            {"stage": job.stage, "status": "manual_retry", "at": _now()},
        ]
    scope_dict = scope_to_dict(scope)
    if get_settings().ingest_sync:
        run_with_retries(job_id, scope_dict)
    else:
        enqueue(job_id, scope_dict)
    with scoped_session(scope, role="app") as db:
        return job_view(db, job_id)


# --------------------------------------------------------------------------- process


def _set_stage(db: Session, job: IngestionJob, stage: str, status: str = "running", **extra):
    job.stage, job.status = stage, status
    job.stage_history = [
        *job.stage_history,
        {"stage": stage, "status": status, "at": _now(), **extra},
    ]
    job.updated_at = datetime.now(UTC)


def process(job_id, scope_dict: dict, *, raise_transient: bool = False) -> str:
    """Run one attempt. Returns 'completed' | 'failed' | 'retry'."""
    scope = scope_from_dict(scope_dict)
    s = get_settings()
    job_id = uuid.UUID(str(job_id))

    with scoped_session(scope, role="app") as db:
        job = db.get(IngestionJob, job_id)
        if job is None:
            log.error("job %s not visible in scope", job_id)
            return "failed"
        if job.status in ("completed", "duplicate"):
            return "completed"
        doc = db.get(SourceDocument, job.document_id)
        job.attempts += 1
        attempt, max_attempts = job.attempts, job.max_attempts
        _set_stage(db, job, "validated", attempt=attempt)
        doc.status = "processing"
        storage_path, file_type, filename, checksum = (
            doc.storage_path,
            doc.file_type,
            doc.filename,
            doc.checksum_sha256,
        )
        prev_doc_id, doc_id = doc.supersedes_document_id, doc.document_id

    try:
        content = Path(storage_path).read_bytes()
        parsed: ParseResult = PARSERS[file_type](content, filename)
        with scoped_session(scope, role="app") as db:
            _set_stage(db, db.get(IngestionJob, job_id), "parsed", rows=len(parsed.records))
            roster = Roster.load(db)
            date_format = db.execute(
                text("SELECT date_format FROM tenants WHERE tenant_id = :t"), {"t": scope.tenant_id}
            ).scalar_one()
        drafts, failures = normalize(
            parsed, roster, date_format=date_format, review_threshold=s.ocr_review_threshold
        )
        if not drafts and parsed.records:
            raise PermanentError("no row could be normalized (see row failures)")

        with scoped_session(scope, role="app") as db:
            job = db.get(IngestionJob, job_id)
            _set_stage(db, job, "normalized", valid=len(drafts), failed=len(failures))
            _persist(db, scope, doc_id, filename, checksum, parsed.method, drafts)
            doc = db.get(SourceDocument, doc_id)
            narrative = _persist_narrative(db, scope, doc, checksum, parsed.narrative)
            diff = _supersede(db, prev_doc_id, drafts) if prev_doc_id else None
            doc.status = "completed"
            if doc.entity_id is None:
                entities = {d["entity_id"] for d in drafts}
                if len(entities) == 1:
                    doc.entity_id = entities.pop()
            _set_stage(db, job, "persisted", records=len(drafts))
            job.counts = {
                "rows_total": len(parsed.records),
                "records_created": len(drafts),
                "row_failures": len(failures),
                "review_required": sum(d["review_required"] for d in drafts),
                "skipped_rows": len(parsed.skipped_rows),
                "narrative_chunks": narrative["chunks"],
                "suspicious_chunks": len(narrative["suspicious"]),
                "pii_masked": narrative["pii_masked"],
                **({"diff": diff} if diff else {}),
            }
            job.errors = [f.to_dict() for f in failures[:200]]
            warnings = list(parsed.warnings)
            if parsed.restricted_columns:
                warnings.append(
                    "restricted columns dropped at ingestion: "
                    + ", ".join(parsed.restricted_columns)
                )
            warnings += [
                f"possible prompt injection at {loc} ({', '.join(rules)}): stored as data, flagged"
                for loc, rules in narrative["suspicious"]
            ]
            job.warnings = warnings
            _set_stage(db, job, "completed", "completed")
        return "completed"

    except Exception as exc:  # noqa: BLE001 - classify every failure onto the job
        permanent = isinstance(exc, IngestionError) and exc.permanent
        will_retry = not permanent and attempt < max_attempts
        message = (
            str(exc) if isinstance(exc, IngestionError) else f"{exc.__class__.__name__}: {exc}"
        )
        if not isinstance(exc, IngestionError):
            log.exception("ingestion job %s attempt %s failed", job_id, attempt)
        with scoped_session(scope, role="app") as db:
            job = db.get(IngestionJob, job_id)
            job.errors = [
                *job.errors,
                {"stage": job.stage, "attempt": attempt, "message": message[:500]},
            ]
            if will_retry:
                _set_stage(db, job, job.stage, "retrying", attempt=attempt)
            else:
                _set_stage(db, job, job.stage, "failed", attempt=attempt)
                db.get(SourceDocument, doc_id).status = "failed"
        if will_retry and raise_transient:
            raise
        return "retry" if will_retry else "failed"


def _persist(db: Session, scope: DbScope, doc_id, filename, checksum, method, drafts):
    if not drafts:
        return
    rows = [
        {
            **d,
            "record_id": uuid.uuid5(NS_RECORD, f"{checksum}|{d['source_locator']}"),
            "source_document_id": doc_id,
            "product_id": scope.product_id,
            "tenant_id": scope.tenant_id,
            "module": scope.module,
            "classification": "internal",
            "source_file": filename,
            "extraction_method": method,
            "is_active": True,
        }
        for d in drafts
    ]
    stmt = pg_insert(AttendanceRecord).values(rows)
    db.execute(stmt.on_conflict_do_nothing(index_elements=["source_document_id", "source_locator"]))


def _persist_narrative(db: Session, scope: DbScope, doc: SourceDocument, checksum, blocks) -> dict:
    """Narrative text -> document_chunks (embedded in Step 9).

    Raw text is kept for audit (never readable by rag_reader); retrieval only ever
    sees text_masked. Injection attempts are flagged, not removed.
    """
    stats = {"chunks": 0, "suspicious": [], "pii_masked": 0}
    rows = []
    for b in blocks:
        masked, counts = pii.mask_text(b.text)
        rules = injection.detect(b.text)
        if rules:
            stats["suspicious"].append((b.locator, rules))
        stats["pii_masked"] += sum(counts.values())
        confidential = any(k in b.section.lower() for k in CONFIDENTIAL_SECTIONS)
        rows.append(
            {
                "chunk_id": uuid.uuid5(NS_RECORD, f"{checksum}|narrative|{b.locator}"),
                "source_document_id": doc.document_id,
                "product_id": scope.product_id,
                "tenant_id": scope.tenant_id,
                "module": scope.module,
                "entity_id": doc.entity_id,
                "classification": "confidential" if confidential else "internal",
                "chunk_type": "narrative",
                "text": b.text,
                "text_masked": masked,
                "locator": b.locator,
                "suspicious": bool(rules),
                "is_active": True,
            }
        )
    if rows:
        db.execute(
            pg_insert(DocumentChunk)
            .values(rows)
            .on_conflict_do_nothing(index_elements=["chunk_id"])
        )
    stats["chunks"] = len(rows)
    return stats


def _supersede(db: Session, prev_doc_id, drafts) -> dict:
    """Deactivate the previous version's records and describe what changed."""
    old = {
        (r.employee_id, r.attendance_date): (r.status, r.check_in, r.check_out)
        for r in db.execute(
            text(
                "SELECT employee_id, attendance_date, status, check_in, check_out "
                "FROM attendance_records WHERE source_document_id = :d AND is_active"
            ),
            {"d": prev_doc_id},
        )
    }
    new = {
        (d["employee_id"], d["attendance_date"]): (d["status"], d["check_in"], d["check_out"])
        for d in drafts
    }
    changed = [
        {"employee_id": k[0], "date": k[1].isoformat(), "from": old[k][0], "to": new[k][0]}
        for k in sorted(new.keys() & old.keys())
        if new[k] != old[k]
    ]
    db.execute(
        update(AttendanceRecord)
        .where(AttendanceRecord.source_document_id == prev_doc_id)
        .values(is_active=False)
    )
    db.execute(
        update(DocumentChunk)
        .where(DocumentChunk.source_document_id == prev_doc_id)
        .values(is_active=False)
    )
    db.execute(
        update(SourceDocument)
        .where(SourceDocument.document_id == prev_doc_id)
        .values(status="superseded")
    )
    return {
        "previous_document_id": str(prev_doc_id),
        "added": len(new.keys() - old.keys()),
        "removed": len(old.keys() - new.keys()),
        "changed": len(changed),
        "changed_rows": changed[:50],
    }


# --------------------------------------------------------------------------- views


def job_view(db: Session, job_id, *, duplicate_of=None, checksum=None) -> dict:
    job = db.get(IngestionJob, job_id)
    if job is None:
        raise AppError(404, "NOT_FOUND", "Job not found.")
    doc = db.get(SourceDocument, job.document_id) if job.document_id else None
    return {
        "job_id": str(job.job_id),
        "document_id": str(job.document_id) if job.document_id else None,
        "filename": doc.filename if doc else None,
        "file_type": doc.file_type if doc else None,
        "checksum_sha256": (doc.checksum_sha256 if doc else checksum),
        "logical_name": doc.logical_name if doc else None,
        "version": doc.version if doc else None,
        "supersedes_document_id": str(doc.supersedes_document_id)
        if doc and doc.supersedes_document_id
        else None,
        "duplicate_of": str(duplicate_of) if duplicate_of else None,
        "status": job.status,
        "stage": job.stage,
        "completed": job.status in ("completed", "duplicate"),
        "attempts": job.attempts,
        "max_attempts": job.max_attempts,
        "validation": {"accepted": True, "file_type": doc.file_type if doc else None},
        "counts": job.counts,
        "failures": job.errors,
        "warnings": job.warnings,
        "stage_history": job.stage_history,
    }
