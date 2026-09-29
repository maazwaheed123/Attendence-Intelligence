"""Row cards and section-aware narrative windows."""

import datetime as dt

import pytest

from app.ingestion.chunking import MAX_WORDS, OVERLAP, row_card_text, split_block
from app.ingestion.types import NarrativeBlock

pytestmark = pytest.mark.unit

RECORD = {
    "attendance_date": dt.date(2026, 9, 1),
    "employee_id": "E001",
    "employee_name": "Alice Johnson",
    "department": "Engineering",
    "status": "present",
    "check_in": dt.time(9, 5),
    "check_out": dt.time(18, 2),
    "source_file": "attendance.xlsx",
    "source_locator": "sheet=Sep;row=5",
    "review_required": False,
}


def test_row_card_format():
    assert row_card_text(RECORD) == (
        "2026-09-01 | E001 Alice Johnson | Engineering | Present | 09:05-18:02 | "
        "source attendance.xlsx sheet=Sep;row=5"
    )


def test_row_card_without_times_and_awaiting_review():
    card = row_card_text(
        {
            **RECORD,
            "check_in": None,
            "check_out": None,
            "status": "unknown",
            "review_required": True,
        }
    )
    assert card == (
        "2026-09-01 | E001 Alice Johnson | Engineering | Unknown | "
        "source attendance.xlsx sheet=Sep;row=5 | awaiting review"
    )
    assert "?-17:00" in row_card_text({**RECORD, "check_in": None, "check_out": "17:00"})


def test_short_block_is_one_chunk():
    b = NarrativeBlock("section=Notes;para=1", "Notes", "Bob was late on 07/09.")
    assert [(c.locator, c.text) for c in split_block(b)] == [(b.locator, b.text)]


def test_long_block_is_split_with_overlap_inside_its_section():
    words = [f"w{i}" for i in range(700)]
    b = NarrativeBlock("section=Manager remarks;para=1", "Manager remarks", " ".join(words))
    chunks = split_block(b)
    assert [c.locator for c in chunks] == [f"{b.locator};part={i}" for i in (1, 2, 3)]
    assert all(c.section == "Manager remarks" for c in chunks)
    first, second = chunks[0].text.split(), chunks[1].text.split()
    assert len(first) == MAX_WORDS
    assert first[-OVERLAP:] == second[:OVERLAP]
    assert chunks[-1].text.split()[-1] == "w699"
    covered = {w for c in chunks for w in c.text.split()}
    assert covered == set(words)


def test_exact_window_size_is_not_split():
    b = NarrativeBlock("x", "S", " ".join(["w"] * MAX_WORDS))
    assert len(split_block(b)) == 1
