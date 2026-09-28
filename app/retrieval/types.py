"""Shared query-understanding types: modes, intents, slots."""

import datetime as dt
from dataclasses import asdict, dataclass, field

MODES = ("structured", "document", "hybrid", "out_of_scope")

# Intents the template engine can answer deterministically (fallback + cross-check).
TEMPLATE_INTENTS = (
    "list_by_status",
    "count_by_status",
    "attendance_pct",
    "rank",
    "employee_status_on_date",
    "hours",
)
# "other" = structured but only the LLM SQL path can answer it.
INTENTS = (*TEMPLATE_INTENTS, "other")

STATUSES = ("present", "absent", "leave", "holiday", "wfh", "half_day", "unknown")


@dataclass
class Slots:
    date_from: dt.date | None = None
    date_to: dt.date | None = None
    period_label: str | None = None  # "week 2", "September 2026", ...
    entity_id: str | None = None
    entity_name: str | None = None
    employee_id: str | None = None
    employee_name: str | None = None
    extra_employees: list[str] = field(default_factory=list)
    unresolved_person: bool = False  # a name that matches nobody in scope
    unresolved_entity: bool = False  # a department that is not in scope
    status: str | None = None
    rank_direction: str | None = None  # highest | lowest
    group_by: str | None = None  # employee | department
    aggregate: str | None = None  # total | average (hours)

    @property
    def single_date(self) -> bool:
        return self.date_from is not None and self.date_from == self.date_to

    def public(self) -> dict:
        d = asdict(self)
        for k in ("date_from", "date_to"):
            d[k] = d[k].isoformat() if d[k] else None
        return d


@dataclass
class Classification:
    mode: str
    intent: str | None
    slots: Slots
    source: str = "rules"  # rules | llm
    reason: str | None = None  # out_of_scope category: off_topic | pii | unsafe
    flags: list[str] = field(default_factory=list)  # injection rule names

    @property
    def templatable(self) -> bool:
        return self.intent in TEMPLATE_INTENTS
