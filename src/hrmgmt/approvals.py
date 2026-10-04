"""Who may decide on whose request. Decided on the server, never by the screen.

A Team Lead or Project Manager decides for people on their own project (that comes from the copy
of Audit Core's assignments). When a person has no Team Lead or Project Manager, HR decides. The
CEO may decide anything. Nobody ever decides their own request, the CEO included."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Connection

from hrmgmt import permissions as perm
from hrmgmt import workcontext as wc
from hrmgmt.authz import Authorizer
from hrmgmt.principal import has_permission
from hrmgmt.security import HumanPrincipal


def is_ceo(authorizer: Authorizer, user_id: str) -> bool:
    """The CEO is whoever holds the permission only the CEO has: approving a payroll run."""
    try:
        return authorizer.is_allowed(user_id=user_id, permission_key=perm.HR_PAYROLL_APPROVE)
    except Exception:
        return False


def decides_attendance(
    conn: Connection,
    authorizer: Authorizer,
    actor: HumanPrincipal,
    employee_id: str,
    at: datetime,
) -> bool:
    employee_user = wc.employee_user_id(conn, employee_id)
    if employee_user is not None and employee_user == actor.user_id:
        return False
    approvers = wc.project_approvers(conn, employee_id, at, ("TL", "PM"))
    if actor.user_id in approvers:
        return True
    if is_ceo(authorizer, actor.user_id):
        return True
    if not approvers:
        return has_permission(authorizer, actor, perm.HR_ATTENDANCE_READ_ALL)
    return False


# ---- leave -----------------------------------------------------------------------------------


def leave_rule(
    conn: Connection,
    authorizer: Authorizer,
    employee_id: str,
    leave_type: str,
    at: datetime,
) -> str:
    """Who approves this person's leave: AUTO (the CEO's own), CEO, PM, TL_PM or HR.

    PC: any Team Lead or Project Manager of their project. Team Lead: a Project Manager.
    Project Manager, HR and Finance staff: the CEO. Unpaid leave, and people with no project
    role: HR. If the people the rule names do not exist, HR decides."""
    user = wc.employee_user_id(conn, employee_id)
    if user is not None and is_ceo(authorizer, user):
        return "AUTO"
    if leave_type == "UNPAID":
        return "HR"
    if user is not None and (
        _holds(authorizer, user, perm.HR_LEAVE_REVIEW)
        or _holds(authorizer, user, perm.HR_CLAIM_REVIEW_FINANCE)
    ):
        return "CEO"
    roles = wc.project_roles(conn, employee_id, at)
    if "PM" in roles:
        return "CEO"
    if "TL" in roles:
        return "PM" if wc.project_approvers(conn, employee_id, at, ("PM",)) else "HR"
    if "PC" in roles:
        return "TL_PM" if wc.project_approvers(conn, employee_id, at, ("TL", "PM")) else "HR"
    return "HR"


def _holds(authorizer: Authorizer, user_id: str, key: str) -> bool:
    try:
        return authorizer.is_allowed(user_id=user_id, permission_key=key)
    except Exception:
        return False


def decides_leave(
    conn: Connection,
    authorizer: Authorizer,
    actor: HumanPrincipal,
    employee_id: str,
    rule: str,
    at: datetime,
) -> bool:
    employee_user = wc.employee_user_id(conn, employee_id)
    if employee_user is not None and employee_user == actor.user_id:
        return False
    if rule == "AUTO":
        return False
    if is_ceo(authorizer, actor.user_id):
        return True
    if rule == "CEO":
        return False
    if rule == "HR":
        return has_permission(authorizer, actor, perm.HR_LEAVE_REVIEW)
    roles = ("PM",) if rule == "PM" else ("TL", "PM")
    return actor.user_id in wc.project_approvers(conn, employee_id, at, roles)
