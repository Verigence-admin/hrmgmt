from __future__ import annotations

import json
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text

from hrmgmt import permissions as perm
from tests.support import OUTLET, World, ist, jpeg

MONTH = "2026-10"
UNCONFIRMED_STATUTORY = {
    "pf": {"enabled": False},
    "esi": {"enabled": False},
    "pt": {"enabled": False, "slabs": []},
}


@pytest.fixture()
def world(migrated_engine):
    w = World(migrated_engine, now=ist(2026, 10, 12, 11, 0))  # Monday 12 October 2026
    w.clean_assignments()
    with migrated_engine.begin() as conn:
        conn.execute(text("DELETE FROM hr.payslip"))
        conn.execute(text("UPDATE hr.claim SET payroll_run_id = NULL"))
        conn.execute(text("DELETE FROM hr.payroll_line"))
        conn.execute(text("DELETE FROM hr.payroll_run"))
        conn.execute(text("DELETE FROM hr.salary_structure"))
        conn.execute(text("DELETE FROM hr.claim_event"))
        conn.execute(text("DELETE FROM hr.claim_receipt"))
        conn.execute(text("DELETE FROM hr.claim"))
        conn.execute(text("DELETE FROM hr.setting"))
        conn.execute(
            text(
                "UPDATE hr.statutory_config SET config = CAST(:c AS jsonb), confirmed_at = NULL,"
                " confirmed_by = NULL, confirmation_note = NULL"
            ),
            {"c": json.dumps(UNCONFIRMED_STATUTORY)},
        )
    return w


class Team:
    """The people a payroll month needs, each with only the permissions their role carries."""

    def __init__(self, world: World):
        self.w = world
        self.hr = world.grant(
            str(uuid.uuid4()),
            perm.HR_SALARY_PROPOSE,
            perm.HR_PAYROLL_PREPARE,
            perm.HR_PAYROLL_READ,
            perm.HR_SETTINGS_MANAGE,
            perm.HR_EMPLOYEE_MANAGE,
            perm.HR_CLAIM_REVIEW,
        )
        self.finance = world.grant(str(uuid.uuid4()), perm.HR_SALARY_APPROVE, perm.HR_PAYROLL_READ)
        self.ceo = world.grant(str(uuid.uuid4()), perm.HR_PAYROLL_APPROVE, perm.HR_PAYROLL_READ)

    def employee(self):
        emp, user = self.w.employee(self.hr, date_of_joining="2026-01-05")
        return emp["employeeId"], user

    def call(self, method, path, user, **kw):
        return getattr(self.w.client, method)(f"/hr/v1{path}", headers=self.w.headers(user), **kw)

    def salary(self, eid, gross="30000", approve=True, **extra):
        body = {"employee_id": eid, "gross_monthly": gross, "effective_from": "2026-01-01"}
        body.update(extra)
        r = self.call("post", "/payroll/structures", self.hr, json=body)
        assert r.status_code == 201, r.text
        sid = r.json()["structureId"]
        if approve:
            d = self.call(
                "post",
                f"/payroll/structures/{sid}/decision",
                self.finance,
                json={"decision": "APPROVE"},
            )
            assert d.status_code == 200, d.text
        return r.json()

    def confirm_statutory(self):
        r = self.call(
            "put",
            "/payroll/statutory",
            self.hr,
            json={
                "pf": {
                    "enabled": True,
                    "employee_rate_pct": "12",
                    "employer_rate_pct": "12",
                    "wage_ceiling": "15000",
                },
                "esi": {"enabled": False},
                "pt": {"enabled": True, "slabs": [{"from": "0", "to": None, "monthly": "200"}]},
            },
        )
        assert r.status_code == 200, r.text
        r = self.call(
            "post", "/payroll/statutory/confirm", self.hr, json={"note": "Confirmed by CA on file"}
        )
        assert r.status_code == 200, r.text

    def run(self, month=MONTH):
        r = self.call("post", "/payroll/runs", self.hr, json={"month": month})
        assert r.status_code == 201, r.text
        return r.json()


@pytest.fixture()
def team(world):
    return Team(world)


# ---- salary structures ---------------------------------------------------------------------


def test_default_template_is_picked_by_gross_and_parts_add_up(team):
    eid, _ = team.employee()
    low = team.salary(eid, "18000", approve=False)
    high = team.salary(eid, "40000", approve=False)
    for s, gross in ((low, "18000"), (high, "40000")):
        total = sum(Decimal(c["amount"]) for c in s["components"])
        assert total == Decimal(gross) and s["status"] == "PROPOSED"
    assert [c["code"] for c in low["components"]] == ["BASIC", "HRA", "OTHER"]
    assert [c["code"] for c in high["components"]] == ["BASIC", "HRA", "SPECIAL"]


def test_the_21k_to_25k_band_needs_a_chosen_and_confirmed_template(team):
    eid, _ = team.employee()
    body = {"employee_id": eid, "gross_monthly": "22000", "effective_from": "2026-01-01"}
    r = team.call("post", "/payroll/structures", team.hr, json=body)
    assert r.status_code == 422 and r.json()["code"] == "SALARY_BAND_NEEDS_CHOICE"
    templates = team.call("get", "/payroll/templates", team.hr).json()["items"]
    chosen = next(t for t in templates if t["code"] == "BELOW_21K")["templateId"]
    r = team.call("post", "/payroll/structures", team.hr, json={**body, "template_id": chosen})
    assert r.status_code == 422 and r.json()["code"] == "SALARY_BAND_NEEDS_CHOICE"
    r = team.call(
        "post",
        "/payroll/structures",
        team.hr,
        json={**body, "template_id": chosen, "band_confirmed": True},
    )
    assert r.status_code == 201
    # the edges of the band are inside it, the values just outside are not
    for gross, code in (("21001", 422), ("25000", 422), ("21000", 201), ("25001", 201)):
        r = team.call("post", "/payroll/structures", team.hr, json={**body, "gross_monthly": gross})
        assert r.status_code == code, gross


def test_only_finance_decides_a_salary_and_never_their_own_proposal_or_salary(team, world):
    eid, user = team.employee()
    proposed = team.salary(eid, approve=False)
    sid = proposed["structureId"]
    decision = {"decision": "APPROVE"}
    assert (
        team.call("post", f"/payroll/structures/{sid}/decision", team.hr, json=decision).status_code
        == 403
    )
    both = world.grant(str(uuid.uuid4()), perm.HR_SALARY_PROPOSE, perm.HR_SALARY_APPROVE)
    mine = team.call(
        "post",
        "/payroll/structures",
        both,
        json={"employee_id": eid, "gross_monthly": "30000", "effective_from": "2026-02-01"},
    ).json()["structureId"]
    assert (
        team.call("post", f"/payroll/structures/{mine}/decision", both, json=decision).status_code
        == 403
    )
    world.grant(user, perm.HR_SALARY_APPROVE)
    assert (
        team.call("post", f"/payroll/structures/{sid}/decision", user, json=decision).status_code
        == 403
    )
    reject = team.call(
        "post", f"/payroll/structures/{sid}/decision", team.finance, json={"decision": "REJECT"}
    )
    assert reject.status_code == 422 and reject.json()["code"] == "APPROVAL_NOTE_REQUIRED"
    ok = team.call("post", f"/payroll/structures/{sid}/decision", team.finance, json=decision)
    assert ok.json()["status"] == "APPROVED"
    again = team.call("post", f"/payroll/structures/{sid}/decision", team.finance, json=decision)
    assert again.status_code == 409


def test_salaries_are_visible_only_to_salary_and_payroll_roles(team, world):
    eid, user = team.employee()
    team.salary(eid)
    assert team.call("get", "/payroll/structures", user).status_code == 403
    assert team.call("get", "/payroll/structures", team.finance).status_code == 200
    assert team.call("get", "/payroll/templates", user).status_code == 403


def test_a_template_must_have_exactly_one_remainder(team):
    comps = [
        {"code": "BASIC", "label": "Basic", "basis": "PERCENT_GROSS", "value": "50"},
        {"code": "OTHER", "label": "Other", "basis": "PERCENT_GROSS", "value": "50"},
    ]
    r = team.call(
        "post",
        "/payroll/templates?code=BAD_ONE",
        team.hr,
        json={"name": "Bad template", "components": comps},
    )
    assert r.status_code == 422 and r.json()["code"] == "TEMPLATE_INVALID"
    comps[1]["basis"] = "REMAINDER"
    comps[1].pop("value")
    ok = team.call(
        "post",
        "/payroll/templates?code=GOOD_ONE",
        team.hr,
        json={"name": "Good template", "components": comps},
    )
    assert ok.status_code == 201
    dup = team.call(
        "post",
        "/payroll/templates?code=GOOD_ONE",
        team.hr,
        json={"name": "Good template", "components": comps},
    )
    assert dup.status_code == 409


# ---- statutory settings --------------------------------------------------------------------


def test_statutory_settings_are_validated_and_a_change_cancels_the_confirmation(team):
    r = team.call("put", "/payroll/statutory", team.hr, json={"pf": {"enabled": True}})
    assert r.status_code == 422 and r.json()["code"] == "STATUTORY_INVALID"
    team.confirm_statutory()
    assert team.call("get", "/payroll/statutory", team.hr).json()["confirmed"] is True
    changed = team.call(
        "put",
        "/payroll/statutory",
        team.hr,
        json={"pf": {"enabled": False}, "esi": {"enabled": False}, "pt": {"enabled": False}},
    ).json()
    assert changed["confirmed"] is False
    assert team.call("get", "/payroll/statutory", str(uuid.uuid4())).status_code == 403


# ---- runs ----------------------------------------------------------------------------------


def test_a_run_lists_people_without_a_salary_and_one_live_run_per_month(team):
    eid, _ = team.employee()
    team.salary(eid, "30000")
    run = team.run()
    assert [ln["employeeId"] for ln in run["lines"]] == [eid]
    assert (
        any(s["reason"] == "No approved salary structure." for s in run["skipped"])
        or not run["skipped"]
    )
    dup = team.call("post", "/payroll/runs", team.hr, json={"month": MONTH})
    assert dup.status_code == 409 and dup.json()["code"] == "PAYROLL_RUN_EXISTS"
    cancelled = team.call("post", f"/payroll/runs/{run['runId']}/cancel", team.hr)
    assert cancelled.json()["status"] == "CANCELLED"
    assert team.call("post", "/payroll/runs", team.hr, json={"month": MONTH}).status_code == 201


def test_unpaid_leave_and_extra_loss_of_pay_reduce_the_pay_by_calendar_days(team):
    eid, _ = team.employee()
    team.salary(eid, "31000", approve=True)
    run = team.run()
    line = run["lines"][0]
    assert Decimal(line["grossEarned"]) == Decimal("31000.00") and line["lopDays"] == "0"
    r = team.call(
        "put",
        f"/payroll/runs/{run['runId']}/lines/{eid}/inputs",
        team.hr,
        json={"extra_lop_days": "3", "adjustments": []},
    )
    assert r.status_code == 200, r.text
    # October has 31 days: 28 paid days of 31
    assert r.json()["paidDays"] == "28" and Decimal(r.json()["grossEarned"]) == Decimal("28000.00")
    adj = team.call(
        "put",
        f"/payroll/runs/{run['runId']}/lines/{eid}/inputs",
        team.hr,
        json={
            "extra_lop_days": "0",
            "adjustments": [{"label": "Festival bonus", "amount": "1000"}],
        },
    )
    assert Decimal(adj.json()["netPay"]) == Decimal("32000.00")
    zero = team.call(
        "put",
        f"/payroll/runs/{run['runId']}/lines/{eid}/inputs",
        team.hr,
        json={"adjustments": [{"label": "Nothing", "amount": "0"}]},
    )
    assert zero.status_code == 422


def test_recompute_keeps_hr_inputs(team):
    eid, _ = team.employee()
    team.salary(eid, "31000")
    run = team.run()
    team.call(
        "put",
        f"/payroll/runs/{run['runId']}/lines/{eid}/inputs",
        team.hr,
        json={"extra_lop_days": "1", "adjustments": []},
    )
    again = team.call("post", f"/payroll/runs/{run['runId']}/recompute", team.hr).json()
    assert again["lines"][0]["extraLopDays"] == 1.0


def test_an_empty_run_cannot_be_submitted(team):
    run = team.run()
    r = team.call("post", f"/payroll/runs/{run['runId']}/submit", team.hr)
    assert r.status_code == 422 and r.json()["code"] == "PAYROLL_RUN_EMPTY"


def test_only_permitted_roles_touch_payroll(team, world):
    eid, user = team.employee()
    team.salary(eid)
    assert team.call("post", "/payroll/runs", user, json={"month": MONTH}).status_code == 403
    assert team.call("get", "/payroll/runs", user).status_code == 403
    assert (
        team.call("post", "/payroll/runs", team.finance, json={"month": MONTH}).status_code == 403
    )
    bad = team.call("post", "/payroll/runs", team.hr, json={"month": "October"})
    assert bad.status_code == 422


def test_approval_needs_confirmed_statutory_settings_and_a_different_person(team):
    eid, _ = team.employee()
    team.salary(eid)
    run = team.run()
    rid = run["runId"]
    assert (
        team.call("post", f"/payroll/runs/{rid}/approve", team.ceo).status_code == 409
    )  # still a draft
    team.call("post", f"/payroll/runs/{rid}/submit", team.hr)
    unconfirmed = team.call("post", f"/payroll/runs/{rid}/approve", team.ceo)
    assert unconfirmed.status_code == 409
    assert unconfirmed.json()["code"] == "PAYROLL_STATUTORY_UNCONFIRMED"
    back = team.call("post", f"/payroll/runs/{rid}/send-back", team.ceo, json={})
    assert back.status_code == 422
    sent = team.call(
        "post", f"/payroll/runs/{rid}/send-back", team.ceo, json={"note": "Confirm CA first"}
    )
    assert sent.json()["status"] == "DRAFT" and sent.json()["note"] == "Confirm CA first"
    team.confirm_statutory()
    team.call("post", f"/payroll/runs/{rid}/recompute", team.hr)
    team.call("post", f"/payroll/runs/{rid}/submit", team.hr)
    preparer_with_power = team.w.grant(team.hr, perm.HR_PAYROLL_APPROVE)
    assert team.call("post", f"/payroll/runs/{rid}/approve", preparer_with_power).status_code == 403
    team.w.grants[team.hr].discard(perm.HR_PAYROLL_APPROVE)
    assert team.call("post", f"/payroll/runs/{rid}/approve", team.hr).status_code == 403
    assert (
        team.call("post", f"/payroll/runs/{rid}/approve", team.ceo).json()["status"] == "APPROVED"
    )


def test_statutory_deductions_use_the_confirmed_settings(team):
    eid, _ = team.employee()
    team.salary(eid, "31000")
    team.confirm_statutory()
    run = team.run()
    detail = team.call("get", f"/payroll/runs/{run['runId']}/lines/{eid}", team.hr).json()
    codes = {d["code"]: Decimal(d["amount"]) for d in detail["figures"]["deductions"]}
    # 31,000 uses the above-25k template: Basic is 40% = 12,400, under the 15,000 ceiling, 12% of it
    assert codes["PF_EMPLOYEE"] == Decimal("1488")
    assert codes["PROFESSIONAL_TAX"] == Decimal("200.00")


def test_an_approved_run_is_locked(team):
    eid, _ = team.employee()
    team.salary(eid)
    team.confirm_statutory()
    run = team.run()
    rid = run["runId"]
    team.call("post", f"/payroll/runs/{rid}/submit", team.hr)
    team.call("post", f"/payroll/runs/{rid}/approve", team.ceo)
    for path, user in (
        (f"/payroll/runs/{rid}/recompute", team.hr),
        (f"/payroll/runs/{rid}/cancel", team.hr),
        (f"/payroll/runs/{rid}/submit", team.hr),
    ):
        assert team.call("post", path, user).status_code == 409, path
    locked = team.call(
        "put", f"/payroll/runs/{rid}/lines/{eid}/inputs", team.hr, json={"extra_lop_days": "1"}
    )
    assert locked.status_code == 409 and locked.json()["code"] == "PAYROLL_RUN_LOCKED"


# ---- payslips and reimbursements -------------------------------------------------------------


def _approved_claim(world: World, team: Team, user: str) -> str:
    world.assign(user, "PC", outlet=OUTLET)
    r = world.client.post(
        "/hr/v1/claims",
        headers=world.headers(user),
        data={
            "category": "BIKE_TAXI",
            "expense_date": "2026-10-10",
            "description": "Showroom visit",
            "amount": "500",
        },
        files=[("receipts", ("r.jpg", jpeg(), "image/jpeg"))],
    )
    assert r.status_code == 201, r.text
    cid = r.json()["claimId"]
    lead = world.grant(str(uuid.uuid4()))
    world.assign(lead, "TL")
    for who in (lead, team.hr):
        d = world.client.post(
            f"/hr/v1/approvals/claims/{cid}/decision",
            json={"decision": "APPROVE"},
            headers=world.headers(who),
        )
        if d.json().get("status") == "APPROVED":
            break
    assert (
        world.client.get(f"/hr/v1/claims/{cid}", headers=world.headers(user)).json()["status"]
        == "APPROVED"
    )
    return cid


def test_payslips_are_made_on_approval_claims_follow_and_pdfs_are_private(team, world):
    eid, user = team.employee()
    other_eid, other_user = team.employee()
    team.salary(eid, "31000")
    team.salary(other_eid, "31000")
    claim = _approved_claim(world, team, user)
    team.confirm_statutory()
    run = team.run()
    rid = run["runId"]
    me = next(ln for ln in run["lines"] if ln["employeeId"] == eid)
    assert Decimal(me["reimbursements"]) == Decimal("500.00")
    assert Decimal(me["payableTotal"]) == Decimal(me["netPay"]) + Decimal("500.00")

    team.call("post", f"/payroll/runs/{rid}/submit", team.hr)
    approved = team.call("post", f"/payroll/runs/{rid}/approve", team.ceo)
    assert approved.status_code == 200, approved.text
    slips = [k for k in world.storage.objects if k.startswith("payslips/2026/10/")]
    assert len(slips) == 2 and all(k.endswith(".pdf") for k in slips)

    status = world.client.get(f"/hr/v1/claims/{claim}", headers=world.headers(user)).json()[
        "status"
    ]
    assert status == "HANDED_TO_PAYROLL"
    assert (
        team.call(
            "post", f"/payroll/runs/{rid}/mark-paid", team.ceo, json={"payment_date": "2026-10-12"}
        ).status_code
        == 403
    )

    future = team.call(
        "post", f"/payroll/runs/{rid}/mark-paid", team.hr, json={"payment_date": "2026-10-20"}
    )
    assert future.status_code == 422
    paid = team.call(
        "post", f"/payroll/runs/{rid}/mark-paid", team.hr, json={"payment_date": "2026-10-12"}
    )
    assert paid.json()["status"] == "PAID"
    status = world.client.get(f"/hr/v1/claims/{claim}", headers=world.headers(user)).json()[
        "status"
    ]
    assert status == "PAID"
    assert (
        team.call(
            "post", f"/payroll/runs/{rid}/mark-paid", team.hr, json={"payment_date": "2026-10-12"}
        ).status_code
        == 409
    )

    mine = team.call("get", "/payslips", user).json()["items"]
    assert [p["month"] for p in mine] == [MONTH]
    pdf = team.call("get", f"/payslips/{mine[0]['payslipId']}/pdf", user)
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    assert pdf.headers["cache-control"] == "private, no-store"
    # someone else's payslip is not found for another employee
    assert team.call("get", f"/payslips/{mine[0]['payslipId']}/pdf", other_user).status_code == 404
    # HR with payroll access may open it, and the view is recorded
    assert team.call("get", f"/payslips/{mine[0]['payslipId']}/pdf", team.hr).status_code == 200
    with world.engine.connect() as conn:
        viewed = conn.execute(
            text(
                "SELECT count(*) FROM hr.audit_log WHERE action = 'PAYSLIP_VIEWED' AND actor_user_id = :u"
            ),
            {"u": team.hr},
        ).scalar_one()
    assert viewed == 1
    listed = team.call("get", f"/payroll/runs/{rid}/payslips", team.hr).json()["items"]
    assert {p["employeeId"] for p in listed} >= {eid, other_eid}


def test_payslip_storage_failure_approves_nothing(team, world):
    eid, _ = team.employee()
    team.salary(eid)
    team.confirm_statutory()
    run = team.run()
    rid = run["runId"]
    team.call("post", f"/payroll/runs/{rid}/submit", team.hr)
    world.storage.fail = True
    r = team.call("post", f"/payroll/runs/{rid}/approve", team.ceo)
    assert r.status_code == 503
    world.storage.fail = False
    assert team.call("get", f"/payroll/runs/{rid}", team.hr).json()["status"] == "SUBMITTED"
    assert team.call("post", f"/payroll/runs/{rid}/approve", team.ceo).status_code == 200


def test_a_person_without_an_employee_record_has_no_payslips(team, world):
    stranger = str(uuid.uuid4())
    r = team.call("get", "/payslips", stranger)
    assert r.status_code in (403, 404)
