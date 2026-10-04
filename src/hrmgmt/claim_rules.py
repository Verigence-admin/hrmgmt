"""Reimbursement rules, decided on the server.

Travel and meals are separate. The monthly travel total decides who finally approves a travel
claim: HR up to the Finance threshold, Finance above it. A claim older than the allowed months
needs an extra Finance exception approval. A claim goes into the payroll of the month it was
submitted in if submitted by the cut-off day, otherwise the next month."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Connection, text

from hrmgmt import settings_store as cfg
from hrmgmt import workcontext as wc
from hrmgmt.approvals import is_ceo
from hrmgmt.authz import Authorizer
from hrmgmt.errors import ApiError, conflict

OPEN_STATUSES = ("SUBMITTED", "CORRECTION_REQUESTED", "APPROVED", "HANDED_TO_PAYROLL", "PAID")
MAX_AMOUNT = Decimal("100000")


def month_start(day: date) -> date:
    return day.replace(day=1)


def add_months(day: date, months: int) -> date:
    index = day.year * 12 + (day.month - 1) + months
    return date(index // 12, index % 12 + 1, 1)


def payroll_month_for(submitted: date, cutoff_day: int) -> date:
    first = month_start(submitted)
    return first if submitted.day <= cutoff_day else add_months(first, 1)


def is_stale(expense: date, submitted: date, months: int) -> bool:
    """Older than `months` calendar months before the submission date."""
    limit = add_months(month_start(submitted), -months)
    limit = limit.replace(day=min(submitted.day, _days_in_month(limit)))
    return expense < limit


def _days_in_month(first: date) -> int:
    return (add_months(first, 1) - first).days


def travel_total(
    conn: Connection, employee_id: str, month: date, exclude_claim: str | None = None
) -> Decimal:
    """What the person has claimed for travel in an expense month, rejected and cancelled excluded."""
    row = conn.execute(
        text(
            "SELECT coalesce(sum(c.amount), 0) FROM hr.claim c JOIN hr.claim_category k"
            " ON k.category_code = c.category_code"
            " WHERE c.employee_id = CAST(:e AS uuid) AND k.kind = 'TRAVEL'"
            " AND c.status NOT IN ('REJECTED', 'CANCELLED')"
            " AND c.expense_date >= :a AND c.expense_date < :b"
            " AND (CAST(:x AS uuid) IS NULL OR c.claim_id <> CAST(:x AS uuid))"
        ),
        {"e": employee_id, "a": month, "b": add_months(month, 1), "x": exclude_claim},
    ).scalar_one()
    return Decimal(row)


def meals_total(
    conn: Connection, employee_id: str, month: date, exclude_claim: str | None = None
) -> Decimal:
    row = conn.execute(
        text(
            "SELECT coalesce(sum(c.amount), 0) FROM hr.claim c JOIN hr.claim_category k"
            " ON k.category_code = c.category_code"
            " WHERE c.employee_id = CAST(:e AS uuid) AND k.kind = 'MEALS'"
            " AND c.status NOT IN ('REJECTED', 'CANCELLED')"
            " AND c.expense_date >= :a AND c.expense_date < :b"
            " AND (CAST(:x AS uuid) IS NULL OR c.claim_id <> CAST(:x AS uuid))"
        ),
        {"e": employee_id, "a": month, "b": add_months(month, 1), "x": exclude_claim},
    ).scalar_one()
    return Decimal(row)


@dataclass(frozen=True)
class Plan:
    stages: list[str]
    stale: bool
    travel_total: Decimal


def check_limits_and_plan(
    conn: Connection,
    authorizer: Authorizer,
    *,
    employee_id: str,
    kind: str,
    amount: Decimal,
    expense_date: date,
    submitted: datetime,
    submitted_date: date,
    exclude_claim: str | None = None,
) -> Plan:
    """Raises if the claim is not allowed; otherwise says which reviews it needs, in order."""
    values = cfg.load_all(conn)
    month = month_start(expense_date)
    if amount > MAX_AMOUNT:
        raise ApiError(422, "CLAIM_AMOUNT_INVALID", "That amount is too large for one claim.")
    total = Decimal(0)
    if kind == "TRAVEL":
        total = travel_total(conn, employee_id, month, exclude_claim) + amount
        limit = Decimal(str(values["claims.travel_monthly_limit"]))
        if total > limit:
            used = total - amount
            raise conflict(
                "CLAIM_LIMIT_EXCEEDED",
                f"The monthly travel limit is ₹{limit:,.0f}. You have claimed ₹{used:,.2f} for "
                f"{month:%B %Y}, so this claim cannot be added.",
            )
    else:
        limit_value = values["claims.meals_monthly_limit"]
        if limit_value is not None:
            limit = Decimal(str(limit_value))
            used = meals_total(conn, employee_id, month, exclude_claim)
            if used + amount > limit:
                raise conflict(
                    "CLAIM_LIMIT_EXCEEDED",
                    f"The monthly meals limit is ₹{limit:,.0f}. You have claimed ₹{used:,.2f} for "
                    f"{month:%B %Y}, so this claim cannot be added.",
                )

    stale = is_stale(expense_date, submitted_date, int(values["claims.stale_after_months"]))
    stages: list[str] = []
    user = wc.employee_user_id(conn, employee_id)
    ceo = bool(user and is_ceo(authorizer, user))
    roles = wc.project_roles(conn, employee_id, submitted)
    if (
        not ceo
        and "PC" in roles
        and not roles & {"TL", "PM"}
        and wc.project_approvers(conn, employee_id, submitted, ("TL", "PM"))
    ):
        stages.append("TL_PM")
    over_threshold = kind == "TRAVEL" and total > Decimal(str(values["claims.finance_threshold"]))
    stages.append("FINANCE" if (over_threshold or ceo) else "HR")
    if stale and stages[-1] != "FINANCE":
        stages.append("FINANCE")  # the exception approval, in addition to the usual review
    return Plan(stages=stages, stale=stale, travel_total=total)
