import datetime as dt

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, field_validator

from app import orchestrator
from app.api.deps import enforce_request_context, require
from app.api.errors import AppError
from app.security.context import SecurityContext
from app.security.rbac import Permission

router = APIRouter(prefix="/v1", tags=["query"])


class QueryFilters(BaseModel):
    date_from: dt.date | None = None
    date_to: dt.date | None = None
    entity_id: str | None = Field(None, max_length=64)
    employee_id: str | None = Field(None, max_length=32)


class QueryRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=500)
    filters: QueryFilters | None = None
    include_debug: bool = False

    @field_validator("question")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("question must not be blank")
        return v


@router.post("/query", summary="Ask an attendance question (answers only from permitted data)")
def query(
    body: QueryRequest,
    request: Request,
    ctx: SecurityContext = Depends(require(Permission.QUERY)),
) -> dict:
    filters = body.filters or QueryFilters()
    # Explicit filters outside the token scope are refused BEFORE any retrieval.
    enforce_request_context(ctx, entity_id=filters.entity_id)
    if ctx.role == "employee" and filters.employee_id not in (None, ctx.employee_id):
        raise AppError(403, "CONTEXT_MISMATCH", "employee_id is outside the authenticated scope.")
    if filters.date_from and filters.date_to and filters.date_from > filters.date_to:
        raise AppError(422, "VALIDATION_ERROR", "filters.date_from must not be after date_to.")
    return orchestrator.answer(
        ctx,
        request.state.request_id,
        body.question,
        filters.model_dump(exclude_none=True),
        include_debug=body.include_debug,
    )


@router.get("/query/{request_id}", summary="A stored answer (only within your scope)")
def get_query(request_id: str, ctx: SecurityContext = Depends(require(Permission.QUERY))) -> dict:
    stored = orchestrator.stored_response(ctx, request_id[:128])
    if stored is None:
        raise AppError(404, "NOT_FOUND", "Not found.")
    return stored
