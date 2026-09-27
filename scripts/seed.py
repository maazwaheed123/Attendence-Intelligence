"""Seed reference data: products, tenants, entities and the employee roster.

Usage (inside the api container):  python -m scripts.seed
Runs as the schema owner; idempotent (safe to re-run). PII is stored encrypted.
"""

import json
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.session import owner_session
from app.security.crypto import encrypt
from scripts.datagen.truth import load_spec

GROUND_TRUTH = Path(__file__).resolve().parents[1] / "data" / "ground_truth" / "ground_truth.json"


def seed(session: Session) -> dict:
    spec = load_spec()
    truth = json.loads(GROUND_TRUTH.read_text(encoding="utf-8"))

    for pid, name in spec["products"].items():
        session.execute(
            text(
                "INSERT INTO products VALUES (:p, :n) "
                "ON CONFLICT (product_id) DO UPDATE SET name = :n"
            ),
            {"p": pid, "n": name},
        )
    for tid, t in spec["tenants"].items():
        session.execute(
            text(
                "INSERT INTO tenants (tenant_id, name, date_format) VALUES (:t, :n, :f) "
                "ON CONFLICT (tenant_id) DO UPDATE SET name = :n, date_format = :f"
            ),
            {"t": tid, "n": t["name"], "f": t["date_format"]},
        )
        for eid, ename in t["entities"].items():
            session.execute(
                text(
                    "INSERT INTO entities (tenant_id, entity_id, name) VALUES (:t, :e, :n) "
                    "ON CONFLICT (tenant_id, entity_id) DO UPDATE SET name = :n"
                ),
                {"t": tid, "e": eid, "n": ename},
            )

    tenant_products = {
        ("tenant_a", "attendance_ai"),
        ("tenant_b", "attendance_ai"),
        ("tenant_a", "hrms_ai"),
    }
    for tid, pid in sorted(tenant_products):
        session.execute(
            text("INSERT INTO tenant_products VALUES (:t, :p) ON CONFLICT DO NOTHING"),
            {"t": tid, "p": pid},
        )

    roster = [(e, spec["default_product"]) for e in truth["employees"].values()]
    decoy_ids = {r["employee_id"] for r in truth["other_product_rows"]}
    roster += [
        (e, "hrms_ai")
        for e in truth["employees"].values()
        if e["tenant_id"] == "tenant_a" and e["employee_id"] in decoy_ids
    ]
    for e, pid in roster:
        session.execute(
            text(
                """
                INSERT INTO employees (product_id, tenant_id, employee_id, employee_name, entity_id,
                                       phone_enc, national_id_enc, email_enc)
                VALUES (:p, :t, :id, :name, :ent, :ph, :nid, :em)
                ON CONFLICT (product_id, tenant_id, employee_id) DO UPDATE
                SET employee_name = :name, entity_id = :ent,
                    phone_enc = :ph, national_id_enc = :nid, email_enc = :em
                """
            ),
            {
                "p": pid,
                "t": e["tenant_id"],
                "id": e["employee_id"],
                "name": e["employee_name"],
                "ent": e["entity_id"],
                "ph": encrypt(e["phone"]),
                "nid": encrypt(e["national_id"]),
                "em": encrypt(e["email"]),
            },
        )
    return {
        "products": len(spec["products"]),
        "tenants": len(spec["tenants"]),
        "employees": len(roster),
    }


def main() -> None:
    with owner_session() as s:
        print("seeded:", seed(s))


if __name__ == "__main__":
    main()
