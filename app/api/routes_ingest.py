import uuid

from fastapi import APIRouter, Depends, File, Form, Response, UploadFile
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.deps import db_reader, enforce_request_context, require
from app.api.errors import AppError
from app.db.session import scoped_session
from app.ingestion import service
from app.security.context import SecurityContext
from app.security.rbac import Permission

router = APIRouter(prefix="/v1", tags=["ingestion"])


def _uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise AppError(404, "NOT_FOUND", "Not found.") from exc


@router.post(
    "/ingest",
    summary="Upload an attendance file (CSV, XLSX; more formats in later steps)",
    responses={202: {"description": "Queued for background processing"}},
)
async def ingest(
    response: Response,
    file: UploadFile = File(...),
    logical_name: str | None = Form(None, description="Groups versions of the same file"),
    entity_id: str | None = Form(None),
    product_id: str | None = Form(None),
    tenant_id: str | None = Form(None),
    module: str | None = Form(None),
    ctx: SecurityContext = Depends(require(Permission.INGEST)),
) -> dict:
    enforce_request_context(
        ctx, product_id=product_id, tenant_id=tenant_id, module=module, entity_id=entity_id
    )
    content = await file.read()
    result = service.submit(
        ctx, file.filename or "upload", content, logical_name=logical_name, entity_id=entity_id
    )
    if result["status"] in ("queued", "running", "retrying"):
        response.status_code = 202
    return result


@router.get("/jobs/{job_id}", summary="Processing status of an ingestion job")
def get_job(job_id: str, ctx: SecurityContext = Depends(require(Permission.INGEST))) -> dict:
    with scoped_session(ctx.to_scope(), role="app") as db:
        return service.job_view(db, _uuid(job_id))


@router.post("/jobs/{job_id}/retry", summary="Retry a failed job")
def retry(job_id: str, ctx: SecurityContext = Depends(require(Permission.INGEST))) -> dict:
    return service.retry_job(ctx, _uuid(job_id))


@router.get("/jobs", summary="List jobs (e.g. ?status=failed for the failed-file queue)")
def list_jobs(
    status: str | None = None,
    limit: int = 50,
    ctx: SecurityContext = Depends(require(Permission.INGEST)),
) -> dict:
    with scoped_session(ctx.to_scope(), role="app") as db:
        rows = db.execute(
            text(
                "SELECT j.job_id, j.status, j.stage, j.attempts, j.created_at, d.filename "
                "FROM ingestion_jobs j "
                "LEFT JOIN source_documents d ON d.document_id = j.document_id "
                "WHERE (CAST(:st AS text) IS NULL OR j.status = :st) "
                "ORDER BY j.created_at DESC LIMIT :lim"
            ),
            {"st": status, "lim": max(1, min(limit, 500))},
        ).mappings()
        return {"jobs": [{**r, "job_id": str(r["job_id"])} for r in rows]}


@router.get("/documents", summary="Source documents visible in your scope")
def documents(db: Session = Depends(db_reader)) -> dict:
    rows = db.execute(
        text(
            "SELECT document_id, filename, file_type, logical_name, version, status, entity_id, "
            "created_at FROM source_documents ORDER BY created_at DESC"
        )
    ).mappings()
    return {"documents": [{**r, "document_id": str(r["document_id"])} for r in rows]}


@router.get(
    "/documents/{document_id}/records",
    summary="Canonical records extracted from a document (traceability)",
)
def document_records(document_id: str, limit: int = 100, db: Session = Depends(db_reader)) -> dict:
    rows = db.execute(
        text(
            "SELECT record_id, employee_id, employee_name, department, attendance_date, status, "
            "check_in, check_out, total_hours, source_file, source_locator, extraction_method, "
            "extraction_confidence, review_required, review_reasons, is_active "
            "FROM attendance_records WHERE source_document_id = :d "
            "ORDER BY attendance_date, employee_id LIMIT :lim"
        ),
        {"d": _uuid(document_id), "lim": max(1, min(limit, 1000))},
    ).mappings()
    return {"records": [{**r, "record_id": str(r["record_id"])} for r in rows]}
