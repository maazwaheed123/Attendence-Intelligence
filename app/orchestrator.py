"""Query orchestrator: question -> classification -> scoped retrieval -> answer.

Pipeline (structured mode, Step 8):
  1. load the caller's directory (roster, entities, data coverage) through RLS
  2. classify (rules, then LLM JSON only if needed) and resolve dates/names in scope
  3. refuse / "no data" early for out-of-scope questions, names that resolve to
     nobody in scope, and periods outside the visible data
  4. LLM SQL (validated, one repair round) and, for templatable intents, the
     deterministic template SQL; both run as rag_reader under RLS; compare
  5. system-built citations from the same row filter; conflicts -> needs_review;
     records awaiting review -> warnings (or needs_review when they are the only evidence)
  6. LLM phrasing, grounded against the rows; template text on any mismatch
  7. confidence, persistence (question redacted + hashed), audit
Repeat questions are served from the Redis query cache (app/cache.py, keyed by
the full scope + data/feedback versions); a hit is still re-finalized under the
caller's scope, persisted and audited under its own request id.
The model never sees data from outside the caller's scope: every prompt is built
from the question and rows that RLS already filtered.
"""

import copy
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field

from sqlalchemy import text

from app import cache
from app.config import get_settings
from app.db.session import scoped_session
from app.feedback import store as feedback_store
from app.feedback import templating as tpl
from app.generation import answer_templates as T
from app.generation import document_answer
from app.generation.phrasing import phrase
from app.generation.prompts import PROMPT_VERSION
from app.generation.router import TEMPLATE, get_router
from app.governance import audit, confidence, grounding, postprocess
from app.retrieval import RETRIEVAL_VERSION, classifier, documents, fusion, rewrite
from app.retrieval.sql import executor, generator, lineage, templates
from app.retrieval.sql.executor import SqlExecutionError
from app.security import pii
from app.security.context import SecurityContext

log = logging.getLogger(__name__)


@dataclass
class _Answer:
    status: str  # answered | unavailable | needs_review
    text: str
    mode: str = "structured"
    reason: str | None = None
    citations: list[dict] = field(default_factory=list)
    citation_total: int = 0
    conf: confidence.Confidence | None = None


class QueryRun:
    def __init__(self, ctx: SecurityContext, request_id: str, question: str, filters: dict | None):
        self.ctx = ctx
        self.request_id = request_id
        self.question = question.strip()
        self.filters = filters or {}
        self.scope = ctx.to_scope()
        self.router = get_router()
        self.audit_ctx = {"request_id": request_id, "context": audit.context_fields(ctx)}
        self.settings = get_settings()
        self.warnings: list[str] = []
        self.failed_attempts: list[dict] = []
        self.llm_used = None  # LLMResult whose text became the answer
        self.sql_executed: str | None = None
        self.debug: dict = {}
        self.intent: str | None = None
        self.retrieved: set[str] = set()  # ids retrieval returned for THIS request
        self.flagged: list[str] = []  # texts of flagged (possible injection) chunks
        self.applied: dict | None = None  # feedback example used for this answer
        self.cache_status = "off"  # off | miss | hit

    # ------------------------------------------------------------------ entry
    def execute(self) -> dict:
        start = time.perf_counter()
        key = cache.key_for(self.ctx, self.question, self.filters)
        hit = cache.get(key)
        if hit is not None:
            return self._replay(hit, start)
        self.cache_status = "miss" if key else "off"
        self.directory = rewrite.load_directory(self.scope)
        cls = classifier.classify(self.question, self.directory, self.router, self.audit_ctx)
        self.intent = cls.intent
        self.slots = cls.slots
        self._apply_filters()
        self.debug.update(
            mode=cls.mode, intent=cls.intent, classified_by=cls.source, cache=self.cache_status
        )
        if cls.flags:
            self.warnings.append(
                "The question contained instructions aimed at the system; they were ignored."
            )
        result = self._route(cls)
        response = postprocess.finalize(
            self._response(result),
            ctx=self.ctx,
            directory=self.directory,
            retrieved=self.retrieved,
            flagged_texts=self.flagged,
        )
        response["applied_feedback"] = self.applied if response["status"] != "unavailable" else None
        if response["applied_feedback"]:
            self._record_feedback_use()
        self._persist(response, cls.mode)
        self._audit(response, int((time.perf_counter() - start) * 1000))
        if key and cache.cacheable(response):
            cache.put(
                key,
                {
                    "response": response,
                    "mode": cls.mode,
                    "intent": self.intent,
                    "sql": self.sql_executed,
                    "retrieved": sorted(self.retrieved),
                    "flagged": self.flagged,
                    "applied": self.applied,
                    "debug": self.debug_info,
                },
            )
        return response

    def _replay(self, hit: dict, start: float) -> dict:
        """A cache hit: same governance as a fresh answer, new request id."""
        self.cache_status = "hit"
        self.directory = rewrite.load_directory(self.scope)
        self.intent, self.sql_executed = hit["intent"], hit["sql"]
        self.retrieved, self.flagged = set(hit["retrieved"]), hit["flagged"]
        cached = copy.deepcopy(hit["response"])
        cached.update(request_id=self.request_id, applied_feedback=None)
        response = postprocess.finalize(
            cached,
            ctx=self.ctx,
            directory=self.directory,
            retrieved=self.retrieved,
            flagged_texts=self.flagged,
        )
        self.applied = hit["applied"] if response["status"] != "unavailable" else None
        response["applied_feedback"] = self.applied
        if self.applied:
            self._record_feedback_use()
        self.debug_info = {**hit["debug"], "cache": "hit"}
        self._persist(response, hit["mode"])
        self._audit(response, int((time.perf_counter() - start) * 1000))
        return response

    # ------------------------------------------------------------------ routing
    def _route(self, cls) -> _Answer:
        if cls.mode == "out_of_scope":
            key = {"pii": "not_permitted", "unsafe": "unsafe"}.get(cls.reason, "out_of_scope")
            reason = "not_permitted" if cls.reason == "pii" else "out_of_scope"
            return self._unavailable(reason, T.UNAVAILABLE[key], mode="none")
        if cls.intent is None:
            if cls.mode in ("document", "hybrid"):
                return self._document()
            return self._unavailable(
                "insufficient_evidence", T.UNAVAILABLE["unsupported"], mode="none"
            )
        answer = self._structured()
        if cls.mode == "hybrid" and answer.status != "unavailable":
            answer = self._attach_evidence(answer)
        return answer

    # ------------------------------------------------------------------ documents
    def _document(self) -> _Answer:
        s = self.slots
        if s.unresolved_person or s.unresolved_entity:
            return self._no_data(s.date_from is not None)
        ev = documents.retrieve(self.scope, self.question, s)
        self._track(ev)
        self.warnings += ev.warnings
        self.debug["evidence"] = [
            {"tag": i.tag, "chunk_id": i.chunk_id, "score": i.score, "reasons": i.reasons}
            for i in ev.items
        ]
        if not ev.sufficient:
            return self._unavailable(
                "insufficient_evidence", T.UNAVAILABLE["no_document_evidence"], mode="document"
            )
        note = document_answer.flagged_note(ev)
        if note:
            self.warnings.append(note)
        style = feedback_store.match(self.scope, "document", self.question, self._question_vector())
        if style:
            self.applied = {k: style[k] for k in ("example_id", "version", "similarity")}
        result, attempts = document_answer.generate(
            self.question,
            ev,
            self.router,
            audit=self.audit_ctx,
            style=style["style_notes"] if style else None,
        )
        self.failed_attempts = attempts
        answer, used = None, []
        doc = document_answer.parsed(result)
        if doc is not None and doc.insufficient:
            self.llm_used = result
            return self._unavailable(
                "insufficient_evidence", T.UNAVAILABLE["no_document_evidence"], mode="document"
            )
        if doc is not None and doc.answer.strip():
            text_, used, unknown = document_answer.map_citations(doc.answer, doc.citations, ev)
            if unknown:
                self.warnings.append(
                    f"Removed citation tags not present in the evidence: {', '.join(unknown)}."
                )
            kept, dropped = document_answer.supported_sentences(text_, ev, self.question)
            unsafe = postprocess.injected({"answer": text_}, self.flagged)
            if used and kept and not unsafe:
                answer, self.llm_used = kept, result
                used = document_answer.cited_items(kept, ev) or used
                if dropped:
                    self.warnings.append(
                        f"{dropped} statement(s) not supported by the cited evidence were removed."
                    )
            else:
                self.debug["grounding_problems"] = ["unsafe output"] if unsafe else ["unsupported"]
                self.warnings.append(
                    "The generated answer repeated flagged instructions; the evidence is quoted "
                    "instead."
                    if unsafe
                    else "The generated answer was not supported by the cited evidence; "
                    "the evidence is quoted instead."
                )
        if answer is None:
            answer, used = document_answer.extractive(ev)
        conf = confidence.score_document(
            grounded=True,
            top_score=max(i.score for i in used) if used else 0.0,
            cited=len(used),
            sufficient=ev.sufficient,
            flagged=sum(i.suspicious for i in ev.items),
            high=self.settings.conf_high,
            low=self.settings.conf_low,
        )
        if conf.band == "low":
            answer = f"Low confidence: {answer} ({conf.explanation})"
        citations = [{**i.citation(), "tag": i.tag} for i in used]
        return _Answer(
            "answered", answer, mode="document", citations=citations,
            citation_total=len(citations), conf=conf,
        )  # fmt: skip

    def _track(self, ev) -> None:
        self.retrieved |= {i.chunk_id for i in ev.items}
        self.flagged += [i.text for i in ev.items if i.suspicious]

    def _attach_evidence(self, a: _Answer) -> _Answer:
        """Hybrid: the structured answer plus the narrative text that supports it."""
        s = self.slots
        ev = documents.retrieve(self.scope, self.question, s, top=4, narrative_only=True)
        self._track(ev)
        self.warnings += ev.warnings
        usable = [
            i
            for i in ev.items
            if i.chunk_type == "narrative"
            and not i.suspicious
            and (not s.employee_id or fusion.mentions_employee(i.text, s))
            and (not s.single_date or fusion.mentions_date(i.text, s.date_from))
        ][:2]
        a.mode = "hybrid"
        if not usable:
            self.warnings.append("No supporting document text found in your permitted scope.")
            return a
        quotes = "; ".join(f'"{i.text[:200]}" ({i.source_file}, {i.locator})' for i in usable)
        a.text = f"{a.text} Supporting evidence: {quotes}."
        a.citations = [*a.citations, *({**i.citation(), "tag": i.tag} for i in usable)]
        a.citation_total += len(usable)
        return a

    def _apply_filters(self) -> None:
        s, f, d = self.slots, self.filters, self.directory
        if self.ctx.role == "employee" and not (s.employee_id or s.unresolved_person):
            emp = d.employee(self.ctx.employee_id)  # self-only scope: "my attendance"
            if emp:
                s.employee_id, s.employee_name = emp[0], emp[1]
        if f.get("date_from") or f.get("date_to"):
            s.date_from = f.get("date_from") or s.date_from or d.coverage[0]
            s.date_to = f.get("date_to") or s.date_to or d.coverage[1]
            s.period_label = None
        if f.get("entity_id"):
            s.entity_id = f["entity_id"]
            s.entity_name = d.entity_name(s.entity_id) or s.entity_id
            s.unresolved_entity = False
        if f.get("employee_id"):
            emp = d.employee(f["employee_id"])
            s.employee_id, s.employee_name = (emp[0], emp[1]) if emp else (None, None)
            s.unresolved_person = emp is None

    def _structured(self) -> _Answer:
        s = self.slots
        explicit_period = s.date_from is not None
        if s.unresolved_person or s.unresolved_entity:
            return self._no_data(explicit_period)
        cov_min, cov_max = self.directory.coverage
        if cov_min is None:
            return self._no_data(explicit_period)
        if not explicit_period:
            s.date_from, s.date_to, s.period_label = cov_min, cov_max, None
        if s.date_from > s.date_to or s.date_to < cov_min or s.date_from > cov_max:
            return self._no_data(explicit_period)

        tq = templates.build(self.intent, s) if not s.extra_employees else None
        gen = generator.generate(self.question, s, self.router, self.audit_ctx)
        self.failed_attempts = gen.attempts
        if gen.rejections:
            self.warnings.append(f"Model SQL rejected by the validator: {gen.rejections[-1]}")
        llm_rows = self._run_llm_sql(gen.sql)
        t_rows = executor.run(self.scope, tq.sql, tq.params).rows if tq else None
        self.debug.update(template_sql=tq.sql if tq else None, llm_sql=gen.sql)

        if t_rows is not None:
            rows, self.sql_executed = t_rows, tq.sql
            if llm_rows is None:
                dual = "template_only"
            elif templates.agree(self.intent, t_rows, llm_rows):
                dual = "agree"
            else:
                dual = "disagree"
                self.warnings.append(
                    "Model SQL disagreed with the deterministic query; "
                    "the deterministic result was used."
                )
                log.warning(
                    "dual-path discrepancy request_id=%s intent=%s", self.request_id, self.intent
                )
        elif llm_rows is not None:
            rows, self.sql_executed, dual = llm_rows, gen.sql, "llm_only"
        else:
            return self._unavailable(
                "insufficient_evidence",
                "This question needs a language model to translate it into a query, and none "
                "is available right now. Try a simpler attendance question.",
            )
        return self._finish(rows, tq, gen, dual, explicit_period)

    def _run_llm_sql(self, sql: str | None) -> list[dict] | None:
        if not sql:
            return None
        try:
            return executor.run(self.scope, sql).rows
        except SqlExecutionError as exc:
            self.warnings.append("Model SQL failed to execute and was discarded.")
            log.info("LLM SQL execution failed request_id=%s: %s", self.request_id, exc)
            return None

    # ------------------------------------------------------------------ finishing
    def _finish(self, rows, tq, gen, dual, explicit_period) -> _Answer:
        s, fmt = self.slots, self.directory.date_format
        pending = lineage.pending_review(self.scope, s)
        if tq is not None:
            base_where, base_params = templates.base_where(s)
            present = executor.run(
                self.scope,
                f"SELECT count(*) AS n FROM v_attendance WHERE {base_where}",  # noqa: S608
                base_params,
            ).rows[0]["n"]
            answerable = present > 0 and templates.has_answer(self.intent, rows)
        else:
            answerable = bool(rows) and any(v is not None for r in rows for v in r.values())
        if not answerable:
            if pending:
                return self._pending_only(pending)
            return self._no_data(explicit_period)
        if pending:
            files = T.join(sorted({p["source_file"] for p in pending}))
            n = len(pending)
            self.warnings.append(
                f"{n} record{'s' if n != 1 else ''} from {files} "
                f"await{'s' if n == 1 else ''} review and {'is' if n == 1 else 'are'} not included."
            )

        lin = self._lineage(rows, tq, gen)
        self.retrieved |= {c["record_id"] for c in lin.citations}
        needs_review = self.intent == "employee_status_on_date" and bool(rows[0].get("conflict"))
        if lin.conflict_days and not needs_review:
            self.warnings.append(
                f"{lin.conflict_days} employee-day{'s' if lin.conflict_days != 1 else ''} with "
                "conflicting sources excluded from the result (needs review)."
            )

        reference = T.render(self.intent, s, rows, fmt) if tq is not None else T.generic(rows)
        answer = reference
        improved = self._apply_feedback(rows) if tq is not None and not needs_review else None
        if improved:
            answer = improved  # reviewer-approved wording, values recomputed from these rows
        elif not needs_review:
            answer = self._phrase(rows, reference if tq is not None else None) or reference
        check = self._ground(answer, rows)
        if not check.ok and answer != reference:
            self.applied = None
            self.warnings.append(
                "The phrased answer was not supported by the data; a deterministic answer was used."
            )
            self.debug["grounding_problems"] = check.problems
            self.llm_used, answer = None, reference
            check = self._ground(answer, rows)

        coverage = 1.0 if lin.citations else 0.0
        conf = confidence.score(
            confidence.Signals(
                sql_valid=True,
                dual_path=dual,
                grounded=check.ok,
                citation_coverage=coverage,
                mean_extraction_confidence=lin.mean_confidence,
                needs_review=needs_review,
                conflict_days=lin.conflict_days,
            ),
            high=self.settings.conf_high,
            low=self.settings.conf_low,
        )
        status = "needs_review" if needs_review else "answered"
        if status == "answered" and conf.band == "low":
            answer = f"Low confidence: {answer} ({conf.explanation})"
        return _Answer(status, answer, citations=lin.citations, citation_total=lin.total, conf=conf)

    # ------------------------------------------------------------------ feedback
    def _question_vector(self) -> list[float] | None:
        from app.retrieval.embeddings import EmbeddingUnavailable, get_embedder

        try:
            return get_embedder().embed_query(self.question)
        except EmbeddingUnavailable:
            return None

    def _apply_feedback(self, rows) -> str | None:
        if self.intent not in tpl.FEEDBACK_INTENTS:
            return None
        sig = tpl.signature(self.intent, self.slots)
        ex = feedback_store.match(self.scope, sig, self.question, self._question_vector())
        if not ex or not ex.get("answer_template"):
            return None
        facts = tpl.facts(self.intent, self.slots, rows, self.directory.date_format)
        text_ = tpl.render(ex["answer_template"], facts)
        if not text_:
            return None
        self.applied = {
            "example_id": ex["example_id"],
            "version": ex["version"],
            "similarity": ex["similarity"],
        }
        return text_

    def _record_feedback_use(self) -> None:
        feedback_store.record_applied(self.scope, self.applied["example_id"], self.request_id)
        audit.record(
            "feedback_applied",
            self.request_id,
            outcome="applied",
            details=dict(self.applied),
            **audit.context_fields(self.ctx),
        )

    def analyze(self) -> dict:
        """Rules-only understanding + the live template result for this question (no
        model calls). Used by feedback submission to templatize an ideal output."""
        self.directory = rewrite.load_directory(self.scope)
        cls = classifier.classify(self.question, self.directory, None)
        self.intent, self.slots = cls.intent, cls.slots
        self._apply_filters()
        s = self.slots
        out = {
            "mode": cls.mode,
            "intent": cls.intent,
            "slots": s,
            "template": None,
            "rows": [],
            "answerable": False,
            "date_format": self.directory.date_format,
        }
        cov_min, cov_max = self.directory.coverage
        if cls.intent is None or s.unresolved_person or s.unresolved_entity or cov_min is None:
            return out
        if s.date_from is None:
            s.date_from, s.date_to = cov_min, cov_max
        tq = templates.build(cls.intent, s)
        if tq is None:
            return out
        rows = executor.run(self.scope, tq.sql, tq.params).rows
        out.update(template=tq, rows=rows, answerable=templates.has_answer(cls.intent, rows))
        return out

    def _lineage(self, rows, tq, gen) -> lineage.Lineage:
        s = self.slots
        if tq is not None:
            if self.intent in ("list_by_status", "count_by_status") and not rows_have_facts(rows):
                where, params = templates.base_where(s)  # "nobody was X": cite the day's records
            else:
                where, params = tq.where, tq.params
            group = None
            if self.intent == "rank":
                col = tq.group_cols[0]
                group = {col: [r[col] for r in templates.winners(rows)]}
            return lineage.for_template(self.scope, where, params, group)
        return lineage.for_llm_sql(self.scope, gen.sql, rows)

    def _phrase(self, rows, reference) -> str | None:
        result, attempts = phrase(
            self.question, rows, self.router, reference=reference, audit=self.audit_ctx
        )
        if result is None:
            self.failed_attempts = attempts or self.failed_attempts
            return None
        self.llm_used = result
        return result.parsed.answer.strip() or None

    def _ground(self, answer: str, rows: list[dict]) -> grounding.Grounding:
        s = self.slots
        allowed = " ".join(
            filter(
                None,
                [
                    s.period_label,
                    s.entity_id,
                    s.entity_name,
                    s.employee_id,
                    s.employee_name,
                    T.period_text(s, self.directory.date_format),
                ],
            )
        )
        return grounding.check(
            answer,
            rows,
            question=self.question,
            period=(s.date_from, s.date_to),
            allowed_text=allowed,
        )

    # ------------------------------------------------------------------ non-answers
    def _unavailable(self, reason: str, text_: str, mode: str = "structured") -> _Answer:
        return _Answer(
            "unavailable", text_, mode=mode, reason=reason, conf=confidence.unavailable(reason)
        )

    def _no_data(self, explicit_period: bool) -> _Answer:
        text_ = T.no_data(self.slots, self.directory.date_format, has_period=explicit_period)
        return self._unavailable("no_data_in_scope", text_)

    def _pending_only(self, pending: list[dict]) -> _Answer:
        self.retrieved |= {p["record_id"] for p in pending}
        citations = [
            {
                "record_id": p["record_id"],
                "source_file": p["source_file"],
                "locator": p["source_locator"],
                "excerpt": f"{lineage.excerpt({**p, 'conflict': False})} | awaiting review",
            }
            for p in pending[: lineage.MAX_CITATIONS]
        ]
        mean = sum(float(p["extraction_confidence"] or 0) for p in pending) / len(pending)
        conf = confidence.score(
            confidence.Signals(True, "template_only", True, 1.0, mean, needs_review=True),
            high=self.settings.conf_high,
            low=self.settings.conf_low,
        )
        text_ = T.pending_only(self.slots, pending, self.directory.date_format)
        return _Answer(
            "needs_review", text_, citations=citations, citation_total=len(pending), conf=conf
        )

    # ------------------------------------------------------------------ output
    def _provider_fields(self) -> dict:
        if self.llm_used is not None:
            r = self.llm_used
            return {"provider": r.provider, "model": r.model, "fallback_path": r.fallback_path}
        failed = [
            a["provider"] for a in self.failed_attempts if a.get("error") != "skipped_external"
        ]
        path = ">".join(dict.fromkeys(failed)) if failed else ""
        return {
            "provider": TEMPLATE,
            "model": "deterministic",
            "fallback_path": f"{path}>{TEMPLATE}" if path else TEMPLATE,
        }

    def _response(self, a: _Answer) -> dict:
        ctx = self.ctx
        response = {
            "request_id": self.request_id,
            "status": a.status,
            "answer": a.text,
            "retrieval_mode": a.mode,
            "context": {
                "tenant_id": ctx.tenant_id,
                "product_id": ctx.product_id,
                "module": ctx.module,
                "role": ctx.role,
                "entity_scope": list(ctx.entities),
            },
            "citations": a.citations,
            "citation_total": a.citation_total,
            "confidence": a.conf.value,
            "confidence_band": a.conf.band,
            "confidence_explanation": a.conf.explanation,
            "unavailable_reason": a.reason,
            **self._provider_fields(),
            "prompt_version": PROMPT_VERSION,
            "retrieval_version": RETRIEVAL_VERSION,
            "warnings": self.warnings,
        }
        self.debug_info = {**self.debug, "sql": self.sql_executed, "slots": self.slots.public()}
        return response

    def _persist(self, response: dict, mode: str) -> None:
        masked, _ = pii.mask_text(self.question)
        try:
            with scoped_session(self.scope, role="app") as s:
                s.execute(
                    text(
                        """INSERT INTO query_responses (request_id, product_id, tenant_id, module,
                           entity_scope, role, sub, question_redacted, question_hash, mode,
                           sql_executed, response, provider, model, prompt_version,
                           retrieval_version)
                           VALUES (:rid, :p, :t, :m, :es, :role, :sub, :q, :qh, :mode, :sql,
                                   CAST(:resp AS jsonb), :prov, :model, :pv, :rv)
                           ON CONFLICT (request_id) DO NOTHING"""
                    ),
                    {
                        "rid": self.request_id,
                        "p": self.ctx.product_id,
                        "t": self.ctx.tenant_id,
                        "m": self.ctx.module,
                        "es": list(self.ctx.entities),
                        "role": self.ctx.role,
                        "sub": self.ctx.sub,
                        "q": masked,
                        "qh": question_hash(self.question),
                        "mode": mode,
                        "sql": self.sql_executed,
                        "resp": json.dumps(response),
                        "prov": response["provider"],
                        "model": response["model"],
                        "pv": PROMPT_VERSION,
                        "rv": RETRIEVAL_VERSION,
                    },
                )
        except Exception:  # noqa: BLE001 - the answer is still returned; the audit shows it
            log.exception("failed to persist query response request_id=%s", self.request_id)
            response["warnings"].append("The response could not be stored for later retrieval.")

    def _audit(self, response: dict, latency_ms: int) -> None:
        outcome = response["status"]
        if response["unavailable_reason"] == "no_data_in_scope":
            outcome = "filtered_or_absent"  # never distinguishes "denied" from "absent"
        audit.record(
            "query",
            self.request_id,
            query_mode=response["retrieval_mode"],
            retrieved_source_ids=[
                c.get("chunk_id") or c["record_id"] for c in response["citations"]
            ],
            provider=response["provider"],
            model=response["model"],
            fallback_path=response["fallback_path"],
            confidence=response["confidence"],
            outcome=outcome,
            latency_ms=latency_ms,
            details={
                "intent": self.intent,
                "status": response["status"],
                "unavailable_reason": response["unavailable_reason"],
                "question_hash": question_hash(self.question),
                "sql_hash": hashlib.sha256(self.sql_executed.encode()).hexdigest()
                if self.sql_executed
                else None,
                "citation_total": response["citation_total"],
                "warnings": response["warnings"],
                "cache": self.cache_status,
            },
            **audit.context_fields(self.ctx),
        )


def rows_have_facts(rows: list[dict]) -> bool:
    if not rows:
        return False
    return any(v not in (None, 0) for v in rows[0].values())


def question_hash(question: str) -> str:
    return hashlib.sha256(" ".join(question.lower().split()).encode()).hexdigest()


def answer(
    ctx: SecurityContext,
    request_id: str,
    question: str,
    filters: dict | None = None,
    include_debug: bool = False,
) -> dict:
    run = QueryRun(ctx, request_id, question, filters)
    response = run.execute()
    if include_debug:
        response = {**response, "debug": run.debug_info}
    return response


def stored_response(ctx: SecurityContext, request_id: str) -> dict | None:
    with scoped_session(ctx.to_scope(), role="reader") as s:
        return s.execute(
            text("SELECT response FROM query_responses WHERE request_id = :r"), {"r": request_id}
        ).scalar()
