"""Per-file manifest: which truth rows a file contains, and exactly where.

Locator convention (same everywhere): row numbers count the header row as row 1,
the way a person reads a sheet or table.
"""

from dataclasses import asdict, dataclass, field

STATUS_WORDS = {
    "present": "Present",
    "absent": "Absent",
    "leave": "Leave",
    "wfh": "Work From Home",
    "half_day": "Half Day",
    "holiday": "Holiday",
}
STATUS_CODES = {
    "present": "P",
    "absent": "A",
    "leave": "L",
    "wfh": "WFH",
    "half_day": "HD",
    "holiday": "H",
}
STATUS_PDF = {
    "present": "present",
    "absent": "absent",
    "leave": "on leave",
    "wfh": "remote",
    "half_day": "half-day",
    "holiday": "holiday",
}


@dataclass
class Manifest:
    filename: str
    format: str
    tenant_id: str
    product_id: str = "attendance_ai"
    logical_name: str = ""
    version: int = 1
    kind: str = "attendance"  # attendance | narrative | invalid
    description: str = ""
    expected_failure: str | None = None
    rows: list[dict] = field(default_factory=list)
    narrative: list[dict] = field(default_factory=list)

    def add_row(
        self, locator, truth, *, status=None, rendered=None, expected_review=False, note=None
    ):
        rendered_status = status or truth["status"]
        self.rows.append(
            {
                "locator": locator,
                "tenant_id": truth["tenant_id"],
                "employee_id": truth["employee_id"],
                "attendance_date": truth["attendance_date"],
                "status": rendered_status,
                "matches_truth": rendered_status == truth["status"],
                "expected_review": expected_review,
                "rendered": rendered or {},
                "note": note,
            }
        )

    def add_narrative(self, locator, text, *, section, flags=None):
        self.narrative.append(
            {"locator": locator, "section": section, "text": text, "flags": flags or []}
        )

    def to_dict(self):
        return asdict(self)
