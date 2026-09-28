import datetime as dt
from typing import Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field, model_validator

from app.api.deps import enforce_request_context, require
from app.api.errors import AppError
from app.export import dataset, renderers
from app.governance import audit
from app.security.context import SecurityContext
from app.security.rbac import Permission

router = APIRouter(prefix="/v1", tags=["export"])


class ExportFilters(BaseModel):
    date_from: dt.date | None = None
    date_to: dt.date | None = None
    entity_id: str | None = Field(None, max_length=64)
    employee_id: str | None = Field(None, max_length=32)
    status: Literal[dataset.STATUSES] | None = None  # type: ignore[valid-type]


class ExportRequest(BaseModel):
    source: Literal["records", "query"] = "records"
    request_id: str | None = Field(None, min_length=1, max_length=128)
    filters: ExportFilters | None = None
    format: Literal["json", "xlsx", "pdf"] = "json"

    @model_validator(mode="after")
    def _check(self):
        if self.source == "query" and not self.request_id:
            raise ValueError("request_id is required when source is 'query'")
        f = self.filters
        if f and f.date_from and f.date_to and f.date_from > f.date_to:
            raise ValueError("filters.date_from must not be after date_to")
        return self


@router.post(
    "/export",
    summary="Export permitted records (or a stored answer's evidence) as JSON, XLSX or PDF",
    response_class=Response,
)
def export(
    body: ExportRequest,
    request: Request,
    ctx: SecurityContext = Depends(require(Permission.EXPORT)),
) -> Response:
    filters = body.filters or ExportFilters()
    # Explicit filters outside the token scope are refused BEFORE any read.
    enforce_request_context(ctx, entity_id=filters.entity_id)
    rid = request.state.request_id
    try:
        ds = dataset.build(ctx, rid, body.model_dump(exclude_none=True))
    except dataset.ExportNotFound as exc:
        # same message as any unknown id: another scope's request does not "exist"
        raise AppError(404, "NOT_FOUND", "Not found.") from exc
    content = renderers.RENDERERS[body.format](ds)
    checksum = ds.metadata["checksum"]
    audit.record(
        "export",
        rid,
        outcome="ok",
        retrieved_source_ids=ds.row_ids[:500],
        details={
            "format": body.format,
            "source": body.source,
            "source_request_id": body.request_id,
            "record_count": ds.metadata["record_count"],
            "truncated": ds.metadata["truncated"],
            "checksum": checksum,
            "classification": ds.metadata["classification"],
        },
        **audit.context_fields(ctx),
    )
    return Response(
        content=content,
        media_type=renderers.MEDIA_TYPES[body.format],
        headers={
            "Content-Disposition": f'attachment; filename="attendance-export-{rid}.{body.format}"',
            "X-Export-Checksum": checksum,
            "X-Export-Record-Count": str(ds.metadata["record_count"]),
        },
    )
