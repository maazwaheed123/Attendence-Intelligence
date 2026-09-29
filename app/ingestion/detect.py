"""File-type detection from CONTENT (magic bytes), cross-checked with the extension.

A file is accepted only when both agree, so a renamed executable or a corrupt
workbook is rejected at the door instead of reaching a parser.
"""

import io
import zipfile
from pathlib import PurePath

EXTENSIONS = {
    ".csv": "csv",
    ".txt": "csv",
    ".xlsx": "xlsx",
    ".docx": "docx",
    ".pdf": "pdf",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".tif": "image",
    ".tiff": "image",
}


def sniff(content: bytes) -> str | None:
    head = content[:8]
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"\x89PNG") or head.startswith(b"\xff\xd8\xff"):
        return "image"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "image"
    if head.startswith(b"PK\x03\x04"):
        try:
            names = set(zipfile.ZipFile(io.BytesIO(content)).namelist())
        except zipfile.BadZipFile:
            return None
        if "[Content_Types].xml" in names:
            if any(n.startswith("xl/") for n in names):
                return "xlsx"
            if any(n.startswith("word/") for n in names):
                return "docx"
        return None
    if b"\x00" in content[:4096]:
        return None
    try:
        content[:4096].decode("utf-8-sig")
    except UnicodeDecodeError:
        return None
    return "csv"


def detect(filename: str, content: bytes) -> tuple[str | None, str | None]:
    """Returns (file_type, problem). file_type is None when rejected."""
    ext = PurePath(filename).suffix.lower()
    expected = EXTENSIONS.get(ext)
    if expected is None:
        return None, f"unsupported file extension '{ext or '(none)'}'"
    actual = sniff(content)
    if actual != expected:
        return None, f"file content does not match its '{ext}' extension"
    return actual, None
