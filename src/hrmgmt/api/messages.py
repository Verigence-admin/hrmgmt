from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import Connection, text

from hrmgmt import message_templates as tpl
from hrmgmt import permissions as perm
from hrmgmt import settings_store as cfg
from hrmgmt import validators as v
from hrmgmt.api.employees import _uuid, get_provisioner
from hrmgmt.audit import record_audit
from hrmgmt.db import get_conn
from hrmgmt.email_html import inline_images, welcome_html
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
    # Verigence user ids (the Users group), whether or not the person is an employee.
    user_ids: list[str] = Field(min_length=1, max_length=BATCH_LIMIT)
    # A one-off wording for this send (GENERAL only). The saved template is not changed.
    subject: str | None = Field(default=None, max_length=200)
    body: str | None = Field(default=None, max_length=5000)


def _log(
    conn: Connection,
    user_id: str,
    name: str | None,
    code: str,
    status: str,
    reason: str | None,
    by: str,
) -> None:
    employee_id = conn.execute(
        text("SELECT employee_id FROM hr.employee WHERE security_user_id = CAST(:u AS uuid)"),
        {"u": user_id},
    ).scalar_one_or_none()
    conn.execute(
        text(
            "INSERT INTO hr.message_log (employee_id, user_id, recipient_name, channel, template_code,"
            " status, reason_code, sent_by) VALUES (CAST(:e AS uuid), CAST(:u AS uuid), :n, 'EMAIL',"
            " :t, :s, :r, :by)"
        ),
        {"e": employee_id, "u": user_id, "n": name, "t": code, "s": status, "r": reason, "by": by},
    )


def _result(
    user_id: str, name: str | None, status: str, code: str | None = None, message: str | None = None
) -> dict[str, Any]:
    return {"userId": user_id, "name": name, "status": status, "code": code, "message": message}


def _shared_values(conn: Connection) -> dict[str, str]:
    settings = cfg.load_all(conn)
    return {
        "company": str(settings.get("company.name") or "").strip() or "Verigence",
        "sign_in_link": str(settings.get("email.sign_in_url") or ""),
        "app_link": str(settings.get("email.app_download_url") or ""),
    }


def _wording(conn: Connection, body: SendIn | TestIn) -> tuple[str, str]:
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
    return subject, text_body


def _designed(template: str, values: dict[str, str]) -> dict[str, Any]:
    """The Welcome email also goes out as a designed page with pictures; the text above it stays
    as the plain version for mail apps that cannot show a page."""
    if template != tpl.WELCOME:
        return {}
    return {"html_body": welcome_html(values), "images": inline_images()}


def _need_provisioner(provisioner: UserProvisioner | None) -> UserProvisioner:
    if provisioner is None:
        raise dependency_unavailable("The login service is not configured.")
    return provisioner


@router.get("/recipients")
def recipients(
    q: Annotated[str | None, Query(max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    _: HumanPrincipal = Depends(can_send),
    provisioner: UserProvisioner | None = Depends(get_provisioner),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    """The Verigence Users group, for choosing who to message. Employees are tagged, not required."""
    try:
        users = _need_provisioner(provisioner).list_users(q=q, limit=limit, offset=offset)
    except ProvisioningError as exc:
        raise dependency_unavailable(
            f"The user list could not be loaded ({exc.code}). Please try again."
        ) from exc
    linked = {
        str(r["security_user_id"]): str(r["employee_id"])
        for r in conn.execute(
            text(
                "SELECT employee_id, security_user_id FROM hr.employee"
                " WHERE security_user_id = ANY(CAST(:ids AS uuid[]))"
            ),
            {"ids": [u.user_id for u in users]},
        ).mappings()
    }
    return {
        "items": [
            {
                "userId": u.user_id,
                "displayName": u.display_name,
                "email": u.email,
                "status": u.status,
                "isEmployee": u.is_employee,
                "employeeId": linked.get(u.user_id),
            }
            for u in users
        ]
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
    """Sends to each chosen Verigence user, one attempt each. A WELCOME message first sets a fresh
    temporary password for the user (Security refuses a user SuperAdmin has not allowed yet), then
    emails it. The password is never stored, logged or audited."""
    mailer = senders.get(body.channel)
    if mailer is None:
        raise ApiError(
            503,
            "MESSAGE_CHANNEL_NOT_AVAILABLE",
            "Email is not set up yet. Ask your administrator."
            if body.channel == "EMAIL"
            else "WhatsApp is not set up yet.",
        )
    ids = list(dict.fromkeys(_uuid(i) for i in body.user_ids))
    subject, text_body = _wording(conn, body)
    shared = _shared_values(conn)
    prov = _need_provisioner(provisioner)
    try:
        found = {u.user_id: u for u in prov.list_users(ids=ids, limit=len(ids))}
    except ProvisioningError as exc:
        raise dependency_unavailable(
            f"The users could not be looked up ({exc.code}). Please try again."
        ) from exc
    results: list[dict[str, Any]] = []
    for uid in ids:
        person = found.get(uid)
        if person is None:
            results.append(_result(uid, None, "SKIPPED", "NOT_FOUND", "That user was not found."))
            continue
        name = person.display_name
        if person.status not in ("ACTIVE", "PENDING"):
            _log(conn, uid, name, body.template, "SKIPPED", "NOT_ACTIVE", user.user_id)
            results.append(_result(uid, name, "SKIPPED", "NOT_ACTIVE", "That user is not active."))
            continue
        if not person.email:
            _log(conn, uid, name, body.template, "SKIPPED", "NO_EMAIL", user.user_id)
            results.append(
                _result(uid, name, "SKIPPED", "NO_EMAIL", "That user has no email address.")
            )
            continue
        values = {**shared, "name": name or "there", "login_id": person.email}
        if body.template == tpl.WELCOME:
            password = generate_initial_password()
            try:
                login_email = prov.set_password(user_id=uid, password=password)
            except ProvisioningError as exc:
                if exc.code == "LOGIN_NOT_ACTIVE":
                    msg = "Waiting for SuperAdmin to allow this user (Users, Pending Approvals)."
                    _log(conn, uid, name, body.template, "SKIPPED", exc.code, user.user_id)
                    results.append(_result(uid, name, "SKIPPED", exc.code, msg))
                else:
                    _log(conn, uid, name, body.template, "FAILED", exc.code, user.user_id)
                    results.append(
                        _result(
                            uid,
                            name,
                            "FAILED",
                            exc.code,
                            "The temporary password could not be set. Nothing was sent.",
                        )
                    )
                continue
            values["login_id"] = login_email or person.email
            values["temp_password"] = password
        try:
            mailer.send(
                to=person.email,
                subject=tpl.render(subject, values),
                body=tpl.render(text_body, values),
                **_designed(body.template, values),
            )
        except MailError as exc:
            _log(conn, uid, name, body.template, "FAILED", exc.code, user.user_id)
            note = (
                "The password was reset but the email did not go. Send it again."
                if body.template == tpl.WELCOME
                else "The email did not go."
            )
            results.append(_result(uid, name, "FAILED", exc.code, note))
            continue
        _log(conn, uid, name, body.template, "SENT", None, user.user_id)
        record_audit(
            conn,
            actor_user_id=user.user_id,
            action="WELCOME_EMAIL_SENT" if body.template == tpl.WELCOME else "EMAIL_SENT",
            entity_type="user",
            entity_id=uid,
            changes={"template": body.template},
            request=request,
        )
        results.append(_result(uid, name, "SENT"))
    return {"results": results}


class TestIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    channel: Literal["EMAIL", "WHATSAPP"] = "EMAIL"
    template: Literal["GENERAL", "WELCOME"]
    to: str = Field(max_length=320)
    subject: str | None = Field(default=None, max_length=200)
    body: str | None = Field(default=None, max_length=5000)

    @field_validator("to")
    @classmethod
    def _to(cls, value: str) -> str:
        return v.clean_email(value)


TEST_PASSWORD = "TEST-ONLY-NOT-A-REAL-PASSWORD"


@router.post("/test")
def send_test(
    body: TestIn,
    request: Request,
    user: HumanPrincipal = Depends(can_send),
    senders: dict[str, Mailer] = Depends(get_senders),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    """Sends the message to one address HR types, filled with sample values and a clearly fake
    password. No login is touched and nothing is written to the send history."""
    mailer = senders.get(body.channel)
    if mailer is None:
        raise ApiError(
            503,
            "MESSAGE_CHANNEL_NOT_AVAILABLE",
            "Email is not set up yet. Ask your administrator."
            if body.channel == "EMAIL"
            else "WhatsApp is not set up yet.",
        )
    subject, text_body = _wording(conn, body)
    values = {
        **_shared_values(conn),
        "name": "Test Person",
        "login_id": body.to,
        "temp_password": TEST_PASSWORD,
    }
    try:
        mailer.send(
            to=body.to,
            subject="[TEST] " + tpl.render(subject, values),
            body="This is a test. No login was changed.\n\n" + tpl.render(text_body, values),
            **_designed(body.template, values),
        )
    except MailError as exc:
        return {"status": "FAILED", "code": exc.code, "message": "The test email did not go."}
    record_audit(
        conn,
        actor_user_id=user.user_id,
        action="MESSAGE_TEST_SENT",
        entity_type="message_template",
        entity_id=body.template,
        changes={"template": body.template},
        request=request,
    )
    return {"status": "SENT", "code": None, "message": None}


@router.get("/log")
def log(
    user_id: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    _: HumanPrincipal = Depends(can_send),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    uid = _uuid(user_id) if user_id else None
    rows = conn.execute(
        text(
            "SELECT l.log_id, l.user_id, l.employee_id, l.recipient_name, l.channel, l.template_code,"
            " l.status, l.reason_code, l.sent_by, l.sent_at FROM hr.message_log l"
            " WHERE (CAST(:u AS uuid) IS NULL OR l.user_id = CAST(:u AS uuid))"
            " ORDER BY l.log_id DESC LIMIT :n"
        ),
        {"u": uid, "n": limit},
    ).mappings()
    return {
        "items": [
            {
                "logId": r["log_id"],
                "userId": str(r["user_id"]) if r["user_id"] else None,
                "employeeId": str(r["employee_id"]) if r["employee_id"] else None,
                "name": r["recipient_name"],
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
