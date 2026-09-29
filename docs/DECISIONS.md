# Design decisions

Each entry: the decision, why, and what it costs.

## Architecture and stores

**D1. One PostgreSQL for records, vectors, full text, feedback and audit.**
Row-Level Security then protects every retrieval mode through one mechanism: SQL,
pgvector, FTS, trigram, feedback matching, stored responses and exports all read through
the same policies. Separate Qdrant/OpenSearch stores would each need their own
filter logic, which is where isolation bugs usually hide. Cost: no true BM25, and vector
search scale is bounded by Postgres (fine for this data; the stores sit behind
`app/retrieval/stores.py` classes, so they can be swapped).

**D2. Fail-closed scope in the database.** The scope is written with
`set_config(..., true)` (transaction-local); a missing setting matches nothing. RLS is
`FORCE`d so even the table owner is filtered when it is not a superuser. Request code never
connects as the owner.

**D3. Metrics are defined in the `v_attendance` view.** `is_scheduled` and
`present_value` encode the business rules (holidays excluded, half day = 0.5, conflicting
sources excluded and flagged), so model-written SQL cannot redefine "attendance %".
Metric: `100 * sum(present_value) / sum(is_scheduled)` over pooled employee-days.

**D4. Least-privilege roles.** `rag_reader` (all query-time reads) has column-level
grants that exclude PII, raw values and raw chunk text; `app_rw` cannot delete; audit is
insert-only.

## Ingestion

**D5. Idempotency by SHA-256, versioning by logical name.** Same bytes → the same
document (reported as duplicate); a changed file with the same logical name becomes the
next version, supersedes the old records and reports the diff. A version finishing after
its successor is retired. Version numbers are assigned under a transaction-scoped advisory
lock per (product, tenant, module, logical name), so two concurrent uploads of the same
logical name become v1 and v2 instead of two v1s.

**D6. Uncertain values are never facts.** Marks like `P?`, low OCR confidence
(< 0.75) and vision-reported uncertain fields put the record in review; review records are
excluded from `v_attendance` and surface as warnings or `needs_review`.

**D7. Ingestion writes with system clearance.** A manager must be able to upload a
file that contains confidential manager remarks; the chunks are still classified
`confidential` and only readable by callers with that clearance. Product/tenant/entity
isolation still applies to the upload itself.

**D8. OCR = Tesseract + a local vision model, reconciled.** Tesseract with OpenCV
clean-up reads printed scans well; `qwen2.5vl:3b` transcribes handwriting with an explicit
"copy, don't guess; mark ? and list uncertain fields" prompt. Without the vision model the
system degrades to Tesseract only (confidence capped at 0.6, always review) and never
invents values.
Added after testing with the real model: qwen2.5vl reports doubt in free form ("Status",
"Check In", or the doubtful value itself such as "Absent") rather than as field names, and
notes such as "crossed out and rewritten". Both are mapped onto fields, and anything that
cannot be mapped counts as doubt about the status, so a warning from the model is never
ignored. An uncertain status only becomes a fact again when Tesseract independently reads
the same value. Vision calls run in the worker with their own `VISION_TIMEOUT_S` (600 s),
because one handwritten image takes ~4 minutes on CPU.

**D9. Transient vs permanent failures.** Corrupt/empty/unsupported
files fail permanently with a reason; transient failures retry (RQ `Retry`, 10 s / 30 s),
then land in the failed queue with a manual retry endpoint. The worker must run with
`--with-scheduler` or delayed retries never fire.

## Retrieval and answers

**D10. Rules-first classification.** Deterministic rules name the intent and slots
for the common question shapes; the LLM JSON classifier is only used when the rules
cannot. Faster on CPU and not steerable by prompt injection in the question.

**D11. Dual-path structured answers.** For templatable intents both a deterministic
template query and the model's SQL run (both as `rag_reader`); agreement raises confidence,
disagreement falls back to the template. The model phrases the answer from the rows; any
number, date, id or name not present in the rows/question/period forces the template text.

**D12. Names resolve only through the caller's scoped directory.** An unknown name
and a name in another tenant/entity produce the identical "No attendance data for the
requested employee in your permitted scope." — no existence leak.

**D13. Embeddings: Ollama `nomic-embed-text`.** Already available
locally, 768-dim, no extra model download. Only masked chunk text is embedded.

**D14. No cross-encoder download; `LexicalReranker`.** A deterministic reranker
(cosine, term overlap, employee/date match, narrative preference, named document)
behind an interface; avoids a 1.1 GB CPU model.

**D15. Manager remarks are confidential; manager tokens carry internal clearance.**
So the remark in Q5/Q6 is visible to the HR admin but not to the engineering manager — a
deliberate demonstration of classification-based access (tested both ways).

**D16. A model "insufficient" verdict on a named document quotes it.** Live qwen
7b called the injection memo insufficient (probably because of the flagged paragraph).
When the question names an in-scope document with non-flagged chunks, those chunks are
quoted extractively with a warning; every other "insufficient" verdict still refuses.

## Governance

**D17. One output guard for every answer.** `postprocess.finalize()` validates
citations against the retrieved set, withholds answers that mention out-of-scope entities,
blocks instruction-like output and echoes of flagged chunks, masks PII by clearance and
validates the schema, falling back to a safe response.

**D18. Controlled unavailability.** `no_data_in_scope`, `out_of_scope`, `not_permitted`,
`insufficient_evidence`, `blocked` — each with fixed text and no model call for refusals.

**D19. Provider chain with a deterministic last resort.** qwen2.5 7b → 3b →
template engine, a Redis circuit breaker per provider, a JSON repair round, every call
audited. Models down = service "degraded", not "down".

## Feedback loop

**D20. Feedback becomes a template, not a fine-tune.** An approved ideal output is
turned into a template whose values are recomputed from live rows on every reuse, so a
correction can never freeze stale numbers or carry one tenant's data to another. Ideal
outputs whose numbers are not supported by the evidence are rejected. Examples are
versioned per scope lineage (product, tenant, module, entity, intent signature), can be
deactivated and rolled back, and match only equivalent questions (same intent signature +
similarity ≥ 0.85) within the caller's RLS scope.

## Cache, exports, UI

**D21. Query cache keyed by the full scope and data/feedback versions.** Entries
are per user (safest; lower hit ratio), include today's date, and are invalidated by any
completed ingestion or feedback change. Degraded or blocked answers are never cached; a
hit is re-finalized, persisted and audited.

**D22. Exports come from one RLS-scoped dataset.** JSON/XLSX/PDF are renderers
over the same rows, so they cannot disagree; a sha256 checksum over the ordered ids is in
every file and the `X-Export-Checksum` header. Exporting a stored answer re-reads its
citations in the caller's scope.

**D23. The UI talks only to the public API.** Dev tokens per persona; the UI hides
what a role cannot do, but every decision is still made by the API.

## Environment

**D24. Local only, no API keys.** Everything runs in Docker Compose plus Ollama on the host;
the OpenAI-compatible adapter can point at a cloud provider by configuration, gated per
tenant by `allow_external_llm`.

**D25. Schema owner is the Postgres superuser in the MVP.** Used only for migrations, seeding
and maintenance. Production: a separate non-superuser owner.
