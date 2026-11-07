from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, text

from hrmgmt import permissions as perm
from hrmgmt import settings_store as cfg
from hrmgmt import workcontext as wc
from hrmgmt.audit import record_audit
from hrmgmt.db import get_conn, get_engine
from hrmgmt.errors import ApiError, conflict, dependency_unavailable
from hrmgmt.principal import require_permission
from hrmgmt.security import HumanPrincipal

router = APIRouter(prefix="/hr/v1", tags=["Settings"])

can_manage_settings = require_permission(perm.HR_SETTINGS_MANAGE)


# ---- settings ------------------------------------------------------------------------------


@router.get("/settings")
def list_settings(
    _: HumanPrincipal = Depends(can_manage_settings), conn: Connection = Depends(get_conn)
) -> dict[str, Any]:
    values = cfg.load_all(conn)
    return {
        "items": [
            {
                "key": key,
                "group": spec.group,
                "label": spec.label,
                "kind": spec.kind,
                "value": values[key],
                "default": spec.default,
            }
            for key, spec in cfg.SPECS.items()
        ]
    }


class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    values: dict[str, Any] = Field(min_length=1, max_length=40)


@router.put("/settings")
def update_settings(
    body: SettingsUpdate,
    request: Request,
    user: HumanPrincipal = Depends(can_manage_settings),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    before = cfg.load_all(conn)
    changes: dict[str, Any] = {}
    cleaned = {key: cfg.validate(key, value) for key, value in body.values.items()}
    # The late time must come after the standard time, and early check-out before the standard.
    merged = {**before, **cleaned}
    if merged["attendance.late_after"] < merged["attendance.check_in_standard"]:
        raise ApiError(
            422, "HR_SETTING_INVALID", "Late check-in must be after the standard check-in time."
        )
    if merged["attendance.check_out_earliest"] > merged["attendance.check_out_standard"]:
        raise ApiError(
            422,
            "HR_SETTING_INVALID",
            "Earliest check-out must not be after the standard check-out.",
        )
    if not (
        merged["attendance.late_after"]
        < merged["attendance.half_day_split"]
        < merged["attendance.check_out_earliest"]
    ):
        raise ApiError(
            422,
            "HR_SETTING_INVALID",
            "The half-day split must be after the late check-in time and before the earliest"
            " check-out.",
        )
    if merged["claims.finance_threshold"] > merged["claims.travel_monthly_limit"]:
        raise ApiError(
            422,
            "HR_SETTING_INVALID",
            "The Finance threshold cannot exceed the monthly travel limit.",
        )
    for key, value in cleaned.items():
        if before[key] == value:
            continue
        conn.execute(
            text(
                "INSERT INTO hr.setting (key, value, updated_by) VALUES (:k, CAST(:v AS jsonb), :u)"
                " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now(),"
                " updated_by = EXCLUDED.updated_by"
            ),
            {"k": key, "v": json.dumps(value), "u": user.user_id},
        )
        changes[key] = {"from": before[key], "to": value}
    if changes:
        record_audit(
            conn,
            actor_user_id=user.user_id,
            action="SETTINGS_UPDATED",
            entity_type="settings",
            entity_id="company",
            changes=changes,
            request=request,
        )
    return list_settings(user, conn)


# ---- work context (copy of Audit Core assignments) -----------------------------------------


@router.get("/work-context/status")
def work_context_status(
    _: HumanPrincipal = Depends(can_manage_settings), conn: Connection = Depends(get_conn)
) -> dict[str, Any]:
    s = wc.sync_status(conn)
    return {
        "lastAttemptAt": s["last_attempt_at"].isoformat() if s.get("last_attempt_at") else None,
        "lastSuccessAt": s["last_success_at"].isoformat() if s.get("last_success_at") else None,
        "lastStatus": s.get("last_status"),
        "lastError": s.get("last_error"),
        "assignmentsSeen": s.get("assignments_seen"),
    }


@router.post("/work-context/refresh")
def refresh_work_context(
    request: Request,
    user: HumanPrincipal = Depends(can_manage_settings),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    """Pull the work context from Audit Core now. One attempt, at least five minutes apart."""
    client = getattr(request.app.state, "workcontext", None)
    if client is None:
        raise dependency_unavailable("The link to Audit Core is not configured.")
    conn.commit()  # no open transaction while another service is called
    result = wc.run_sync(get_engine(), client)
    if result.skipped:
        raise conflict("WORK_CONTEXT_TOO_SOON", result.error or "Tried a moment ago.")
    record_audit(
        conn,
        actor_user_id=user.user_id,
        action="WORK_CONTEXT_REFRESHED" if result.ok else "WORK_CONTEXT_REFRESH_FAILED",
        entity_type="work_context",
        entity_id="audit-core",
        changes={"assignments": result.seen, "error": result.error},
        request=request,
    )
    return {"ok": result.ok, "assignmentsSeen": result.seen, "error": result.error}
