import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.api.deps import require
from app.api.errors import AppError
from app.feedback import service, store
from app.governance import audit
from app.security.context import SecurityContext
from app.security.rbac import Permission

router = APIRouter(prefix="/v1", tags=["feedback"])


class FeedbackRequest(BaseModel):
    original_request_id: str = Field(..., min_length=1, max_length=128)
    question: str | None = Field(None, max_length=500)
    agent_response: str | None = Field(None, max_length=5000)
    feedback: str = Field(..., min_length=1, max_length=2000)
    ideal_final_output: str = Field(..., min_length=1, max_length=2000)
    approve: bool = True


def _example(ctx: SecurityContext, example_id: str) -> dict:
    try:
        uuid.UUID(example_id)
    except ValueError as exc:
        raise AppError(404, "NOT_FOUND", "Not found.") from exc
    ex = store.get(ctx.to_scope(), example_id)  # RLS: other scopes see nothing
    if ex is None:
        raise AppError(404, "NOT_FOUND", "Not found.")
    return ex


def _may_manage(ctx: SecurityContext, ex: dict) -> None:
    # hr_admin manages any example in scope; reviewers only their own (PLAN D.2)
    if ctx.role != "hr_admin" and ex["reviewer_id"] != ctx.sub:
        raise AppError(403, "FORBIDDEN", "Reviewers may only manage their own feedback.")


def _audit(event: str, request: Request, ctx: SecurityContext, ex: dict, **details) -> None:
    audit.record(
        event,
        request.state.request_id,
        outcome="ok",
        details={"example_id": ex["example_id"], "version": ex["version"], **details},
        **audit.context_fields(ctx),
    )


@router.post("/feedback", summary="Submit a correction + ideal output for a previous answer")
def submit(
    body: FeedbackRequest,
    request: Request,
    ctx: SecurityContext = Depends(require(Permission.FEEDBACK_SUBMIT)),
) -> dict:
    if not body.approve:
        raise AppError(422, "VALIDATION_ERROR", "Feedback must be approved (approve=true).")
    return service.submit(ctx, request.state.request_id, body.model_dump())


@router.get("/feedback", summary="Feedback examples in your scope")
def list_feedback(
    status: str | None = None,
    limit: int = 100,
    ctx: SecurityContext = Depends(require(Permission.FEEDBACK_SUBMIT)),
) -> dict:
    return {"examples": store.list_examples(ctx.to_scope(), status, limit)}


@router.get("/feedback/{example_id}", summary="One feedback example")
def get_feedback(
    example_id: str, ctx: SecurityContext = Depends(require(Permission.FEEDBACK_SUBMIT))
) -> dict:
    return _example(ctx, example_id)


@router.post("/feedback/{example_id}/deactivate", summary="Stop applying an example")
def deactivate(
    example_id: str,
    request: Request,
    ctx: SecurityContext = Depends(require(Permission.FEEDBACK_MANAGE)),
) -> dict:
    ex = _example(ctx, example_id)
    _may_manage(ctx, ex)
    store.set_status(ctx.to_scope(), example_id, "inactive")
    _audit("feedback_deactivated", request, ctx, ex)
    return _example(ctx, example_id)


@router.post("/feedback/{example_id}/rollback", summary="Reactivate the previous version")
def rollback(
    example_id: str,
    request: Request,
    ctx: SecurityContext = Depends(require(Permission.FEEDBACK_MANAGE)),
) -> dict:
    ex = _example(ctx, example_id)
    _may_manage(ctx, ex)
    restored = store.rollback(ctx.to_scope(), ex)
    if restored is None:
        raise AppError(409, "CONFLICT", "No earlier version to roll back to.")
    _audit("feedback_rolled_back", request, ctx, ex, restored=restored["example_id"])
    return {"rolled_back": ex["example_id"], "active": restored}
