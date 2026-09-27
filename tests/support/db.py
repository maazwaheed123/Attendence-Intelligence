"""Test-database helpers: migrations, seeding, and loading the sample corpus."""

import json
import uuid
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from app.config import get_settings
from app.db.session import owner_session
from app.security.scope import DbScope
from scripts.datagen.truth import load_spec
from scripts.seed import seed

ROOT = Path(__file__).resolve().parents[2]
GT = ROOT / "data" / "ground_truth"
NS = uuid.UUID("7d9c0e1a-5b1e-4c55-9f53-0b6f3c1f2a10")

METHOD = {
    "csv": "csv",
    "xlsx": "xlsx",
    "docx": "docx",
    "pdf": "pdf_text",
    "image": "ocr_tesseract",
    "pdf_scanned": "ocr_tesseract",
}
MUTABLE_TABLES = (
    "audit_events, feedback_examples, query_responses, document_chunks, attendance_records, "
    "ingestion_jobs, source_documents, employees, entities, tenant_products, tenants, products"
)


def alembic_config() -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "app" / "db" / "migrations"))
    cfg.attributes["url"] = get_settings().database_url_owner.get_secret_value()
    return cfg


def migrate_fresh() -> None:
    cfg = alembic_config()
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")


def persona_scope(persona: str, **overrides) -> DbScope:
    p = load_spec()["personas"][persona]
    fields = {
        "product_id": p["product"],
        "tenant_id": p["tenant"],
        "module": "attendance",
        "entities": tuple(p["entities"]),
        "clearance": p["clearance"],
        "employee_id": p.get("employee_id"),
        "sub": f"user:{persona}",
    }
    fields.update(overrides)
    return DbScope(**fields)


def _confidence(fmt: str, expected_review: bool) -> float:
    if expected_review:
        return 0.4
    return {"image": 0.85, "pdf_scanned": 0.88}.get(fmt, 0.99)


def reset_corpus() -> dict:
    """Truncate, seed, and load every attendance manifest row + chunks + misc rows."""
    manifests = json.loads((GT / "manifests.json").read_text(encoding="utf-8"))
    truth = json.loads((GT / "ground_truth.json").read_text(encoding="utf-8"))
    tidx = {
        (r["tenant_id"], r["product_id"], r["employee_id"], r["attendance_date"]): r
        for r in truth["rows"] + truth["other_product_rows"]
    }
    counts = {"documents": 0, "records": 0, "chunks": 0}
    with owner_session() as s:
        s.execute(text(f"TRUNCATE {MUTABLE_TABLES} RESTART IDENTITY CASCADE"))
        seed(s)
        for name in manifests["corpus_order"]:
            m = manifests["files"][name]
            if m["kind"] == "invalid":
                continue
            doc_id = uuid.uuid5(NS, m["sha256"])
            entities = {
                truth["employees"][f"{m['tenant_id']}:{r['employee_id']}"]["entity_id"]
                for r in m["rows"]
            }
            doc_entity = (
                entities.pop() if len(entities) == 1 else (None if entities else "engineering")
            )
            s.execute(
                text(
                    """INSERT INTO source_documents (document_id, product_id, tenant_id, module,
                       entity_id, logical_name, filename, file_type, checksum_sha256, size_bytes,
                       version, status, classification, storage_path, uploaded_by)
                       VALUES (:d, :p, :t, 'attendance', :e, :ln, :fn, :ft, :sha, 1, :v,
                               'completed', 'internal', :sp, 'test-loader')"""
                ),
                {
                    "d": doc_id,
                    "p": m["product_id"],
                    "t": m["tenant_id"],
                    "e": doc_entity,
                    "ln": m["logical_name"],
                    "fn": name,
                    "ft": m["format"],
                    "sha": m["sha256"],
                    "v": m["version"],
                    "sp": f"data/generated/{name}",
                },
            )
            counts["documents"] += 1
            superseded = name == "tenant_a_sep.csv"
            for row in m["rows"]:
                t = tidx[
                    (row["tenant_id"], m["product_id"], row["employee_id"], row["attendance_date"])
                ]
                same = row["status"] == t["status"]
                rid = uuid.uuid5(NS, f"{m['sha256']}|{row['locator']}")
                s.execute(
                    text(
                        """INSERT INTO attendance_records (record_id, source_document_id, product_id,
                           tenant_id, module, entity_id, classification, employee_id, employee_name,
                           department, attendance_date, status, check_in, check_out, total_hours,
                           source_file, source_locator, raw_values, extraction_method,
                           extraction_confidence, review_required, review_reasons, is_active)
                           VALUES (:rid, :d, :p, :t, 'attendance', :e, 'internal', :emp, :name, :dept,
                                   :date, :st, :cin, :cout, :hrs, :fn, :loc, CAST(:raw AS jsonb),
                                   :meth, :conf, :rev, :reasons, :active)"""
                    ),
                    {
                        "rid": rid,
                        "d": doc_id,
                        "p": m["product_id"],
                        "t": row["tenant_id"],
                        "e": t["entity_id"],
                        "emp": row["employee_id"],
                        "name": t["employee_name"],
                        "dept": t["department"],
                        "date": row["attendance_date"],
                        "st": row["status"],
                        "cin": t["check_in"] if same else None,
                        "cout": t["check_out"] if same else None,
                        "hrs": t["total_hours"] if same else None,
                        "fn": name,
                        "loc": row["locator"],
                        "raw": json.dumps(row["rendered"]),
                        "meth": METHOD[m["format"]],
                        "conf": _confidence(m["format"], row["expected_review"]),
                        "rev": row["expected_review"],
                        "reasons": ["test fixture: ambiguous"] if row["expected_review"] else [],
                        "active": not superseded,
                    },
                )
                counts["records"] += 1
                s.execute(
                    text(
                        """INSERT INTO document_chunks (chunk_id, source_document_id, record_id,
                           product_id, tenant_id, module, entity_id, employee_id, classification,
                           chunk_type, text, text_masked, locator, is_active)
                           VALUES (:c, :d, :rid, :p, :t, 'attendance', :e, :emp, 'internal',
                                   'row_card', :txt, :txt, :loc, :active)"""
                    ),
                    {
                        "c": uuid.uuid5(NS, f"card|{rid}"),
                        "d": doc_id,
                        "rid": rid,
                        "p": m["product_id"],
                        "t": row["tenant_id"],
                        "e": t["entity_id"],
                        "emp": row["employee_id"],
                        "txt": f"{row['attendance_date']} {row['employee_id']} {t['employee_name']} {row['status']}",
                        "loc": row["locator"],
                        "active": not superseded,
                    },
                )
                counts["chunks"] += 1
            for n in m["narrative"]:
                confidential = n["section"] == "Manager remarks"
                s.execute(
                    text(
                        """INSERT INTO document_chunks (chunk_id, source_document_id, product_id,
                           tenant_id, module, entity_id, classification, chunk_type, text,
                           text_masked, locator, suspicious)
                           VALUES (:c, :d, :p, :t, 'attendance', :e, :cls, 'narrative', :txt, :txt,
                                   :loc, :sus)"""
                    ),
                    {
                        "c": uuid.uuid5(NS, f"narr|{m['sha256']}|{n['locator']}"),
                        "d": doc_id,
                        "p": m["product_id"],
                        "t": m["tenant_id"],
                        "e": doc_entity,
                        "cls": "confidential" if confidential else "internal",
                        "txt": n["text"],
                        "loc": n["locator"],
                        "sus": "prompt_injection" in n["flags"],
                    },
                )
                counts["chunks"] += 1

        # One row per tenant in the remaining tenant-bearing tables (RLS coverage).
        for tenant in ("tenant_a", "tenant_b"):
            s.execute(
                text(
                    """INSERT INTO ingestion_jobs (job_id, product_id, tenant_id, module, entity_id,
                       stage, status, created_by)
                       VALUES (:j, 'attendance_ai', :t, 'attendance', 'engineering', 'completed',
                               'completed', 'test-loader')"""
                ),
                {"j": uuid.uuid5(NS, f"job|{tenant}"), "t": tenant},
            )
            s.execute(
                text(
                    """INSERT INTO query_responses (request_id, product_id, tenant_id, module,
                       entity_scope, role, sub, question_redacted, question_hash, response)
                       VALUES (:r, 'attendance_ai', :t, 'attendance', ARRAY['engineering'],
                               'manager', :sub, 'q', repeat('0', 64), '{}')"""
                ),
                {"r": f"req_{tenant}", "t": tenant, "sub": f"user:{tenant}"},
            )
            s.execute(
                text(
                    """INSERT INTO feedback_examples (example_id, lineage_id, version, status,
                       question, intent_signature, feedback, ideal_final_output, product_id,
                       tenant_id, module, entity_id, reviewer_id)
                       VALUES (:x, :x, 1, 'active', 'q', 'sig', 'fb', 'ideal', 'attendance_ai', :t,
                               'attendance', 'engineering', 'user:reviewer')"""
                ),
                {"x": uuid.uuid5(NS, f"fb|{tenant}"), "t": tenant},
            )
            s.execute(
                text(
                    """INSERT INTO audit_events (request_id, product_id, tenant_id, module,
                       event_type, outcome) VALUES (:r, 'attendance_ai', :t, 'attendance',
                       'query', 'answered')"""
                ),
                {"r": f"req_{tenant}", "t": tenant},
            )
    return counts
