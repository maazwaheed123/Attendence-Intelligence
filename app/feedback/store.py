"""Versioned, tenant-scoped feedback examples (all access through RLS-scoped sessions).

Lineage = one (product, tenant, module, entity, intent signature): a new approved
example becomes version N+1 and the previous active version turns inactive.
Rollback reactivates the newest earlier non-rejected version.
"""

import re
import uuid

from rapidfuzz import fuzz
from sqlalchemy import text

from app import cache
from app.config import get_settings
from app.db.session import scoped_session
from app.retrieval.indexing import to_pgvector
from app.security.scope import DbScope

_STOP = frozenset(
    {
        "what", "was", "were", "is", "are", "the", "a", "an", "of", "for", "in", "on", "at",
        "to", "did", "do", "does", "how", "me", "show", "tell", "please",
    }
)  # fmt: skip


def normalize_question(q: str) -> str:
    """Lexical form for paraphrase matching: no possessives, '%' == 'percentage'."""
    q = q.lower().replace("%", " percentage ").replace("percent ", "percentage ")
    q = re.sub(r"['’]s", "", q)
    words = [w for w in re.findall(r"[a-z0-9]+", q) if w not in _STOP]
    return " ".join(words)


NS_LINEAGE = uuid.UUID("3c1d7a52-8f0b-4e7a-9a3e-1b2f6c4d5e60")
PUBLIC_COLS = (
    "example_id, lineage_id, version, status, question, intent_signature, original_request_id, "
    "feedback, ideal_final_output, answer_template, style_notes, product_id, tenant_id, module, "
    "entity_id, role_scope, classification, "
    "reviewer_id, approved_by, approved_at, validation, times_applied, last_applied_request_id, "
    "model, prompt_version, retrieval_version, created_at, updated_at"
)


def lineage_id(scope: DbScope, entity_id: str | None, sig: str) -> uuid.UUID:
    key = f"{scope.product_id}|{scope.tenant_id}|{scope.module}|{entity_id or '*'}|{sig}"
    return uuid.uuid5(NS_LINEAGE, key)


def _json(row) -> dict:
    d = dict(row)
    for k, v in d.items():
        if isinstance(v, uuid.UUID):
            d[k] = str(v)
        elif hasattr(v, "isoformat"):
            d[k] = v.isoformat()
    return d


def insert(scope: DbScope, row: dict, vector: list[float] | None) -> dict:
    """Insert as the next version of its lineage; an active insert retires the old one."""
    with scoped_session(scope, role="app") as s:
        s.execute(text("SELECT pg_advisory_xact_lock(hashtext(:l))"), {"l": str(row["lineage_id"])})
        current = s.execute(
            text("SELECT coalesce(max(version), 0) FROM feedback_examples WHERE lineage_id = :l"),
            {"l": row["lineage_id"]},
        ).scalar_one()
        row = {**row, "version": current + 1}
        if row["status"] == "active":
            s.execute(
                text(
                    "UPDATE feedback_examples SET status = 'inactive', updated_at = now() "
                    "WHERE lineage_id = :l AND status = 'active'"
                ),
                {"l": row["lineage_id"]},
            )
        json_cols = ("validation", "original_response")
        values = [f"CAST(:{c} AS jsonb)" if c in json_cols else f":{c}" for c in row]
        s.execute(
            text(
                f"INSERT INTO feedback_examples ({', '.join(row)}, question_embedding) "  # noqa: S608
                f"VALUES ({', '.join(values)}, CAST(:vec AS vector))"
            ),
            {**row, "vec": to_pgvector(vector) if vector else None},
        )
    cache.bump_feedback_version(scope.tenant_id, scope.product_id)
    return get(scope, row["example_id"])


def get(scope: DbScope, example_id) -> dict | None:
    with scoped_session(scope, role="reader") as s:
        row = (
            s.execute(
                text(f"SELECT {PUBLIC_COLS} FROM feedback_examples WHERE example_id = :e"),  # noqa: S608
                {"e": str(example_id)},
            )
            .mappings()
            .first()
        )
    return _json(row) if row else None


def list_examples(scope: DbScope, status: str | None = None, limit: int = 100) -> list[dict]:
    with scoped_session(scope, role="reader") as s:
        rows = (
            s.execute(
                text(
                    f"SELECT {PUBLIC_COLS} FROM feedback_examples "  # noqa: S608
                    "WHERE (CAST(:st AS text) IS NULL OR status = :st) "
                    "ORDER BY lineage_id, version DESC LIMIT :n"
                ),
                {"st": status, "n": max(1, min(limit, 500))},
            )
            .mappings()
            .all()
        )
    return [_json(r) for r in rows]


def set_status(scope: DbScope, example_id, status: str) -> None:
    with scoped_session(scope, role="app") as s:
        s.execute(
            text(
                "UPDATE feedback_examples SET status = :st, updated_at = now() "
                "WHERE example_id = :e"
            ),
            {"st": status, "e": str(example_id)},
        )
    cache.bump_feedback_version(scope.tenant_id, scope.product_id)


def rollback(scope: DbScope, example: dict) -> dict | None:
    """Deactivate `example`'s lineage head and reactivate the previous good version."""
    with scoped_session(scope, role="app") as s:
        s.execute(text("SELECT pg_advisory_xact_lock(hashtext(:l))"), {"l": example["lineage_id"]})
        target = s.execute(
            text(
                "SELECT example_id FROM feedback_examples WHERE lineage_id = :l AND version < :v "
                "AND status <> 'rejected' ORDER BY version DESC LIMIT 1"
            ),
            {"l": example["lineage_id"], "v": example["version"]},
        ).scalar()
        if target is None:
            return None
        s.execute(
            text(
                "UPDATE feedback_examples SET status = 'inactive', updated_at = now() "
                "WHERE lineage_id = :l AND status = 'active'"
            ),
            {"l": example["lineage_id"]},
        )
        s.execute(
            text(
                "UPDATE feedback_examples SET status = 'active', updated_at = now() "
                "WHERE example_id = :e"
            ),
            {"e": target},
        )
    cache.bump_feedback_version(scope.tenant_id, scope.product_id)
    return get(scope, target)


def match(scope: DbScope, sig: str, question: str, vector: list[float] | None) -> dict | None:
    """Best ACTIVE example visible in this scope (RLS: tenant, product, module, entity,
    clearance) with the same intent signature and a similar question.

    similarity = max(cosine of question embeddings, token-set ratio): the signature
    already guarantees the same kind of question; the similarity only has to confirm it.
    """
    threshold = get_settings().feedback_match_threshold
    with scoped_session(scope, role="reader") as s:
        rows = (
            s.execute(
                text(
                    "SELECT example_id, version, question, answer_template, style_notes, "
                    "CASE WHEN question_embedding IS NULL OR CAST(:v AS vector) IS NULL THEN NULL "
                    "ELSE 1 - (question_embedding <=> CAST(:v AS vector)) END AS cosine "
                    "FROM feedback_examples WHERE status = 'active' AND intent_signature = :sig"
                ),
                {"sig": sig, "v": to_pgvector(vector) if vector else None},
            )
            .mappings()
            .all()
        )
    best = None
    for r in rows:
        lexical = (
            fuzz.token_set_ratio(normalize_question(question), normalize_question(r["question"]))
            / 100
        )
        similarity = max(float(r["cosine"] or 0), lexical)
        if similarity >= threshold and (best is None or similarity > best["similarity"]):
            best = {**_json(r), "similarity": round(similarity, 3)}
    return best


def record_applied(scope: DbScope, example_id: str, request_id: str) -> None:
    with scoped_session(scope, role="app") as s:
        s.execute(
            text(
                "UPDATE feedback_examples SET times_applied = times_applied + 1, "
                "last_applied_request_id = :r WHERE example_id = :e"
            ),
            {"r": request_id, "e": example_id},
        )
