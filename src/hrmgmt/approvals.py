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
