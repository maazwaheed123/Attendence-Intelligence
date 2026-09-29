"""Data passed between ingestion stages."""

from dataclasses import dataclass, field
from typing import Any


class IngestionError(Exception):
    """Base class. `permanent` errors are not retried."""

    permanent = True


class PermanentError(IngestionError):
    """Bad input: retrying cannot help (no header row, corrupt workbook, ...)."""


class TransientError(IngestionError):
    """Temporary failure (I/O, dependency down): safe to retry."""

    permanent = False


@dataclass
class RawRecord:
    """One attendance fact as found in the source, before normalization."""

    locator: str
    values: dict[str, Any]
    raw: dict[str, Any]
    confidence: float = 0.99
    notes: list[str] = field(default_factory=list)


@dataclass
class NarrativeBlock:
    locator: str
    section: str
    text: str


@dataclass
class ParseResult:
    method: str
    records: list[RawRecord] = field(default_factory=list)
    narrative: list[NarrativeBlock] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    restricted_columns: list[str] = field(default_factory=list)
    skipped_rows: list[dict] = field(default_factory=list)


@dataclass
class RowFailure:
    locator: str
    reason: str

    def to_dict(self) -> dict:
        return {"locator": self.locator, "reason": self.reason}
