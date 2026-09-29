"""Expected answers, computed from ground truth + file manifests.

Evidence status of each (tenant, employee, date) fact after ingesting the whole
sample corpus in order:
  clean        at least one non-review source states the true value
  review_only  every source for it needs human review (ambiguous OCR)
  conflict     sources disagree (e.g. letter says present, CSV says absent)
Only `clean` facts count in aggregates; the others must be surfaced as
"needs review", never presented as fact.

Metric rules (identical to the v_attendance view in Step 2):
  scheduled day   = status != holiday
  present value   = 1 for present/wfh, 0.5 for half_day, 0 otherwise
  attendance %    = 100 * sum(present value) / sum(scheduled)   (pooled employee-days)
"""

from collections import defaultdict

PRESENT_VALUE = {"present": 1.0, "wfh": 1.0, "half_day": 0.5}


def evidence_status(truth_rows, manifests, spec) -> dict:
    """Map (tenant, emp, date) -> clean | review_only | conflict."""
    sources = defaultdict(list)
    for m in manifests:
        if m.kind != "attendance" or m.product_id != spec["default_product"]:
            continue
        superseded = m.filename == "tenant_a_sep.csv"
        for row in m.rows:
            if superseded:
                continue
            key = (row["tenant_id"], row["employee_id"], row["attendance_date"])
            sources[key].append(row)

    out = {}
    for r in truth_rows:
        key = (r["tenant_id"], r["employee_id"], r["attendance_date"])
        rows = sources.get(key)
        if not rows:
            raise AssertionError(f"truth row not covered by any source file: {key}")
        clean = [x for x in rows if not x["expected_review"]]
        if not clean:
            out[key] = "review_only"
        elif len({x["status"] for x in clean}) > 1:
            out[key] = "conflict"
        else:
            if clean[0]["status"] != r["status"]:
                raise AssertionError(f"clean sources disagree with truth for {key}")
            out[key] = "clean"
    return out


def _pct(rows) -> float | None:
    sched = sum(1 for r in rows if r["status"] != "holiday")
    if not sched:
        return None
    return round(100 * sum(PRESENT_VALUE.get(r["status"], 0) for r in rows) / sched, 2)


def _in_scope(row, scope) -> bool:
    if row["tenant_id"] != scope["tenant"] or row["product_id"] != scope["product"]:
        return False
    if scope["entities"] != ["*"] and row["entity_id"] not in scope["entities"]:
        return False
    return not scope.get("employee_id") or row["employee_id"] == scope["employee_id"]


def _rank(groups: dict) -> dict:
    vals = {k: _pct(v) for k, v in groups.items()}
    vals = {k: v for k, v in vals.items() if v is not None}
    if not vals:
        return {}
    hi, lo = max(vals.values()), min(vals.values())
    top = sorted(k for k, v in vals.items() if v == hi)
    bottom = sorted(k for k, v in vals.items() if v == lo)
    return {
        "highest": {"keys": top, "value": hi, "unique": len(top) == 1},
        "lowest": {"keys": bottom, "value": lo, "unique": len(bottom) == 1},
    }


def scope_metrics(rows, periods) -> dict:
    by_date = defaultdict(lambda: defaultdict(list))
    for r in rows:
        by_date[r["attendance_date"]][r["status"]].append(r["employee_id"])
    result = {
        "record_count": len(rows),
        "by_date": {
            d: {s: sorted(ids) for s, ids in sorted(st.items())}
            for d, st in sorted(by_date.items())
        },
        "periods": {},
    }
    for name, (lo, hi) in periods.items():
        pr = [r for r in rows if lo <= r["attendance_date"] <= hi]
        by_emp, by_dept = defaultdict(list), defaultdict(list)
        for r in pr:
            by_emp[r["employee_id"]].append(r)
            by_dept[r["entity_id"]].append(r)
        result["periods"][name] = {
            "range": [lo, hi],
            "attendance_pct": _pct(pr),
            "present_days": sum(PRESENT_VALUE.get(r["status"], 0) for r in pr),
            "scheduled_days": sum(1 for r in pr if r["status"] != "holiday"),
            "by_employee": {k: _pct(v) for k, v in sorted(by_emp.items())},
            "by_department": {k: _pct(v) for k, v in sorted(by_dept.items())},
            "employee_rank": _rank(by_emp),
            "department_rank": _rank(by_dept),
        }
    return result


def build_expected(truth, manifests, spec) -> dict:
    status = evidence_status(truth["rows"], manifests, spec)
    for r in truth["rows"]:
        r["evidence_status"] = status[(r["tenant_id"], r["employee_id"], r["attendance_date"])]
    for r in truth["other_product_rows"]:
        r["evidence_status"] = "clean"

    all_rows = truth["rows"] + truth["other_product_rows"]
    clean = [r for r in all_rows if r["evidence_status"] == "clean"]
    periods = {"month": [spec["period"]["start"], spec["period"]["end"]], **spec["weeks"]}

    scopes = {}
    for persona, p in spec["personas"].items():
        scope = {"tenant": p["tenant"], "product": p["product"], "entities": p["entities"]}
        if p.get("employee_id"):
            scope["employee_id"] = p["employee_id"]
        in_scope = [r for r in clean if _in_scope(r, scope)]
        excluded = [
            {k: r[k] for k in ("employee_id", "attendance_date", "status", "evidence_status")}
            for r in all_rows
            if r["evidence_status"] != "clean" and _in_scope(r, scope)
        ]
        scopes[persona] = {
            "scope": scope,
            "role": p["role"],
            "excluded_needs_review": excluded,
            **scope_metrics(in_scope, periods),
        }

    return {
        "metric_rules": {
            "scheduled_day": "status != holiday",
            "present_value": PRESENT_VALUE,
            "attendance_pct": "100 * sum(present_value) / sum(scheduled), pooled employee-days",
            "counted_facts": "evidence_status == clean only",
        },
        "periods": periods,
        "evidence_summary": {
            s: sum(1 for r in all_rows if r["evidence_status"] == s)
            for s in ("clean", "review_only", "conflict")
        },
        "by_persona": scopes,
    }


def assert_unique_rankings(expected: dict) -> None:
    """The demo questions need unambiguous highest/lowest answers."""
    checks = [
        ("a_hr_admin", "month", "employee_rank", "highest"),
        ("a_hr_admin", "month", "employee_rank", "lowest"),
        ("a_hr_admin", "month", "department_rank", "highest"),
        ("a_hr_admin", "month", "department_rank", "lowest"),
        ("a_hr_admin", "week2", "employee_rank", "lowest"),
        ("a_eng_manager", "month", "employee_rank", "highest"),
        ("a_eng_manager", "month", "employee_rank", "lowest"),
        ("b_manager", "month", "employee_rank", "highest"),
        ("b_manager", "month", "employee_rank", "lowest"),
        ("b_manager", "month", "department_rank", "highest"),
    ]
    for persona, period, rank, side in checks:
        entry = expected["by_persona"][persona]["periods"][period][rank][side]
        if not entry["unique"]:
            raise AssertionError(f"tie for {persona}/{period}/{rank}/{side}: {entry}")
