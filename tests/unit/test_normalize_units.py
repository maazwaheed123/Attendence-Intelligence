"""Status vocabulary, date/time parsing, header mapping, file detection."""

from datetime import date, datetime, time

import pytest

from app.ingestion.detect import detect
from app.ingestion.normalize.datetimes import parse_date, parse_time, total_hours
from app.ingestion.normalize.mapping import find_header, map_header
from app.ingestion.normalize.status import map_status

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("P", "present"),
        ("present", "present"),
        (" Present ", "present"),
        ("✓", "present"),
        ("A", "absent"),
        ("Absent", "absent"),
        ("not present", "absent"),
        ("L", "leave"),
        ("on leave", "leave"),
        ("Sick Leave", "leave"),
        ("PL", "leave"),
        ("H", "holiday"),
        ("Public Holiday", "holiday"),
        ("WFH", "wfh"),
        ("remote", "wfh"),
        ("Work From Home", "wfh"),
        ("HD", "half_day"),
        ("half-day", "half_day"),
        ("Half Day", "half_day"),
    ],
)
def test_status_synonyms(raw, expected):
    assert map_status(raw) == (expected, True)


@pytest.mark.parametrize("raw", ["P?", "maybe", "", None, "~~Absent~~"])
def test_unknown_status(raw):
    assert map_status(raw) == ("unknown", False)


@pytest.mark.parametrize(
    ("raw", "fmt", "expected"),
    [
        ("2026-09-01", "DD/MM/YYYY", date(2026, 9, 1)),
        ("01/09/2026", "DD/MM/YYYY", date(2026, 9, 1)),
        ("01/09/2026", "MM/DD/YYYY", date(2026, 1, 9)),
        ("16-Sep-2026", "DD/MM/YYYY", date(2026, 9, 16)),
        ("15 September 2026", "DD/MM/YYYY", date(2026, 9, 15)),
        (date(2026, 9, 3), "DD/MM/YYYY", date(2026, 9, 3)),
        (datetime(2026, 9, 3, 0, 0), "DD/MM/YYYY", date(2026, 9, 3)),
        (46266, "DD/MM/YYYY", date(2026, 9, 1)),
    ],
)
def test_dates(raw, fmt, expected):
    assert parse_date(raw, fmt) == (expected, None)


@pytest.mark.parametrize("raw", ["09/29/2026", "2026-13-01", "yesterday", "", None, "01/01/1850"])
def test_bad_dates(raw):
    d, problem = parse_date(raw, "DD/MM/YYYY")
    assert d is None and problem


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("09:05", time(9, 5)),
        ("9:05", time(9, 5)),
        ("9:05 AM", time(9, 5)),
        ("5:40 PM", time(17, 40)),
        ("12:10 AM", time(0, 10)),
        (time(8, 1, 30), time(8, 1)),
        ("-", None),
        ("", None),
        (None, None),
    ],
)
def test_times(raw, expected):
    assert parse_time(raw) == (expected, None)


def test_bad_time_reported():
    t, problem = parse_time("25:99")
    assert t is None and problem


def test_total_hours_incl_overnight():
    assert total_hours(time(9, 0), time(17, 30)) == 8.5
    assert total_hours(time(22, 0), time(6, 0)) == 8.0
    assert total_hours(None, time(6, 0)) is None


@pytest.mark.parametrize(
    ("header", "canon"),
    [
        ("Emp Code", "employee_id"),
        ("Staff ID", "employee_id"),
        ("employee_id", "employee_id"),
        ("Employee Name", "employee_name"),
        ("Dept", "department"),
        ("Day", "attendance_date"),
        ("Attendance", "status"),
        ("Clock In", "check_in"),
        ("Out Time", "check_out"),
        ("Phone", "restricted"),
        ("National ID", "restricted"),
        ("Email", "restricted"),
        ("Notes", None),
    ],
)
def test_header_synonyms(header, canon):
    assert map_header(header) == canon


def test_header_row_found_below_title_rows():
    rows = [
        ["Acme Corp - Sales"],
        ["Period: ..."],
        [],
        ["Emp Code", "Employee Name", "Date", "Attendance"],
        ["E1", "x", "d", "P"],
    ]
    idx, mapping, restricted = find_header(rows)
    assert idx == 3 and set(mapping.values()) == {
        "employee_id",
        "employee_name",
        "attendance_date",
        "status",
    }
    assert restricted == []


@pytest.mark.parametrize(
    ("name", "content", "ok"),
    [
        ("a.csv", b"date,status\n", True),
        ("a.pdf", b"%PDF-1.4 ...", True),
        ("a.png", b"\x89PNG\r\n\x1a\n...", True),
        ("a.csv", b"MZ\x90\x00\x03\x00\x00\x00", False),
        ("a.xlsx", b"not a zip", False),
        ("a.exe", b"MZ", False),
        ("a.pdf", b"date,status\n", False),
        ("noext", b"x", False),
    ],
)
def test_detect(name, content, ok):
    ftype, problem = detect(name, content)
    assert (ftype is not None) is ok
    assert (problem is None) is ok
