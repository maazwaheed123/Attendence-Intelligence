"""Date/time parsing with an explicit tenant date format (no silent day/month guessing)."""

import re
from datetime import date, datetime, time, timedelta

_MONTHS = "%d-%b-%Y", "%d %b %Y", "%d %B %Y", "%d-%B-%Y", "%b %d %Y", "%B %d %Y", "%b %d, %Y"
_NUMERIC = re.compile(r"^(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})$")
_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
_TIME = re.compile(r"^(\d{1,2})[:.](\d{2})(?::\d{2})?\s*([ap]\.?m\.?)?$", re.I)
EXCEL_EPOCH = date(1899, 12, 30)
MIN_DATE, MAX_DATE = date(2000, 1, 1), date(2100, 12, 31)


def parse_date(value, date_format: str = "DD/MM/YYYY") -> tuple[date | None, str | None]:
    """Returns (date, problem). Numeric dates follow the tenant's configured format."""
    if value is None or value == "":
        return None, "missing date"
    if isinstance(value, datetime):
        d = value.date()
    elif isinstance(value, date):
        d = value
    elif isinstance(value, int | float):
        d = EXCEL_EPOCH + timedelta(days=int(value))
    else:
        s = str(value).strip()
        d = None
        if m := _ISO.match(s):
            try:
                d = date(int(m[1]), int(m[2]), int(m[3]))
            except ValueError:
                return None, f"invalid date '{s}'"
        elif m := _NUMERIC.match(s):
            a, b, y = int(m[1]), int(m[2]), int(m[3])
            day, month = (a, b) if date_format.upper().startswith("DD") else (b, a)
            try:
                d = date(y, month, day)
            except ValueError:
                return None, f"date '{s}' does not match tenant format {date_format}"
        else:
            for fmt in _MONTHS:
                try:
                    d = datetime.strptime(s, fmt).date()
                    break
                except ValueError:
                    continue
        if d is None:
            return None, f"unrecognized date '{s}'"
    if not MIN_DATE <= d <= MAX_DATE:
        return None, f"date {d.isoformat()} out of range"
    return d, None


def parse_time(value) -> tuple[time | None, str | None]:
    """Returns (time, problem). Empty / '-' means 'not recorded' (no problem)."""
    if value is None:
        return None, None
    if isinstance(value, datetime):
        return value.time().replace(second=0, microsecond=0), None
    if isinstance(value, time):
        return value.replace(second=0, microsecond=0), None
    s = str(value).strip()
    if s in ("", "-", "--", "n/a", "na", "N/A"):
        return None, None
    m = _TIME.match(s)
    if not m:
        return None, f"unrecognized time '{s}'"
    h, mi, ampm = int(m[1]), int(m[2]), (m[3] or "").lower().replace(".", "")
    if ampm == "pm" and h < 12:
        h += 12
    elif ampm == "am" and h == 12:
        h = 0
    if h > 23 or mi > 59:
        return None, f"invalid time '{s}'"
    return time(h, mi), None


def total_hours(check_in: time | None, check_out: time | None) -> float | None:
    if not (check_in and check_out):
        return None
    start = datetime.combine(date(2000, 1, 1), check_in)
    end = datetime.combine(date(2000, 1, 1), check_out)
    if end < start:
        end += timedelta(days=1)
    return round((end - start).total_seconds() / 3600, 2)
