"""HR permission keys.

These strings must exist in Security's permission catalog (module `hr`) exactly as written.
HR permissions are company-wide: they are held through Security module roles (HRADMIN,
FINANCEADMIN, CEO), never through a project assignment. Ordinary employees need none of them:
what an employee may do with their own record comes from being that employee, not from a role.
"""

from __future__ import annotations

HR_EMPLOYEE_READ = "hr.employee.read"
HR_EMPLOYEE_MANAGE = "hr.employee.manage"
HR_SENSITIVE_READ = "hr.sensitive.read"
HR_AUDIT_READ = "hr.audit.read"
HR_SETTINGS_MANAGE = "hr.settings.manage"
HR_ATTENDANCE_READ_ALL = "hr.attendance.read_all"
HR_LEAVE_REVIEW = "hr.leave.review"
HR_CLAIM_REVIEW = "hr.claim.review"
HR_CLAIM_REVIEW_FINANCE = "hr.claim.review_finance"
HR_SALARY_PROPOSE = "hr.salary.propose"
HR_SALARY_APPROVE = "hr.salary.approve"
HR_PAYROLL_READ = "hr.payroll.read"
HR_PAYROLL_PREPARE = "hr.payroll.prepare"
HR_PAYROLL_APPROVE = "hr.payroll.approve"
# Feedback & Support tickets. No HR role holds it: only SuperAdmin passes it, through Security.
HR_SUPPORT_MANAGE = "hr.support.manage"

# Shown to the UI by GET /hr/v1/me so it can choose navigation. The server still re-checks
# every protected request; the UI list is a convenience, never the authority.
ALL_PERMISSIONS: tuple[str, ...] = (
    HR_EMPLOYEE_READ,
    HR_EMPLOYEE_MANAGE,
    HR_SENSITIVE_READ,
    HR_AUDIT_READ,
    HR_SETTINGS_MANAGE,
    HR_ATTENDANCE_READ_ALL,
    HR_LEAVE_REVIEW,
    HR_CLAIM_REVIEW,
    HR_CLAIM_REVIEW_FINANCE,
    HR_SALARY_PROPOSE,
    HR_SALARY_APPROVE,
    HR_PAYROLL_READ,
    HR_PAYROLL_PREPARE,
    HR_PAYROLL_APPROVE,
    HR_SUPPORT_MANAGE,
)
