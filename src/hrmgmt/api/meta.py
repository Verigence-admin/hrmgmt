from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Connection, text

from hrmgmt import permissions as perm
from hrmgmt.authz import Authorizer
from hrmgmt.db import get_conn
from hrmgmt.principal import current_user, get_authorizer, has_permission, require_permission
from hrmgmt.security import HumanPrincipal

router = APIRouter(prefix="/hr/v1", tags=["HR"])

can_read_audit = require_permission(perm.HR_AUDIT_READ)


@router.get("/me")
def me(
    user: HumanPrincipal = Depends(current_user),
    authorizer: Authorizer = Depends(get_authorizer),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    """Who the caller is, which HR powers they hold, and whether an employee record is linked to
    their login, so the UI can pick navigation in one call. The server re-checks every protected
    request regardless of this answer."""
    granted = [p for p in perm.ALL_PERMISSIONS if has_permission(authorizer, user, p)]
    return {
        "userId": user.user_id,
        "permissions": granted,
        "employeeId": _own_employee_id_or_none(conn, user.user_id),
    }


def _own_employee_id_or_none(conn: Connection, user_id: str) -> str | None:
    try:
        key = str(uuid.UUID(user_id))
    except ValueError:
        return None
    row = conn.execute(
        text(
            "SELECT employee_id FROM hr.employee"
            " WHERE security_user_id = CAST(:u AS uuid) AND employment_status = 'ACTIVE'"
        ),
        {"u": key},
    ).first()
    return str(row[0]) if row else None


@router.get("/designations")
def designations(
    _: HumanPrincipal = Depends(current_user),
    conn: Connection = Depends(get_conn),
) -> list[dict[str, Any]]:
    rows = conn.execute(
        text("SELECT code, label FROM hr.designation WHERE active ORDER BY sort_order")
    ).mappings()
    return [{"code": r["code"], "label": r["label"]} for r in rows]


@router.get("/audit")
def audit_history(
    entity_type: Annotated[str | None, Query(max_length=60)] = None,
    entity_id: Annotated[str | None, Query(max_length=80)] = None,
    before_id: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    _: HumanPrincipal = Depends(can_read_audit),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    rows = (
        conn.execute(
            text(
                """
            SELECT audit_id, occurred_at, actor_user_id, action, entity_type, entity_id, changes
            FROM hr.audit_log
            WHERE (CAST(:etype AS text) IS NULL OR entity_type = :etype)
              AND (CAST(:eid AS text) IS NULL OR entity_id = :eid)
              AND (CAST(:before AS bigint) IS NULL OR audit_id < :before)
            ORDER BY audit_id DESC
            LIMIT :limit
            """
            ),
            {"etype": entity_type, "eid": entity_id, "before": before_id, "limit": limit},
        )
        .mappings()
        .all()
    )
    items = [
        {
            "auditId": r["audit_id"],
            "occurredAt": r["occurred_at"].isoformat(),
            "actorUserId": r["actor_user_id"],
            "action": r["action"],
            "entityType": r["entity_type"],
            "entityId": r["entity_id"],
            "changes": r["changes"],
        }
        for r in rows
    ]
    return {"items": items, "nextBeforeId": items[-1]["auditId"] if len(items) == limit else None}
