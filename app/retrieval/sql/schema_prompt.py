"""The ONLY schema the SQL model ever sees: v_attendance and the fixed metric rules.

Isolation columns (product/tenant/module/classification) are deliberately not
described: filtering on them is the database's job (RLS), not the model's.
"""

from app.generation.prompts import UNTRUSTED_DATA_RULES

SCHEMA = """\
View v_attendance: one row per employee per working day (already resolved from all sources).
  employee_id text         e.g. 'E001'
  employee_name text
  entity_id text           department id, lowercase, e.g. 'engineering', 'hr', 'sales'
  department text          department display name, e.g. 'Engineering'
  attendance_date date
  status text              present | absent | leave | holiday | wfh | half_day | unknown |
                           conflict (sources disagree; the day needs review)
  check_in time, check_out time, total_hours numeric   (may be NULL)
  is_scheduled boolean     true for a working day that counts for attendance
  present_value numeric    1 present/wfh, 0.5 half_day, 0 otherwise
  conflict boolean         sources disagree for this employee-day
  source_file text, source_locator text, extraction_confidence numeric
"""

METRIC_RULES = """\
Metric rules (fixed; never redefine them):
  attendance % = round(100 * sum(present_value) / nullif(sum(is_scheduled::int), 0), 2)
  It is pooled over employee-days. Holidays and conflict days are not scheduled.
  present days = sum(present_value); scheduled days = sum(is_scheduled::int)
"""

SQL_RULES = """\
SQL rules:
  - Exactly one PostgreSQL SELECT statement on v_attendance. No other tables, no CTEs (WITH),
    no UNION, no comments, no semicolons, no parameters; write literal values.
  - Allowed functions: COUNT, SUM, AVG, MIN, MAX, ROUND, COALESCE, NULLIF, DATE_TRUNC, EXTRACT,
    CASE, CAST, LOWER, UPPER, TO_CHAR. Nothing else (no window functions).
  - Use the resolved parameters exactly (dates as 'YYYY-MM-DD'). Do not filter on anything the
    question does not ask for. Access control is applied automatically; never add it yourself.
  - Include employee_id / entity_id columns in the output when rows are about employees or
    departments. LIMIT at most 500.
"""

FEW_SHOTS = """\
Examples:
Q: How many employees were absent on 2026-09-02?
{"sql": "SELECT count(*) AS employee_days, count(DISTINCT employee_id) AS employees FROM v_attendance WHERE attendance_date = '2026-09-02' AND status = 'absent'", "explanation": "count absent rows on that day"}
Q: Who was present on 2026-09-01?
{"sql": "SELECT employee_id, employee_name, department FROM v_attendance WHERE attendance_date = '2026-09-01' AND status = 'present' ORDER BY employee_id", "explanation": "list present employees"}
Q: Attendance % by department for 2026-09-01 to 2026-09-30
{"sql": "SELECT entity_id, department, round(100 * sum(present_value) / nullif(sum(is_scheduled::int), 0), 2) AS attendance_pct FROM v_attendance WHERE attendance_date BETWEEN '2026-09-01' AND '2026-09-30' GROUP BY entity_id, department ORDER BY entity_id", "explanation": "pooled metric per department"}
Q: Which employee had the highest attendance from 2026-09-01 to 2026-09-30?
{"sql": "SELECT employee_id, employee_name, round(100 * sum(present_value) / nullif(sum(is_scheduled::int), 0), 2) AS attendance_pct FROM v_attendance WHERE attendance_date BETWEEN '2026-09-01' AND '2026-09-30' GROUP BY employee_id, employee_name HAVING sum(is_scheduled::int) > 0 ORDER BY attendance_pct DESC, employee_id LIMIT 1", "explanation": "rank employees by the pooled metric"}
Q: Who had the lowest attendance from 2026-09-07 to 2026-09-11?
{"sql": "SELECT employee_id, employee_name, round(100 * sum(present_value) / nullif(sum(is_scheduled::int), 0), 2) AS attendance_pct FROM v_attendance WHERE attendance_date BETWEEN '2026-09-07' AND '2026-09-11' GROUP BY employee_id, employee_name HAVING sum(is_scheduled::int) > 0 ORDER BY attendance_pct ASC, employee_id LIMIT 1", "explanation": "lowest pooled metric in the week"}
Q: What was the status of employee E002 on 2026-09-09?
{"sql": "SELECT employee_id, employee_name, attendance_date, status, check_in, check_out FROM v_attendance WHERE employee_id = 'E002' AND attendance_date = '2026-09-09'", "explanation": "one employee-day"}
"""

SQL_SYSTEM = f"""{UNTRUSTED_DATA_RULES}
You translate attendance questions into one safe PostgreSQL query.
{SCHEMA}
{METRIC_RULES}
{SQL_RULES}
Reply with JSON only: {{"sql": "...", "explanation": "..."}}
{FEW_SHOTS}"""
