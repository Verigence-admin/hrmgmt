"""People who have left: their last day, for reports (product owner 2026-11-09).

Someone who quit, was suspended or was terminated is shown in attendance reports for the days
they were employed, up to and including their last day, and not after ("keep up to exit date").
Their last day is the effective date of the approved status change that made them not active.
Someone not active with no such change on file has no last day and is not shown at all.

The same list goes to Audit Core (workcontext.report_leavers), which ends their project roles
after that day and keeps them out of its own reports from then on.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import Connection, text

# The last day of employee ``e`` (NULL while active). Use with ``hr.employee e``.
LAST_DAY_SQL = (
    "(CASE WHEN e.employment_status <> 'ACTIVE' THEN"
    " (SELECT max(c.effective_date) FROM hr.employee_status_change c"
    "  WHERE c.employee_id = e.employee_id AND c.status = 'APPROVED'"
    "    AND c.to_status = e.employment_status) END)"
)


def employed_on_or_after(param: str) -> str:
    """WHERE fragment: active, or left on or after the date bound to ``:param``."""
    return f"(e.employment_status = 'ACTIVE' OR {LAST_DAY_SQL} >= :{param})"


def after_last_day(last_day: date | None, day: date) -> bool:
    return last_day is not None and day > last_day


def last_days(conn: Connection, employee_ids: list[str]) -> dict[str, date | None]:
    """employee -> last day, for those of ``employee_ids`` who are not active."""
    if not employee_ids:
        return {}
    return {
        str(r[0]): r[1]
        for r in conn.execute(
            text(
                f"SELECT e.employee_id, {LAST_DAY_SQL} FROM hr.employee e"
                " WHERE e.employee_id = ANY(CAST(:ids AS uuid[])) AND e.employment_status <> 'ACTIVE'"
            ),
            {"ids": employee_ids},
        )
    }


def leavers(conn: Connection) -> list[dict[str, Any]]:
    """Everyone not active who has a Verigence login, with their last day, for Audit Core."""
    rows = conn.execute(
        text(
            f"SELECT e.security_user_id::text AS uid, e.employee_code, e.employment_status,"
            f" {LAST_DAY_SQL} AS last_day FROM hr.employee e"
            " WHERE e.employment_status <> 'ACTIVE' AND e.security_user_id IS NOT NULL"
            " ORDER BY e.employee_code"
        )
    ).mappings()
    return [
        {
            "securityUserId": r["uid"],
            "employeeCode": r["employee_code"],
            "status": r["employment_status"],
            "exitDate": r["last_day"].isoformat() if r["last_day"] else None,
        }
        for r in rows
    ]
