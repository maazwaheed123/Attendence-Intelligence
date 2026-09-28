"""Structured retrieval: LLM-written SQL (validated) and deterministic intent templates.

Safety layers for LLM-written SQL, outermost first:
  1. the prompt shows ONLY v_attendance (schema_prompt.py)
  2. sqlglot allow-list validator (validator.py)
  3. rag_reader role: SELECT-only, no PII columns
  4. READ ONLY transaction + statement_timeout (db.session.scoped_session)
  5. row cap (executor.py)
  6. v_attendance fixes the business metric, so SQL cannot redefine "attendance %"
  7. Row-Level Security: whatever the SQL says, rows outside the caller's scope
     do not exist for it. The LLM cannot influence this layer.
"""
