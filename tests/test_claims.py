from __future__ import annotations

import io
import uuid
from datetime import date

import pytest
from PIL import Image
from sqlalchemy import text

from hrmgmt import claim_rules as cr
from hrmgmt import permissions as perm
from tests.support import OUTLET, World, ist, jpeg


@pytest.fixture()
def world(migrated_engine):
    w = World(migrated_engine, now=ist(2026, 10, 12, 11, 0))  # Monday 12 October 2026
    w.clean_assignments()
    with migrated_engine.begin() as conn:
        conn.execute(text("DELETE FROM hr.claim_event"))
        conn.execute(text("DELETE FROM hr.claim_receipt"))
        conn.execute(text("DELETE FROM hr.claim"))
        conn.execute(text("DELETE FROM hr.setting"))
    return w


def person(world: World, role: str | None = "PC", tenant="tenant-a", perms=()):
    hr = world.grant(str(uuid.uuid4()), perm.HR_EMPLOYEE_MANAGE)
    emp, user = world.employee(hr)
    if role:
        world.assign(user, role, tenant=tenant, outlet=OUTLET if role == "PC" else None)
    if perms:
        world.grant(user, *perms)
    return emp, user


def submit(
    world, user, *, category="BIKE_TAXI", amount="500", on="2026-10-10", receipt=True, **extra
):
    data = {"category": category, "expense_date": on, "description": "Showroom visit"}
    if amount is not None:
        data["amount"] = amount
    data.update(extra)
    files = [("receipts", ("r.jpg", jpeg(), "image/jpeg"))] if receipt else []
    return world.client.post(
        "/hr/v1/claims", headers=world.headers(user), data=data, files=files or None
    )


def decide(world, user, cid, decision="APPROVE", note=None):
    return world.client.post(
        f"/hr/v1/approvals/claims/{cid}/decision",
        json={"decision": decision, "note": note},
        headers=world.headers(user),
    )


def inbox(world, user):
    return [
        i["claimId"]
        for i in world.client.get("/hr/v1/approvals/claims", headers=world.headers(user)).json()[
            "items"
        ]
    ]


def reviewers(world):
    hr = world.grant(str(uuid.uuid4()), perm.HR_CLAIM_REVIEW)
    fin = world.grant(str(uuid.uuid4()), perm.HR_CLAIM_REVIEW_FINANCE)
    return hr, fin


# ---- the rules in isolation ----------------------------------------------------------------


def test_payroll_month_follows_the_cutoff_day():
    assert cr.payroll_month_for(date(2026, 10, 25), 25) == date(2026, 10, 1)
    assert cr.payroll_month_for(date(2026, 10, 26), 25) == date(2026, 11, 1)
    assert cr.payroll_month_for(date(2026, 12, 28), 25) == date(2027, 1, 1)


def test_stale_means_older_than_the_allowed_months():
    submitted = date(2026, 10, 12)
    assert not cr.is_stale(date(2026, 8, 12), submitted, 2)
    assert cr.is_stale(date(2026, 8, 11), submitted, 2)
    assert not cr.is_stale(date(2026, 10, 1), submitted, 2)
    assert cr.is_stale(date(2025, 1, 1), submitted, 2)
    assert not cr.is_stale(date(2026, 12, 31), date(2027, 2, 28), 2)  # month ends do not misfire


# ---- submitting ----------------------------------------------------------------------------


def test_categories_and_the_rules_shown_to_the_employee(world):
    _, user = person(world)
    cats = world.client.get("/hr/v1/claims/categories", headers=world.headers(user)).json()["items"]
    assert [c["code"] for c in cats] == [
        "BIKE_TAXI",
        "CAR_TAXI",
        "PERSONAL_BIKE",
        "OUTSTATION_TRAIN",
        "MEALS",
    ]
    assert all(c["taxable"] for c in cats)  # nothing is assumed tax-free
    s = world.client.get("/hr/v1/claims/summary", headers=world.headers(user)).json()
    assert (
        s["travelLimit"] == 5000.0
        and s["financeThreshold"] == 3000.0
        and s["nextPayrollMonth"] == "2026-10"
    )
    assert any("25th" in r for r in s["rules"])


def test_a_pc_claim_waits_for_the_team_lead_then_hr(world):
    _, pc = person(world, "PC")
    _, tl = person(world, "TL")
    hr, _ = reviewers(world)
    r = submit(world, pc)
    assert r.status_code == 201, r.text
    claim = r.json()
    assert claim["status"] == "SUBMITTED" and claim["stagePlan"] == ["TL_PM", "HR"]
    assert (
        claim["waitingFor"] == "Team Lead or Project Manager" and claim["payrollMonth"] == "2026-10"
    )
    assert len(claim["receipts"]) == 1 and claim["history"][0]["event"] == "SUBMITTED"
    cid = claim["claimId"]
    assert inbox(world, tl) == [cid] and inbox(world, hr) == []
    assert decide(world, tl, cid).json()["status"] == "SUBMITTED"  # moves on to HR
    assert inbox(world, tl) == [] and inbox(world, hr) == [cid]
    mine = world.client.get(f"/hr/v1/claims/{cid}", headers=world.headers(pc)).json()
    assert mine["waitingFor"] == "HR"
    assert decide(world, hr, cid).json()["status"] == "APPROVED"
    done = world.client.get(f"/hr/v1/claims/{cid}", headers=world.headers(pc)).json()
    assert done["status"] == "APPROVED" and done["waitingFor"] is None
    assert [h["event"] for h in done["history"]] == ["SUBMITTED", "STAGE_APPROVED", "APPROVED"]


def test_someone_with_no_project_goes_straight_to_hr(world):
    _, staff = person(world, None)
    assert submit(world, staff).json()["stagePlan"] == ["HR"]


def test_receipts_are_required_checked_and_limited(world):
    _, user = person(world)
    assert submit(world, user, receipt=False).json()["code"] == "CLAIM_RECEIPT_REQUIRED"
    bad = world.client.post(
        "/hr/v1/claims",
        headers=world.headers(user),
        data={"category": "BIKE_TAXI", "expense_date": "2026-10-10", "amount": "100"},
        files=[("receipts", ("r.txt", b"hello", "text/plain"))],
    )
    assert bad.status_code == 422 and bad.json()["code"] == "CLAIM_RECEIPT_INVALID"
    many = world.client.post(
        "/hr/v1/claims",
        headers=world.headers(user),
        data={"category": "BIKE_TAXI", "expense_date": "2026-10-10", "amount": "100"},
        files=[("receipts", (f"r{i}.jpg", jpeg(), "image/jpeg")) for i in range(6)],
    )
    assert many.status_code == 422
    pdf = world.client.post(
        "/hr/v1/claims",
        headers=world.headers(user),
        data={"category": "BIKE_TAXI", "expense_date": "2026-10-10", "amount": "100"},
        files=[("receipts", ("t.pdf", b"%PDF-1.4 test", "application/pdf"))],
    )
    assert pdf.status_code == 201 and pdf.json()["receipts"][0]["contentType"] == "application/pdf"


def test_receipt_photos_are_re_encoded_without_hidden_data(world):
    _, user = person(world)
    image = Image.new("RGB", (400, 300), (5, 5, 5))
    exif = Image.Exif()
    exif[0x010F] = "SecretPhoneMaker"
    out = io.BytesIO()
    image.save(out, format="JPEG", exif=exif)
    r = world.client.post(
        "/hr/v1/claims",
        headers=world.headers(user),
        data={"category": "MEALS", "expense_date": "2026-10-10", "amount": "150"},
        files=[("receipts", ("r.jpg", out.getvalue(), "image/jpeg"))],
    )
    assert r.status_code == 201
    stored = next(v for k, v in world.storage.objects.items() if k.startswith("claims/"))[0]
    assert b"SecretPhoneMaker" not in stored


def test_amounts_dates_and_categories_are_validated(world):
    _, user = person(world)
    assert submit(world, user, amount="abc").json()["code"] == "CLAIM_AMOUNT_INVALID"
    assert submit(world, user, amount="10.123").json()["code"] == "CLAIM_AMOUNT_INVALID"
    assert submit(world, user, amount="0").json()["code"] == "CLAIM_AMOUNT_INVALID"
    assert submit(world, user, amount="-5").json()["code"] == "CLAIM_AMOUNT_INVALID"
    assert submit(world, user, on="2026-12-01").json()["code"] == "CLAIM_DATE_INVALID"
    assert submit(world, user, on="not-a-date").json()["code"] == "CLAIM_DATE_INVALID"
    assert submit(world, user, category="TELEPORT").json()["code"] == "CLAIM_CATEGORY_UNKNOWN"


def test_personal_bike_is_paid_per_km_at_the_rate_hr_sets(world):
    _, user = person(world)
    first = submit(
        world, user, category="PERSONAL_BIKE", amount=None, distance_km="10", receipt=False
    )
    assert first.json()["code"] == "CLAIM_RATE_NOT_SET"
    hr = world.grant(str(uuid.uuid4()), perm.HR_SETTINGS_MANAGE)
    world.client.put(
        "/hr/v1/settings",
        json={"values": {"claims.personal_bike_rate_per_km": 4.5}},
        headers=world.headers(hr),
    )
    r = submit(world, user, category="PERSONAL_BIKE", amount=None, distance_km="10", receipt=False)
    assert r.status_code == 201 and r.json()["amount"] == 45.0 and r.json()["distanceKm"] == 10.0


def test_the_monthly_travel_limit_blocks_and_rejected_claims_do_not_count(world):
    _, user = person(world, None)
    _, fin = reviewers(world)
    big = submit(world, user, amount="4800").json()
    over = submit(world, user, amount="300")
    assert (
        over.status_code == 409
        and over.json()["code"] == "CLAIM_LIMIT_EXCEEDED"
        and "5,000" in over.json()["detail"]
    )
    assert submit(world, user, amount="200").status_code == 201  # exactly at the limit is fine
    decide(world, fin, big["claimId"], "REJECT", "No bill match")
    assert (
        submit(world, user, amount="4800", on="2026-10-11").status_code == 201
    )  # the rejected one freed the room
    assert (
        submit(world, user, amount="100", on="2026-09-10").status_code == 201
    )  # another month, own limit
    s = world.client.get("/hr/v1/claims/summary?month=2026-10", headers=world.headers(user)).json()
    assert s["travelUsed"] == 5000.0 and s["travelRemaining"] == 0.0


def test_travel_above_the_finance_threshold_goes_to_finance_meals_do_not_count(world):
    _, user = person(world, None)
    hr, fin = reviewers(world)
    first = submit(world, user, amount="2000").json()
    assert first["stagePlan"] == ["HR"]
    meals = submit(world, user, category="MEALS", amount="2500").json()
    assert meals["stagePlan"] == ["HR"]  # meals are outside the travel total
    second = submit(world, user, amount="1500").json()  # month's travel 3,500 > 3,000
    assert second["stagePlan"] == ["FINANCE"]
    assert sorted(inbox(world, hr)) == sorted([first["claimId"], meals["claimId"]])
    assert inbox(world, fin) == [second["claimId"]]
    assert (
        decide(world, hr, second["claimId"]).status_code == 404
    )  # HR does not decide a Finance claim
    assert decide(world, fin, second["claimId"]).json()["status"] == "APPROVED"


def test_exactly_at_the_threshold_stays_with_hr(world):
    _, user = person(world, None)
    assert submit(world, user, amount="3000").json()["stagePlan"] == ["HR"]


def test_meals_limit_applies_only_once_hr_sets_one(world):
    _, user = person(world, None)
    assert submit(world, user, category="MEALS", amount="9000").status_code == 201
    hr = world.grant(str(uuid.uuid4()), perm.HR_SETTINGS_MANAGE)
    assert (
        world.client.put(
            "/hr/v1/settings",
            json={"values": {"claims.meals_monthly_limit": 10000}},
            headers=world.headers(hr),
        ).status_code
        == 200
    )
    r = submit(world, user, category="MEALS", amount="2000")
    assert r.status_code == 409 and "meals limit" in r.json()["detail"]


def test_an_old_claim_needs_a_finance_exception_in_addition(world):
    _, user = person(world, None)
    hr, fin = reviewers(world)
    old = submit(world, user, amount="500", on="2026-07-20").json()
    assert old["stale"] is True and old["stagePlan"] == ["HR", "FINANCE"]
    assert decide(world, hr, old["claimId"]).json()["status"] == "SUBMITTED"
    assert inbox(world, fin) == [old["claimId"]]
    assert decide(world, fin, old["claimId"]).json()["status"] == "APPROVED"
    # over the threshold and old: Finance already reviews it, no extra step
    _, other = person(world, None)
    both = submit(world, other, amount="3500", on="2026-07-20").json()
    assert both["stagePlan"] == ["FINANCE"]


def test_cutoff_day_decides_the_payroll_month(world):
    _, user = person(world, None)
    world.clock.set(ist(2026, 10, 25, 23, 0))
    assert submit(world, user, amount="100").json()["payrollMonth"] == "2026-10"
    world.clock.set(ist(2026, 10, 26, 9, 0))
    assert submit(world, user, amount="100").json()["payrollMonth"] == "2026-11"


# ---- who may decide -------------------------------------------------------------------------


def test_only_the_right_people_see_and_decide_and_never_their_own(world):
    _, pc = person(world, "PC")
    _, tl = person(world, "TL")
    _, other_tl = person(world, "TL", tenant="tenant-b")
    hr, fin = reviewers(world)
    cid = submit(world, pc).json()["claimId"]
    assert inbox(world, other_tl) == [] and inbox(world, pc) == [] and inbox(world, fin) == []
    for who in (other_tl, pc, hr, fin):
        assert decide(world, who, cid).status_code == 404
    assert (
        world.client.get(f"/hr/v1/claims/{cid}", headers=world.headers(other_tl)).status_code == 404
    )
    assert world.client.get(f"/hr/v1/claims/{cid}", headers=world.headers(tl)).status_code == 200
    # the CEO may decide at any stage, but never their own
    ceo = world.grant(str(uuid.uuid4()), perm.HR_PAYROLL_APPROVE)
    assert decide(world, ceo, cid).status_code == 200
    _, boss = person(world, None, perms=[perm.HR_PAYROLL_APPROVE])
    own = submit(world, boss, amount="100").json()
    assert own["stagePlan"] == ["FINANCE"]
    assert decide(world, boss, own["claimId"]).status_code == 404


def test_reject_and_correction_need_a_reason_and_stop_the_claim(world):
    _, user = person(world, None)
    hr, _ = reviewers(world)
    cid = submit(world, user).json()["claimId"]
    assert decide(world, hr, cid, "REJECT").json()["code"] == "APPROVAL_NOTE_REQUIRED"
    assert decide(world, hr, cid, "CORRECTION").json()["code"] == "APPROVAL_NOTE_REQUIRED"
    assert decide(world, hr, cid, "REJECT", "Not a work trip").json()["status"] == "REJECTED"
    assert decide(world, hr, cid).status_code == 409
    mine = world.client.get(f"/hr/v1/claims/{cid}", headers=world.headers(user)).json()
    assert mine["history"][-1]["note"] == "Not a work trip"


def test_correction_lets_the_employee_fix_and_resubmit_from_the_start(world):
    _, pc = person(world, "PC")
    _, tl = person(world, "TL")
    hr, _ = reviewers(world)
    claim = submit(world, pc, amount="400").json()
    cid, receipt_id = claim["claimId"], claim["receipts"][0]["receiptId"]
    decide(world, tl, cid)
    assert (
        decide(world, hr, cid, "CORRECTION", "Amount does not match the bill").json()["status"]
        == "CORRECTION_REQUESTED"
    )
    fixed = world.client.post(
        f"/hr/v1/claims/{cid}/resubmit",
        headers=world.headers(pc),
        data={
            "category": "BIKE_TAXI",
            "expense_date": "2026-10-10",
            "amount": "380",
            "remove_receipts": receipt_id,
        },
        files=[("receipts", ("new.jpg", jpeg(), "image/jpeg"))],
    )
    assert fixed.status_code == 200, fixed.text
    body = fixed.json()
    assert (
        body["status"] == "SUBMITTED"
        and body["amount"] == 380.0
        and body["stagePlan"] == ["TL_PM", "HR"]
        and body["stage"] == "TL_PM"
    )
    assert [r["receiptId"] for r in body["receipts"]] != [receipt_id] and len(body["receipts"]) == 1
    assert inbox(world, tl) == [cid]
    # only a claim sent back may be changed
    again = world.client.post(
        f"/hr/v1/claims/{cid}/resubmit",
        headers=world.headers(pc),
        data={"category": "BIKE_TAXI", "expense_date": "2026-10-10", "amount": "100"},
    )
    assert again.status_code == 409


def test_cancel_only_before_approval_and_only_your_own(world):
    _, user = person(world, None)
    _, other = person(world, None)
    hr, _ = reviewers(world)
    cid = submit(world, user).json()["claimId"]
    assert (
        world.client.post(f"/hr/v1/claims/{cid}/cancel", headers=world.headers(other)).status_code
        == 404
    )
    assert (
        world.client.post(f"/hr/v1/claims/{cid}/cancel", headers=world.headers(user)).json()[
            "status"
        ]
        == "CANCELLED"
    )
    cid2 = submit(world, user).json()["claimId"]
    decide(world, hr, cid2)
    assert (
        world.client.post(f"/hr/v1/claims/{cid2}/cancel", headers=world.headers(user)).status_code
        == 409
    )


def test_receipts_open_for_the_owner_reviewers_and_the_team_lead_with_views_recorded(
    world, migrated_engine
):
    emp, pc = person(world, "PC")
    _, tl = person(world, "TL")
    hr, _ = reviewers(world)
    stranger = str(uuid.uuid4())
    claim = submit(world, pc).json()
    path = f"/hr/v1/claims/{claim['claimId']}/receipts/{claim['receipts'][0]['receiptId']}"
    assert world.client.get(path, headers=world.headers(pc)).status_code == 200
    assert world.client.get(path, headers=world.headers(tl)).status_code == 200
    assert world.client.get(path, headers=world.headers(hr)).status_code == 200
    assert world.client.get(path, headers=world.headers(stranger)).status_code == 404
    with migrated_engine.connect() as c:
        views = c.execute(
            text(
                "SELECT count(*) FROM hr.audit_log WHERE action = 'CLAIM_RECEIPT_VIEWED' AND entity_id = :e"
            ),
            {"e": emp["employeeId"]},
        ).scalar_one()
    assert views == 2  # the owner's own view is not logged


def test_hr_and_finance_can_follow_every_claim_others_cannot(world):
    _, user = person(world, None)
    hr, fin = reviewers(world)
    submit(world, user)
    assert (
        len(world.client.get("/hr/v1/claims/review", headers=world.headers(hr)).json()["items"])
        == 1
    )
    assert (
        len(
            world.client.get(
                "/hr/v1/claims/review?status=SUBMITTED&month=2026-10", headers=world.headers(fin)
            ).json()["items"]
        )
        == 1
    )
    assert world.client.get("/hr/v1/claims/review", headers=world.headers(user)).status_code == 403
    assert (
        world.client.get("/hr/v1/claims/review?status=NOPE", headers=world.headers(hr)).status_code
        == 422
    )


def test_hr_can_change_whether_a_category_is_taxable(world):
    _, user = person(world, None)
    hr = world.grant(str(uuid.uuid4()), perm.HR_SETTINGS_MANAGE)
    assert (
        world.client.patch(
            "/hr/v1/claims/categories/MEALS", json={"taxable": False}, headers=world.headers(user)
        ).status_code
        == 403
    )
    assert (
        world.client.patch(
            "/hr/v1/claims/categories/MEALS", json={"taxable": False}, headers=world.headers(hr)
        ).status_code
        == 200
    )
    cats = world.client.get("/hr/v1/claims/categories", headers=world.headers(user)).json()["items"]
    assert next(c for c in cats if c["code"] == "MEALS")["taxable"] is False
    assert (
        world.client.patch(
            "/hr/v1/claims/categories/NOPE", json={"taxable": True}, headers=world.headers(hr)
        ).status_code
        == 404
    )
