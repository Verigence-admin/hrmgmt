from __future__ import annotations

import json
import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, text

from hrmgmt import payroll_calc as pcalc
from hrmgmt import payroll_data as pdata
from hrmgmt import permissions as perm
from hrmgmt import settings_store as cfg
from hrmgmt import validators as v
from hrmgmt import workcontext as wc
from hrmgmt.api.attendance import get_clock, get_storage
from hrmgmt.api.employees import _own_employee_id, _uuid
from hrmgmt.audit import record_audit
from hrmgmt.authz import Authorizer
from hrmgmt.db import get_conn
from hrmgmt.errors import ApiError, conflict, dependency_unavailable, forbidden, not_found
from hrmgmt.payslip_pdf import render_payslip
from hrmgmt.principal import current_user, get_authorizer, has_permission, require_permission
from hrmgmt.security import HumanPrincipal
from hrmgmt.storage import ObjectStorage, StorageError
from hrmgmt.timeutil import Clock, ist_date

router = APIRouter(prefix="/hr/v1", tags=["Payroll"])

can_read = require_permission(perm.HR_PAYROLL_READ)
can_prepare = require_permission(perm.HR_PAYROLL_PREPARE)
can_approve = require_permission(perm.HR_PAYROLL_APPROVE)
can_propose = require_permission(perm.HR_SALARY_PROPOSE)
can_decide_salary = require_permission(perm.HR_SALARY_APPROVE)
can_settings = require_permission(perm.HR_SETTINGS_MANAGE)

# A salary between these (inclusive) has no template yet: HR must choose one on purpose.
BAND_LOW = Decimal("21001")
BAND_HIGH = Decimal("25000")


def _month(value: str) -> date:
    try:
        year, mon = (int(x) for x in value.split("-"))
        return date(year, mon, 1)
    except ValueError as exc:
        raise ApiError(422, "HR_VALIDATION_FAILED", "Month must look like 2026-10.") from exc


# ---- templates -----------------------------------------------------------------------------


def _template_view(r: Any) -> dict[str, Any]:
    return {
        "templateId": str(r["template_id"]),
        "code": r["code"],
        "name": r["name"],
        "description": r["description"],
        "components": r["components"],
        "active": r["active"],
    }


@router.get("/payroll/templates")
def templates(
    user: HumanPrincipal = Depends(current_user),
    authorizer: Authorizer = Depends(get_authorizer),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    if not any(
        has_permission(authorizer, user, k)
        for k in (perm.HR_SALARY_PROPOSE, perm.HR_SALARY_APPROVE, perm.HR_PAYROLL_READ)
    ):
        raise forbidden()
    rows = conn.execute(text("SELECT * FROM hr.salary_template ORDER BY code")).mappings()
    return {
        "items": [_template_view(r) for r in rows],
        "bandNote": "A gross between ₹21,001 and ₹25,000 has no template yet. HR chooses one on purpose.",
    }


class ComponentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,19}$")
    label: str = Field(min_length=2, max_length=60)
    basis: Literal["PERCENT_GROSS", "PERCENT_BASIC", "FIXED", "REMAINDER"]
    value: Decimal | None = Field(default=None, ge=0, le=10_000_000)
    pf_wage: bool = False
    esi_wage: bool = True


class TemplateIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=3, max_length=100)
    description: str | None = Field(default=None, max_length=300)
    components: list[ComponentIn] = Field(min_length=2, max_length=15)
    active: bool = True


def _check_template(components: list[ComponentIn]) -> list[dict[str, Any]]:
    out = [c.model_dump(mode="json") for c in components]
    try:
        pcalc.resolve_components(out, Decimal("100000"))  # shape check against a sample gross
    except pcalc.PayrollError as exc:
        raise ApiError(422, "TEMPLATE_INVALID", str(exc)) from exc
    return out


@router.put("/payroll/templates/{template_id}")
def update_template(
    template_id: str,
    body: TemplateIn,
    request: Request,
    user: HumanPrincipal = Depends(can_propose),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    tid = _uuid(template_id)
    comps = _check_template(body.components)
    done = conn.execute(
        text(
            "UPDATE hr.salary_template SET name = :n, description = :d, components = CAST(:c AS jsonb), active = :a, updated_at = now(), updated_by = :u WHERE template_id = CAST(:t AS uuid)"
        ),
        {
            "n": body.name,
            "d": body.description,
            "c": json.dumps(comps),
            "a": body.active,
            "u": user.user_id,
            "t": tid,
        },
    ).rowcount
    if not done:
        raise not_found("Template not found.")
    record_audit(
        conn,
        actor_user_id=user.user_id,
        action="SALARY_TEMPLATE_UPDATED",
        entity_type="salary_template",
        entity_id=tid,
        changes={"name": body.name},
        request=request,
    )
    return _template_view(
        conn.execute(
            text("SELECT * FROM hr.salary_template WHERE template_id = CAST(:t AS uuid)"),
            {"t": tid},
        )
        .mappings()
        .one()
    )


@router.post("/payroll/templates", status_code=201)
def create_template(
    body: TemplateIn,
    code: Annotated[str, Query(pattern=r"^[A-Z][A-Z0-9_]{2,29}$")],
    request: Request,
    user: HumanPrincipal = Depends(can_propose),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    comps = _check_template(body.components)
    if conn.execute(text("SELECT 1 FROM hr.salary_template WHERE code = :c"), {"c": code}).first():
        raise conflict("TEMPLATE_CODE_EXISTS", "A template with this code already exists.")
    tid = str(
        conn.execute(
            text(
                "INSERT INTO hr.salary_template (code, name, description, components, active, updated_by) VALUES (:c, :n, :d, CAST(:comp AS jsonb), :a, :u) RETURNING template_id"
            ),
            {
                "c": code,
                "n": body.name,
                "d": body.description,
                "comp": json.dumps(comps),
                "a": body.active,
                "u": user.user_id,
            },
        ).scalar_one()
    )
    record_audit(
        conn,
        actor_user_id=user.user_id,
        action="SALARY_TEMPLATE_CREATED",
        entity_type="salary_template",
        entity_id=tid,
        changes={"code": code},
        request=request,
    )
    return _template_view(
        conn.execute(
            text("SELECT * FROM hr.salary_template WHERE template_id = CAST(:t AS uuid)"),
            {"t": tid},
        )
        .mappings()
        .one()
    )


# ---- salary structures ---------------------------------------------------------------------


def _structure_view(r: Any) -> dict[str, Any]:
    return {
        "structureId": str(r["structure_id"]),
        "employeeId": str(r["employee_id"]),
        "templateId": str(r["template_id"]) if r["template_id"] else None,
        "grossMonthly": float(r["gross_monthly"]),
        "components": r["components"],
        "effectiveFrom": r["effective_from"].isoformat(),
        "status": r["status"],
        "note": r["note"],
        "proposedAt": r["proposed_at"].isoformat(),
        "decidedAt": r["decided_at"].isoformat() if r["decided_at"] else None,
        "decisionNote": r["decision_note"],
    }


class StructureIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    employee_id: str
    gross_monthly: Decimal = Field(gt=0, le=10_000_000, decimal_places=2)
    effective_from: date
    template_id: str | None = None
    band_confirmed: bool = False
    note: str | None = Field(default=None, max_length=300)


@router.post("/payroll/structures", status_code=201)
def propose_structure(
    body: StructureIn,
    request: Request,
    user: HumanPrincipal = Depends(can_propose),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    return create_structure(conn, body, user, request)


def create_structure(
    conn: Connection, body: StructureIn, user: HumanPrincipal, request: Request
) -> dict[str, Any]:
    """Proposes a salary structure. Shared by the single proposal and the bulk import, so both
    apply the same template and band rules."""
    eid = _uuid(body.employee_id)
    if (
        conn.execute(
            text("SELECT 1 FROM hr.employee WHERE employee_id = CAST(:e AS uuid)"), {"e": eid}
        ).first()
        is None
    ):
        raise not_found("Employee not found.")
    gross = body.gross_monthly
    template = None
    if body.template_id:
        template = (
            conn.execute(
                text(
                    "SELECT * FROM hr.salary_template WHERE template_id = CAST(:t AS uuid) AND active"
                ),
                {"t": _uuid(body.template_id)},
            )
            .mappings()
            .first()
        )
        if template is None:
            raise ApiError(422, "TEMPLATE_UNKNOWN", "Choose one of the listed templates.")
    if BAND_LOW <= gross <= BAND_HIGH:
        if template is None or not body.band_confirmed:
            raise ApiError(
                422,
                "SALARY_BAND_NEEDS_CHOICE",
                "A gross between ₹21,001 and ₹25,000 has no template yet. Choose a template yourself and confirm it.",
            )
    elif template is None:
        code = "BELOW_21K" if gross < BAND_LOW else "ABOVE_25K"
        template = (
            conn.execute(
                text("SELECT * FROM hr.salary_template WHERE code = :c AND active"), {"c": code}
            )
            .mappings()
            .first()
        )
        if template is None:
            raise ApiError(
                422, "TEMPLATE_UNKNOWN", "The default template is not available. Choose one."
            )
    try:
        components = pcalc.resolve_components(list(template["components"]), gross)
    except pcalc.PayrollError as exc:
        raise ApiError(422, "TEMPLATE_INVALID", str(exc)) from exc
    sid = str(
        conn.execute(
            text(
                "INSERT INTO hr.salary_structure (employee_id, template_id, gross_monthly, components, effective_from, note, proposed_by)"
                " VALUES (CAST(:e AS uuid), :t, :g, CAST(:c AS jsonb), :d, :n, :u) RETURNING structure_id"
            ),
            {
                "e": eid,
                "t": template["template_id"],
                "g": gross,
                "c": json.dumps(components),
                "d": body.effective_from,
                "n": body.note,
                "u": user.user_id,
            },
        ).scalar_one()
    )
    record_audit(
        conn,
        actor_user_id=user.user_id,
        action="SALARY_PROPOSED",
        entity_type="employee",
        entity_id=eid,
        changes={
            "structureId": sid,
            "effectiveFrom": body.effective_from.isoformat(),
            "template": template["code"],
        },
        request=request,
    )
    return _structure_view(
        conn.execute(
            text("SELECT * FROM hr.salary_structure WHERE structure_id = CAST(:s AS uuid)"),
            {"s": sid},
        )
        .mappings()
        .one()
    )


def _may_read_salaries(authorizer: Authorizer, user: HumanPrincipal) -> bool:
    return any(
        has_permission(authorizer, user, k)
        for k in (perm.HR_SALARY_PROPOSE, perm.HR_SALARY_APPROVE, perm.HR_PAYROLL_READ)
    )


@router.get("/payroll/structures")
def structures(
    employee_id: Annotated[str | None, Query()] = None,
    status: Annotated[
        Literal["PROPOSED", "APPROVED", "REJECTED", "SUPERSEDED"] | None, Query()
    ] = None,
    user: HumanPrincipal = Depends(current_user),
    authorizer: Authorizer = Depends(get_authorizer),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    if not _may_read_salaries(authorizer, user):
        raise forbidden()
    eid = _uuid(employee_id) if employee_id else None
    rows = conn.execute(
        text(
            "SELECT s.*, e.employee_code, e.full_name FROM hr.salary_structure s JOIN hr.employee e ON e.employee_id = s.employee_id"
            " WHERE (CAST(:e AS uuid) IS NULL OR s.employee_id = CAST(:e AS uuid)) AND (CAST(:s AS text) IS NULL OR s.status = :s)"
            " ORDER BY s.proposed_at DESC LIMIT 300"
        ),
        {"e": eid, "s": status},
    ).mappings()
    return {
        "items": [
            {
                **_structure_view(r),
                "employeeCode": r["employee_code"],
                "employeeName": r["full_name"],
            }
            for r in rows
        ]
    }


class SalaryDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    decision: Literal["APPROVE", "REJECT"]
    note: str | None = Field(default=None, max_length=300)


@router.post("/payroll/structures/{structure_id}/decision")
def decide_structure(
    structure_id: str,
    body: SalaryDecision,
    request: Request,
    user: HumanPrincipal = Depends(can_decide_salary),
    clock: Clock = Depends(get_clock),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    sid = _uuid(structure_id)
    row = (
        conn.execute(
            text(
                "SELECT * FROM hr.salary_structure WHERE structure_id = CAST(:s AS uuid) FOR UPDATE"
            ),
            {"s": sid},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise not_found("Salary structure not found.")
    if row["status"] != "PROPOSED":
        raise conflict("APPROVAL_ALREADY_DECIDED", "This salary has already been decided.")
    if (
        row["proposed_by"] == user.user_id
        or wc.employee_user_id(conn, str(row["employee_id"])) == user.user_id
    ):
        raise forbidden("You cannot approve your own proposal or your own salary.")
    if body.decision == "REJECT" and not (body.note and body.note.strip()):
        raise ApiError(422, "APPROVAL_NOTE_REQUIRED", "Say why you are rejecting it.")
    status = "APPROVED" if body.decision == "APPROVE" else "REJECTED"
    if status == "APPROVED":
        conn.execute(
            text(
                "UPDATE hr.salary_structure SET status = 'SUPERSEDED' WHERE employee_id = :e AND status = 'APPROVED' AND effective_from = :d"
            ),
            {"e": row["employee_id"], "d": row["effective_from"]},
        )
    conn.execute(
        text(
            "UPDATE hr.salary_structure SET status = :s, decided_by = :u, decided_at = :n, decision_note = :note WHERE structure_id = CAST(:i AS uuid)"
        ),
        {
            "s": status,
            "u": user.user_id,
            "n": clock(),
            "note": (body.note or "").strip() or None,
            "i": sid,
        },
    )
    record_audit(
        conn,
        actor_user_id=user.user_id,
        action="SALARY_" + status,
        entity_type="employee",
        entity_id=str(row["employee_id"]),
        changes={"structureId": sid, "effectiveFrom": row["effective_from"].isoformat()},
        request=request,
    )
    return {"structureId": sid, "status": status}


# ---- statutory configuration -----------------------------------------------------------------


def _statutory_view(row: Any) -> dict[str, Any]:
    return {
        "config": row["config"],
        "updatedAt": row["updated_at"].isoformat(),
        "confirmed": row["confirmed_at"] is not None,
        "confirmedAt": row["confirmed_at"].isoformat() if row["confirmed_at"] else None,
        "confirmationNote": row["confirmation_note"],
        "note": "Rates, ceilings and slabs are entered by HR. They must be confirmed by your CA before a payroll run can be approved.",
    }


@router.get("/payroll/statutory")
def get_statutory(
    user: HumanPrincipal = Depends(current_user),
    authorizer: Authorizer = Depends(get_authorizer),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    if not (
        has_permission(authorizer, user, perm.HR_PAYROLL_READ)
        or has_permission(authorizer, user, perm.HR_SETTINGS_MANAGE)
    ):
        raise forbidden()
    return _statutory_view(
        conn.execute(text("SELECT * FROM hr.statutory_config WHERE singleton")).mappings().one()
    )


class StatutoryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pf: dict[str, Any] = Field(default_factory=dict)
    esi: dict[str, Any] = Field(default_factory=dict)
    pt: dict[str, Any] = Field(default_factory=dict)


@router.put("/payroll/statutory")
def put_statutory(
    body: StatutoryIn,
    request: Request,
    user: HumanPrincipal = Depends(can_settings),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    try:
        clean = pcalc.validate_statutory(body.model_dump())
    except (pcalc.PayrollError, ArithmeticError, KeyError, TypeError, ValueError) as exc:
        raise ApiError(
            422,
            "STATUTORY_INVALID",
            str(exc) if isinstance(exc, pcalc.PayrollError) else "A number or slab is not valid.",
        ) from exc
    # A change cancels any earlier confirmation: the CA confirms exactly what is saved.
    conn.execute(
        text(
            "UPDATE hr.statutory_config SET config = CAST(:c AS jsonb), updated_at = now(), updated_by = :u, confirmed_at = NULL, confirmed_by = NULL, confirmation_note = NULL WHERE singleton"
        ),
        {"c": json.dumps(clean), "u": user.user_id},
    )
    record_audit(
        conn,
        actor_user_id=user.user_id,
        action="STATUTORY_CONFIG_UPDATED",
        entity_type="statutory_config",
        entity_id="company",
        changes={
            "pf": clean["pf"]["enabled"],
            "esi": clean["esi"]["enabled"],
            "pt": clean["pt"]["enabled"],
        },
        request=request,
    )
    return _statutory_view(
        conn.execute(text("SELECT * FROM hr.statutory_config WHERE singleton")).mappings().one()
    )


class Confirmation(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    note: str = Field(min_length=5, max_length=300)


@router.post("/payroll/statutory/confirm")
def confirm_statutory(
    body: Confirmation,
    request: Request,
    user: HumanPrincipal = Depends(can_settings),
    clock: Clock = Depends(get_clock),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    """Records that the CA has confirmed the saved settings (who confirmed, in the note)."""
    conn.execute(
        text(
            "UPDATE hr.statutory_config SET confirmed_at = :n, confirmed_by = :u, confirmation_note = :note WHERE singleton"
        ),
        {"n": clock(), "u": user.user_id, "note": body.note},
    )
    record_audit(
        conn,
        actor_user_id=user.user_id,
        action="STATUTORY_CONFIG_CONFIRMED",
        entity_type="statutory_config",
        entity_id="company",
        changes={"note": body.note},
        request=request,
    )
    return _statutory_view(
        conn.execute(text("SELECT * FROM hr.statutory_config WHERE singleton")).mappings().one()
    )


# ---- runs ----------------------------------------------------------------------------------


def _run(conn: Connection, run_id: str, lock: bool = False) -> Any:
    row = (
        conn.execute(
            text(
                "SELECT * FROM hr.payroll_run WHERE run_id = CAST(:r AS uuid)"
                + (" FOR UPDATE" if lock else "")
            ),
            {"r": run_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise not_found("Payroll run not found.")
    return row


def _line_summary(r: Any) -> dict[str, Any]:
    f = r["figures"]
    return {
        "employeeId": str(r["employee_id"]),
        "employeeCode": r["employee_code"],
        "employeeName": r["employee_name"],
        "designation": r["designation"],
        "paidDays": f["days"]["paidDays"],
        "lopDays": f["days"]["lopDays"],
        "absentDays": f["days"]["absentDays"],
        "extraLopDays": float(r["extra_lop_days"]),
        "grossEarned": f["gross_earned"],
        "totalDeductions": f["total_deductions"],
        "netPay": str(r["net_pay"]),
        "reimbursements": f["reimbursement_total"],
        "payableTotal": str(r["payable_total"]),
        "adjustments": r["adjustments"],
    }


def _run_view(conn: Connection, row: Any, with_lines: bool = True) -> dict[str, Any]:
    out = {
        "runId": str(row["run_id"]),
        "payMonth": row["pay_month"].isoformat()[:7],
        "status": row["status"],
        "statutoryConfirmed": row["statutory_confirmed"],
        "skipped": row["skipped"],
        "createdAt": row["created_at"].isoformat(),
        "submittedAt": row["submitted_at"].isoformat() if row["submitted_at"] else None,
        "approvedAt": row["approved_at"].isoformat() if row["approved_at"] else None,
        "paidAt": row["paid_at"].isoformat() if row["paid_at"] else None,
        "paymentDate": row["payment_date"].isoformat() if row["payment_date"] else None,
        "note": row["note"],
    }
    totals = (
        conn.execute(
            text(
                "SELECT count(*) AS n, coalesce(sum(net_pay), 0) AS net, coalesce(sum(payable_total), 0) AS pay FROM hr.payroll_line WHERE run_id = :r"
            ),
            {"r": row["run_id"]},
        )
        .mappings()
        .one()
    )
    out["totals"] = {
        "people": totals["n"],
        "netPay": str(totals["net"]),
        "payable": str(totals["pay"]),
    }
    if with_lines:
        lines = conn.execute(
            text("SELECT * FROM hr.payroll_line WHERE run_id = :r ORDER BY employee_code"),
            {"r": row["run_id"]},
        ).mappings()
        out["lines"] = [_line_summary(r) for r in lines]
    return out


@router.get("/payroll/runs")
def runs(
    _: HumanPrincipal = Depends(can_read), conn: Connection = Depends(get_conn)
) -> dict[str, Any]:
    rows = (
        conn.execute(
            text("SELECT * FROM hr.payroll_run ORDER BY pay_month DESC, created_at DESC LIMIT 60")
        )
        .mappings()
        .all()
    )
    return {"items": [_run_view(conn, r, with_lines=False) for r in rows]}


class RunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    month: str = Field(max_length=7)


def _current_statutory(conn: Connection) -> tuple[dict[str, Any], bool]:
    row = (
        conn.execute(text("SELECT config, confirmed_at FROM hr.statutory_config WHERE singleton"))
        .mappings()
        .one()
    )
    try:
        return pcalc.validate_statutory(row["config"]), row["confirmed_at"] is not None
    except pcalc.PayrollError as exc:
        raise ApiError(
            422, "STATUTORY_INVALID", f"The statutory settings need attention: {exc}"
        ) from exc


@router.post("/payroll/runs", status_code=201)
def create_run(
    body: RunIn,
    request: Request,
    user: HumanPrincipal = Depends(can_prepare),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    month = _month(body.month)
    statutory, confirmed = _current_statutory(conn)
    if conn.execute(
        text("SELECT 1 FROM hr.payroll_run WHERE pay_month = :m AND status <> 'CANCELLED'"),
        {"m": month},
    ).first():
        raise conflict("PAYROLL_RUN_EXISTS", "There is already a payroll run for this month.")
    run_id = str(
        conn.execute(
            text(
                "INSERT INTO hr.payroll_run (pay_month, statutory_config, statutory_confirmed, created_by) VALUES (:m, CAST(:c AS jsonb), :ok, :u) RETURNING run_id"
            ),
            {"m": month, "c": json.dumps(statutory), "ok": confirmed, "u": user.user_id},
        ).scalar_one()
    )
    skipped, written = pdata.prepare_lines(conn, run_id, month, statutory)
    conn.execute(
        text(
            "UPDATE hr.payroll_run SET skipped = CAST(:s AS jsonb) WHERE run_id = CAST(:r AS uuid)"
        ),
        {"s": json.dumps(skipped), "r": run_id},
    )
    record_audit(
        conn,
        actor_user_id=user.user_id,
        action="PAYROLL_RUN_CREATED",
        entity_type="payroll_run",
        entity_id=run_id,
        changes={"month": body.month, "people": written, "skipped": len(skipped)},
        request=request,
    )
    return _run_view(conn, _run(conn, run_id))


@router.get("/payroll/runs/{run_id}")
def run_detail(
    run_id: str, _: HumanPrincipal = Depends(can_read), conn: Connection = Depends(get_conn)
) -> dict[str, Any]:
    return _run_view(conn, _run(conn, _uuid(run_id)))


@router.get("/payroll/runs/{run_id}/lines/{employee_id}")
def line_detail(
    run_id: str,
    employee_id: str,
    _: HumanPrincipal = Depends(can_read),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    row = (
        conn.execute(
            text(
                "SELECT * FROM hr.payroll_line WHERE run_id = CAST(:r AS uuid) AND employee_id = CAST(:e AS uuid)"
            ),
            {"r": _uuid(run_id), "e": _uuid(employee_id)},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise not_found("Line not found.")
    return {**_line_summary(row), "figures": row["figures"]}


def _draft(row: Any) -> None:
    if row["status"] != "DRAFT":
        raise conflict(
            "PAYROLL_RUN_LOCKED",
            "This run is no longer a draft. Send it back first, or correct it in the next month.",
        )


@router.post("/payroll/runs/{run_id}/recompute")
def recompute(
    run_id: str,
    request: Request,
    user: HumanPrincipal = Depends(can_prepare),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    rid = _uuid(run_id)
    row = _run(conn, rid, lock=True)
    _draft(row)
    statutory, confirmed = _current_statutory(conn)
    keep = {
        str(r["employee_id"]): (Decimal(r["extra_lop_days"]), list(r["adjustments"]))
        for r in conn.execute(
            text(
                "SELECT employee_id, extra_lop_days, adjustments FROM hr.payroll_line WHERE run_id = CAST(:r AS uuid)"
            ),
            {"r": rid},
        ).mappings()
    }
    skipped, written = pdata.prepare_lines(conn, rid, row["pay_month"], statutory, keep)
    conn.execute(
        text(
            "UPDATE hr.payroll_run SET skipped = CAST(:s AS jsonb), statutory_config = CAST(:c AS jsonb), statutory_confirmed = :ok WHERE run_id = CAST(:r AS uuid)"
        ),
        {"s": json.dumps(skipped), "c": json.dumps(statutory), "ok": confirmed, "r": rid},
    )
    record_audit(
        conn,
        actor_user_id=user.user_id,
        action="PAYROLL_RUN_RECOMPUTED",
        entity_type="payroll_run",
        entity_id=rid,
        changes={"people": written, "skipped": len(skipped)},
        request=request,
    )
    return _run_view(conn, _run(conn, rid))


class AdjustmentIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    label: str = Field(min_length=2, max_length=80)
    amount: Decimal = Field(max_digits=12, decimal_places=2)
    taxable: bool = True
    note: str | None = Field(default=None, max_length=200)


class InputsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    extra_lop_days: Decimal = Field(default=Decimal(0), ge=0, le=31, decimal_places=1)
    adjustments: list[AdjustmentIn] = Field(default_factory=list, max_length=10)


@router.put("/payroll/runs/{run_id}/lines/{employee_id}/inputs")
def set_inputs(
    run_id: str,
    employee_id: str,
    body: InputsIn,
    request: Request,
    user: HumanPrincipal = Depends(can_prepare),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    rid, eid = _uuid(run_id), _uuid(employee_id)
    run = _run(conn, rid, lock=True)
    _draft(run)
    if (
        conn.execute(
            text(
                "SELECT 1 FROM hr.payroll_line WHERE run_id = CAST(:r AS uuid) AND employee_id = CAST(:e AS uuid)"
            ),
            {"r": rid, "e": eid},
        ).first()
        is None
    ):
        raise not_found("Line not found.")
    if any(a.amount == 0 for a in body.adjustments):
        raise ApiError(422, "HR_VALIDATION_FAILED", "An adjustment cannot be zero.")
    statutory = pcalc.validate_statutory(run["statutory_config"])
    emp = (
        conn.execute(
            text(
                "SELECT e.employee_id, e.date_of_joining FROM hr.employee e WHERE e.employee_id = CAST(:e AS uuid)"
            ),
            {"e": eid},
        )
        .mappings()
        .one()
    )
    adjustments = [a.model_dump(mode="json") for a in body.adjustments]
    try:
        structure, figures = pdata.build_line(
            conn,
            emp,
            run["pay_month"],
            statutory,
            body.extra_lop_days,
            adjustments,
            pdata.declared_holidays(conn, run["pay_month"]),
        )
    except pcalc.PayrollError as exc:
        raise ApiError(422, "PAYROLL_LINE_INVALID", str(exc)) from exc
    conn.execute(
        text(
            "UPDATE hr.payroll_line SET extra_lop_days = :x, adjustments = CAST(:a AS jsonb), figures = CAST(:f AS jsonb), net_pay = :n, payable_total = :p, structure_id = :s WHERE run_id = CAST(:r AS uuid) AND employee_id = CAST(:e AS uuid)"
        ),
        {
            "x": body.extra_lop_days,
            "a": json.dumps(adjustments),
            "f": json.dumps(figures),
            "n": figures["net_pay"],
            "p": figures["payable_total"],
            "s": structure["structure_id"],
            "r": rid,
            "e": eid,
        },
    )
    record_audit(
        conn,
        actor_user_id=user.user_id,
        action="PAYROLL_LINE_INPUTS_SET",
        entity_type="employee",
        entity_id=eid,
        changes={
            "runId": rid,
            "extraLopDays": float(body.extra_lop_days),
            "adjustments": len(adjustments),
        },
        request=request,
    )
    row = (
        conn.execute(
            text(
                "SELECT * FROM hr.payroll_line WHERE run_id = CAST(:r AS uuid) AND employee_id = CAST(:e AS uuid)"
            ),
            {"r": rid, "e": eid},
        )
        .mappings()
        .one()
    )
    return {**_line_summary(row), "figures": row["figures"]}


class NoteIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    note: str | None = Field(default=None, max_length=300)


@router.post("/payroll/runs/{run_id}/submit")
def submit_run(
    run_id: str,
    request: Request,
    user: HumanPrincipal = Depends(can_prepare),
    clock: Clock = Depends(get_clock),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    rid = _uuid(run_id)
    row = _run(conn, rid, lock=True)
    _draft(row)
    people = conn.execute(
        text("SELECT count(*) FROM hr.payroll_line WHERE run_id = CAST(:r AS uuid)"), {"r": rid}
    ).scalar_one()
    if people == 0:
        raise ApiError(
            422,
            "PAYROLL_RUN_EMPTY",
            "There is nobody in this run yet. Approve salary structures first, then recompute.",
        )
    conn.execute(
        text(
            "UPDATE hr.payroll_run SET status = 'SUBMITTED', submitted_by = :u, submitted_at = :n WHERE run_id = CAST(:r AS uuid)"
        ),
        {"u": user.user_id, "n": clock(), "r": rid},
    )
    record_audit(
        conn,
        actor_user_id=user.user_id,
        action="PAYROLL_RUN_SUBMITTED",
        entity_type="payroll_run",
        entity_id=rid,
        changes={"people": people},
        request=request,
    )
    return _run_view(conn, _run(conn, rid))


@router.post("/payroll/runs/{run_id}/send-back")
def send_back(
    run_id: str,
    body: NoteIn,
    request: Request,
    user: HumanPrincipal = Depends(current_user),
    authorizer: Authorizer = Depends(get_authorizer),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    if not (
        has_permission(authorizer, user, perm.HR_PAYROLL_APPROVE)
        or has_permission(authorizer, user, perm.HR_PAYROLL_PREPARE)
    ):
        raise forbidden()
    rid = _uuid(run_id)
    row = _run(conn, rid, lock=True)
    if row["status"] != "SUBMITTED":
        raise conflict("PAYROLL_RUN_STATE", "Only a run waiting for approval can be sent back.")
    if not (body.note and body.note.strip()):
        raise ApiError(422, "APPROVAL_NOTE_REQUIRED", "Say what needs to change.")
    conn.execute(
        text(
            "UPDATE hr.payroll_run SET status = 'DRAFT', note = :n, submitted_by = NULL, submitted_at = NULL WHERE run_id = CAST(:r AS uuid)"
        ),
        {"n": body.note.strip(), "r": rid},
    )
    record_audit(
        conn,
        actor_user_id=user.user_id,
        action="PAYROLL_RUN_SENT_BACK",
        entity_type="payroll_run",
        entity_id=rid,
        changes={"note": body.note.strip()},
        request=request,
    )
    return _run_view(conn, _run(conn, rid))


@router.post("/payroll/runs/{run_id}/cancel")
def cancel_run(
    run_id: str,
    request: Request,
    user: HumanPrincipal = Depends(can_prepare),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    rid = _uuid(run_id)
    row = _run(conn, rid, lock=True)
    if row["status"] not in ("DRAFT", "SUBMITTED"):
        raise conflict(
            "PAYROLL_RUN_LOCKED",
            "An approved run cannot be cancelled. Correct it in the next month.",
        )
    conn.execute(
        text("UPDATE hr.payroll_run SET status = 'CANCELLED' WHERE run_id = CAST(:r AS uuid)"),
        {"r": rid},
    )
    record_audit(
        conn,
        actor_user_id=user.user_id,
        action="PAYROLL_RUN_CANCELLED",
        entity_type="payroll_run",
        entity_id=rid,
        changes={},
        request=request,
    )
    return _run_view(conn, _run(conn, rid), with_lines=False)


@router.post("/payroll/runs/{run_id}/approve")
def approve_run(
    run_id: str,
    request: Request,
    user: HumanPrincipal = Depends(can_approve),
    clock: Clock = Depends(get_clock),
    storage: ObjectStorage | None = Depends(get_storage),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    """The CEO approves. The run is locked, a payslip is made for everyone in it, and the approved
    reimbursements in it are handed to payroll."""
    rid = _uuid(run_id)
    run = _run(conn, rid, lock=True)
    if run["status"] != "SUBMITTED":
        raise conflict("PAYROLL_RUN_STATE", "Only a run waiting for approval can be approved.")
    if run["submitted_by"] == user.user_id or run["created_by"] == user.user_id:
        raise forbidden("You cannot approve a run you prepared yourself.")
    if not run["statutory_confirmed"]:
        raise conflict(
            "PAYROLL_STATUTORY_UNCONFIRMED",
            "The statutory settings have not been confirmed by your CA. Confirm them, recompute the run and submit it again.",
        )
    if storage is None:
        raise dependency_unavailable("File storage is not configured, so payslips cannot be saved.")
    now = clock()
    company = {
        k.removeprefix("company."): str(v_ or "")
        for k, v_ in cfg.load_all(conn).items()
        if k.startswith("company.")
    }
    lines = (
        conn.execute(
            text(
                "SELECT l.*, s.pan FROM hr.payroll_line l LEFT JOIN hr.employee_sensitive s ON s.employee_id = l.employee_id WHERE l.run_id = CAST(:r AS uuid) ORDER BY l.employee_code"
            ),
            {"r": rid},
        )
        .mappings()
        .all()
    )
    month = run["pay_month"]
    made: list[tuple[str, str, str]] = []
    for ln in lines:
        pdf = render_payslip(
            company=company,
            employee={
                "name": ln["employee_name"],
                "code": ln["employee_code"],
                "designation": ln["designation"],
                "pan_masked": v.mask_pan(ln["pan"]),
            },
            month=month,
            figures=ln["figures"],
        )
        payslip_id = str(uuid.uuid4())
        key = f"payslips/{month:%Y}/{month:%m}/{ln['employee_id']}/{payslip_id}.pdf"
        try:
            storage.put(key, pdf, "application/pdf")
        except StorageError as exc:
            raise dependency_unavailable(
                "A payslip could not be saved, so nothing was approved. Please try again."
            ) from exc
        made.append((payslip_id, str(ln["employee_id"]), key))
    for payslip_id, eid, key in made:
        conn.execute(
            text(
                "INSERT INTO hr.payslip (payslip_id, run_id, employee_id, pay_month, file_key, issued_at) VALUES (CAST(:p AS uuid), CAST(:r AS uuid), CAST(:e AS uuid), :m, :k, :n)"
            ),
            {"p": payslip_id, "r": rid, "e": eid, "m": month, "k": key, "n": now},
        )
    claim_ids = [rc["claimId"] for ln in lines for rc in ln["figures"]["reimbursements"]]
    if claim_ids:
        conn.execute(
            text(
                "UPDATE hr.claim SET status = 'HANDED_TO_PAYROLL', payroll_run_id = CAST(:r AS uuid), updated_at = :n WHERE claim_id = ANY(CAST(:ids AS uuid[])) AND status = 'APPROVED' AND payroll_run_id IS NULL"
            ),
            {"r": rid, "n": now, "ids": claim_ids},
        )
        for cid in claim_ids:
            conn.execute(
                text(
                    "INSERT INTO hr.claim_event (claim_id, actor, event_type, note) VALUES (CAST(:c AS uuid), :a, 'HANDED_TO_PAYROLL', :n)"
                ),
                {"c": cid, "a": user.user_id, "n": f"Payroll {month:%B %Y}"},
            )
    conn.execute(
        text(
            "UPDATE hr.payroll_run SET status = 'APPROVED', approved_by = :u, approved_at = :n WHERE run_id = CAST(:r AS uuid)"
        ),
        {"u": user.user_id, "n": now, "r": rid},
    )
    record_audit(
        conn,
        actor_user_id=user.user_id,
        action="PAYROLL_RUN_APPROVED",
        entity_type="payroll_run",
        entity_id=rid,
        changes={"month": month.isoformat()[:7], "payslips": len(made), "claims": len(claim_ids)},
        request=request,
    )
    return _run_view(conn, _run(conn, rid))


class PaidIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    payment_date: date


@router.post("/payroll/runs/{run_id}/mark-paid")
def mark_paid(
    run_id: str,
    body: PaidIn,
    request: Request,
    user: HumanPrincipal = Depends(can_prepare),
    clock: Clock = Depends(get_clock),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    rid = _uuid(run_id)
    run = _run(conn, rid, lock=True)
    if run["status"] != "APPROVED":
        raise conflict("PAYROLL_RUN_STATE", "Only an approved run can be marked paid.")
    if body.payment_date > ist_date(clock()):
        raise ApiError(422, "HR_VALIDATION_FAILED", "The payment date cannot be in the future.")
    now = clock()
    conn.execute(
        text(
            "UPDATE hr.payroll_run SET status = 'PAID', paid_at = :n, payment_date = :d WHERE run_id = CAST(:r AS uuid)"
        ),
        {"n": now, "d": body.payment_date, "r": rid},
    )
    paid = [
        str(r[0])
        for r in conn.execute(
            text(
                "UPDATE hr.claim SET status = 'PAID', updated_at = :n WHERE payroll_run_id = CAST(:r AS uuid) AND status = 'HANDED_TO_PAYROLL' RETURNING claim_id"
            ),
            {"n": now, "r": rid},
        )
    ]
    for cid in paid:
        conn.execute(
            text(
                "INSERT INTO hr.claim_event (claim_id, actor, event_type, note) VALUES (CAST(:c AS uuid), :a, 'PAID', :n)"
            ),
            {"c": cid, "a": user.user_id, "n": f"Paid on {body.payment_date.isoformat()}"},
        )
    record_audit(
        conn,
        actor_user_id=user.user_id,
        action="PAYROLL_RUN_PAID",
        entity_type="payroll_run",
        entity_id=rid,
        changes={"paymentDate": body.payment_date.isoformat(), "claims": len(paid)},
        request=request,
    )
    return _run_view(conn, _run(conn, rid), with_lines=False)


# ---- payslips ------------------------------------------------------------------------------


@router.get("/payslips")
def my_payslips(
    user: HumanPrincipal = Depends(current_user), conn: Connection = Depends(get_conn)
) -> dict[str, Any]:
    employee_id = _own_employee_id(conn, user)
    rows = conn.execute(
        text(
            "SELECT p.payslip_id, p.pay_month, p.issued_at, l.net_pay, l.payable_total FROM hr.payslip p JOIN hr.payroll_line l ON l.run_id = p.run_id AND l.employee_id = p.employee_id WHERE p.employee_id = CAST(:e AS uuid) ORDER BY p.pay_month DESC"
        ),
        {"e": employee_id},
    ).mappings()
    return {
        "items": [
            {
                "payslipId": str(r["payslip_id"]),
                "month": r["pay_month"].isoformat()[:7],
                "issuedAt": r["issued_at"].isoformat(),
                "netPay": str(r["net_pay"]),
                "payable": str(r["payable_total"]),
            }
            for r in rows
        ]
    }


@router.get("/me/salary")
def my_salary(
    user: HumanPrincipal = Depends(current_user),
    conn: Connection = Depends(get_conn),
    clock: Clock = Depends(get_clock),
) -> dict[str, Any]:
    """A person's own approved salary, and nobody else's: the employee is found from the login.
    A proposal still waiting for approval is not shown to the employee."""
    employee_id = _own_employee_id(conn, user)
    today = ist_date(clock())
    rows = (
        conn.execute(
            text(
                "SELECT * FROM hr.salary_structure WHERE employee_id = CAST(:e AS uuid)"
                " AND status = 'APPROVED' ORDER BY effective_from DESC, proposed_at DESC"
            ),
            {"e": employee_id},
        )
        .mappings()
        .all()
    )
    current = next((r for r in rows if r["effective_from"] <= today), None)
    upcoming = [r for r in rows if r["effective_from"] > today]
    view = None
    if current is not None:
        view = {
            "grossMonthly": float(current["gross_monthly"]),
            "components": current["components"],
            "effectiveFrom": current["effective_from"].isoformat(),
        }
    return {
        "current": view,
        "upcomingFrom": upcoming[-1]["effective_from"].isoformat() if upcoming else None,
    }


@router.get("/payroll/runs/{run_id}/payslips")
def run_payslips(
    run_id: str, _: HumanPrincipal = Depends(can_read), conn: Connection = Depends(get_conn)
) -> dict[str, Any]:
    rows = conn.execute(
        text(
            "SELECT p.payslip_id, p.employee_id, l.employee_code, l.employee_name FROM hr.payslip p JOIN hr.payroll_line l ON l.run_id = p.run_id AND l.employee_id = p.employee_id WHERE p.run_id = CAST(:r AS uuid) ORDER BY l.employee_code"
        ),
        {"r": _uuid(run_id)},
    ).mappings()
    return {
        "items": [
            {
                "payslipId": str(r["payslip_id"]),
                "employeeId": str(r["employee_id"]),
                "employeeCode": r["employee_code"],
                "employeeName": r["employee_name"],
            }
            for r in rows
        ]
    }


@router.get("/payslips/{payslip_id}/pdf")
def payslip_pdf(
    payslip_id: str,
    request: Request,
    user: HumanPrincipal = Depends(current_user),
    authorizer: Authorizer = Depends(get_authorizer),
    storage: ObjectStorage | None = Depends(get_storage),
    conn: Connection = Depends(get_conn),
) -> Response:
    """A person's own payslip; HR with payroll access may open anyone's, and that is recorded."""
    row = (
        conn.execute(
            text("SELECT * FROM hr.payslip WHERE payslip_id = CAST(:p AS uuid)"),
            {"p": _uuid(payslip_id)},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise not_found("Payslip not found.")
    own = wc.employee_user_id(conn, str(row["employee_id"])) == user.user_id
    if not own:
        if not has_permission(authorizer, user, perm.HR_PAYROLL_READ):
            raise not_found("Payslip not found.")
        record_audit(
            conn,
            actor_user_id=user.user_id,
            action="PAYSLIP_VIEWED",
            entity_type="employee",
            entity_id=str(row["employee_id"]),
            changes={
                "payslipId": str(row["payslip_id"]),
                "month": row["pay_month"].isoformat()[:7],
            },
            request=request,
        )
    if storage is None:
        raise dependency_unavailable("File storage is not configured.")
    try:
        data = storage.get(row["file_key"])
    except StorageError as exc:
        raise dependency_unavailable("The payslip could not be loaded.") from exc
    return Response(
        content=data,
        media_type="application/pdf",
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": f'inline; filename="payslip-{row["pay_month"]:%Y-%m}.pdf"',
        },
    )
