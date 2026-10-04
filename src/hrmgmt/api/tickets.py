"""Feedback & Support: an employee raises a ticket (a summary, the issue and up to five files of
10 MB each), SuperAdmin answers, and either side can add to the conversation until it is closed.
The sender's name and email are kept on the ticket as they were when it was raised."""

from __future__ import annotations

import re
import uuid
from datetime import timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, text

from hrmgmt import permissions as perm
from hrmgmt.api.attendance import get_clock, get_storage
from hrmgmt.api.employees import _own_employee_id, _uuid
from hrmgmt.audit import record_audit
from hrmgmt.authz import Authorizer
from hrmgmt.db import get_conn
from hrmgmt.errors import ApiError, dependency_unavailable, not_found
from hrmgmt.principal import current_user, get_authorizer, has_permission, require_permission
from hrmgmt.security import HumanPrincipal
from hrmgmt.storage import ObjectStorage, StorageError
from hrmgmt.timeutil import Clock

router = APIRouter(prefix="/hr/v1", tags=["Feedback and support"])

can_support = require_permission(perm.HR_SUPPORT_MANAGE)

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_FILES = 5
MAX_TICKETS_PER_DAY = 10
MAX_MESSAGES_PER_DAY = 40
# Served as they are only when they cannot run in the browser; everything else downloads as bytes.
_SAFE_TYPES = {
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
    "application/pdf",
    "text/plain",
}

Status = Literal["OPEN", "IN_PROGRESS", "CLOSED"]


def _file_name(raw: str | None) -> str:
    name = re.sub(r"[^\w.\- ()]", "_", (raw or "").replace("\\", "/").split("/")[-1]).strip()
    return name[:120] or "attachment"


async def _read_files(files: list[UploadFile]) -> list[tuple[bytes, str, str]]:
    sent = [f for f in files if f.filename or f.size]
    if len(sent) > MAX_FILES:
        raise ApiError(422, "TICKET_FILE_INVALID", f"Attach at most {MAX_FILES} files.")
    out = []
    for f in sent:
        data = await f.read(MAX_FILE_BYTES + 1)
        name = _file_name(f.filename)
        if not data:
            raise ApiError(422, "TICKET_FILE_INVALID", f"{name} is empty.")
        if len(data) > MAX_FILE_BYTES:
            raise ApiError(413, "TICKET_FILE_TOO_LARGE", f"{name} is larger than 10 MB.")
        out.append((data, name, (f.content_type or "application/octet-stream")[:100]))
    return out


def _store_files(
    conn: Connection,
    storage: ObjectStorage | None,
    ticket_id: str,
    message_id: str,
    files: list[tuple[bytes, str, str]],
) -> None:
    if not files:
        return
    if storage is None:
        raise dependency_unavailable("File storage is not configured.")
    for data, name, ctype in files:
        key = f"tickets/{ticket_id}/{uuid.uuid4().hex}"
        try:
            storage.put(key, data, ctype)
        except StorageError as exc:
            raise dependency_unavailable("A file could not be saved. Please try again.") from exc
        conn.execute(
            text(
                "INSERT INTO hr.ticket_file (ticket_id, message_id, file_key, file_name,"
                " content_type, size_bytes) VALUES (CAST(:t AS uuid), CAST(:m AS uuid), :k, :n, :c, :s)"
            ),
            {"t": ticket_id, "m": message_id, "k": key, "n": name, "c": ctype, "s": len(data)},
        )


def _add_message(
    conn: Connection, ticket_id: str, user_id: str, kind: str, name: str, body: str
) -> str:
    return str(
        conn.execute(
            text(
                "INSERT INTO hr.ticket_message (ticket_id, author_user_id, author_kind, author_name, body)"
                " VALUES (CAST(:t AS uuid), :u, :k, :n, :b) RETURNING message_id"
            ),
            {"t": ticket_id, "u": user_id, "k": kind, "n": name, "b": body},
        ).scalar_one()
    )


def _row(r: Any) -> dict[str, Any]:
    return {
        "ticketId": str(r["ticket_id"]),
        "ticketNo": r["ticket_no"],
        "summary": r["summary"],
        "employeeId": str(r["employee_id"]),
        "employeeCode": r["employee_code"],
        "employeeName": r["employee_name"],
        "employeeEmail": r["employee_email"],
        "page": r["page_path"],
        "status": r["status"],
        "adminNote": r["admin_note"],
        "createdAt": r["created_at"].isoformat(),
        "updatedAt": r["updated_at"].isoformat(),
        "fileCount": r["file_count"],
        "messageCount": r["message_count"],
    }


_LIST_SQL = """
    SELECT t.*,
           (SELECT count(*) FROM hr.ticket_file f WHERE f.ticket_id = t.ticket_id) AS file_count,
           (SELECT count(*) FROM hr.ticket_message m WHERE m.ticket_id = t.ticket_id) AS message_count
    FROM hr.ticket t
"""


@router.post("/me/tickets", status_code=201)
async def raise_ticket(
    request: Request,
    summary: Annotated[str, Form(max_length=150)],
    issue: Annotated[str, Form(max_length=4000)],
    page: Annotated[str | None, Form(max_length=200)] = None,
    files: list[UploadFile] = File(default_factory=list),
    user: HumanPrincipal = Depends(current_user),
    clock: Clock = Depends(get_clock),
    storage: ObjectStorage | None = Depends(get_storage),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    summary, issue = " ".join(summary.split()), issue.strip()
    if not summary:
        raise ApiError(422, "TICKET_SUMMARY_REQUIRED", "Give the ticket a short summary.")
    if not issue:
        raise ApiError(422, "TICKET_ISSUE_REQUIRED", "Please describe the issue or feedback.")
    employee_id = _own_employee_id(conn, user)
    person = (
        conn.execute(
            text(
                "SELECT employee_code, full_name, personal_email FROM hr.employee"
                " WHERE employee_id = CAST(:e AS uuid)"
            ),
            {"e": employee_id},
        )
        .mappings()
        .one()
    )
    recent = conn.execute(
        text(
            "SELECT count(*) FROM hr.ticket WHERE employee_id = CAST(:e AS uuid)"
            " AND created_at > :since"
        ),
        {"e": employee_id, "since": clock() - timedelta(days=1)},
    ).scalar_one()
    if recent >= MAX_TICKETS_PER_DAY:
        raise ApiError(
            429, "TICKET_LIMIT", "You have raised several tickets today. Please try again tomorrow."
        )
    attached = await _read_files(files)
    ticket_id = str(
        conn.execute(
            text(
                "INSERT INTO hr.ticket (employee_id, employee_code, employee_name, employee_email,"
                " summary, page_path) VALUES (CAST(:e AS uuid), :c, :n, :m, :s, :p)"
                " RETURNING ticket_id"
            ),
            {
                "e": employee_id,
                "c": person["employee_code"],
                "n": person["full_name"],
                "m": person["personal_email"],
                "s": summary,
                "p": (page or "").strip() or None,
            },
        ).scalar_one()
    )
    message_id = _add_message(conn, ticket_id, user.user_id, "EMPLOYEE", person["full_name"], issue)
    _store_files(conn, storage, ticket_id, message_id, attached)
    ticket_no = conn.execute(
        text("SELECT ticket_no FROM hr.ticket WHERE ticket_id = CAST(:t AS uuid)"), {"t": ticket_id}
    ).scalar_one()
    return {"ticketId": ticket_id, "ticketNo": ticket_no}


@router.get("/me/tickets")
def my_tickets(
    user: HumanPrincipal = Depends(current_user), conn: Connection = Depends(get_conn)
) -> dict[str, Any]:
    employee_id = _own_employee_id(conn, user)
    rows = (
        conn.execute(
            text(
                _LIST_SQL
                + " WHERE t.employee_id = CAST(:e AS uuid) ORDER BY t.created_at DESC LIMIT 200"
            ),
            {"e": employee_id},
        )
        .mappings()
        .all()
    )
    return {"items": [_row(r) for r in rows]}


@router.get("/tickets")
def all_tickets(
    status: Annotated[Status | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    _: HumanPrincipal = Depends(can_support),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    counts = dict(
        conn.execute(text("SELECT status, count(*) FROM hr.ticket GROUP BY status")).all()
    )
    rows = (
        conn.execute(
            text(
                _LIST_SQL + " WHERE (CAST(:s AS text) IS NULL OR t.status = :s)"
                " ORDER BY t.created_at DESC LIMIT :limit OFFSET :offset"
            ),
            {"s": status, "limit": limit, "offset": offset},
        )
        .mappings()
        .all()
    )
    return {
        "total": sum(counts.values()),
        "open": counts.get("OPEN", 0) + counts.get("IN_PROGRESS", 0),
        "items": [_row(r) for r in rows],
    }


def _visible_ticket(
    conn: Connection,
    authorizer: Authorizer,
    user: HumanPrincipal,
    ticket_id: str,
) -> tuple[Any, bool]:
    """(the ticket row, True when the caller is support). 404 for anyone who may not see it."""
    row = (
        conn.execute(text(_LIST_SQL + " WHERE t.ticket_id = CAST(:t AS uuid)"), {"t": ticket_id})
        .mappings()
        .first()
    )
    if row is None:
        raise not_found("Ticket not found.")
    support = has_permission(authorizer, user, perm.HR_SUPPORT_MANAGE)
    if support:
        return row, True
    own = conn.execute(
        text(
            "SELECT 1 FROM hr.employee WHERE employee_id = CAST(:e AS uuid)"
            " AND security_user_id = CAST(:u AS uuid)"
        ),
        {"e": str(row["employee_id"]), "u": _uuid(user.user_id)},
    ).first()
    if own is None:
        raise not_found("Ticket not found.")
    return row, False


@router.get("/tickets/{ticket_id}")
def ticket_detail(
    ticket_id: str,
    user: HumanPrincipal = Depends(current_user),
    authorizer: Authorizer = Depends(get_authorizer),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    tid = _uuid(ticket_id)
    row, support = _visible_ticket(conn, authorizer, user, tid)
    files: dict[str, list[dict[str, Any]]] = {}
    for f in conn.execute(
        text(
            "SELECT file_id, message_id, file_name, size_bytes FROM hr.ticket_file"
            " WHERE ticket_id = CAST(:t AS uuid) ORDER BY created_at"
        ),
        {"t": tid},
    ).mappings():
        files.setdefault(str(f["message_id"]), []).append(
            {"fileId": str(f["file_id"]), "fileName": f["file_name"], "sizeBytes": f["size_bytes"]}
        )
    messages = [
        {
            "messageId": str(m["message_id"]),
            "authorKind": m["author_kind"],
            "authorName": m["author_name"],
            "body": m["body"],
            "createdAt": m["created_at"].isoformat(),
            "files": files.get(str(m["message_id"]), []),
        }
        for m in conn.execute(
            text(
                "SELECT message_id, author_kind, author_name, body, created_at FROM hr.ticket_message"
                " WHERE ticket_id = CAST(:t AS uuid) ORDER BY created_at, message_id"
            ),
            {"t": tid},
        ).mappings()
    ]
    return {**_row(row), "isSupport": support, "messages": messages}


@router.post("/tickets/{ticket_id}/messages", status_code=201)
async def add_reply(
    ticket_id: str,
    body: Annotated[str, Form(max_length=4000)],
    files: list[UploadFile] = File(default_factory=list),
    user: HumanPrincipal = Depends(current_user),
    authorizer: Authorizer = Depends(get_authorizer),
    clock: Clock = Depends(get_clock),
    storage: ObjectStorage | None = Depends(get_storage),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    tid = _uuid(ticket_id)
    row, support = _visible_ticket(conn, authorizer, user, tid)
    body = body.strip()
    if not body:
        raise ApiError(422, "TICKET_MESSAGE_REQUIRED", "Write a message first.")
    recent = conn.execute(
        text(
            "SELECT count(*) FROM hr.ticket_message WHERE ticket_id = CAST(:t AS uuid)"
            " AND author_user_id = :u AND created_at > :since"
        ),
        {"t": tid, "u": user.user_id, "since": clock() - timedelta(days=1)},
    ).scalar_one()
    if recent >= MAX_MESSAGES_PER_DAY:
        raise ApiError(429, "TICKET_LIMIT", "Too many messages today. Please try again tomorrow.")
    attached = await _read_files(files)
    name = "Support"
    if not support:
        name = row["employee_name"]
    else:
        mine = conn.execute(
            text("SELECT full_name FROM hr.employee WHERE security_user_id = CAST(:u AS uuid)"),
            {"u": _uuid(user.user_id)},
        ).first()
        name = mine[0] if mine else "Support"
    message_id = _add_message(
        conn, tid, user.user_id, "SUPPORT" if support else "EMPLOYEE", name, body
    )
    _store_files(conn, storage, tid, message_id, attached)
    # An answer from the employee reopens a closed ticket; a first answer from support starts work on it.
    new_status = row["status"]
    if not support and row["status"] == "CLOSED":
        new_status = "OPEN"
    elif support and row["status"] == "OPEN":
        new_status = "IN_PROGRESS"
    conn.execute(
        text(
            "UPDATE hr.ticket SET status = :s, updated_at = now() WHERE ticket_id = CAST(:t AS uuid)"
        ),
        {"s": new_status, "t": tid},
    )
    return {"messageId": message_id, "status": new_status}


class TicketUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    status: Status
    admin_note: str | None = Field(default=None, alias="adminNote", max_length=2000)


@router.patch("/tickets/{ticket_id}")
def update_ticket(
    ticket_id: str,
    body: TicketUpdate,
    request: Request,
    user: HumanPrincipal = Depends(can_support),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    tid = _uuid(ticket_id)
    row = conn.execute(
        text(
            "UPDATE hr.ticket SET status = :s, admin_note = :n, updated_at = now(), updated_by = :u"
            " WHERE ticket_id = CAST(:t AS uuid) RETURNING status, admin_note"
        ),
        {
            "s": body.status,
            "n": (body.admin_note or "").strip() or None,
            "u": user.user_id,
            "t": tid,
        },
    ).first()
    if row is None:
        raise not_found("Ticket not found.")
    record_audit(
        conn,
        actor_user_id=user.user_id,
        action="TICKET_UPDATED",
        entity_type="ticket",
        entity_id=tid,
        changes={"status": body.status},
        request=request,
    )
    return {"ticketId": tid, "status": row[0], "adminNote": row[1]}


@router.get("/tickets/{ticket_id}/files/{file_id}")
def ticket_file(
    ticket_id: str,
    file_id: str,
    user: HumanPrincipal = Depends(current_user),
    authorizer: Authorizer = Depends(get_authorizer),
    storage: ObjectStorage | None = Depends(get_storage),
    conn: Connection = Depends(get_conn),
) -> Response:
    tid, fid = _uuid(ticket_id), _uuid(file_id)
    _visible_ticket(conn, authorizer, user, tid)
    rec = (
        conn.execute(
            text(
                "SELECT file_key, file_name, content_type FROM hr.ticket_file"
                " WHERE file_id = CAST(:f AS uuid) AND ticket_id = CAST(:t AS uuid)"
            ),
            {"f": fid, "t": tid},
        )
        .mappings()
        .first()
    )
    if rec is None:
        raise not_found("File not found.")
    if storage is None:
        raise dependency_unavailable("File storage is not configured.")
    try:
        data = storage.get(rec["file_key"])
    except StorageError as exc:
        raise dependency_unavailable("The file could not be loaded.") from exc
    ctype = (
        rec["content_type"] if rec["content_type"] in _SAFE_TYPES else "application/octet-stream"
    )
    return Response(
        content=data,
        media_type=ctype,
        headers={
            "Content-Disposition": f'attachment; filename="{rec["file_name"]}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )
