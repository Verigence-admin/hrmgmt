from __future__ import annotations

import json
from typing import Any

from fastapi import Request
from sqlalchemy import Connection, text


def client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for", "")
    first = forwarded.split(",")[0].strip()
    if first:
        return first[:64]
    return request.client.host if request.client else None


def record_audit(
    conn: Connection,
    *,
    actor_user_id: str,
    action: str,
    entity_type: str,
    entity_id: str,
    changes: dict[str, Any] | None = None,
    request: Request | None = None,
) -> int:
    """Append one audit row in the caller's transaction, so the action and its audit commit or
    fail together. `changes` carries field names and old/new values for ordinary fields; for a
    protected field (PAN, Aadhaar, bank account) pass only {"field": "changed"} or
    {"field": "revealed"}, never the value."""
    row = conn.execute(
        text(
            """
            INSERT INTO hr.audit_log
                (actor_user_id, action, entity_type, entity_id, changes, request_id, client_ip)
            VALUES (:actor, :action, :etype, :eid, CAST(:changes AS jsonb), :rid, :ip)
            RETURNING audit_id
            """
        ),
        {
            "actor": actor_user_id,
            "action": action,
            "etype": entity_type,
            "eid": entity_id,
            "changes": json.dumps(changes or {}, default=str),
            "rid": (request.headers.get("x-correlation-id", "")[:80] or None) if request else None,
            "ip": client_ip(request) if request else None,
        },
    ).scalar_one()
    return int(row)
