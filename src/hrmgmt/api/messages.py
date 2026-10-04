from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, text

from hrmgmt import message_templates as tpl
from hrmgmt import permissions as perm
from hrmgmt import settings_store as cfg
from hrmgmt.api.employees import _uuid, get_provisioner
from hrmgmt.audit import record_audit
from hrmgmt.db import get_conn
from hrmgmt.errors import ApiError, dependency_unavailable
from hrmgmt.mailer import Mailer, MailError
from hrmgmt.passwords import generate_initial_password
from hrmgmt.principal import require_permission
from hrmgmt.provisioning import ProvisioningError, UserProvisioner
from hrmgmt.security import HumanPrincipal

router = APIRouter(prefix="/hr/v1/messages", tags=["Employee messages"])

can_send = require_permission(perm.HR_EMPLOYEE_MANAGE)
can_edit_templates = require_permission(perm.HR_SETTINGS_MANAGE)

BATCH_LIMIT = 5


def get_senders(request: Request) -> dict[str, Mailer]:
    """The channels that are set up, by name. Email today; WhatsApp or SMS later add an entry."""
    senders: dict[str, Mailer] = {}
    mailer = getattr(request.app.state, "mailer", None)
    if mailer is not None:
        senders["EMAIL"] = mailer
    return senders


def _template(conn: Connection, code: str) -> dict[str, Any]:
    default = tpl.DEFAULTS[code]
    row = (
        conn.execute(
            text("SELECT subject, body, updated_at FROM hr.message_template WHERE code = :c"),
            {"c": code},
        )
        .mappings()
        .first()
    )
    return {
        "code": code,
        "name": default["name"],
        "subject": row["subject"] if row else default["subject"],
        "body": row["body"] if row else default["body"],
        "placeholders": list(tpl.PLACEHOLDERS[code]),
        "customised": row is not None,
        "updatedAt": row["updated_at"].isoformat() if row else None,
    }


@router.get("/templates")
def templates(
    _: HumanPrincipal = Depends(can_send),
    senders: dict[str, Mailer] = Depends(get_senders),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    return {
        "items": [_template(conn, code) for code in tpl.CODES],
        "channels": {"EMAIL": "EMAIL" in senders, "WHATSAPP": "WHATSAPP" in senders},
        "mailConfigured": "EMAIL" in senders,
    }


class TemplateIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=5000)


@router.put("/templates/{code}")
def update_template(
    code: Literal["GENERAL", "WELCOME"],
    body: TemplateIn,
    request: Request,
    user: HumanPrincipal = Depends(can_edit_templates),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    try:
        tpl.validate(code, body.subject, body.body)
    except tpl.TemplateError as exc:
        raise ApiError(422, "MESSAGE_TEMPLATE_INVALID", str(exc)) from exc
    conn.execute(
        text(
            "INSERT INTO hr.message_template (code, name, subject, body, updated_by)"
            " VALUES (:c, :n, :s, :b, :u)"
            " ON CONFLICT (code) DO UPDATE SET subject = EXCLUDED.subject, body = EXCLUDED.body,"
            " updated_at = now(), updated_by = EXCLUDED.updated_by"
        ),
        {
            "c": code,
            "n": tpl.DEFAULTS[code]["name"],
            "s": body.subject,
            "b": body.body,
            "u": user.user_id,
        },
    )
    record_audit(
        conn,
        actor_user_id=user.user_id,
        action="MESSAGE_TEMPLATE_UPDATED",
        entity_type="message_template",
        entity_id=code,
        changes={"template": code},
        request=request,
    )
    return _template(conn, code)


class SendIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    channel: Literal["EMAIL", "WHATSAPP"] = "EMAIL"
    template: Literal["GENERAL", "WELCOME"]
    employee_ids: list[str] = Field(min_length=1, max_length=BATCH_LIMIT)
    # A one-off wording for this send (GENERAL only). The saved template is not changed.
    subject: str | None = Field(default=None, max_length=200)
    body: str | None = Field(default=None, max_length=5000)


def _log(
    conn: Connection, employee_id: str, code: str, status: str, reason: str | None, by: str
) -> None:
    conn.execute(
        text(
            "INSERT INTO hr.message_log (employee_id, template_code, status, reason_code, sent_by)"
            " VALUES (CAST(:e AS uuid), :t, :s, :r, :u)"
        ),
        {"e": employee_id, "t": code, "s": status, "r": reason, "u": by},
    )


def _result(
    emp: Any, status: str, code: str | None = None, message: str | None = None
) -> dict[str, Any]:
    return {
        "employeeId": str(emp["employee_id"]),
        "name": emp["full_name"],
        "status": status,
        "code": code,
        "message": message,
    }


@router.post("/send")
def send(
    body: SendIn,
    request: Request,
    user: HumanPrincipal = Depends(can_send),
    senders: dict[str, Mailer] = Depends(get_senders),
    provisioner: UserProvisioner | None = Depends(get_provisioner),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    """Sends to each chosen employee, one attempt each. A WELCOME message first sets a fresh
    temporary password for the person's Verigence login (Security refuses a login that SuperAdmin
    has not allowed yet), then emails it. The password is never stored, logged or audited."""
    mailer = senders.get(body.channel)
    if mailer is None:
        raise ApiError(
            503,
            "MESSAGE_CHANNEL_NOT_AVAILABLE",
            "Email is not set up yet. Ask your administrator."
            if body.channel == "EMAIL"
            else "WhatsApp is not set up yet.",
        )
    ids = list(dict.fromkeys(_uuid(i) for i in body.employee_ids))
    saved = _template(conn, body.template)
    subject, text_body = saved["subject"], saved["body"]
    if body.subject is not None or body.body is not None:
        if body.template != tpl.GENERAL:
            raise ApiError(
                422,
                "MESSAGE_TEMPLATE_INVALID",
                "Only the general message can be reworded for one send.",
            )
        subject = body.subject if body.subject is not None else subject
        text_body = body.body if body.body is not None else text_body
        try:
            tpl.validate(tpl.GENERAL, subject, text_body)
        except tpl.TemplateError as exc:
            raise ApiError(422, "MESSAGE_TEMPLATE_INVALID", str(exc)) from exc
    settings = cfg.load_all(conn)
    shared = {
        "company": str(settings.get("company.name") or "").strip() or "Verigence",
        "sign_in_link": str(settings.get("email.sign_in_url") or ""),
        "app_link": str(settings.get("email.app_download_url") or ""),
    }
    rows = (
        conn.execute(
            text(
                "SELECT employee_id, full_name, personal_email, employment_status, security_user_id"
                " FROM hr.employee WHERE employee_id = ANY(CAST(:ids AS uuid[]))"
            ),
            {"ids": ids},
        )
        .mappings()
        .all()
    )
    by_id = {str(r["employee_id"]): r for r in rows}
    results: list[dict[str, Any]] = []
    for eid in ids:
        emp = by_id.get(eid)
        if emp is None:
            results.append(
                {
                    "employeeId": eid,
                    "name": None,
                    "status": "SKIPPED",
                    "code": "NOT_FOUND",
                    "message": "Employee not found.",
                }
            )
            continue
        if emp["employment_status"] != "ACTIVE":
            _log(conn, eid, body.template, "SKIPPED", "NOT_ACTIVE", user.user_id)
            results.append(_result(emp, "SKIPPED", "NOT_ACTIVE", "The employee is not active."))
            continue
        values = {**shared, "name": emp["full_name"], "login_id": emp["personal_email"]}
        if body.template == tpl.WELCOME:
            if emp["security_user_id"] is None:
                _log(conn, eid, body.template, "SKIPPED", "NO_LOGIN", user.user_id)
                results.append(
                    _result(
                        emp,
                        "SKIPPED",
                        "NO_LOGIN",
                        "There is no Verigence login yet. Create or link it first.",
                    )
                )
                continue
            if provisioner is None:
                raise dependency_unavailable("The login service is not configured.")
            password = generate_initial_password()
            try:
                login_email = provisioner.set_password(
                    user_id=str(emp["security_user_id"]), password=password
                )
            except ProvisioningError as exc:
                if exc.code == "LOGIN_NOT_ACTIVE":
                    msg = "The login is waiting for SuperAdmin to allow it (Users, Pending Approvals)."
                    _log(conn, eid, body.template, "SKIPPED", exc.code, user.user_id)
                    results.append(_result(emp, "SKIPPED", exc.code, msg))
                else:
                    _log(conn, eid, body.template, "FAILED", exc.code, user.user_id)
                    results.append(
                        _result(
                            emp,
                            "FAILED",
                            exc.code,
                            "The temporary password could not be set. Nothing was sent.",
                        )
                    )
                continue
            values["login_id"] = login_email or emp["personal_email"]
            values["temp_password"] = password
        try:
            mailer.send(
                to=emp["personal_email"],
                subject=tpl.render(subject, values),
                body=tpl.render(text_body, values),
            )
        except MailError as exc:
            _log(conn, eid, body.template, "FAILED", exc.code, user.user_id)
            note = (
                "The password was reset but the email did not go. Send it again."
                if body.template == tpl.WELCOME
                else "The email did not go."
            )
            results.append(_result(emp, "FAILED", exc.code, note))
            continue
        _log(conn, eid, body.template, "SENT", None, user.user_id)
        record_audit(
            conn,
            actor_user_id=user.user_id,
            action="WELCOME_EMAIL_SENT" if body.template == tpl.WELCOME else "EMAIL_SENT",
            entity_type="employee",
            entity_id=eid,
            changes={"template": body.template},
            request=request,
        )
        results.append(_result(emp, "SENT"))
    return {"results": results}


@router.get("/log")
def log(
    employee_id: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    _: HumanPrincipal = Depends(can_send),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    eid = _uuid(employee_id) if employee_id else None
    rows = conn.execute(
        text(
            "SELECT l.log_id, l.employee_id, e.employee_code, e.full_name, l.channel, l.template_code, l.status,"
            " l.reason_code, l.sent_by, l.sent_at FROM hr.message_log l"
            " JOIN hr.employee e ON e.employee_id = l.employee_id"
            " WHERE (CAST(:e AS uuid) IS NULL OR l.employee_id = CAST(:e AS uuid))"
            " ORDER BY l.log_id DESC LIMIT :n"
        ),
        {"e": eid, "n": limit},
    ).mappings()
    return {
        "items": [
            {
                "logId": r["log_id"],
                "employeeId": str(r["employee_id"]),
                "employeeCode": r["employee_code"],
                "employeeName": r["full_name"],
                "channel": r["channel"],
                "template": r["template_code"],
                "status": r["status"],
                "reason": r["reason_code"],
                "sentBy": r["sent_by"],
                "sentAt": r["sent_at"].isoformat(),
            }
            for r in rows
        ]
    }
