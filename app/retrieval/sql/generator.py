"""Text-to-SQL through the model router, with validation and one repair round.

Flow: generate -> validate -> (rejected) regenerate once with the validator's
reason -> validate. A second rejection, or no available provider, returns None
and the caller falls back to the template engine.
"""

import logging
from dataclasses import dataclass, field

from pydantic import BaseModel

from app.generation.providers.base import LLMResult, ProviderUnavailable
from app.retrieval.sql.schema_prompt import SQL_SYSTEM
from app.retrieval.sql.validator import SqlRejected, validate
from app.retrieval.types import Slots

log = logging.getLogger(__name__)


class SqlDraft(BaseModel):
    sql: str
    explanation: str = ""


@dataclass
class Generated:
    sql: str | None
    result: LLMResult | None = None
    rejections: list[str] = field(default_factory=list)
    attempts: list[dict] = field(default_factory=list)


def resolved_parameters(s: Slots) -> str:
    lines = []
    if s.date_from:
        lines.append(
            f"date: {s.date_from.isoformat()}"
            if s.single_date
            else f"date range: {s.date_from.isoformat()} to {s.date_to.isoformat()}"
        )
    if s.employee_id:
        lines.append(f"employee_id: {s.employee_id}")
    if s.extra_employees:
        lines.append(f"other employee_ids: {', '.join(s.extra_employees)}")
    if s.entity_id:
        lines.append(f"entity_id: {s.entity_id}")
    if s.status:
        lines.append(f"status: {s.status}")
    if s.rank_direction:
        lines.append(f"rank: {s.rank_direction} ({s.group_by or 'employee'})")
    return "\n".join(lines) or "none"


def generate(question: str, s: Slots, router, audit: dict | None = None) -> Generated:
    messages = [
        {"role": "system", "content": SQL_SYSTEM},
        {
            "role": "user",
            "content": f"TASK: sql\n<question>{question}</question>\n"
            f"Resolved parameters:\n{resolved_parameters(s)}",
        },
    ]
    out = Generated(sql=None)
    for attempt in range(2):
        try:
            result = router.complete(
                messages, response_model=SqlDraft, purpose="text_to_sql", audit=audit
            )
        except ProviderUnavailable as exc:
            out.attempts = exc.attempts
            return out
        out.result = result
        try:
            out.sql = validate(result.parsed.sql)
            return out
        except SqlRejected as exc:
            out.rejections.append(str(exc))
            log.info("LLM SQL rejected (attempt %d): %s", attempt + 1, exc)
            messages = [
                *messages,
                {"role": "assistant", "content": result.text[:2000]},
                {
                    "role": "user",
                    "content": f"TASK: sql\nThe SQL was rejected: {exc}. "
                    "Reply with corrected JSON that follows the SQL rules.",
                },
            ]
    return out
