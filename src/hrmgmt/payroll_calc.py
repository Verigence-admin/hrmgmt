"""Payroll arithmetic. Pure functions on Decimal; no database, no clock.

Nothing here knows a statutory rate: every rate, ceiling and slab comes from the configuration HR
enters and the CA confirms. Money is never a float. Every amount is rounded half-up to paise
unless a scheme's own rounding says otherwise."""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Any

PAISE = Decimal("0.01")
RUPEE = Decimal("1")
ZERO = Decimal("0")


class PayrollError(ValueError):
    """A salary or setting cannot be turned into a payslip. The message is safe to show HR."""


def money(value: Any) -> Decimal:
    try:
        return Decimal(str(value)).quantize(PAISE, rounding=ROUND_HALF_UP)
    except Exception as exc:
        raise PayrollError(f"{value!r} is not an amount.") from exc


def days_in_month(month: date) -> int:
    return calendar.monthrange(month.year, month.month)[1]


# ---- salary structures -----------------------------------------------------------------------

BASES = ("PERCENT_GROSS", "PERCENT_BASIC", "FIXED", "REMAINDER")


def resolve_components(
    template: list[dict[str, Any]], gross_monthly: Decimal
) -> list[dict[str, Any]]:
    """Turns a template into rupee amounts for a gross. Percent of gross and fixed amounts first,
    then percent of Basic, and one REMAINDER component takes whatever is left, so the parts always
    add up to the gross exactly."""
    gross = money(gross_monthly)
    if gross <= 0:
        raise PayrollError("The monthly gross must be more than zero.")
    codes = [c["code"] for c in template]
    if len(set(codes)) != len(codes):
        raise PayrollError("A template cannot list the same component twice.")
    remainders = [c for c in template if c.get("basis") == "REMAINDER"]
    if len(remainders) != 1:
        raise PayrollError("A template needs exactly one remainder component.")
    amounts: dict[str, Decimal] = {}
    for c in template:
        basis = c.get("basis")
        if basis not in BASES:
            raise PayrollError(f"{c.get('code')}: unknown basis {basis}.")
        if basis == "PERCENT_GROSS":
            amounts[c["code"]] = money(gross * Decimal(str(c["value"])) / 100)
        elif basis == "FIXED":
            amounts[c["code"]] = money(c["value"])
    for c in template:
        if c.get("basis") == "PERCENT_BASIC":
            if "BASIC" not in amounts:
                raise PayrollError("A percent-of-Basic component needs a Basic component first.")
            amounts[c["code"]] = money(amounts["BASIC"] * Decimal(str(c["value"])) / 100)
    left = gross - sum(amounts.values(), ZERO)
    if left < 0:
        raise PayrollError("The components add up to more than the gross.")
    amounts[remainders[0]["code"]] = left
    return [
        {
            "code": c["code"],
            "label": c["label"],
            "amount": str(amounts[c["code"]]),
            "pf_wage": bool(c.get("pf_wage")),
            "esi_wage": bool(c.get("esi_wage", True)),
        }
        for c in template
    ]


# ---- statutory configuration ----------------------------------------------------------------

_ROUNDING = {"NEAREST": ROUND_HALF_UP, "UP": ROUND_CEILING, "DOWN": ROUND_FLOOR}


def _round(value: Decimal, mode: str) -> Decimal:
    if mode not in _ROUNDING:
        raise PayrollError(f"Unknown rounding {mode}.")
    return value.quantize(RUPEE, rounding=_ROUNDING[mode])


def validate_statutory(config: dict[str, Any]) -> dict[str, Any]:
    """Checks the shape and fills in nothing: an enabled scheme must have every number entered."""
    out: dict[str, Any] = {}
    for scheme, fields in (
        ("pf", ("employee_rate_pct", "employer_rate_pct")),
        ("esi", ("employee_rate_pct", "employer_rate_pct", "gross_threshold")),
    ):
        part = dict(config.get(scheme) or {})
        part["enabled"] = bool(part.get("enabled"))
        part.setdefault("rounding", "NEAREST")
        if part["rounding"] not in _ROUNDING:
            raise PayrollError(f"{scheme.upper()}: unknown rounding.")
        if part["enabled"]:
            for f in fields:
                if part.get(f) in (None, ""):
                    raise PayrollError(
                        f"{scheme.upper()}: enter {f.replace('_', ' ')}, or turn the scheme off."
                    )
                value = Decimal(str(part[f]))
                if value < 0 or (f.endswith("_pct") and value > 100):
                    raise PayrollError(f"{scheme.upper()}: {f.replace('_', ' ')} is not valid.")
                part[f] = str(value)
        if scheme == "pf":
            ceiling = part.get("wage_ceiling")
            part["wage_ceiling"] = None if ceiling in (None, "") else str(Decimal(str(ceiling)))
        out[scheme] = part
    pt = dict(config.get("pt") or {})
    pt["enabled"] = bool(pt.get("enabled"))
    slabs = []
    for s in pt.get("slabs") or []:
        frm = Decimal(str(s["from"]))
        to = None if s.get("to") in (None, "") else Decimal(str(s["to"]))
        monthly = Decimal(str(s["monthly"]))
        if frm < 0 or monthly < 0 or (to is not None and to < frm):
            raise PayrollError("Professional tax: a slab is not valid.")
        slabs.append(
            {"from": str(frm), "to": None if to is None else str(to), "monthly": str(monthly)}
        )
    slabs.sort(key=lambda s: Decimal(str(s["from"])))
    if pt["enabled"] and not slabs:
        raise PayrollError("Professional tax: add at least one slab, or turn it off.")
    pt["slabs"] = slabs
    pt["state"] = (pt.get("state") or "").strip() or None
    out["pt"] = pt
    return out


# ---- one employee's month --------------------------------------------------------------------


@dataclass(frozen=True)
class Days:
    in_month: int
    working_days: int
    present_days: Decimal
    paid_leave_days: Decimal
    unpaid_leave_days: Decimal
    extra_lop_days: Decimal
    before_joining_days: int

    @property
    def lop_days(self) -> Decimal:
        total = self.unpaid_leave_days + self.extra_lop_days + Decimal(self.before_joining_days)
        return min(total, Decimal(self.in_month))

    @property
    def paid_days(self) -> Decimal:
        return Decimal(self.in_month) - self.lop_days

    @property
    def absent_days(self) -> Decimal:
        value = (
            Decimal(self.working_days)
            - self.present_days
            - self.paid_leave_days
            - self.unpaid_leave_days
        )
        return max(value, ZERO)


def compute_line(
    *,
    structure: list[dict[str, Any]],
    days: Days,
    statutory: dict[str, Any],
    claims: list[dict[str, Any]],
    adjustments: list[dict[str, Any]],
    pf_applicable: bool = True,
) -> dict[str, Any]:
    """The payslip numbers for one person for one month. PF is left out when the salary says it
    does not apply (a choice open only at a gross of ₹25,000 or more), and ESI is left out when no
    component of the salary counts as ESI wage."""
    factor = days.paid_days / Decimal(days.in_month)
    earnings = []
    pf_wage = ZERO
    esi_wage = ZERO
    for c in structure:
        full = Decimal(c["amount"])
        earned = money(full * factor)
        earnings.append(
            {"code": c["code"], "label": c["label"], "monthly": str(full), "amount": str(earned)}
        )
        if c.get("pf_wage"):
            pf_wage += earned
        if c.get("esi_wage", True):
            esi_wage += earned
    gross_full = sum((Decimal(c["amount"]) for c in structure), ZERO)
    gross_earned = sum((Decimal(e["amount"]) for e in earnings), ZERO)

    deductions: list[dict[str, Any]] = []
    employer: list[dict[str, Any]] = []
    pf = statutory["pf"]
    if pf["enabled"] and pf_applicable:
        base = pf_wage
        if pf.get("wage_ceiling") is not None:
            base = min(base, Decimal(pf["wage_ceiling"]))
        deductions.append(
            {
                "code": "PF_EMPLOYEE",
                "label": "Provident Fund (employee)",
                "amount": str(
                    _round(base * Decimal(pf["employee_rate_pct"]) / 100, pf["rounding"])
                ),
            }
        )
        employer.append(
            {
                "code": "PF_EMPLOYER",
                "label": "Provident Fund (employer)",
                "amount": str(
                    _round(base * Decimal(pf["employer_rate_pct"]) / 100, pf["rounding"])
                ),
            }
        )
    esi = statutory["esi"]
    if esi["enabled"] and esi_wage > 0 and gross_full <= Decimal(esi["gross_threshold"]):
        deductions.append(
            {
                "code": "ESI_EMPLOYEE",
                "label": "ESI (employee)",
                "amount": str(
                    _round(esi_wage * Decimal(esi["employee_rate_pct"]) / 100, esi["rounding"])
                ),
            }
        )
        employer.append(
            {
                "code": "ESI_EMPLOYER",
                "label": "ESI (employer)",
                "amount": str(
                    _round(esi_wage * Decimal(esi["employer_rate_pct"]) / 100, esi["rounding"])
                ),
            }
        )
    pt = statutory["pt"]
    if pt["enabled"]:
        amount = ZERO
        for slab in pt["slabs"]:
            top = None if slab["to"] is None else Decimal(slab["to"])
            if gross_earned >= Decimal(slab["from"]) and (top is None or gross_earned <= top):
                amount = Decimal(slab["monthly"])
        deductions.append(
            {"code": "PROFESSIONAL_TAX", "label": "Professional tax", "amount": str(money(amount))}
        )

    adj_earn = ZERO
    adj_ded = ZERO
    adj_lines = []
    for a in adjustments:
        amount = money(a["amount"])
        adj_lines.append(
            {
                "label": a["label"],
                "amount": str(amount),
                "taxable": bool(a.get("taxable", True)),
                "note": a.get("note"),
            }
        )
        if amount >= 0:
            adj_earn += amount
        else:
            adj_ded += -amount
    statutory_ded = sum((Decimal(d["amount"]) for d in deductions), ZERO)
    net = gross_earned + adj_earn - statutory_ded - adj_ded
    if net < 0:
        raise PayrollError("The deductions are more than the pay.")
    reimbursed = ZERO
    reimb_lines = []
    for c in claims:
        amount = money(c["amount"])
        reimbursed += amount
        reimb_lines.append(
            {
                "claimId": c["claim_id"],
                "label": c["label"],
                "date": c["date"],
                "amount": str(amount),
                "taxable": bool(c["taxable"]),
            }
        )
    return {
        "days": {
            "inMonth": days.in_month,
            "workingDays": days.working_days,
            "presentDays": str(days.present_days),
            "paidLeaveDays": str(days.paid_leave_days),
            "unpaidLeaveDays": str(days.unpaid_leave_days),
            "extraLopDays": str(days.extra_lop_days),
            "beforeJoiningDays": days.before_joining_days,
            "absentDays": str(days.absent_days),
            "lopDays": str(days.lop_days),
            "paidDays": str(days.paid_days),
        },
        "earnings": earnings,
        "deductions": deductions,
        "employer": employer,
        "adjustments": adj_lines,
        "reimbursements": reimb_lines,
        "gross_full": str(gross_full),
        "gross_earned": str(gross_earned),
        "total_deductions": str(statutory_ded + adj_ded),
        "net_pay": str(net),
        "reimbursement_total": str(reimbursed),
        "payable_total": str(net + reimbursed),
    }
