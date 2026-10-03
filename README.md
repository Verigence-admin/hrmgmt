# Verigence HRMgmt

HR service for Verigence: employee records, attendance with geofencing, leave, reimbursement,
salary structures, payroll and payslips. One Indian legal entity, IST, INR.

Design and decisions: [docs/DESIGN.md](docs/DESIGN.md).
Holidays (tentative, HR declares the final list): [docs/holidays-2026-tentative.md](docs/holidays-2026-tentative.md).

## Boundaries

- **Employee information lives here, outside any project workspace.** It is company-level data in
  the `hr` schema. It is not tenant/project scoped and Audit Core never stores it. Projects only
  supply work context (a person's project role and assigned outlets) through a read-only call.
- **Security** is the source of login and of HR permissions (module `hr`: HRADMIN, FINANCEADMIN,
  CEO), asked company-wide with no project. An employee reaches their own record because they are
  that employee, not through a role.
- **No code is imported from Audit Core or Security.** Cross-service data comes through APIs.
- Personal data (PAN, Aadhaar, bank, documents) is never written to logs; audit rows record that a
  protected field changed or was revealed, not its value.

## Run locally

```
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e ".[test,lint]" cryptography
export DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/hrtest
PYTHONPATH=src alembic upgrade head
pytest -q && ruff check . && ruff format --check . && mypy src
PYTHONPATH=src uvicorn hrmgmt.main:app_factory --factory --reload
```

See `.env.example` for configuration. Migrations are forward-only; Alembic keeps its history in
`hr.alembic_version` and refuses to run if the database records a revision this repository lacks.
