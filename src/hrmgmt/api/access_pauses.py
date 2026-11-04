"""Team Leads paused for approvals past the Monday 09:00 deadline (product owner, 4-5 Nov 2026).
SuperAdmin sees who is paused, for which requests, and restores their access with a note."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, text

from hrmgmt import permissions as perm
from hrmgmt.api.attendance import get_clock
from hrmgmt.api.employees import get_provisioner
from hrmgmt.audit import record_audit
from hrmgmt.db import get_conn
from hrmgmt.errors import ApiError, conflict, dependency_unavailable, not_found
from hrmgmt.principal import require_permission
from hrmgmt.provisioning import ProvisioningError, UserProvisioner
from hrmgmt.security import HumanPrincipal
from hrmgmt.timeutil import Clock

router = APIRouter(prefix="/hr/v1/admin/approver-pauses", tags=["Approver pauses"])

can_manage = require_permission(perm.HR_ACCESS_PAUSE_MANAGE)


def _view(r: Any) -> dict[str, Any]:
    return {
        "pauseId": str(r.pause_id),
        "userId": str(r.security_user_id),
        "name": r.full_name or "Not an employee in myHR",
        "employeeCode": r.employee_code,
        "deadlineAt": r.deadline_at.isoformat(),
        "overdueCount": r.overdue_count,
        "overdueItems": r.overdue_items,
        "pausedAt": r.paused_at.isoformat(),
        "pausedInSecurity": r.security_paused,
        "securityError": r.security_error,
        "restoredAt": r.restored_at.isoformat() if r.restored_at else None,
        "restoredBy": r.restored_by,
        "restoreNote": r.restore_note,
    }


_SELECT = """
    SELECT p.*, e.full_name, e.employee_code FROM hr.approver_pause p
    LEFT JOIN hr.employee e ON e.security_user_id = p.security_user_id
"""


@router.get("")
def list_pauses(
    _: HumanPrincipal = Depends(can_manage), conn: Connection = Depends(get_conn)
) -> dict[str, Any]:
    rows = conn.execute(
        text(
            _SELECT + " WHERE p.restored_at IS NULL OR p.restored_at > now() - interval '60 days'"
            " ORDER BY p.restored_at IS NOT NULL, p.paused_at DESC LIMIT 200"
        )
    )
    items = [_view(r) for r in rows]
    return {
        "items": items,
        "activeCount": sum(1 for i in items if i["restoredAt"] is None),
    }


class RestoreIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str = Field(min_length=3, max_length=500)


@router.post("/{pause_id}/restore")
def restore(
    pause_id: str,
    body: RestoreIn,
    request: Request,
    user: HumanPrincipal = Depends(can_manage),
    clock: Clock = Depends(get_clock),
    provisioner: UserProvisioner | None = Depends(get_provisioner),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    row = conn.execute(
        text(_SELECT + " WHERE p.pause_id = CAST(:p AS uuid) FOR UPDATE OF p"), {"p": pause_id}
    ).first()
    if row is None:
        raise not_found("No such pause.")
    if row.restored_at is not None:
        raise conflict("ALREADY_RESTORED", "This access was already restored.")
    if provisioner is None:
        raise dependency_unavailable("Security is not connected; access cannot be restored now.")
    note = body.note.strip()
    try:
        provisioner.restore_access(
            user_id=str(row.security_user_id), restored_by=user.user_id, note=note
        )
    except ProvisioningError as exc:
        raise ApiError(
            502, "SECURITY_REFUSED", f"Security did not restore the access ({exc.code}): {exc}"
        ) from exc
    # Every pause of this person still open is closed: their access is back as a whole.
    conn.execute(
        text(
            "UPDATE hr.approver_pause SET restored_at = :now, restored_by = :by, restore_note = :n"
            " WHERE security_user_id = :u AND restored_at IS NULL"
        ),
        {"now": clock(), "by": user.user_id, "n": note, "u": row.security_user_id},
    )
    record_audit(
        conn,
        actor_user_id=user.user_id,
        action="APPROVER_ACCESS_RESTORED",
        entity_type="approver_pause",
        entity_id=pause_id,
        changes={"userId": str(row.security_user_id), "note": note},
        request=request,
    )
    after = conn.execute(text(_SELECT + " WHERE p.pause_id = CAST(:p AS uuid)"), {"p": pause_id})
    return _view(after.first())
