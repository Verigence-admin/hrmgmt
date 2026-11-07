"""The holiday calendar: HR or SuperAdmin (hr.settings.manage) adds, changes and removes holidays;
every signed-in person reads the ones that apply to them. What a holiday means for attendance,
leave and payroll is decided in hrmgmt.holidays (product owner, 7 Nov 2026)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, text

from hrmgmt import holidays as hol
from hrmgmt import permissions as perm
from hrmgmt.api.attendance import get_clock
from hrmgmt.audit import record_audit
from hrmgmt.authz import Authorizer, AuthzUnavailableError
from hrmgmt.db import get_conn
from hrmgmt.errors import ApiError, conflict, not_found
from hrmgmt.principal import current_user, get_authorizer, require_permission
from hrmgmt.security import HumanPrincipal
from hrmgmt.timeutil import Clock

router = APIRouter(prefix="/hr/v1", tags=["Holidays"])

can_manage = require_permission(perm.HR_SETTINGS_MANAGE)

DayPart = Literal["FULL_DAY", "MORNING", "AFTERNOON"]
Kind = Literal["HOLIDAY", "WORKING_DAY"]
AppliesTo = Literal["ALL", "SELECTED"]
Status = Literal["TENTATIVE", "DECLARED"]
NOTE = "Tentative dates are not final. Final holidays are declared by HR."
_PART_WORDS = {"FULL_DAY": "full day", "MORNING": "morning off", "AFTERNOON": "afternoon off"}


def _what(r: Any) -> str:
    """How the clash message names an entry: "full day", "morning off", "working Sunday"."""
    return "working Sunday" if r["kind"] == hol.WORKING_DAY else _PART_WORDS[r["day_part"]]


# ---- reading ---------------------------------------------------------------------------------


def _scope_rows(conn: Connection, ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {i: [] for i in ids}
    if not ids:
        return out
    for r in conn.execute(
        text(
            "SELECT holiday_id, tenant_id, project_code, project_name, outlet_id, outlet_code,"
            " outlet_name FROM hr.holiday_scope WHERE holiday_id = ANY(CAST(:ids AS uuid[]))"
            " ORDER BY project_name NULLS LAST, tenant_id, outlet_id IS NOT NULL, outlet_name"
        ),
        {"ids": ids},
    ).mappings():
        out[str(r["holiday_id"])].append(
            {
                "tenantId": r["tenant_id"],
                "projectCode": r["project_code"],
                "projectName": r["project_name"],
                "outletId": str(r["outlet_id"]) if r["outlet_id"] else None,
                "outletCode": r["outlet_code"],
                "outletName": r["outlet_name"],
            }
        )
    return out


def _item(r: Any, scope: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "holidayId": str(r["holiday_id"]),
        "date": r["holiday_date"].isoformat(),
        "name": r["name"],
        "status": r["status"],
        "dayPart": r["day_part"],
        # HOLIDAY, or WORKING_DAY: a Sunday made a working day (7 Nov 2026).
        "kind": r["kind"],
        "appliesTo": r["applies_to"],
        "scope": scope,
        "updatedAt": r["updated_at"].isoformat() if r["updated_at"] else None,
    }


def _live(conn: Connection, where: str, params: dict[str, Any]) -> list[Any]:
    return list(
        conn.execute(
            text(
                "SELECT holiday_id, holiday_date, name, status, day_part, applies_to, kind, updated_at"
                f" FROM hr.holiday WHERE removed_at IS NULL AND {where}"
                " ORDER BY holiday_date, CASE day_part WHEN 'FULL_DAY' THEN 0"
                " WHEN 'MORNING' THEN 1 ELSE 2 END, applies_to, lower(name)"
            ),
            params,
        )
        .mappings()
        .all()
    )


def _one(conn: Connection, holiday_id: str, *, lock: bool = False) -> dict[str, Any] | None:
    row = (
        conn.execute(
            text(
                "SELECT holiday_id, holiday_date, name, status, day_part, applies_to, kind, updated_at"
                " FROM hr.holiday WHERE holiday_id = CAST(:h AS uuid) AND removed_at IS NULL"
                + (" FOR UPDATE" if lock else "")
            ),
            {"h": holiday_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    return _item(row, _scope_rows(conn, [holiday_id])[holiday_id])


def _is_manager(authorizer: Authorizer, user: HumanPrincipal) -> bool:
    try:
        return authorizer.is_allowed(user_id=user.user_id, permission_key=perm.HR_SETTINGS_MANAGE)
    except AuthzUnavailableError:
        return False  # the list still loads; it shows what applies to the caller


def _caller_user_id(conn: Connection, user: HumanPrincipal) -> str | None:
    """The caller's Security user id when they are an active employee, else None (no assignments)."""
    try:
        uid = str(uuid.UUID(user.user_id))
    except ValueError:
        return None
    row = conn.execute(
        text(
            "SELECT 1 FROM hr.employee WHERE security_user_id = CAST(:u AS uuid)"
            " AND employment_status = 'ACTIVE'"
        ),
        {"u": uid},
    ).first()
    return uid if row else None


@router.get("/holidays")
def list_holidays(
    year: Annotated[int | None, Query(ge=2020, le=2100)] = None,
    mine: Annotated[bool, Query()] = False,
    include_working_days: Annotated[bool, Query(alias="includeWorkingDays")] = False,
    user: HumanPrincipal = Depends(current_user),
    authorizer: Authorizer = Depends(get_authorizer),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    """Every signed-in person. HR (hr.settings.manage) sees every holiday with who it applies to;
    everyone else, and HR with mine=true, sees only the holidays that apply to them on their date.
    A TENTATIVE date is only a proposal: only declared ones count as non-working days.

    Each item keeps date, name and status as before, with holidayId, dayPart, appliesTo, kind and
    scope. Working Sundays (kind WORKING_DAY) are listed only with includeWorkingDays=true, so an
    earlier app version never shows one as a holiday."""
    rows = _live(
        conn,
        "(CAST(:y AS int) IS NULL OR extract(year FROM holiday_date) = :y)"
        " AND (CAST(:w AS boolean) OR kind = 'HOLIDAY')",
        {"y": year, "w": include_working_days},
    )
    manager = not mine and _is_manager(authorizer, user)
    if not manager and rows:
        user_id = _caller_user_id(conn, user)
        selected = [r for r in rows if r["applies_to"] == hol.SELECTED]
        keep_ids = {str(r["holiday_id"]) for r in rows if r["applies_to"] == hol.ALL}
        if selected and user_id:
            scopes: dict[str, list[tuple[str, str | None]]] = {
                str(r["holiday_id"]): [] for r in selected
            }
            for hid, tenant, outlet in conn.execute(
                text(
                    "SELECT holiday_id, tenant_id, outlet_id FROM hr.holiday_scope"
                    " WHERE holiday_id = ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": list(scopes)},
            ):
                scopes[str(hid)].append((str(tenant), str(outlet) if outlet else None))
            first = min(r["holiday_date"] for r in selected)
            last = max(r["holiday_date"] for r in selected)
            mine_assignments = hol.assignments_between(conn, [user_id], first, last).get(
                user_id, []
            )
            for r in selected:
                on_day = [a for a in mine_assignments if a.valid_on(r["holiday_date"])]
                if hol.matches(tuple(scopes[str(r["holiday_id"])]), on_day):
                    keep_ids.add(str(r["holiday_id"]))
        rows = [r for r in rows if str(r["holiday_id"]) in keep_ids]
    scope = _scope_rows(conn, [str(r["holiday_id"]) for r in rows]) if manager else {}
    return {
        "items": [_item(r, scope.get(str(r["holiday_id"]), [])) for r in rows],
        "note": NOTE,
    }


@router.get("/holidays/scope-options")
def scope_options(
    _: HumanPrincipal = Depends(can_manage),
    clock: Clock = Depends(get_clock),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    """The projects and their outlets a holiday can be given to, from HR's copy of the work
    context (assignments valid now)."""
    now = clock()
    projects: dict[str, dict[str, Any]] = {}
    for r in conn.execute(
        text(
            "SELECT tenant_id, max(project_code) AS project_code, max(project_name) AS project_name,"
            " outlet_id, max(outlet_code) AS outlet_code, max(outlet_name) AS outlet_name,"
            " count(DISTINCT security_user_id) AS people"
            " FROM hr.work_assignment WHERE valid_from <= :n AND (valid_to IS NULL OR valid_to > :n)"
            " GROUP BY tenant_id, outlet_id"
        ),
        {"n": now},
    ).mappings():
        p = projects.setdefault(
            r["tenant_id"],
            {
                "tenantId": r["tenant_id"],
                "projectCode": r["project_code"],
                "projectName": r["project_name"],
                "outlets": [],
            },
        )
        p["projectCode"] = p["projectCode"] or r["project_code"]
        p["projectName"] = p["projectName"] or r["project_name"]
        if r["outlet_id"] is not None:
            p["outlets"].append(
                {
                    "outletId": str(r["outlet_id"]),
                    "outletCode": r["outlet_code"],
                    "outletName": r["outlet_name"],
                }
            )
    items = sorted(projects.values(), key=lambda p: str(p["projectName"] or p["tenantId"]).lower())
    for p in items:
        p["outlets"].sort(key=lambda o: str(o["outletName"] or o["outletCode"] or "").lower())
    return {"projects": items}


# ---- changing --------------------------------------------------------------------------------


class ScopeIn(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    tenant_id: str = Field(alias="tenantId", min_length=1, max_length=120)
    outlet_id: uuid.UUID | None = Field(default=None, alias="outletId")


class HolidayEntryIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, populate_by_name=True)
    holiday_date: date = Field(alias="date")
    name: str = Field(min_length=2, max_length=120)
    status: Status = "TENTATIVE"
    day_part: DayPart = Field(default="FULL_DAY", alias="dayPart")
    kind: Kind = "HOLIDAY"
    applies_to: AppliesTo = Field(default="ALL", alias="appliesTo")
    scope: list[ScopeIn] = Field(default_factory=list, max_length=500)


def _invalid(detail: str) -> ApiError:
    return ApiError(422, "HOLIDAY_INVALID", detail)


def _labelled_scope(conn: Connection, body: HolidayEntryIn) -> list[dict[str, Any]]:
    """Checks every chosen project and outlet against HR's copy of the work context and takes the
    names from there. A project chosen whole makes its outlets redundant; they are dropped."""
    if body.applies_to == hol.ALL:
        if body.scope:
            raise _invalid("A holiday for the whole company has no projects or outlets chosen.")
        return []
    if not body.scope:
        raise _invalid("Choose at least one project or outlet, or make it for the whole company.")
    whole = {s.tenant_id for s in body.scope if s.outlet_id is None}
    out: dict[tuple[str, str | None], dict[str, Any]] = {}
    for s in body.scope:
        outlet = str(s.outlet_id) if s.outlet_id else None
        if outlet is not None and s.tenant_id in whole:
            continue
        found = (
            conn.execute(
                text(
                    "SELECT max(project_code) AS project_code, max(project_name) AS project_name,"
                    " max(outlet_code) AS outlet_code, max(outlet_name) AS outlet_name, count(*) AS n"
                    " FROM hr.work_assignment WHERE tenant_id = :t"
                    " AND (CAST(:o AS uuid) IS NULL OR outlet_id = CAST(:o AS uuid))"
                ),
                {"t": s.tenant_id, "o": outlet},
            )
            .mappings()
            .one()
        )
        if not found["n"]:
            raise _invalid(
                "One of the chosen projects or outlets is not in the work context. Refresh the"
                " work context and choose again."
            )
        out[(s.tenant_id, outlet)] = {
            "tenantId": s.tenant_id,
            "projectCode": found["project_code"],
            "projectName": found["project_name"],
            "outletId": outlet,
            "outletCode": found["outlet_code"] if outlet else None,
            "outletName": found["outlet_name"] if outlet else None,
        }
    return list(out.values())


def _parts_overlap(a: str, b: str) -> bool:
    return a == hol.FULL_DAY or b == hol.FULL_DAY or a == b


def _scopes_overlap(
    a_all: bool, a: list[dict[str, Any]], b_all: bool, b: list[dict[str, Any]]
) -> bool:
    if a_all or b_all:
        return True
    for x in a:
        for y in b:
            if x["tenantId"] != y["tenantId"]:
                continue
            if x["outletId"] is None or y["outletId"] is None or x["outletId"] == y["outletId"]:
                return True
    return False


def _check_clash(
    conn: Connection,
    day: date,
    day_part: str,
    applies_to: str,
    scope: list[dict[str, Any]],
    exclude: str | None = None,
) -> None:
    """Two holidays covering the same half of the same date for the same people would be counted
    once anyway; the second is refused with the name of the first so HR can change that one."""
    others = [
        r for r in _live(conn, "holiday_date = :d", {"d": day}) if str(r["holiday_id"]) != exclude
    ]
    if not others:
        return
    scopes = _scope_rows(conn, [str(r["holiday_id"]) for r in others])
    for r in others:
        if not _parts_overlap(day_part, r["day_part"]):
            continue
        if _scopes_overlap(
            applies_to == hol.ALL,
            scope,
            r["applies_to"] == hol.ALL,
            scopes[str(r["holiday_id"])],
        ):
            raise conflict(
                "HOLIDAY_CLASH",
                f"{r['name']} ({_what(r)}) already covers some of the same people"
                f" on {day:%d %b %Y}. Change or remove it instead.",
            )


def _write_scope(conn: Connection, holiday_id: str, scope: list[dict[str, Any]]) -> None:
    conn.execute(
        text("DELETE FROM hr.holiday_scope WHERE holiday_id = CAST(:h AS uuid)"), {"h": holiday_id}
    )
    for s in scope:
        conn.execute(
            text(
                "INSERT INTO hr.holiday_scope (holiday_id, tenant_id, project_code, project_name,"
                " outlet_id, outlet_code, outlet_name) VALUES (CAST(:h AS uuid), :t, :pc, :pn,"
                " CAST(:o AS uuid), :oc, :on)"
            ),
            {
                "h": holiday_id,
                "t": s["tenantId"],
                "pc": s["projectCode"],
                "pn": s["projectName"],
                "o": s["outletId"],
                "oc": s["outletCode"],
                "on": s["outletName"],
            },
        )


def _audit_view(item: dict[str, Any] | None) -> dict[str, Any] | None:
    if item is None:
        return None
    return {k: item[k] for k in ("date", "name", "status", "dayPart", "appliesTo", "kind", "scope")}


def _create(
    conn: Connection,
    request: Request,
    user: HumanPrincipal,
    *,
    day: date,
    name: str,
    status: str,
    day_part: str,
    applies_to: str,
    scope: list[dict[str, Any]],
    kind: str = hol.HOLIDAY,
) -> dict[str, Any]:
    _check_clash(conn, day, day_part, applies_to, scope)
    holiday_id = str(
        conn.execute(
            text(
                "INSERT INTO hr.holiday (holiday_date, name, status, day_part, applies_to, kind,"
                " created_by, updated_by) VALUES (:d, :n, :s, :p, :a, :k, :u, :u)"
                " RETURNING holiday_id"
            ),
            {
                "d": day,
                "n": name,
                "s": status,
                "p": day_part,
                "a": applies_to,
                "k": kind,
                "u": user.user_id,
            },
        ).scalar_one()
    )
    _write_scope(conn, holiday_id, scope)
    item = _one(conn, holiday_id)
    assert item is not None
    record_audit(
        conn,
        actor_user_id=user.user_id,
        action="HOLIDAY_ADDED",
        entity_type="holiday",
        entity_id=holiday_id,
        changes={"before": None, "after": _audit_view(item)},
        request=request,
    )
    return item


def _change(
    conn: Connection,
    request: Request,
    user: HumanPrincipal,
    before: dict[str, Any],
    *,
    day: date,
    name: str,
    status: str,
    day_part: str,
    applies_to: str,
    scope: list[dict[str, Any]],
    kind: str = hol.HOLIDAY,
) -> dict[str, Any]:
    holiday_id = before["holidayId"]
    _check_clash(conn, day, day_part, applies_to, scope, exclude=holiday_id)
    conn.execute(
        text(
            "UPDATE hr.holiday SET holiday_date = :d, name = :n, status = :s, day_part = :p,"
            " applies_to = :a, kind = :k, updated_at = now(), updated_by = :u"
            " WHERE holiday_id = CAST(:h AS uuid)"
        ),
        {
            "d": day,
            "n": name,
            "s": status,
            "p": day_part,
            "a": applies_to,
            "k": kind,
            "u": user.user_id,
            "h": holiday_id,
        },
    )
    _write_scope(conn, holiday_id, scope)
    after = _one(conn, holiday_id)
    assert after is not None
    if _audit_view(before) != _audit_view(after):
        record_audit(
            conn,
            actor_user_id=user.user_id,
            action="HOLIDAY_CHANGED",
            entity_type="holiday",
            entity_id=holiday_id,
            changes={"before": _audit_view(before), "after": _audit_view(after)},
            request=request,
        )
    return after


def _remove(
    conn: Connection, request: Request, user: HumanPrincipal, before: dict[str, Any], now: datetime
) -> None:
    conn.execute(
        text(
            "UPDATE hr.holiday SET removed_at = :n, removed_by = :u, updated_at = :n,"
            " updated_by = :u WHERE holiday_id = CAST(:h AS uuid)"
        ),
        {"n": now, "u": user.user_id, "h": before["holidayId"]},
    )
    record_audit(
        conn,
        actor_user_id=user.user_id,
        action="HOLIDAY_REMOVED",
        entity_type="holiday",
        entity_id=before["holidayId"],
        changes={"before": _audit_view(before), "after": None},
        request=request,
    )


def _kind_ok(body: HolidayEntryIn) -> None:
    """A working day is a whole Sunday, and it is either set or not (declared)."""
    if body.kind != hol.WORKING_DAY:
        return
    if body.holiday_date.weekday() != 6:
        raise _invalid("A working Sunday must be on a Sunday. Other days are working days already.")
    if body.day_part != hol.FULL_DAY:
        raise _invalid("A working Sunday is a whole working day; it has no half.")
    if body.status != hol.DECLARED:
        raise _invalid("A working Sunday is declared or not set at all; it cannot be tentative.")


def _year_ok(day: date) -> None:
    if not 2020 <= day.year <= 2100:
        raise _invalid("Choose a date from 2020 to 2100.")


@router.post("/holidays", status_code=201)
def add_holiday(
    body: HolidayEntryIn,
    request: Request,
    user: HumanPrincipal = Depends(can_manage),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    _year_ok(body.holiday_date)
    _kind_ok(body)
    scope = _labelled_scope(conn, body)
    return _create(
        conn,
        request,
        user,
        day=body.holiday_date,
        name=body.name,
        status=body.status,
        day_part=body.day_part,
        applies_to=body.applies_to,
        scope=scope,
        kind=body.kind,
    )


def _found(conn: Connection, holiday_id: str) -> dict[str, Any]:
    try:
        hid = str(uuid.UUID(holiday_id))
    except ValueError as exc:
        raise not_found("Holiday not found.") from exc
    before = _one(conn, hid, lock=True)
    if before is None:
        raise not_found("Holiday not found.")
    return before


@router.put("/holidays/entries/{holiday_id}")
def change_holiday(
    holiday_id: str,
    body: HolidayEntryIn,
    request: Request,
    user: HumanPrincipal = Depends(can_manage),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    before = _found(conn, holiday_id)
    _year_ok(body.holiday_date)
    _kind_ok(body)
    scope = _labelled_scope(conn, body)
    return _change(
        conn,
        request,
        user,
        before,
        day=body.holiday_date,
        name=body.name,
        status=body.status,
        day_part=body.day_part,
        applies_to=body.applies_to,
        scope=scope,
        kind=body.kind,
    )


@router.delete("/holidays/entries/{holiday_id}")
def remove_holiday(
    holiday_id: str,
    request: Request,
    user: HumanPrincipal = Depends(can_manage),
    clock: Clock = Depends(get_clock),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    """Removed, not deleted: the row stays with who removed it and when."""
    before = _found(conn, holiday_id)
    _remove(conn, request, user, before, clock())
    return {"holidayId": before["holidayId"], "date": before["date"], "removed": True}


# ---- the earlier routes, by date: a full-day holiday for the whole company -------------------


class HolidayIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=2, max_length=120)
    status: Status = "TENTATIVE"


def _company_full_day(conn: Connection, day: date) -> dict[str, Any] | None:
    rows = _live(
        conn,
        "holiday_date = :d AND applies_to = 'ALL' AND day_part = 'FULL_DAY' AND kind = 'HOLIDAY'",
        {"d": day},
    )
    return _one(conn, str(rows[0]["holiday_id"]), lock=True) if rows else None


@router.put("/holidays/{holiday_date}")
def put_holiday(
    holiday_date: date,
    body: HolidayIn,
    request: Request,
    user: HumanPrincipal = Depends(can_manage),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    """Kept for earlier app versions: sets the company-wide full-day holiday on a date."""
    _year_ok(holiday_date)
    before = _company_full_day(conn, holiday_date)
    fields: dict[str, Any] = {
        "day": holiday_date,
        "name": body.name,
        "status": body.status,
        "day_part": hol.FULL_DAY,
        "applies_to": hol.ALL,
        "scope": [],
    }
    item = (
        _change(conn, request, user, before, **fields)
        if before is not None
        else _create(conn, request, user, **fields)
    )
    return {
        "date": item["date"],
        "name": item["name"],
        "status": item["status"],
        "holidayId": item["holidayId"],
    }


@router.delete("/holidays/{holiday_date}")
def delete_holiday(
    holiday_date: date,
    request: Request,
    user: HumanPrincipal = Depends(can_manage),
    clock: Clock = Depends(get_clock),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    """Kept for earlier app versions: removes the company-wide full-day holiday on a date."""
    before = _company_full_day(conn, holiday_date)
    if before is None:
        raise not_found("Holiday not found.")
    _remove(conn, request, user, before, clock())
    return {"date": holiday_date.isoformat(), "removed": True}
