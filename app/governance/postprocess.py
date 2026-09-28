"""Final governance filter applied to EVERY /v1/query response before it leaves.

Order:
  1. citation validation  every cited record/chunk id must be in the set this
                          request actually retrieved (in scope, via RLS); others dropped
  2. leakage guard        the answer and excerpts may only mention employee ids and
                          person names from the caller's scoped directory or the
                          retrieved evidence, and no other tenant id; a hit withholds
                          the answer and writes a security audit event
  3. injection output     an answer that echoes flagged instructions or claims a mode
                          or policy change is withheld
  4. PII masking          role-aware: restricted clearance keeps the last 4 digits
  5. schema validation    the response must match QueryResponse; else a safe fallback
Checks 1-3 are defence in depth: earlier stages already build citations from
retrieval and ground every model sentence; this layer assumes they can fail.
"""

import logging
import re
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from app.governance import audit
from app.retrieval.rewrite import NOT_NAMES, Directory
from app.security import injection, pii
from app.security.context import SecurityContext

log = logging.getLogger(__name__)

WITHHELD = (
    "The answer was withheld because it did not pass the service's safety checks. "
    "Please rephrase the question."
)
_EMP_ID = re.compile(r"\b[A-Z]\d{3,}\b")
_TENANT = re.compile(r"\btenant_[a-z0-9]+\b", re.I)  # not file names: tenant_a_week2.docx
_NAME = re.compile(r"\b([A-Z][a-z]+)\s+([A-Z][a-z]+)\b")
_POLICY_CLAIM = re.compile(
    r"\b(administrator|admin|developer|debug) mode\b|\b(ignor(e|ing)|disregard(ing)?) "
    r"(all |my |the )?(previous |prior )?(instructions|rules)\b|\ball tenants'? (data|records)\b",
    re.I,
)
_COMMON = {
    "relevant", "evidence", "permitted", "scope", "supporting", "overall", "attendance",
    "sources", "result", "low", "confidence", "the", "no", "manager", "remarks", "notes",
    "summary", "engineering", "operations", "report", "section", "page", "core", "hours",
    "remote", "client", "office", "site", "letter", "reminder", "thank", "you", "please",
}  # fmt: skip


class Citation(BaseModel):
    record_id: str | None = None
    chunk_id: str | None = None
    source_file: str
    locator: str
    excerpt: str
    tag: str | None = None


class QueryResponse(BaseModel):
    request_id: str
    status: Literal["answered", "unavailable", "needs_review"]
    answer: str = Field(min_length=1)
    retrieval_mode: Literal["structured", "document", "hybrid", "none"]
    context: dict
    citations: list[Citation]
    citation_total: int = Field(ge=0)
    confidence: float = Field(ge=0, le=1)
    confidence_band: Literal["high", "medium", "low"]
    confidence_explanation: str
    unavailable_reason: str | None
    provider: str | None
    model: str | None
    fallback_path: str
    prompt_version: str
    retrieval_version: str
    warnings: list[str]


def _cid(c: dict) -> str | None:
    return c.get("chunk_id") or c.get("record_id")


def validate_citations(response: dict, retrieved: set[str]) -> int:
    kept = [c for c in response["citations"] if _cid(c) in retrieved]
    dropped = len(response["citations"]) - len(kept)
    if dropped:
        response["citations"] = kept
        response["citation_total"] = min(response["citation_total"], len(kept)) or len(kept)
        response["warnings"].append(
            f"{dropped} citation(s) not retrieved for this request were removed."
        )
    return dropped


def leakage(response: dict, ctx: SecurityContext, directory: Directory) -> list[str]:
    """Kinds of out-of-scope references in the answer or citation excerpts."""
    excerpts = " ".join(c["excerpt"] for c in response["citations"])
    text = f"{response['answer']} {excerpts}"
    allowed_ids = {e[0] for e in directory.employees} | set(_EMP_ID.findall(excerpts))
    vocab = {w.lower() for e in directory.employees for w in e[1].split()}
    vocab |= {w.lower() for _, n in directory.entities for w in n.split()}
    vocab |= {w.lower() for w in re.findall(r"[A-Za-z]+", excerpts)}
    problems = []
    if any(i not in allowed_ids for i in _EMP_ID.findall(text)):
        problems.append("employee_id")
    if any(t.lower() != ctx.tenant_id for t in _TENANT.findall(text)):
        problems.append("tenant_id")
    for m in _NAME.finditer(response["answer"]):
        words = [m[1].lower(), m[2].lower()]
        if all(w not in vocab and w not in NOT_NAMES and w not in _COMMON for w in words):
            problems.append("person_name")
            break
    return problems


def injected(response: dict, flagged_texts: list[str]) -> bool:
    answer = response["answer"]
    if injection.detect(answer) or _POLICY_CLAIM.search(answer):
        return True
    words = answer.lower().split()
    shingles = {" ".join(words[i : i + 6]) for i in range(max(0, len(words) - 5))}
    for flagged in flagged_texts:
        fw = flagged.lower().split()
        if any(" ".join(fw[i : i + 6]) in shingles for i in range(max(0, len(fw) - 5))):
            return True
    return False


def _withhold(response: dict, reason: str) -> None:
    response.update(
        status="unavailable",
        answer=WITHHELD,
        citations=[],
        citation_total=0,
        confidence=0.0,
        confidence_band="low",
        confidence_explanation=f"No answer was produced ({reason}).",
        unavailable_reason="blocked",
    )


def mask(response: dict, ctx: SecurityContext) -> int:
    keep_tail = ctx.clearance == "restricted"
    total = 0
    response["answer"], counts = pii.mask_text(response["answer"], keep_tail=keep_tail)
    total += sum(counts.values())
    for c in response["citations"]:
        c["excerpt"], counts = pii.mask_text(c["excerpt"], keep_tail=keep_tail)
        total += sum(counts.values())
    return total


def finalize(
    response: dict,
    *,
    ctx: SecurityContext,
    directory: Directory,
    retrieved: set[str],
    flagged_texts: list[str],
) -> dict:
    rid = response["request_id"]
    validate_citations(response, retrieved)
    problems = leakage(response, ctx, directory)
    if problems:
        log.warning("leakage guard blocked request_id=%s kinds=%s", rid, problems)
        audit.record(
            "security_block",
            rid,
            outcome="blocked",
            details={"check": "leakage", "kinds": problems},
            **audit.context_fields(ctx),
        )
        _withhold(response, "leakage_guard")
        response["warnings"].append("The answer referenced data outside your permitted scope.")
    elif response["status"] != "unavailable" and injected(response, flagged_texts):
        audit.record(
            "security_block",
            rid,
            outcome="blocked",
            details={"check": "injection_output"},
            **audit.context_fields(ctx),
        )
        _withhold(response, "injection_output_check")
        response["warnings"].append("The answer repeated instructions found in a flagged document.")
    if mask(response, ctx):
        response["warnings"].append("Personal data in the answer was masked.")
    try:
        QueryResponse.model_validate(response)
    except ValidationError as exc:
        log.error("response failed schema validation request_id=%s: %s", rid, exc)
        _withhold(response, "schema_validation")
        response["citations"], response["warnings"] = [], ["Response failed validation."]
    return response
