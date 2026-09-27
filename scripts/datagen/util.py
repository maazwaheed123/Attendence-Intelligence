"""Helpers that make every generated file byte-for-byte reproducible."""

import hashlib
import re
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

FIXED_DT = datetime(2026, 9, 30, 18, 0, 0)
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)
_CORE_TS = re.compile(rb"(<dcterms:(?:created|modified)[^>]*>)[^<]*(</dcterms:)")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def normalize_zip(path: Path) -> None:
    """Rewrite an OOXML (xlsx/docx) zip with fixed timestamps so its checksum is stable."""
    path = Path(path)
    with zipfile.ZipFile(path) as src:
        entries = [(i.filename, src.read(i.filename)) for i in src.infolist()]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as dst:
        for name, data in entries:
            if name == "docProps/core.xml":
                data = _CORE_TS.sub(rb"\g<1>2026-09-30T18:00:00Z\g<2>", data)
            info = zipfile.ZipInfo(name, date_time=_ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            dst.writestr(info, data)


def daterange(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def dmy(d: date) -> str:
    return d.strftime("%d/%m/%Y")


def to_date(s: str) -> date:
    return date.fromisoformat(s)
