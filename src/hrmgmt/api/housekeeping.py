"""Housekeeping: SuperAdmin clears old attendance, leave or reimbursement records for a day or a
month, so the data stays manageable. It first shows what would go (a preview that changes
nothing); the delete needs the word DELETE typed. Records that a submitted, approved or paid
payroll already used are never touched, and every purge is written to the history with its
counts (never the people)."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

import structlog
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, text

from hrmgmt import permissions as perm
from hrmgmt.api.attendance import get_clock, get_storage
from hrmgmt.audit import record_audit
from hrmgmt.db import get_conn
from hrmgmt.errors import ApiError
from hrmgmt.principal import require_permission
from hrmgmt.security import HumanPrincipal
from hrmgmt.storage import ObjectStorage, StorageError
from hrmgmt.timeutil import Clock, ist_date

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/hr/v1/housekeeping", tags=["HR housekeeping"])

can_clean = require_permission(perm.HR_HOUSEKEEPING_MANAGE)

MAX_DAYS = 31
# A payroll in one of these states has already used the records of its month.
_LOCKING_RUNS = "('SUBMITTED', 'APPROVED', 'PAID')"
# A reimbursement in one of these states is already in a payroll.
_LOCKED_CLAIMS = "('HANDED_TO_PAYROLL', 'PAID')"

Kind = Literal["ATTENDANCE", "LEAVE", "CLAIMS"]


class Scope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Kind
    from_date: date
    to_date: date


class PurgeRequest(Scope):
    confirm: str = Field(max_length=20)


def _check(scope: Scope, today: date) -> None:
    if scope.to_date < scope.from_date:
        raise ApiError(422, "HR_VALIDATION_FAILED", "The end date is before the start date.")
    if scope.to_date > today:
        raise ApiError(422, "HR_VALIDATION_FAILED", "Choose days that have already passed.")
    if (scope.to_date - scope.from_date).days >= MAX_DAYS:
        raise ApiError(422, "HR_VALIDATION_FAILED", f"Choose at most {MAX_DAYS} days at a time.")


def _one(conn: Connection, sql: str, params: dict[str, Any]) -> int:
    return int(conn.execute(text(sql), params).scalar_one())


def _payroll_blocks(conn: Connection, scope: Scope) -> str | None:
    """The first payroll month that already used these records, if any."""
    if scope.kind == "CLAIMS":
        return None  # claims already in a payroll are kept one by one instead
    if scope.kind == "ATTENDANCE":
        sql = (
            "SELECT to_char(pay_month, 'Mon YYYY') FROM hr.payroll_run"
            f" WHERE status IN {_LOCKING_RUNS}"
            " AND pay_month BETWEEN date_trunc('month', CAST(:a AS date))"
            " AND date_trunc('month', CAST(:b AS date)) ORDER BY pay_month LIMIT 1"
        )
    else:
        sql = (
            "SELECT to_char(r.pay_month, 'Mon YYYY') FROM hr.payroll_run r"
            f" WHERE r.status IN {_LOCKING_RUNS} AND EXISTS ("
            " SELECT 1 FROM hr.leave_request l WHERE l.from_date BETWEEN :a AND :b"
            " AND r.pay_month BETWEEN date_trunc('month', l.from_date) AND date_trunc('month', l.to_date)"
            ") ORDER BY r.pay_month LIMIT 1"
        )
    row = conn.execute(text(sql), {"a": scope.from_date, "b": scope.to_date}).first()
    return str(row[0]) if row else None


def _counts(conn: Connection, scope: Scope) -> dict[str, int]:
    p = {"a": scope.from_date, "b": scope.to_date}
    if scope.kind == "ATTENDANCE":
        return {
            "days": _one(
                conn, "SELECT count(*) FROM hr.attendance_day WHERE work_date BETWEEN :a AND :b", p
            ),
            "approvals": _one(
                conn,
                "SELECT count(*) FROM hr.attendance_exception WHERE work_date BETWEEN :a AND :b",
                p,
            ),
            "photos": _one(
                conn,
                "SELECT coalesce(sum((check_in_photo_key IS NOT NULL)::int"
                " + (check_out_photo_key IS NOT NULL)::int), 0) FROM hr.attendance_day"
                " WHERE work_date BETWEEN :a AND :b",
                p,
            ),
        }
    if scope.kind == "LEAVE":
        return {
            "requests": _one(
                conn, "SELECT count(*) FROM hr.leave_request WHERE from_date BETWEEN :a AND :b", p
            ),
            "ledgerRows": _one(
                conn,
                "SELECT count(*) FROM hr.leave_ledger WHERE request_id IN"
                " (SELECT request_id FROM hr.leave_request WHERE from_date BETWEEN :a AND :b)",
                p,
            ),
        }
    pick = f"expense_date BETWEEN :a AND :b AND status NOT IN {_LOCKED_CLAIMS}"
    return {
        "claims": _one(conn, f"SELECT count(*) FROM hr.claim WHERE {pick}", p),
        "receipts": _one(
            conn,
            f"SELECT count(*) FROM hr.claim_receipt WHERE claim_id IN (SELECT claim_id FROM hr.claim WHERE {pick})",
            p,
        ),
        "keptInPayroll": _one(
            conn,
            f"SELECT count(*) FROM hr.claim WHERE expense_date BETWEEN :a AND :b AND status IN {_LOCKED_CLAIMS}",
            p,
        ),
    }


def _view(scope: Scope, counts: dict[str, int], blocked: str | None) -> dict[str, Any]:
    return {
        "kind": scope.kind,
        "from": scope.from_date.isoformat(),
        "to": scope.to_date.isoformat(),
        "counts": counts,
        "blockedBy": (
            f"The payroll for {blocked} has already used these records, so they cannot be deleted."
            if blocked
            else None
        ),
    }


@router.post("/preview")
def preview(
    scope: Scope,
    _: HumanPrincipal = Depends(can_clean),
    clock: Clock = Depends(get_clock),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    """What a purge of this kind and period would remove. Changes nothing."""
    _check(scope, ist_date(clock()))
    return _view(scope, _counts(conn, scope), _payroll_blocks(conn, scope))


def _remove_files(storage: ObjectStorage | None, keys: list[str]) -> int:
    """Best effort, after the records are gone. Returns how many files could not be removed."""
    failed = 0
    for key in keys:
        try:
            if storage is None:
                raise StorageError("no storage")
            storage.delete(key)
        except StorageError:
            failed += 1
    return failed


def _purge(conn: Connection, scope: Scope) -> list[str]:
    """Deletes inside the caller's transaction; returns the stored files to remove afterwards."""
    p = {"a": scope.from_date, "b": scope.to_date}
    if scope.kind == "ATTENDANCE":
        rows = conn.execute(
            text(
                "SELECT check_in_photo_key, check_out_photo_key FROM hr.attendance_day"
                " WHERE work_date BETWEEN :a AND :b"
            ),
            p,
        )
        keys = [k for pair in rows for k in pair if k]
        conn.execute(
            text("DELETE FROM hr.attendance_exception WHERE work_date BETWEEN :a AND :b"), p
        )
        conn.execute(text("DELETE FROM hr.attendance_day WHERE work_date BETWEEN :a AND :b"), p)
        return keys
    if scope.kind == "LEAVE":
        mine = "SELECT request_id FROM hr.leave_request WHERE from_date BETWEEN :a AND :b"
        # The ledger only allows this delete when the transaction says it is a purge.
        conn.execute(text("SET LOCAL hr.allow_ledger_purge = 'on'"))
        conn.execute(text(f"DELETE FROM hr.leave_ledger WHERE request_id IN ({mine})"), p)
        conn.execute(text("SET LOCAL hr.allow_ledger_purge = 'off'"))
        conn.execute(text("DELETE FROM hr.leave_request WHERE from_date BETWEEN :a AND :b"), p)
        return []
    mine = f"SELECT claim_id FROM hr.claim WHERE expense_date BETWEEN :a AND :b AND status NOT IN {_LOCKED_CLAIMS}"
    keys = [
        r[0]
        for r in conn.execute(
            text(f"SELECT file_key FROM hr.claim_receipt WHERE claim_id IN ({mine})"), p
        )
    ]
    conn.execute(text(f"DELETE FROM hr.claim_receipt WHERE claim_id IN ({mine})"), p)
    conn.execute(text(f"DELETE FROM hr.claim_event WHERE claim_id IN ({mine})"), p)
    conn.execute(text(f"DELETE FROM hr.claim WHERE claim_id IN ({mine})"), p)
    return keys


@router.post("/purge")
def purge(
    body: PurgeRequest,
    request: Request,
    user: HumanPrincipal = Depends(can_clean),
    clock: Clock = Depends(get_clock),
    storage: ObjectStorage | None = Depends(get_storage),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    """Delete the records. Needs the word DELETE typed, and is refused for a month a payroll used."""
    scope = Scope(kind=body.kind, from_date=body.from_date, to_date=body.to_date)
    _check(scope, ist_date(clock()))
    if body.confirm != "DELETE":
        raise ApiError(422, "HOUSEKEEPING_NOT_CONFIRMED", "Type DELETE to confirm.")
    blocked = _payroll_blocks(conn, scope)
    if blocked:
        raise ApiError(
            409,
            "HOUSEKEEPING_PAYROLL_USED",
            f"The payroll for {blocked} has already used these records, so they cannot be deleted.",
        )
    counts = _counts(conn, scope)
    keys = _purge(conn, scope)
    record_audit(
        conn,
        actor_user_id=user.user_id,
        action="HOUSEKEEPING_PURGE",
        entity_type="housekeeping",
        entity_id=f"{scope.kind}:{scope.from_date.isoformat()}..{scope.to_date.isoformat()}",
        changes={"counts": counts},
        request=request,
    )
    # The records are gone for good before their files are touched.
    conn.commit()
    left = _remove_files(storage, keys)
    if left:
        logger.warning("hr_housekeeping_files_left", kind=scope.kind, files=left)
    return {**_view(scope, counts, None), "filesNotRemoved": left}
