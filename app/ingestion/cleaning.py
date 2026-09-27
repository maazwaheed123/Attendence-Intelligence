"""Text cleaning for page-based documents: repeated headers/footers, page numbers."""

import re
from collections import Counter

PAGE_NUMBER = re.compile(r"^\s*(page\s*)?\d+(\s*(of|/)\s*\d+)?\s*$", re.I)


def _norm(line: str) -> str:
    return re.sub(r"\s+", " ", line).strip()


def remove_repeated_lines(
    pages: list[list[str]], min_share: float = 0.5
) -> tuple[list[list[str]], set[str]]:
    """Drop page numbers everywhere, and lines repeated on >= min_share of pages
    (only when there are at least 2 pages). Returns (clean_pages, removed_lines)."""
    removed: set[str] = set()
    repeated: set[str] = set()
    if len(pages) >= 2:
        counts = Counter(line for page in pages for line in {_norm(x) for x in page if x.strip()})
        repeated = {line for line, n in counts.items() if n >= 2 and n / len(pages) >= min_share}
    cleaned = []
    for page in pages:
        keep = []
        for line in page:
            n = _norm(line)
            if not n:
                continue
            if PAGE_NUMBER.match(n) or n in repeated:
                removed.add(n)
                continue
            keep.append(n)
        cleaned.append(keep)
    return cleaned, removed


def dedupe_lines(lines: list[str]) -> list[str]:
    seen, out = set(), []
    for line in lines:
        if line not in seen:
            seen.add(line)
            out.append(line)
    return out
