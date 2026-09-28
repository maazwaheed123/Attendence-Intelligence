"""Merge, deduplicate, rerank and check retrieved evidence (pure functions).

  rrf()          Reciprocal Rank Fusion (k=60) over vector / keyword / trigram lists
  dedupe()       same record, same text (hash) or near-duplicate text (> 97% similar)
  rerank()       deterministic reranker (see LexicalReranker) -> top N above a floor
  sufficient()   do the survivors cover the question's required slots?

Reranker substitution: bge-reranker-base would be a 1.1 GB download; the
LexicalReranker needs none and is explainable. It implements the same Reranker
interface, so a cross-encoder can replace it by config later.
"""

import datetime as dt
import hashlib
import re
from dataclasses import dataclass, field, replace
from typing import Protocol

from rapidfuzz import fuzz

from app.retrieval.stores import Hit
from app.retrieval.types import Slots

RRF_K = 60
TOP_N = 6
SCORE_FLOOR = 0.2
NEAR_DUPLICATE = 97.0
_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    {
        "the", "a", "an", "of", "on", "in", "at", "to", "for", "and", "or", "was", "were",
        "is", "are", "did", "do", "does", "what", "who", "which", "how", "show", "me",
        "about", "any", "all", "this", "that", "with", "by", "from", "please", "tell",
        "give", "evidence", "proof",
    }
)  # fmt: skip
_DOC_WORDS = re.compile(
    r"\b(note|noted|remarks?|letter|memo|comments?|summar\w*|why|explain)\b", re.I
)


@dataclass
class Scored:
    hit: Hit
    rrf: float = 0.0
    sources: list[str] = field(default_factory=list)
    score: float = 0.0  # rerank score, 0..1
    reasons: list[str] = field(default_factory=list)


def rrf(*ranked_lists: list[Hit], k: int = RRF_K) -> list[Scored]:
    merged: dict[str, Scored] = {}
    for hits in ranked_lists:
        for rank, h in enumerate(hits, start=1):
            s = merged.setdefault(h.chunk_id, Scored(h))
            s.rrf += 1.0 / (k + rank)
            if h.source not in s.sources:
                s.sources.append(h.source)
            if h.source == "vector":  # keep the cosine for the reranker
                s.hit = replace(s.hit, score=h.score, source="vector")
    return sorted(merged.values(), key=lambda s: (-s.rrf, s.hit.chunk_id))


def _norm(t: str) -> str:
    return " ".join(_WORD.findall(t.lower()))


def dedupe(items: list[Scored]) -> list[Scored]:
    kept: list[Scored] = []
    records, hashes = set(), set()
    for s in items:
        h = s.hit
        digest = hashlib.sha256(_norm(h.text).encode()).hexdigest()
        if (h.record_id and h.record_id in records) or digest in hashes:
            continue
        if any(fuzz.ratio(_norm(h.text), _norm(k.hit.text)) > NEAR_DUPLICATE for k in kept):
            continue
        kept.append(s)
        hashes.add(digest)
        if h.record_id:
            records.add(h.record_id)
    return kept


def date_forms(d: dt.date) -> list[str]:
    """How a date may be written in evidence: 2026-09-03, 03/09/2026, 03/09, 3 Sep, 3 September."""
    return [
        d.isoformat(),
        d.strftime("%d/%m/%Y"),
        d.strftime("%d/%m"),
        f"{d.day} {d.strftime('%b')}",
        f"{d.day} {d.strftime('%B')}",
    ]


def mentions_date(text: str, d: dt.date) -> bool:
    low = text.lower()
    return any(f.lower() in low for f in date_forms(d))


def mentions_employee(text: str, s: Slots) -> bool:
    if not s.employee_id:
        return False
    return s.employee_id in text or bool(s.employee_name and s.employee_name.split()[0] in text)


class Reranker(Protocol):
    def score(self, question: str, slots: Slots, item: Scored) -> Scored: ...


class LexicalReranker:
    """Score in 0..1 = semantic similarity + query-term overlap + slot matches.

    weights: cosine 0.35, term overlap 0.30, employee match 0.15, date match 0.15,
    narrative chunk for a note/remark/letter question 0.15, chunk from a document the
    question names ("the injection memo") 0.30. Chunks about a DIFFERENT employee
    than the one asked about are pushed down.
    """

    def __init__(self, named_docs: set[str] | None = None):
        self.named_docs = named_docs or set()

    def score(self, question: str, slots: Slots, item: Scored) -> Scored:
        h = item.hit
        terms = {t for t in _WORD.findall(question.lower()) if t not in _STOP and len(t) > 1}
        words = set(_WORD.findall(h.text.lower()))
        overlap = len(terms & words) / len(terms) if terms else 0.0
        cosine = max(0.0, h.score) if h.source == "vector" else 0.0
        value = 0.35 * cosine + 0.30 * overlap
        reasons = [f"cos {cosine:.2f}", f"terms {overlap:.2f}"]
        if slots.employee_id:
            if mentions_employee(h.text, slots) or h.employee_id == slots.employee_id:
                value += 0.15
                reasons.append("employee")
            elif h.employee_id:  # a row card about someone else
                value -= 0.15
        if slots.date_from and slots.single_date and mentions_date(h.text, slots.date_from):
            value += 0.15
            reasons.append("date")
        if h.chunk_type == "narrative" and _DOC_WORDS.search(question):
            value += 0.15
            reasons.append("narrative")
        if h.source_document_id in self.named_docs:
            value += 0.30
            reasons.append("named document")
        return replace(item, score=round(max(0.0, min(1.0, value)), 4), reasons=reasons)


def named_documents(question: str, files: dict[str, str]) -> set[str]:
    """Documents the question names by file name: every distinctive token of the file
    stem (e.g. injection_memo.docx -> injection, memo) appears in the question."""
    q = set(_WORD.findall(question.lower()))
    out = set()
    for doc_id, name in files.items():
        stem = name.rsplit(".", 1)[0].lower()
        tokens = [t for t in re.split(r"[^a-z0-9]+", stem) if len(t) > 2 and not t.isdigit()]
        tokens = [t for t in tokens if t not in {"tenant", "sep", "final", "copy"}]
        if len(tokens) >= 2 and all(t in q for t in tokens):
            out.add(doc_id)
    return out


def rerank(
    question: str,
    slots: Slots,
    items: list[Scored],
    reranker: Reranker | None = None,
    *,
    top: int = TOP_N,
    floor: float = SCORE_FLOOR,
) -> list[Scored]:
    reranker = reranker or LexicalReranker()
    scored = [reranker.score(question, slots, i) for i in items]
    scored.sort(key=lambda s: (-s.score, -s.rrf, s.hit.chunk_id))
    return [s for s in scored if s.score >= floor][:top]


def sufficient(slots: Slots, items: list[Scored]) -> tuple[bool, list[str]]:
    """Required slots (employee, single date) must each be covered by some evidence."""
    missing = []
    usable = [i for i in items if not i.hit.suspicious]
    if not usable:
        return False, ["no evidence"]
    if slots.employee_id and not any(
        mentions_employee(i.hit.text, slots) or i.hit.employee_id == slots.employee_id
        for i in usable
    ):
        missing.append("employee")
    if slots.single_date and not any(mentions_date(i.hit.text, slots.date_from) for i in usable):
        missing.append("date")
    return not missing, missing
