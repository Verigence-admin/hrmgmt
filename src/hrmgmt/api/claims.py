from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, text

from hrmgmt import claim_rules as cr
from hrmgmt import permissions as perm
from hrmgmt import settings_store as cfg
from hrmgmt import workcontext as wc
from hrmgmt.api.attendance import get_clock, get_storage
from hrmgmt.api.employees import _own_employee_id, _uuid
from hrmgmt.approvals import decides_claim_stage, is_ceo
from hrmgmt.audit import record_audit
from hrmgmt.authz import Authorizer
from hrmgmt.db import get_conn
from hrmgmt.errors import ApiError, conflict, dependency_unavailable, not_found
from hrmgmt.principal import current_user, get_authorizer, has_permission
from hrmgmt.receipts import (
    MAX_RECEIPT_BYTES,
    MAX_RECEIPTS_PER_CLAIM,
    ReceiptError,
    normalise_receipt,
)
from hrmgmt.security import HumanPrincipal
from hrmgmt.storage import ObjectStorage, StorageError
from hrmgmt.timeutil import Clock, ist_date

router = APIRouter(prefix="/hr/v1", tags=["Reimbursement"])

_EDITABLE = ("CORRECTION_REQUESTED",)
_CANCELLABLE = ("SUBMITTED", "CORRECTION_REQUESTED")
STAGE_LABELS = {"TL_PM": "Team Lead or Project Manager", "HR": "HR", "FINANCE": "Finance"}


def _money(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def _event(
    conn: Connection,
    claim_id: str,
    actor: str | None,
    kind: str,
    stage: str | None = None,
    note: str | None = None,
) -> None:
    conn.execute(
        text(
            "INSERT INTO hr.claim_event (claim_id, actor, event_type, stage, note) VALUES (CAST(:c AS uuid), :a, :t, :s, :n)"
        ),
        {"c": claim_id, "a": actor, "t": kind, "s": stage, "n": note},
    )


def _view(r: Any, labels: dict[str, str] | None = None) -> dict[str, Any]:
    stage = (
        r["stage_plan"][r["stage_index"]]
        if r["status"] == "SUBMITTED" and r["stage_index"] < len(r["stage_plan"])
        else None
    )
    return {
        "claimId": str(r["claim_id"]),
        "category": r["category_code"],
        "categoryLabel": (labels or {}).get(r["category_code"], r["category_code"]),
        "expenseDate": r["expense_date"].isoformat(),
        "amount": _money(r["amount"]),
        "distanceKm": float(r["distance_km"]) if r["distance_km"] is not None else None,
        "description": r["description"],
        "status": r["status"],
        "waitingFor": STAGE_LABELS.get(stage) if stage else None,
        "stage": stage,
        "stagePlan": list(r["stage_plan"]),
        "stale": r["stale"],
        "payrollMonth": r["payroll_month"].isoformat()[:7],
        "payrollRunId": str(r["payroll_run_id"]) if r["payroll_run_id"] else None,
        "submittedAt": r["submitted_at"].isoformat(),
    }


def _labels(conn: Connection) -> dict[str, str]:
    return {
        r[0]: r[1] for r in conn.execute(text("SELECT category_code, label FROM hr.claim_category"))
    }


def _category(conn: Connection, code: str) -> Any:
    row = (
        conn.execute(
            text("SELECT * FROM hr.claim_category WHERE category_code = :c AND active"), {"c": code}
        )
        .mappings()
        .first()
    )
    if row is None:
        raise ApiError(422, "CLAIM_CATEGORY_UNKNOWN", "Choose one of the listed categories.")
    return row


def _amount_for(
    conn: Connection, category: Any, amount: str | None, distance_km: str | None
) -> tuple[Decimal, Decimal | None]:
    try:
        if category["per_km"]:
            rate = Decimal(str(cfg.load_all(conn)["claims.personal_bike_rate_per_km"]))
            if rate <= 0:
                raise ApiError(
                    422,
                    "CLAIM_RATE_NOT_SET",
                    "HR has not set the per-km rate yet, so this category cannot be used.",
                )
            km = Decimal(distance_km or "")
            if km <= 0 or km > Decimal("2000"):
                raise ApiError(422, "CLAIM_AMOUNT_INVALID", "Enter the distance in km.")
            return (km * rate).quantize(Decimal("0.01")), km.quantize(Decimal("0.1"))
        value = Decimal(amount or "")
    except InvalidOperation as exc:
        raise ApiError(422, "CLAIM_AMOUNT_INVALID", "Enter a valid amount.") from exc
    if value <= 0 or value.as_tuple().exponent < -2:
        raise ApiError(
            422, "CLAIM_AMOUNT_INVALID", "Enter an amount in rupees, with at most two decimals."
        )
    return value, None


async def _read_receipts(files: list[UploadFile]) -> list[tuple[bytes, str, str, str | None]]:
    out = []
    if len(files) > MAX_RECEIPTS_PER_CLAIM:
        raise ApiError(
            422, "CLAIM_RECEIPT_INVALID", f"Attach at most {MAX_RECEIPTS_PER_CLAIM} receipts."
        )
    for f in files:
        if not f.filename and not f.size:
            continue
        data = await f.read(MAX_RECEIPT_BYTES + 1)
        try:
            stored, ctype, ext = normalise_receipt(data)
        except ReceiptError as exc:
            raise ApiError(422, "CLAIM_RECEIPT_INVALID", str(exc)) from exc
        out.append((stored, ctype, ext, f.filename))
    return out


def _store_receipts(
    conn: Connection,
    storage: ObjectStorage | None,
    employee_id: str,
    claim_id: str,
    receipts: list[tuple[bytes, str, str, str | None]],
) -> None:
    if not receipts:
        return
    if storage is None:
        raise dependency_unavailable("Receipt storage is not configured.")
    for data, ctype, ext, name in receipts:
        key = f"claims/{employee_id}/{claim_id}/{uuid.uuid4().hex}.{ext}"
        try:
            storage.put(key, data, ctype)
        except StorageError as exc:
            raise dependency_unavailable("A receipt could not be saved. Please try again.") from exc
        conn.execute(
            text(
                "INSERT INTO hr.claim_receipt (claim_id, file_key, content_type, original_name, size_bytes) VALUES (CAST(:c AS uuid), :k, :t, :n, :s)"
            ),
            {"c": claim_id, "k": key, "t": ctype, "n": (name or "")[:120] or None, "s": len(data)},
        )


def _active_receipt_count(conn: Connection, claim_id: str) -> int:
    return int(
        conn.execute(
            text(
                "SELECT count(*) FROM hr.claim_receipt WHERE claim_id = CAST(:c AS uuid) AND removed_at IS NULL"
            ),
            {"c": claim_id},
        ).scalar_one()
    )


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ApiError(422, "CLAIM_DATE_INVALID", "Enter the expense date.") from exc


def _check_expense_date(expense: date, today: date) -> None:
    if expense > today:
        raise ApiError(422, "CLAIM_DATE_INVALID", "The expense date cannot be in the future.")


# ---- categories and summary ------------------------------------------------------------------


@router.get("/claims/categories")
def categories(
    _: HumanPrincipal = Depends(current_user), conn: Connection = Depends(get_conn)
) -> dict[str, Any]:
    values = cfg.load_all(conn)
    rows = conn.execute(
        text("SELECT * FROM hr.claim_category WHERE active ORDER BY sort_order")
    ).mappings()
    return {
        "items": [
            {
                "code": r["category_code"],
                "label": r["label"],
                "kind": r["kind"],
                "receiptRequired": r["receipt_required"],
                "perKm": r["per_km"],
                "ratePerKm": float(values["claims.personal_bike_rate_per_km"])
                if r["per_km"]
                else None,
                "taxable": r["taxable"],
            }
            for r in rows
        ]
    }


class CategoryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    taxable: bool | None = None
    receipt_required: bool | None = None
    active: bool | None = None


@router.patch("/claims/categories/{code}")
def update_category(
    code: str,
    body: CategoryUpdate,
    request: Request,
    user: HumanPrincipal = Depends(current_user),
    authorizer: Authorizer = Depends(get_authorizer),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    from hrmgmt.errors import forbidden

    if not has_permission(authorizer, user, perm.HR_SETTINGS_MANAGE):
        raise forbidden()
    sent = body.model_dump(exclude_unset=True)
    if not sent:
        raise ApiError(422, "HR_VALIDATION_FAILED", "Nothing to change.")
    cat = (
        conn.execute(text("SELECT * FROM hr.claim_category WHERE category_code = :c"), {"c": code})
        .mappings()
        .first()
    )
    if cat is None:
        raise not_found("Category not found.")
    sets = ", ".join(f"{k} = :{k}" for k in sent)
    conn.execute(
        text(f"UPDATE hr.claim_category SET {sets} WHERE category_code = :code"),
        {**sent, "code": code},
    )
    record_audit(
        conn,
        actor_user_id=user.user_id,
        action="CLAIM_CATEGORY_UPDATED",
        entity_type="claim_category",
        entity_id=code,
        changes=sent,
        request=request,
    )
    return {"code": code, **sent}


@router.get("/claims/summary")
def summary(
    month: Annotated[str | None, Query(max_length=7)] = None,
    user: HumanPrincipal = Depends(current_user),
    clock: Clock = Depends(get_clock),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    employee_id = _own_employee_id(conn, user)
    today = ist_date(clock())
    if month:
        try:
            year, mon = (int(x) for x in month.split("-"))
            first = date(year, mon, 1)
        except ValueError as exc:
            raise ApiError(422, "HR_VALIDATION_FAILED", "Month must look like 2026-10.") from exc
    else:
        first = cr.month_start(today)
    values = cfg.load_all(conn)
    used = cr.travel_total(conn, employee_id, first)
    meals = cr.meals_total(conn, employee_id, first)
    limit = Decimal(str(values["claims.travel_monthly_limit"]))
    meals_limit = values["claims.meals_monthly_limit"]
    cutoff = int(values["claims.cutoff_day"])
    return {
        "month": first.strftime("%Y-%m"),
        "travelUsed": float(used),
        "travelLimit": float(limit),
        "travelRemaining": float(max(limit - used, 0)),
        "financeThreshold": float(values["claims.finance_threshold"]),
        "mealsUsed": float(meals),
        "mealsLimit": float(meals_limit) if meals_limit is not None else None,
        "cutoffDay": cutoff,
        "staleAfterMonths": int(values["claims.stale_after_months"]),
        "nextPayrollMonth": cr.payroll_month_for(today, cutoff).isoformat()[:7],
        "rules": [
            f"Claims submitted by the {cutoff}th go into that month's payroll; later ones go into the next month.",
            f"Travel is limited to ₹{limit:,.0f} a month. A month's travel above ₹{float(values['claims.finance_threshold']):,.0f} is approved by Finance.",
            f"Claims older than {int(values['claims.stale_after_months'])} months also need a Finance exception approval.",
        ],
    }


# ---- submit, edit, cancel --------------------------------------------------------------------


@router.post("/claims", status_code=201)
async def submit_claim(
    request: Request,
    category: str = Form(..., max_length=40),
    expense_date: str = Form(..., max_length=10),
    amount: str | None = Form(None, max_length=14),
    distance_km: str | None = Form(None, max_length=10),
    description: str | None = Form(None, max_length=500),
    receipts: list[UploadFile] = File(default_factory=list),
    user: HumanPrincipal = Depends(current_user),
    authorizer: Authorizer = Depends(get_authorizer),
    clock: Clock = Depends(get_clock),
    storage: ObjectStorage | None = Depends(get_storage),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    employee_id = _own_employee_id(conn, user)
    now = clock()
    today = ist_date(now)
    cat = _category(conn, category)
    expense = _parse_date(expense_date)
    _check_expense_date(expense, today)
    value, km = _amount_for(conn, cat, amount, distance_km)
    files = await _read_receipts(receipts)
    if cat["receipt_required"] and not files:
        raise ApiError(422, "CLAIM_RECEIPT_REQUIRED", "Attach the receipt for this claim.")
    conn.execute(
        text("SELECT employee_id FROM hr.employee WHERE employee_id = CAST(:e AS uuid) FOR UPDATE"),
        {"e": employee_id},
    )
    plan = cr.check_limits_and_plan(
        conn,
        authorizer,
        employee_id=employee_id,
        kind=cat["kind"],
        amount=value,
        expense_date=expense,
        submitted=now,
        submitted_date=today,
    )
    cutoff = int(cfg.get(conn, "claims.cutoff_day"))
    claim_id = str(
        conn.execute(
            text(
                "INSERT INTO hr.claim (employee_id, category_code, expense_date, amount, distance_km, description,"
                " stage_plan, stale, payroll_month, submitted_at)"
                " VALUES (CAST(:e AS uuid), :c, :d, :a, :km, :desc, :plan, :stale, :pm, :n) RETURNING claim_id"
            ),
            {
                "e": employee_id,
                "c": category,
                "d": expense,
                "a": value,
                "km": km,
                "desc": (description or "").strip() or None,
                "plan": plan.stages,
                "stale": plan.stale,
                "pm": cr.payroll_month_for(today, cutoff),
                "n": now,
            },
        ).scalar_one()
    )
    _store_receipts(conn, storage, employee_id, claim_id, files)
    _event(conn, claim_id, user.user_id, "SUBMITTED", plan.stages[0])
    record_audit(
        conn,
        actor_user_id=user.user_id,
        action="CLAIM_SUBMITTED",
        entity_type="employee",
        entity_id=employee_id,
        changes={
            "claimId": claim_id,
            "category": category,
            "amount": float(value),
            "plan": plan.stages,
        },
        request=request,
    )
    return _detail(conn, claim_id)


@router.post("/claims/{claim_id}/resubmit")
async def resubmit_claim(
    claim_id: str,
    request: Request,
    category: str = Form(..., max_length=40),
    expense_date: str = Form(..., max_length=10),
    amount: str | None = Form(None, max_length=14),
    distance_km: str | None = Form(None, max_length=10),
    description: str | None = Form(None, max_length=500),
    remove_receipts: str | None = Form(None, max_length=400),
    receipts: list[UploadFile] = File(default_factory=list),
    user: HumanPrincipal = Depends(current_user),
    authorizer: Authorizer = Depends(get_authorizer),
    clock: Clock = Depends(get_clock),
    storage: ObjectStorage | None = Depends(get_storage),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    employee_id = _own_employee_id(conn, user)
    cid = _uuid(claim_id)
    row = (
        conn.execute(
            text(
                "SELECT * FROM hr.claim WHERE claim_id = CAST(:c AS uuid) AND employee_id = CAST(:e AS uuid) FOR UPDATE"
            ),
            {"c": cid, "e": employee_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise not_found("Claim not found.")
    if row["status"] not in _EDITABLE:
        raise conflict(
            "CLAIM_NOT_EDITABLE", "Only a claim sent back for correction can be changed."
        )
    now = clock()
    today = ist_date(now)
    cat = _category(conn, category)
    expense = _parse_date(expense_date)
    _check_expense_date(expense, today)
    value, km = _amount_for(conn, cat, amount, distance_km)
    if remove_receipts:
        ids = [_uuid(x) for x in remove_receipts.split(",") if x.strip()]
        conn.execute(
            text(
                "UPDATE hr.claim_receipt SET removed_at = now() WHERE claim_id = CAST(:c AS uuid) AND receipt_id = ANY(CAST(:ids AS uuid[]))"
            ),
            {"c": cid, "ids": ids},
        )
    files = await _read_receipts(receipts)
    if _active_receipt_count(conn, cid) + len(files) > MAX_RECEIPTS_PER_CLAIM:
        raise ApiError(
            422,
            "CLAIM_RECEIPT_INVALID",
            f"A claim can have at most {MAX_RECEIPTS_PER_CLAIM} receipts.",
        )
    if cat["receipt_required"] and _active_receipt_count(conn, cid) + len(files) == 0:
        raise ApiError(422, "CLAIM_RECEIPT_REQUIRED", "Attach the receipt for this claim.")
    plan = cr.check_limits_and_plan(
        conn,
        authorizer,
        employee_id=employee_id,
        kind=cat["kind"],
        amount=value,
        expense_date=expense,
        submitted=now,
        submitted_date=today,
        exclude_claim=cid,
    )
    cutoff = int(cfg.get(conn, "claims.cutoff_day"))
    conn.execute(
        text(
            "UPDATE hr.claim SET category_code = :c, expense_date = :d, amount = :a, distance_km = :km, description = :desc,"
            " status = 'SUBMITTED', stage_plan = :plan, stage_index = 0, stale = :stale, payroll_month = :pm,"
            " submitted_at = :n, updated_at = :n WHERE claim_id = CAST(:id AS uuid)"
        ),
        {
            "c": category,
            "d": expense,
            "a": value,
            "km": km,
            "desc": (description or "").strip() or None,
            "plan": plan.stages,
            "stale": plan.stale,
            "pm": cr.payroll_month_for(today, cutoff),
            "n": now,
            "id": cid,
        },
    )
    _store_receipts(conn, storage, employee_id, cid, files)
    _event(conn, cid, user.user_id, "RESUBMITTED", plan.stages[0])
    record_audit(
        conn,
        actor_user_id=user.user_id,
        action="CLAIM_RESUBMITTED",
        entity_type="employee",
        entity_id=employee_id,
        changes={"claimId": cid, "amount": float(value)},
        request=request,
    )
    return _detail(conn, cid)


@router.post("/claims/{claim_id}/cancel")
def cancel_claim(
    claim_id: str,
    request: Request,
    user: HumanPrincipal = Depends(current_user),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    employee_id = _own_employee_id(conn, user)
    cid = _uuid(claim_id)
    row = conn.execute(
        text(
            "SELECT status FROM hr.claim WHERE claim_id = CAST(:c AS uuid) AND employee_id = CAST(:e AS uuid) FOR UPDATE"
        ),
        {"c": cid, "e": employee_id},
    ).first()
    if row is None:
        raise not_found("Claim not found.")
    if row[0] not in _CANCELLABLE:
        raise conflict("CLAIM_NOT_CANCELLABLE", "This claim can no longer be cancelled.")
    conn.execute(
        text(
            "UPDATE hr.claim SET status = 'CANCELLED', updated_at = now() WHERE claim_id = CAST(:c AS uuid)"
        ),
        {"c": cid},
    )
    _event(conn, cid, user.user_id, "CANCELLED")
    record_audit(
        conn,
        actor_user_id=user.user_id,
        action="CLAIM_CANCELLED",
        entity_type="employee",
        entity_id=employee_id,
        changes={"claimId": cid},
        request=request,
    )
    return {"claimId": cid, "status": "CANCELLED"}


# ---- reading -------------------------------------------------------------------------------


def _detail(conn: Connection, claim_id: str) -> dict[str, Any]:
    row = (
        conn.execute(
            text("SELECT * FROM hr.claim WHERE claim_id = CAST(:c AS uuid)"), {"c": claim_id}
        )
        .mappings()
        .one()
    )
    out = _view(row, _labels(conn))
    out["receipts"] = [
        {
            "receiptId": str(r["receipt_id"]),
            "name": r["original_name"],
            "contentType": r["content_type"],
            "sizeBytes": r["size_bytes"],
        }
        for r in conn.execute(
            text(
                "SELECT * FROM hr.claim_receipt WHERE claim_id = CAST(:c AS uuid) AND removed_at IS NULL ORDER BY uploaded_at"
            ),
            {"c": claim_id},
        ).mappings()
    ]
    out["history"] = [
        {
            "at": r["occurred_at"].isoformat(),
            "event": r["event_type"],
            "stage": r["stage"],
            "note": r["note"],
        }
        for r in conn.execute(
            text(
                "SELECT * FROM hr.claim_event WHERE claim_id = CAST(:c AS uuid) ORDER BY event_id"
            ),
            {"c": claim_id},
        ).mappings()
    ]
    return out


@router.get("/claims")
def my_claims(
    user: HumanPrincipal = Depends(current_user), conn: Connection = Depends(get_conn)
) -> dict[str, Any]:
    employee_id = _own_employee_id(conn, user)
    labels = _labels(conn)
    rows = conn.execute(
        text(
            "SELECT * FROM hr.claim WHERE employee_id = CAST(:e AS uuid) ORDER BY expense_date DESC, submitted_at DESC LIMIT 200"
        ),
        {"e": employee_id},
    ).mappings()
    return {"items": [_view(r, labels) for r in rows]}


def _may_see(
    conn: Connection, authorizer: Authorizer, user: HumanPrincipal, row: Any, now: Any
) -> tuple[bool, bool]:
    """(may see, is the owner)."""
    employee_id = str(row["employee_id"])
    if wc.employee_user_id(conn, employee_id) == user.user_id:
        return True, True
    if is_ceo(authorizer, user.user_id):
        return True, False
    if has_permission(authorizer, user, perm.HR_CLAIM_REVIEW) or has_permission(
        authorizer, user, perm.HR_CLAIM_REVIEW_FINANCE
    ):
        return True, False
    if user.user_id in wc.project_approvers(conn, employee_id, now, ("TL", "PM")):
        return True, False
    return False, False


@router.get("/claims/review")
def review_list(
    status: Annotated[str | None, Query(max_length=30)] = None,
    month: Annotated[str | None, Query(max_length=7)] = None,
    user: HumanPrincipal = Depends(current_user),
    authorizer: Authorizer = Depends(get_authorizer),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    """HR and Finance: every claim, to follow it from submission to payment."""
    from hrmgmt.errors import forbidden

    if not (
        is_ceo(authorizer, user.user_id)
        or has_permission(authorizer, user, perm.HR_CLAIM_REVIEW)
        or has_permission(authorizer, user, perm.HR_CLAIM_REVIEW_FINANCE)
    ):
        raise forbidden()
    if status and status not in (
        "SUBMITTED",
        "CORRECTION_REQUESTED",
        "APPROVED",
        "HANDED_TO_PAYROLL",
        "PAID",
        "REJECTED",
        "CANCELLED",
    ):
        raise ApiError(422, "HR_VALIDATION_FAILED", "Unknown status.")
    pm = None
    if month:
        try:
            year, mon = (int(x) for x in month.split("-"))
            pm = date(year, mon, 1)
        except ValueError as exc:
            raise ApiError(422, "HR_VALIDATION_FAILED", "Month must look like 2026-10.") from exc
    labels = _labels(conn)
    rows = conn.execute(
        text(
            "SELECT c.*, e.full_name, e.employee_code FROM hr.claim c JOIN hr.employee e ON e.employee_id = c.employee_id"
            " WHERE (CAST(:s AS text) IS NULL OR c.status = :s) AND (CAST(:m AS date) IS NULL OR c.payroll_month = :m)"
            " ORDER BY c.submitted_at DESC LIMIT 300"
        ),
        {"s": status, "m": pm},
    ).mappings()
    return {
        "items": [
            {
                **_view(r, labels),
                "employeeId": str(r["employee_id"]),
                "employeeName": r["full_name"],
                "employeeCode": r["employee_code"],
            }
            for r in rows
        ]
    }


@router.get("/claims/{claim_id}")
def claim_detail(
    claim_id: str,
    user: HumanPrincipal = Depends(current_user),
    authorizer: Authorizer = Depends(get_authorizer),
    clock: Clock = Depends(get_clock),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    cid = _uuid(claim_id)
    row = (
        conn.execute(
            text(
                "SELECT c.*, e.full_name, e.employee_code FROM hr.claim c JOIN hr.employee e ON e.employee_id = c.employee_id WHERE c.claim_id = CAST(:c AS uuid)"
            ),
            {"c": cid},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise not_found("Claim not found.")
    allowed, owner = _may_see(conn, authorizer, user, row, clock())
    if not allowed:
        raise not_found("Claim not found.")
    out = _detail(conn, cid)
    out["employeeId"] = str(row["employee_id"])
    out["employeeName"] = row["full_name"]
    out["employeeCode"] = row["employee_code"]
    out["isOwner"] = owner
    return out


@router.get("/claims/{claim_id}/receipts/{receipt_id}")
def receipt_file(
    claim_id: str,
    receipt_id: str,
    request: Request,
    user: HumanPrincipal = Depends(current_user),
    authorizer: Authorizer = Depends(get_authorizer),
    clock: Clock = Depends(get_clock),
    storage: ObjectStorage | None = Depends(get_storage),
    conn: Connection = Depends(get_conn),
) -> Response:
    cid, rid = _uuid(claim_id), _uuid(receipt_id)
    row = (
        conn.execute(
            text("SELECT c.employee_id FROM hr.claim c WHERE c.claim_id = CAST(:c AS uuid)"),
            {"c": cid},
        )
        .mappings()
        .first()
    )
    rec = (
        conn.execute(
            text(
                "SELECT * FROM hr.claim_receipt WHERE receipt_id = CAST(:r AS uuid) AND claim_id = CAST(:c AS uuid) AND removed_at IS NULL"
            ),
            {"r": rid, "c": cid},
        )
        .mappings()
        .first()
    )
    if row is None or rec is None:
        raise not_found("Receipt not found.")
    allowed, owner = _may_see(conn, authorizer, user, row, clock())
    if not allowed:
        raise not_found("Receipt not found.")
    if storage is None:
        raise dependency_unavailable("Receipt storage is not configured.")
    if not owner:
        record_audit(
            conn,
            actor_user_id=user.user_id,
            action="CLAIM_RECEIPT_VIEWED",
            entity_type="employee",
            entity_id=str(row["employee_id"]),
            changes={"claimId": cid, "receiptId": rid},
            request=request,
        )
    try:
        data = storage.get(rec["file_key"])
    except StorageError as exc:
        raise dependency_unavailable("The receipt could not be loaded.") from exc
    return Response(
        content=data, media_type=rec["content_type"], headers={"Cache-Control": "private, no-store"}
    )


# ---- approvals -----------------------------------------------------------------------------


@router.get("/approvals/claims")
def claim_approvals(
    user: HumanPrincipal = Depends(current_user),
    authorizer: Authorizer = Depends(get_authorizer),
    clock: Clock = Depends(get_clock),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    """The claims waiting for a review this person may give."""
    now = clock()
    labels = _labels(conn)
    rows = (
        conn.execute(
            text(
                "SELECT c.*, e.full_name, e.employee_code FROM hr.claim c JOIN hr.employee e ON e.employee_id = c.employee_id"
                " WHERE c.status = 'SUBMITTED' ORDER BY c.submitted_at LIMIT 300"
            )
        )
        .mappings()
        .all()
    )
    items = []
    for r in rows:
        stage = r["stage_plan"][r["stage_index"]]
        if decides_claim_stage(conn, authorizer, user, str(r["employee_id"]), stage, now):
            month = cr.month_start(r["expense_date"])
            items.append(
                {
                    **_view(r, labels),
                    "employeeId": str(r["employee_id"]),
                    "employeeName": r["full_name"],
                    "employeeCode": r["employee_code"],
                    "monthTravelTotal": float(cr.travel_total(conn, str(r["employee_id"]), month)),
                }
            )
    return {"items": items}


class ClaimDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    decision: Literal["APPROVE", "REJECT", "CORRECTION"]
    note: str | None = Field(default=None, max_length=500)


@router.post("/approvals/claims/{claim_id}/decision")
def decide_claim(
    claim_id: str,
    body: ClaimDecision,
    request: Request,
    user: HumanPrincipal = Depends(current_user),
    authorizer: Authorizer = Depends(get_authorizer),
    clock: Clock = Depends(get_clock),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    cid = _uuid(claim_id)
    row = (
        conn.execute(
            text("SELECT * FROM hr.claim WHERE claim_id = CAST(:c AS uuid) FOR UPDATE"), {"c": cid}
        )
        .mappings()
        .first()
    )
    if row is None:
        raise not_found("Claim not found.")
    employee_id = str(row["employee_id"])
    now = clock()
    if row["status"] != "SUBMITTED":
        # Tell someone who may see it that it moved on; hide it from everyone else.
        if _may_see(conn, authorizer, user, row, now)[0]:
            raise conflict("APPROVAL_ALREADY_DECIDED", "This claim is not waiting for a review.")
        raise not_found("Claim not found.")
    stage = row["stage_plan"][row["stage_index"]]
    if not decides_claim_stage(conn, authorizer, user, employee_id, stage, now):
        raise not_found("Claim not found.")
    if body.decision in ("REJECT", "CORRECTION") and not (body.note and body.note.strip()):
        raise ApiError(422, "APPROVAL_NOTE_REQUIRED", "Say why, so the employee knows what to do.")
    note = (body.note or "").strip() or None
    if body.decision == "APPROVE":
        last = row["stage_index"] + 1 >= len(row["stage_plan"])
        if last:
            conn.execute(
                text(
                    "UPDATE hr.claim SET status = 'APPROVED', stage_index = stage_index + 1, decided_at = :n, updated_at = :n WHERE claim_id = CAST(:c AS uuid)"
                ),
                {"n": now, "c": cid},
            )
        else:
            conn.execute(
                text(
                    "UPDATE hr.claim SET stage_index = stage_index + 1, updated_at = :n WHERE claim_id = CAST(:c AS uuid)"
                ),
                {"n": now, "c": cid},
            )
        _event(conn, cid, user.user_id, "APPROVED" if last else "STAGE_APPROVED", stage, note)
        status = "APPROVED" if last else "SUBMITTED"
    elif body.decision == "REJECT":
        conn.execute(
            text(
                "UPDATE hr.claim SET status = 'REJECTED', decided_at = :n, updated_at = :n WHERE claim_id = CAST(:c AS uuid)"
            ),
            {"n": now, "c": cid},
        )
        _event(conn, cid, user.user_id, "REJECTED", stage, note)
        status = "REJECTED"
    else:
        conn.execute(
            text(
                "UPDATE hr.claim SET status = 'CORRECTION_REQUESTED', updated_at = :n WHERE claim_id = CAST(:c AS uuid)"
            ),
            {"n": now, "c": cid},
        )
        _event(conn, cid, user.user_id, "CORRECTION_REQUESTED", stage, note)
        status = "CORRECTION_REQUESTED"
    record_audit(
        conn,
        actor_user_id=user.user_id,
        action="CLAIM_" + body.decision,
        entity_type="employee",
        entity_id=employee_id,
        changes={"claimId": cid, "stage": stage, "amount": float(row["amount"])},
        request=request,
    )
    return {"claimId": cid, "status": status}
