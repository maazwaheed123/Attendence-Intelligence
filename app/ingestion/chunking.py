"""Chunk text for retrieval.

Row cards: one short, self-describing line per canonical attendance record, so a
document/hybrid search can find (and cite) individual records:
    2026-09-01 | E001 Alice Johnson | Engineering | Present | 09:05-18:02 |
    source tenant_a_sep_v2.csv row=2
Narrative: parser blocks are already one section each; long blocks are split into
~300-word windows with 50 words of overlap, never across sections.
"""

from dataclasses import dataclass

from app.ingestion.types import NarrativeBlock

MAX_WORDS = 300
OVERLAP = 50

STATUS_LABEL = {
    "present": "Present",
    "absent": "Absent",
    "leave": "Leave",
    "holiday": "Holiday",
    "wfh": "Work from home",
    "half_day": "Half day",
    "unknown": "Unknown",
}


def _hhmm(t) -> str | None:
    if t is None:
        return None
    return t if isinstance(t, str) else t.strftime("%H:%M")


def row_card_text(r: dict) -> str:
    """r: an attendance record (attendance_date, employee_id, employee_name, department,
    status, check_in, check_out, source_file, source_locator, review_required)."""
    d = r["attendance_date"]
    parts = [
        d if isinstance(d, str) else d.isoformat(),
        f"{r['employee_id']} {r.get('employee_name') or ''}".strip(),
        r.get("department") or "",
        STATUS_LABEL.get(r["status"], str(r["status"])),
    ]
    cin, cout = _hhmm(r.get("check_in")), _hhmm(r.get("check_out"))
    if cin or cout:
        parts.append(f"{cin or '?'}-{cout or '?'}")
    parts.append(f"source {r['source_file']} {r['source_locator']}")
    if r.get("review_required"):
        parts.append("awaiting review")
    return " | ".join(p for p in parts if p)


@dataclass
class TextChunk:
    locator: str
    section: str
    text: str


def split_block(block: NarrativeBlock, max_words: int = MAX_WORDS, overlap: int = OVERLAP):
    """One section -> one or more overlapping windows (locator gets ';part=N')."""
    words = block.text.split()
    if len(words) <= max_words:
        return [TextChunk(block.locator, block.section, block.text)]
    step = max_words - overlap
    chunks, start, part = [], 0, 1
    while start < len(words):
        window = words[start : start + max_words]
        chunks.append(TextChunk(f"{block.locator};part={part}", block.section, " ".join(window)))
        if start + max_words >= len(words):
            break
        start += step
        part += 1
    return chunks
