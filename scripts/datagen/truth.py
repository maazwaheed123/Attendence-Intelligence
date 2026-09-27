"""Builds the ground-truth canonical attendance records from seed_spec.yaml.

Every sample file is rendered FROM these records, so each test knows the exact
expected answer. Randomness is seeded per employee, so results do not depend on
iteration order.
"""

import random
from datetime import date, datetime, time, timedelta
from pathlib import Path

import yaml

from scripts.datagen.util import daterange, to_date

SPEC_PATH = Path(__file__).resolve().parent.parent / "seed_spec.yaml"
COUNT_STATUSES = ["absent", "leave", "wfh", "half_day"]


def load_spec(path: Path = SPEC_PATH) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def calendar(spec: dict) -> dict:
    start, end = to_date(spec["period"]["start"]), to_date(spec["period"]["end"])
    holidays = {to_date(h) for h in spec["holidays"]}
    working = [d for d in daterange(start, end) if d.weekday() < 5]
    protected = set()
    for lo, hi in spec["protected_ranges"]:
        protected |= set(daterange(to_date(lo), to_date(hi)))
    return {
        "working": working,
        "holidays": holidays,
        "scheduled": [d for d in working if d not in holidays],
        "protected": protected,
    }


def _t(base: time, minutes: int) -> time:
    return (datetime.combine(date(2000, 1, 1), base) + timedelta(minutes=minutes)).time()


def _times(status: str, late: bool, rng: random.Random):
    if status in ("present", "wfh"):
        cin = _t(time(9, 40), rng.randint(0, 18)) if late else _t(time(8, 40), rng.randint(0, 40))
        cout = _t(time(17, 20), rng.randint(0, 80))
    elif status == "half_day":
        cin = _t(time(8, 50), rng.randint(0, 20))
        cout = _t(time(13, 0), rng.randint(0, 30))
    else:
        return None, None, None
    secs = (
        datetime.combine(date(2000, 1, 1), cout) - datetime.combine(date(2000, 1, 1), cin)
    ).seconds
    return cin.strftime("%H:%M"), cout.strftime("%H:%M"), round(secs / 3600, 2)


def build_truth(spec: dict) -> dict:
    cal = calendar(spec)
    product, module = spec["default_product"], spec["module"]
    employees, rows = {}, []
    phone_idx = 0

    for tenant_id, emps in spec["employees"].items():
        entity_names = spec["tenants"][tenant_id]["entities"]
        for emp in emps:
            rng = random.Random(f"{spec['seed']}-{tenant_id}-{emp['id']}")
            phone_idx += 1
            first, last = emp["name"].lower().split(" ", 1)
            employees[f"{tenant_id}:{emp['id']}"] = {
                "tenant_id": tenant_id,
                "product_id": product,
                "employee_id": emp["id"],
                "employee_name": emp["name"],
                "entity_id": emp["entity"],
                "department": entity_names[emp["entity"]],
                # Fictional contact data: 555-01xx is reserved for fiction.
                "phone": f"+1-555-01{phone_idx:02d}",
                "national_id": f"NID-{rng.randint(100_000_000, 999_999_999)}",
                "email": f"{first}.{last.replace(' ', '')}@{tenant_id}.example",
            }

            status = {d: ("holiday" if d in cal["holidays"] else "present") for d in cal["working"]}
            fixed = {to_date(k): v for k, v in (emp.get("fixed") or {}).items()}
            status.update(fixed)
            pool = [d for d in cal["scheduled"] if d not in fixed and d not in cal["protected"]]
            picks = rng.sample(pool, sum(emp["counts"]))
            i = 0
            for st, n in zip(COUNT_STATUSES, emp["counts"], strict=True):
                for d in picks[i : i + n]:
                    status[d] = st
                i += n

            late = {to_date(x) for x in emp.get("late", [])}
            for d in cal["working"]:
                cin, cout, hours = _times(status[d], d in late, rng)
                rows.append(
                    {
                        "tenant_id": tenant_id,
                        "product_id": product,
                        "module": module,
                        "employee_id": emp["id"],
                        "employee_name": emp["name"],
                        "department": entity_names[emp["entity"]],
                        "entity_id": emp["entity"],
                        "attendance_date": d.isoformat(),
                        "status": status[d],
                        "check_in": cin,
                        "check_out": cout,
                        "total_hours": hours,
                        "classification": "internal",
                    }
                )

    other = []
    for r in spec["other_product_rows"]:
        e = employees[f"tenant_a:{r['employee_id']}"]
        present = r["status"] == "present"
        other.append(
            {
                "tenant_id": "tenant_a",
                "product_id": "hrms_ai",
                "module": module,
                "employee_id": e["employee_id"],
                "employee_name": e["employee_name"],
                "department": e["department"],
                "entity_id": e["entity_id"],
                "attendance_date": r["date"],
                "status": r["status"],
                "check_in": "09:00" if present else None,
                "check_out": "17:00" if present else None,
                "total_hours": 8.0 if present else None,
                "classification": "internal",
            }
        )

    rows.sort(key=lambda r: (r["tenant_id"], r["attendance_date"], r["employee_id"]))
    return {"employees": employees, "rows": rows, "other_product_rows": other}


def index_rows(rows: list[dict]) -> dict:
    return {(r["tenant_id"], r["employee_id"], r["attendance_date"]): r for r in rows}
