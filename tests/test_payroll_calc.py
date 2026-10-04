from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from hrmgmt import payroll_calc as pc

TEMPLATE = [
    {"code": "BASIC", "label": "Basic", "basis": "PERCENT_GROSS", "value": 50, "pf_wage": True},
    {"code": "HRA", "label": "HRA", "basis": "PERCENT_BASIC", "value": 40},
    {"code": "OTHER", "label": "Other allowance", "basis": "REMAINDER"},
]
# Rates below are test numbers only. The product ships with none.
STATUTORY = {
    "pf": {
        "enabled": True,
        "employee_rate_pct": "12",
        "employer_rate_pct": "12",
        "wage_ceiling": "15000",
        "rounding": "NEAREST",
    },
    "esi": {
        "enabled": True,
        "employee_rate_pct": "1",
        "employer_rate_pct": "3",
        "gross_threshold": "21000",
        "rounding": "UP",
    },
    "pt": {
        "enabled": True,
        "state": "Odisha",
        "slabs": [
            {"from": "0", "to": "10000", "monthly": "0"},
            {"from": "10000.01", "to": "20000", "monthly": "100"},
            {"from": "20000.01", "to": None, "monthly": "200"},
        ],
    },
}
OFF = {
    "pf": {"enabled": False, "rounding": "NEAREST"},
    "esi": {"enabled": False, "rounding": "NEAREST"},
    "pt": {"enabled": False, "slabs": []},
}


def full_month(month=date(2026, 10, 1), **over):
    base = dict(
        in_month=31,
        working_days=26,
        present_days=Decimal(26),
        paid_leave_days=Decimal(0),
        unpaid_leave_days=Decimal(0),
        extra_lop_days=Decimal(0),
        before_joining_days=0,
    )
    base.update(over)
    return pc.Days(**base)


def structure(gross):
    return pc.resolve_components(TEMPLATE, Decimal(gross))


def test_components_add_up_to_the_gross_exactly():
    parts = structure("20000")
    amounts = {p["code"]: Decimal(p["amount"]) for p in parts}
    assert amounts == {
        "BASIC": Decimal("10000.00"),
        "HRA": Decimal("4000.00"),
        "OTHER": Decimal("6000.00"),
    }
    odd = structure("17777.77")
    assert sum(Decimal(p["amount"]) for p in odd) == Decimal("17777.77")


def test_bad_templates_and_grosses_are_refused():
    with pytest.raises(pc.PayrollError):
        pc.resolve_components(TEMPLATE, Decimal(0))
    with pytest.raises(pc.PayrollError):
        pc.resolve_components(
            [{"code": "A", "label": "a", "basis": "PERCENT_GROSS", "value": 100}], Decimal(100)
        )  # no remainder
    with pytest.raises(pc.PayrollError):
        pc.resolve_components(
            [
                {"code": "A", "label": "a", "basis": "FIXED", "value": 500},
                {"code": "R", "label": "r", "basis": "REMAINDER"},
            ],
            Decimal(100),
        )  # more than gross
    with pytest.raises(pc.PayrollError):
        pc.resolve_components(
            [
                {"code": "H", "label": "h", "basis": "PERCENT_BASIC", "value": 40},
                {"code": "R", "label": "r", "basis": "REMAINDER"},
            ],
            Decimal(100),
        )
    with pytest.raises(pc.PayrollError):
        pc.resolve_components(TEMPLATE + [TEMPLATE[0]], Decimal(100))


def test_a_full_month_with_no_statutory_settings_pays_the_gross():
    line = pc.compute_line(
        structure=structure("20000"), days=full_month(), statutory=OFF, claims=[], adjustments=[]
    )
    assert line["gross_earned"] == "20000.00" and line["net_pay"] == "20000.00"
    assert line["deductions"] == [] and line["employer"] == [] and line["days"]["paidDays"] == "31"


def test_loss_of_pay_prorates_every_component_by_calendar_days():
    days = full_month(unpaid_leave_days=Decimal(3), extra_lop_days=Decimal("0.5"))
    line = pc.compute_line(
        structure=structure("31000"), days=days, statutory=OFF, claims=[], adjustments=[]
    )
    assert line["days"]["lopDays"] == "3.5" and line["days"]["paidDays"] == "27.5"
    assert Decimal(line["gross_earned"]) == Decimal("27500.00")  # 31000 x 27.5 / 31
    basic = next(e for e in line["earnings"] if e["code"] == "BASIC")
    assert basic["monthly"] == "15500.00" and basic["amount"] == "13750.00"


def test_joining_mid_month_pays_from_the_joining_day():
    days = full_month(before_joining_days=10)
    line = pc.compute_line(
        structure=structure("31000"), days=days, statutory=OFF, claims=[], adjustments=[]
    )
    assert line["days"]["paidDays"] == "21" and line["gross_earned"] == "21000.00"


def test_lop_never_exceeds_the_days_in_the_month():
    days = full_month(unpaid_leave_days=Decimal(40))
    assert days.lop_days == 31 and days.paid_days == 0
    line = pc.compute_line(
        structure=structure("20000"), days=days, statutory=OFF, claims=[], adjustments=[]
    )
    assert line["gross_earned"] == "0.00"


def test_absent_days_are_shown_but_do_not_reduce_pay_unless_hr_enters_them():
    days = full_month(present_days=Decimal(20))
    assert days.absent_days == Decimal(6) and days.lop_days == 0
    assert (
        pc.compute_line(
            structure=structure("20000"), days=days, statutory=OFF, claims=[], adjustments=[]
        )["net_pay"]
        == "20000.00"
    )


def test_provident_fund_uses_the_wage_ceiling_and_the_configured_rounding():
    line = pc.compute_line(
        structure=structure("40000"),
        days=full_month(),
        statutory=STATUTORY,
        claims=[],
        adjustments=[],
    )
    pf = next(d for d in line["deductions"] if d["code"] == "PF_EMPLOYEE")
    er = next(d for d in line["employer"] if d["code"] == "PF_EMPLOYER")
    assert (
        pf["amount"] == "1800" and er["amount"] == "1800"
    )  # 12% of the 15,000 ceiling, not of Basic 20,000
    low = pc.compute_line(
        structure=structure("10000"),
        days=full_month(),
        statutory=STATUTORY,
        claims=[],
        adjustments=[],
    )
    assert (
        next(d for d in low["deductions"] if d["code"] == "PF_EMPLOYEE")["amount"] == "600"
    )  # 12% of Basic 5,000


def test_esi_applies_only_at_or_below_the_threshold_and_can_round_up():
    below = pc.compute_line(
        structure=structure("20000"),
        days=full_month(),
        statutory=STATUTORY,
        claims=[],
        adjustments=[],
    )
    esi = next(d for d in below["deductions"] if d["code"] == "ESI_EMPLOYEE")
    assert esi["amount"] == "200"  # 1% of 20,000
    odd = pc.compute_line(
        structure=structure("20050"),
        days=full_month(),
        statutory=STATUTORY,
        claims=[],
        adjustments=[],
    )
    assert (
        next(d for d in odd["deductions"] if d["code"] == "ESI_EMPLOYEE")["amount"] == "201"
    )  # 200.50 rounds up
    assert (
        next(d for d in odd["employer"] if d["code"] == "ESI_EMPLOYER")["amount"] == "602"
    )  # 601.50 up
    above = pc.compute_line(
        structure=structure("21001"),
        days=full_month(),
        statutory=STATUTORY,
        claims=[],
        adjustments=[],
    )
    assert not [d for d in above["deductions"] if d["code"].startswith("ESI")]


def test_professional_tax_follows_the_slabs_on_earned_pay():
    def pt(gross, **days):
        line = pc.compute_line(
            structure=structure(gross),
            days=full_month(**days),
            statutory=STATUTORY,
            claims=[],
            adjustments=[],
        )
        return next(d for d in line["deductions"] if d["code"] == "PROFESSIONAL_TAX")["amount"]

    assert (
        pt("9000") == "0.00"
        and pt("15000") == "100.00"
        and pt("20000") == "100.00"
        and pt("30000") == "200.00"
    )
    assert (
        pt("30000", unpaid_leave_days=Decimal(16)) == "100.00"
    )  # earned pay fell into a lower slab


def test_adjustments_reimbursements_and_net_pay():
    claims = [
        {
            "claim_id": "c1",
            "label": "Bike taxi",
            "date": "2026-10-10",
            "amount": "450.00",
            "taxable": True,
        }
    ]
    adjustments = [
        {"label": "Incentive", "amount": "1000", "taxable": True},
        {"label": "Advance recovery", "amount": "-500"},
    ]
    line = pc.compute_line(
        structure=structure("20000"),
        days=full_month(),
        statutory=OFF,
        claims=claims,
        adjustments=adjustments,
    )
    assert line["net_pay"] == "20500.00" and line["total_deductions"] == "500.00"
    assert line["reimbursement_total"] == "450.00" and line["payable_total"] == "20950.00"
    assert [a["label"] for a in line["adjustments"]] == ["Incentive", "Advance recovery"]


def test_deductions_larger_than_pay_are_refused():
    with pytest.raises(pc.PayrollError):
        pc.compute_line(
            structure=structure("1000"),
            days=full_month(),
            statutory=OFF,
            claims=[],
            adjustments=[{"label": "x", "amount": "-5000"}],
        )


def test_statutory_validation_demands_every_number_when_a_scheme_is_on():
    assert pc.validate_statutory({})["pf"]["enabled"] is False
    with pytest.raises(pc.PayrollError):
        pc.validate_statutory({"pf": {"enabled": True, "employee_rate_pct": "12"}})
    with pytest.raises(pc.PayrollError):
        pc.validate_statutory(
            {"pf": {"enabled": True, "employee_rate_pct": "120", "employer_rate_pct": "12"}}
        )
    with pytest.raises(pc.PayrollError):
        pc.validate_statutory({"pt": {"enabled": True, "slabs": []}})
    with pytest.raises(pc.PayrollError):
        pc.validate_statutory({"esi": {"enabled": False, "rounding": "SIDEWAYS"}})
    ok = pc.validate_statutory(STATUTORY)
    assert ok["pt"]["slabs"][0]["from"] == "0" and ok["esi"]["rounding"] == "UP"


def test_calendar_helper():
    assert pc.days_in_month(date(2026, 2, 1)) == 28 and pc.days_in_month(date(2028, 2, 1)) == 29
