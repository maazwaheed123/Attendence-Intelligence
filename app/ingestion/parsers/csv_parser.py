import csv
import io

from app.ingestion.parsers.tabular import extract_rows
from app.ingestion.types import ParseResult, PermanentError


def parse(content: bytes, filename: str) -> ParseResult:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise PermanentError("file is not valid UTF-8 text") from exc
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    result = ParseResult(method="csv")
    # CSV locator: physical line number (header = row 1), as a person counts in a sheet.
    if not extract_rows(rows, result, lambda i: f"row={i + 1}", source_label=filename):
        raise PermanentError(
            "no header row found (need a date, a status and an employee id or name column)"
        )
    return result
