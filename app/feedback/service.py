"""Feedback submission pipeline.

  1. the original request must resolve in the caller's scope (RLS) -> else 404
  2. scan feedback + ideal output: PII is masked before storage, prompt-injection
     rules are recorded; an ideal output naming another tenant is rejected
  3. re-analyse the original question under the CALLER's scope (rules only, no
     model) and compute the live template result
  4. templatize the ideal output; values not supported by the live data -> rejected
  5. equivalence check: rendering the template on current data must reproduce the
     ideal output exactly
  6. the intent's template SQL, with this request's values, passes the Step 8 validator
  7. store as the next version of its lineage (active or rejected), audit, and
     verify by re-running the question through the full pipeline
Feedback text can change wording only: values are recomputed, scope comes from the
token, and retrieval stays under RLS.
"""

import datetime as dt
import json
import logging
import re
import uuid

from rapidfuzz import fuzz

from app.api.errors import AppError
from app.feedback import store
from app.feedback import templating as tpl
from app.generation.prompts import PROMPT_VERSION
from app.governance import audit
from app.orchestrator import QueryRun, answer, question_hash
from app.retrieval import RETRIEVAL_VERSION
from app.retrieval.embeddings import EmbeddingUnavailable, get_embedder
from app.retrieval.sql.validator import SqlRejected, validate
from app.security import injection, pii
from app.security.context import SecurityContext

log = logging.getLogger(__name__)
_TENANT = re.compile(r"\btenant_[a-z0-9]+\b", re.I)


def _original(ctx: SecurityContext, request_id: str) -> dict:
    from sqlalchemy import text

    from app.db.session import scoped_session

    with scoped_session(ctx.to_scope(), role="reader") as s:
        row = (
            s.execute(
                text(
                    "SELECT question_redacted, question_hash, response FROM query_responses "
                    "WHERE request_id = :r"
                ),
                {"r": request_id},
            )
            .mappings()
            .first()
        )
    if row is None:
        raise AppError(404, "NOT_FOUND", "Original request not found.")
    return dict(row)


def _literal_sql(sql: str, params: dict) -> str:
    def lit(v):
        if isinstance(v, dt.date):
            return f"'{v.isoformat()}'"
        return "'" + str(v).replace("'", "''") + "'"

    return re.sub(r":(\w+)", lambda m: lit(params[m[1]]) if m[1] in params else m[0], sql)


def _embed(question: str) -> list[float] | None:
    try:
        return get_embedder().embed_query(question)
    except EmbeddingUnavailable:
        return None


def submit(ctx: SecurityContext, request_id: str, body: dict) -> dict:
    original = _original(ctx, body["original_request_id"])
    question = (body.get("question") or original["question_redacted"]).strip()
    if body.get("question") and question_hash(question) != original["question_hash"]:
        raise AppError(422, "VALIDATION_ERROR", "question does not match the original request.")

    feedback, fb_pii = pii.mask_text(body["feedback"])
    ideal, ideal_pii = pii.mask_text(body["ideal_final_output"].strip())
    flags = sorted(set(injection.detect(body["feedback"]) + injection.detect(ideal)))
    validation: dict = {
        "pii_masked": sum(fb_pii.values()) + sum(ideal_pii.values()),
        "injection_flags": flags,
        "scope_from": "token",
    }

    analysis = QueryRun(ctx, request_id, question, None).analyze()
    s, intent = analysis["slots"], analysis["intent"]
    mode = analysis["mode"]
    reasons: list[str] = []
    answer_template = sql_template = style_notes = None
    if mode in ("document", "hybrid") and intent is None:
        sig = "document"
        style_notes = ideal
        validation["kind"] = "style_guidance"
    else:
        sig = tpl.signature(intent, s) if intent else "none"
        validation["kind"] = "answer_template"
        rows = analysis["rows"]
        if intent not in tpl.FEEDBACK_INTENTS or analysis["template"] is None:
            reasons.append("question type is not supported for templated feedback")
        elif not rows or not analysis["answerable"]:
            reasons.append("no data in scope for the original question")
        else:
            facts = tpl.facts(intent, s, rows, analysis["date_format"])
            answer_template, unsupported = tpl.templatize(ideal, facts)
            validation["placeholders"] = tpl.placeholders(answer_template)
            if unsupported:
                reasons.append(
                    "ideal output not supported by evidence: " + ", ".join(sorted(set(unsupported)))
                )
            if not validation["placeholders"]:
                reasons.append("ideal output states none of the answer's facts")
            rendered = tpl.render(answer_template, facts)
            validation["equivalent"] = tpl.normalize(rendered or "") == tpl.normalize(ideal)
            if not validation["equivalent"]:
                reasons.append("template does not reproduce the ideal output")
            tq = analysis["template"]
            try:
                validate(_literal_sql(tq.sql, tq.params))
                sql_template, validation["sql_valid"] = tq.sql, True
            except SqlRejected as exc:
                validation["sql_valid"] = False
                reasons.append(f"sql template rejected: {exc}")
    if any(t.lower() != ctx.tenant_id for t in _TENANT.findall(ideal + " " + feedback)):
        reasons.append("ideal output references data outside the caller's scope")
    if flags:
        reasons.append("feedback contains instructions (possible prompt injection)")

    status = "rejected" if reasons else "active"
    validation["reasons"] = reasons
    entity_id = s.entity_id or (
        ctx.entities[0] if not ctx.all_entities and len(ctx.entities) == 1 else None
    )
    scope = ctx.to_scope()
    row = {
        "example_id": uuid.uuid4(),
        "lineage_id": store.lineage_id(scope, entity_id, sig),
        "status": status,
        "question": pii.mask_text(question)[0],
        "intent_signature": sig,
        "original_request_id": body["original_request_id"],
        "original_response": json.dumps(original["response"]),
        "feedback": feedback,
        "ideal_final_output": ideal,
        "sql_template": sql_template,
        "answer_template": answer_template,
        "style_notes": style_notes,
        "product_id": ctx.product_id,
        "tenant_id": ctx.tenant_id,
        "module": ctx.module,
        "entity_id": entity_id,
        "role_scope": [ctx.role],
        "classification": "internal",
        "reviewer_id": ctx.sub,
        "approved_by": ctx.sub if status == "active" else None,
        "approved_at": dt.datetime.now(dt.UTC) if status == "active" else None,
        "model": original["response"].get("model"),
        "prompt_version": PROMPT_VERSION,
        "retrieval_version": RETRIEVAL_VERSION,
        "validation": json.dumps(validation),
    }
    example = store.insert(scope, row, _embed(question))
    audit.record(
        "feedback_submitted",
        request_id,
        outcome=status,
        details={
            "example_id": example["example_id"],
            "version": example["version"],
            "intent_signature": sig,
            "reasons": reasons,
        },
        **audit.context_fields(ctx),
    )
    verification = None
    if status == "active":
        rerun = answer(ctx, f"{request_id}-verify", question)
        applied = rerun.get("applied_feedback") or {}
        verification = {
            "rerun_request_id": rerun["request_id"],
            "rerun_answer": rerun["answer"],
            "applied": applied.get("example_id") == example["example_id"],
            "matches_ideal": tpl.normalize(rerun["answer"]) == tpl.normalize(ideal),
            "similarity": round(fuzz.ratio(rerun["answer"], ideal) / 100, 3),
        }
    return {
        "example_id": example["example_id"],
        "version": example["version"],
        "status": status,
        "intent_signature": sig,
        "validation": validation,
        "scope": {
            "tenant_id": ctx.tenant_id,
            "product_id": ctx.product_id,
            "module": ctx.module,
            "entity_id": entity_id,
        },
        "verification": verification,
    }
