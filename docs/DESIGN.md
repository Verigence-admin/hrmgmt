# Verigence HR Management (HRMgmt): Design v3

Status: DRAFT for your review. Supersedes `verigence-employee-module-design-v2.md`. Nothing is built yet.
Company: one Indian entity, about 40 employees (32 listed so far). Time zone IST. Currency INR.

Items marked **[ASSUMPTION]** are my reading of your answers where they were open. Each is a configuration value, not hard-coded, and is listed again in section 14 for your confirmation. Items marked **[NEEDS INPUT]** need something only you can give me.

---

## 1. Scope

In scope, first release: employee master and onboarding, self-service profile, designations, attendance with geofencing and photo, leave, reimbursement (travel and meals), payroll with generic salary templates and India statutory basics (PF, ESI, Professional Tax), payslips, audit history, mobile-responsive screens in the Verigence Web app (Capacitor Android included).

**Not in the first release, stated plainly so no screen pretends otherwise:** income tax (TDS) computation and Form 16, statutory returns and challans, gratuity and bonus calculation, bank payment file, notifications by email or WhatsApp, shift rosters and overtime. None of these appears in the UI.

## 2. Architecture

| Piece | Decision |
|---|---|
| Service | New repository `verigence/hrmgmt` (private), FastAPI + SQLAlchemy + Alembic, same stack as Audit Core and Security. Deploys to the existing Railway `attendance` service, renamed `hrmgmt` **[NEEDS INPUT: you create the empty private repo; I could not (403)]**. |
| Database | The shared Neon database, new schema `hr`, its own Alembic version table inside `hr`. No foreign keys to other schemas. Cross-service data comes through APIs, never direct reads of `auditcore` or `security`. |
| Identity and roles | Security stays the source for login and permissions. New Security module `hr` with module roles `HRADMIN`, `FINANCEADMIN`, `CEO`. |
| Project roles | PC, TL and PM come from Audit Core's `business_assignments` (per project). HRMgmt asks Audit Core, it does not copy the roles. |
| Locations | Outlet coordinates come from Audit Core `dealer_outlets` (latitude, longitude), through a new small read-only Audit Core endpoint (section 9). |
| Web | New HR screens in the existing Verigence Web app under `/hr`, no new frontend project. |
| Mobile | The existing Capacitor app. Camera and geolocation through Capacitor plugins. |
| Maps | Google Maps only, Geocoding API for the address on the photo. **[NEEDS INPUT: a Google Maps API key with Geocoding enabled, stored as a Railway secret; billing is on your Google account.]** |

HRMgmt never imports Audit Core or Security code. It validates Security tokens against the Security public keys, as the other services do.

## 3. People: Designation and Role are two separate things

**Designation** (what the person is in the company, assigned by HRAdmin, one per employee). Exactly four values, fixed as you specified: **Auditor, Senior Auditor, Assistant Manager, Manager**. Used by HR and payroll, it grants no access by itself.

**Role** (what the person does in a project, set per project in Audit Core's Project Management, which already exists): **PC, TL, PM**. HRMgmt reads it from Audit Core, it never stores or assigns it. PMO is the same as PM (role key `PM`).

Everyone is onboarded as **Employee** only. Designation and project roles are added afterwards. The `Department` column in your sheet (PC, RM, CRM, HR) is kept as plain information and does not decide any access.

**HR-module access** (company-wide, held per person in Security, not per project): **HRAdmin, FinanceAdmin, CEO**. These are what unlock the HR administration, finance and payroll screens.

- **CEO** holds every permission of TL, PM, HRAdmin and FinanceAdmin. A user can never approve their own request: a self-approval attempt is blocked at the API even for the CEO. The CEO's own requests are auto-recorded without approval **[ASSUMPTION]**.

### Approval authority

| Request | Approver |
|---|---|
| PC leave, late-attendance exception, location exception, travel claim | Any active TL or PM of the PC's project (TL and PM are project-wide in Audit Core) |
| Meals claim, and travel claims while the month's travel total is ₹3,000 or less | HRAdmin, after the TL/PM step for PCs |
| Travel claim that takes the month's travel total above ₹3,000 | FinanceAdmin |
| Claim older than 2 months | FinanceAdmin exception, in addition to the above |
| Leave of a TL | PM; of a PM, HRAdmin or FinanceAdmin | CEO **[ASSUMPTION]** |
| Salary structure change | HRAdmin proposes, FinanceAdmin approves |
| Payroll run | HRAdmin prepares, **CEO approves (CEO alone is enough)** |

The CEO can act at any step. HR sees everything but only approves what is assigned to HR.

## 4. Employee master, onboarding and self-service

**Initial load:** the 32 employees from your sheet, with the fields we have (name, DOB, gender, contact, personal email, qualification, Employee ID `JBR001`..., department, PAN, Aadhaar, address). The rest is completed through onboarding and profile update. Before loading, the data issues I found need fixing at the source: duplicate PAN on Sl. No. 21 and 23, invalid Aadhaar on 10 and 26, malformed contact on 13, and inconsistent qualification spellings (I normalise them in the loader and show a report).

**Onboarding:** HR creates or imports an employee, links the employee to a Security login, and the employee completes the profile. The employee record can exist before a login exists (login optional), and a user is never created silently.

**Employee fields:** identity (code, name, DOB, gender), contact (mobile, personal email, secondary email, address), emergency contact (name, relation, number), qualification, designation, department, joining date, employment status, project tag, photo, documents, bank (account, IFSC, holder), PAN, Aadhaar, UAN and ESI numbers.

**Employee can edit (self-service):** address, emergency contact number, secondary email, own photo. Everything else is read-only to the employee and changed by HR with an audit entry. Edits to sensitive fields (bank, PAN) by HR require a reason.

**Sensitive data:** you decided PAN and Aadhaar are stored in full, unencrypted in the database. I will still enforce: field-level API access (only the employee themselves, HRAdmin, FinanceAdmin for bank, CEO), masked display by default with an explicit "reveal" action that is audited, no sensitive values in logs or error messages, no sensitive values in list endpoints. Document files (identity proofs, certificates) go to a private bucket, downloaded only through short-lived signed links issued after an access check, each issue audited. **[Risk noted once: Aadhaar storage has legal restrictions; please have your CA or lawyer confirm.]**

## 5. Attendance and geofencing

- One check-in and one check-out per working day, per employee. Sunday is the weekly off. Holidays come from a configurable company calendar.
- **Configurable times:** standard check-in 10:30, latest check-in 11:15, earliest check-out 17:00, standard check-out 18:30.
- **Late check-in** (after 11:15) and **early check-out** (before 17:00) show a **warning** and create a request for TL/PM approval. No pay effect for now. An approved exception counts as a normal work day **[as you said]**.
- **Geofence:** PCs only. The PC must be within **500 m of one of their assigned outlets** (Audit Core assignments), distance computed on the server (Haversine), never trusted from the client. TL, PM, HR, FinanceAdmin and CEO are not geofenced, but their location is still recorded.
- **Outside the fence:** the PC cannot silently check in. They submit a reason with the check-in, it becomes a location exception, TL/PM approves, and HR only sees it as a normal day once approved. If an outlet has no coordinates in Audit Core, the screen says so and routes the PC to an exception, it does not guess.
- **Photo with the location printed on it:** the app captures a live photo (camera only). The server stamps the **address (Google Maps), date and time (server clock, IST) and coordinates** onto the image and stores the stamped file. The stamp is made on the server, so it cannot be edited on the phone. The original is not kept.
- Check-in is rejected if the location accuracy is too poor or the position is too old (configurable limits).
- Every attendance event, exception and approval is stored append-only.

## 6. Leave

- Types: **Sick** and **Earned**, **10 days in total per year**, six-day week, Sunday off **[ASSUMPTION: Sick 5 and Earned 5, granted at the start of the year; configurable]**. Unpaid leave is recorded as loss of pay when balance is exhausted and HR approves it.
- Half-day supported. Days are computed on the server, skipping Sundays and holidays.
- Balance is a **ledger** (grants, deductions, adjustments, reversals), never an editable counter.
- Approval per section 3. The employee sees pending, approved, rejected, with reasons, and can cancel a pending request.

## 7. Reimbursement

- **Categories:** Bike taxi, Car or taxi, Personal bike (per-km rate, configurable), Outstation train, Meals. Each claim has category, expense date, amount, description, and one or more receipts (required except where a category is configured otherwise).
- **Limits (configurable):** travel total **₹5,000 per employee per month** **[ASSUMPTION: a claim that would exceed it is blocked with a clear message]**. Meals have their own limit once you set it.
- **₹3,000 rule:** the month's **travel** total decides HRAdmin versus FinanceAdmin **[as you said, aggregate of travel]**. Meals are not counted in it **[ASSUMPTION]**.
- **Cut-off:** claims **submitted by the 25th** of a month go into that month's payroll; later ones roll to the next month **[ASSUMPTION on exact month mapping]**. The screens show this rule.
- **Claims older than 2 months** need a FinanceAdmin exception approval.
- **Review actions:** approve, reject, request correction (employee edits and resubmits), with a reason on reject and correction.
- **Payment tracking:** statuses are Submitted, With TL/PM, With HR or Finance, Correction requested, Approved, **Handed to payroll**, Paid (when the payroll run is finalised), Rejected, Cancelled. The employee and HR can see whether an approved amount has been handed to payroll and in which run.
- **Tax:** nothing is assumed tax-free. Each category has a taxable flag; taxable amounts go to payroll as a taxable earning, tax-free as a non-taxable earning.

## 8. Payroll and payslips

- ~40 employees, one monthly run. Cut-off and pay dates are configurable.
- **Salary structure:** component-based (Basic, HRA, allowances, other earnings, deductions), effective-dated, per employee. **Generic templates** for you to edit later: a template for gross below ₹21,000 and one above ₹25,000. **[NEEDS INPUT: what applies between ₹21,001 and ₹25,000; until you say, a salary in that band must be assigned a template by HR explicitly and the screen shows it.]**
- **Change control:** HRAdmin proposes a structure change, FinanceAdmin approves, then it takes effect from its date. Every version is kept.
- **Statutory basics, all configuration, none hard-coded:** Provident Fund (employee and employer, wage ceiling), ESI (employee and employer, applicability threshold), Professional Tax (state slab table). **Rates, ceilings and slabs are entered by HR and must be confirmed by your CA before go-live.** I do not embed numbers from memory.
- **Run flow:** HRAdmin prepares (attendance and loss-of-pay days frozen at the cut-off, approved reimbursements pulled in, adjustments entered), the run is reviewed, **CEO approves**, the run is locked, payslips are generated. A finalised run is never edited; corrections are made in a new version or an adjustment in the next run.
- **Payslip:** PDF per employee per month with company details, earnings, deductions, employer contributions, net pay, reimbursements shown separately. Immutable once issued; a re-issue after a reversal keeps the old one marked superseded. Employees see only their own. **[NEEDS INPUT: company legal name, address, logo, PF/ESI registration numbers; placeholders until then.]**

## 9. Integrations

**Security** (changes in the Security repo): module `hr` with permissions and module roles HRADMIN, FINANCEADMIN, CEO. FinanceAdmin and HRAdmin role definitions already exist under the old `attendance` module; the new ones are created under `hr` and the old attendance RBAC data is retired with the PC attendance removal (section 12).

**Audit Core** (one small additive change, see section 15 for the isolation rules): a read-only, service-to-service endpoint that returns, for a given project, the active project roles, the assigned outlets with coordinates, and the TL and PM users. HRMgmt pulls it on a schedule and keeps its own copy, so no HR request ever waits on Audit Core. No HR logic enters Audit Core.

**Web** (changes in the Web repo): HR screens under `/hr`, with the navigation shown by role. All employee screens are mobile-first. A shared client for the HRMgmt API.

**Authorisation at runtime:** every endpoint checks the Security token and the HR permission, and checks ownership (an employee can only read or change their own record, claims, leave and payslips). Permission decisions are cached for a short time to avoid a Security call on every request.

## 10. Screens

Employee: Home (today's attendance, check-in and out, pending items), Attendance history, Leave (balance, apply, history), Reimbursements (new claim with receipts, status), Payslips, My Profile (editable fields, documents).
TL/PM: **Approvals** tab for leave, late and location exceptions, travel claims, for PCs of their project, with approve, reject and reasons.
HRAdmin: **Employee Administration portal** (directory, profile, onboarding, designation, project tag, documents, history), holiday calendar, leave and reimbursement settings, reimbursement review, payroll preparation, reports.
FinanceAdmin: salary structure approvals, claim approvals above the threshold and exceptions.
CEO: payroll approval, everything above.
Every screen has loading, empty, error, validation and pending states. A feature that is not built is not shown.

## 11. Quality and safety

- Validated request and response schemas (Pydantic), explicit error codes, no stack traces to clients.
- Versioned Alembic migrations, each tested on a fresh database and on an upgrade; the migration runner refuses to run if the recorded history and the files disagree (the lesson from the last incident).
- Append-only audit table for every HR and payroll action: who, what, old and new value (sensitive values recorded as "changed", not copied), when, from where.
- Money in `numeric`, never float; all payroll arithmetic covered by tests.
- Unit, API and migration tests; ruff, mypy, pytest in CI; no test is skipped to get green.
- Secrets only in Railway variables, never in the repo or logs.

## 12. Removal of the old PC attendance

After the HR attendance is live and verified: remove the PC attendance package, its deploy workflow and its Dockerfile from the Security repo; remove the Security attendance role and roster routes; retire the `attendance` module RBAC rows in Security (13 permissions, HRADMIN role, 440 tenant grants, platform grants) and the `attendance` schema (6 tables, test data), in the same careful order as the last cleanup (database first, then the repo, since both migration runners refuse missing history). The Railway `attendance` service is renamed `hrmgmt` and redeployed from the new repo.

## 13. Delivery phases

| Phase | Content |
|---|---|
| 0 | Repo, CI, Alembic, auth and authorisation skeleton, audit table, Security `hr` module, Audit Core work-context endpoint |
| 1 | Employee master, designations, onboarding, import of the 32 employees, self-service profile, documents, Employee Administration portal |
| 2 | Attendance, geofence, photo stamping, exceptions, TL/PM approvals tab |
| 3 | Leave |
| 4 | Reimbursement |
| 5 | Salary templates, statutory configuration, payroll, payslips |
| 6 | Removal of the old PC attendance, cutover, parallel run of one payroll month |

Each phase is deployed and checked on DEV before the next starts.

## 14. Assumptions and inputs, for your confirmation

1. Leave split Sick 5 and Earned 5, granted yearly.
2. The ₹5,000 travel limit is per employee per month and blocks a claim that would exceed it.
3. The 25th cut-off means "submitted by the 25th goes into that month's payroll".
4. Meals are outside the ₹3,000 rule and approved by HR within their own limit.
5. Leave of TL goes to PM; PM, HR, FinanceAdmin to the CEO; CEO's own leave is recorded without approval.
6. Needs from you: the empty private repo `verigence/hrmgmt`, a Google Maps API key with Geocoding, a storage bucket for documents, the company details for payslips, the statutory settings from your CA, the holiday calendar, the template for the ₹21,001 to ₹25,000 band, the missing 8 employees, and a decision on the duplicate PAN and invalid Aadhaar rows.

## 15. Updates after review (3 October 2026)

- **Employee information sits outside the project workspace.** Employee, salary, leave, claims and
  payslip data are company-level records in the `hr` schema. They are not project (tenant) scoped,
  and Audit Core does not store them. HR permissions are asked of Security company-wide (no project).
  A project only supplies work context (project role, assigned outlets, TL and PM) through a
  read-only call, and the Web screens for HR live under `/hr`, apart from project pages.
- **Holidays.** The tentative list for the rest of 2026 is in `docs/holidays-2026-tentative.md`.
  The calendar carries a status per date (tentative or declared); only declared holidays count as
  non-working days, and the screens say "Tentative. Final holidays are declared by HR" until HR
  declares them.

## 16. The HR module must not affect Audit Core (rules, 3 October 2026)

1. **Separate process.** HRMgmt runs as its own Railway service. It shares no CPU, memory or worker with Audit Core.
2. **Audit Core is never in an HR request path.** Attendance, leave, claims and payroll never call Audit Core while a person waits. HRMgmt keeps its own copy of project roles, assigned outlets and TL/PM lists, refreshed by one scheduled pull per day plus an HR "Refresh" button. If Audit Core is slow or down, HR keeps working on the last copy and shows how old it is. One call per project, a short timeout, no retries.
3. **Audit Core side stays tiny:** one read-only endpoint, one indexed query per project, service-token only, statement timeout, no writes, no new tables, no changes to existing code paths.
4. **Database isolation.** Recommended: HR uses its own database (its own Neon compute and connection limit), not the shared `neondb`, so a payroll run can never slow Audit Core queries or use its connections. The code needs no change for this: it only reads `DATABASE_URL`. Until that exists, HR is capped at 10 connections and runs heavy work (payroll) outside working hours.
5. **Web.** HR screens are a separate, lazily loaded part of the app; the audit screens do not load HR code.
6. **Security.** HR permission checks go to Security with a 60 second reuse of an ALLOW. For about 40 people this is a handful of calls per minute.

## 17. Attendance photo: live camera only (3 October 2026)

- The attendance screen offers only a live camera capture. There is no "choose from gallery or files" control anywhere in the attendance flow.
- **Mobile app (Capacitor):** the Camera plugin is called with the camera as the only source and "save to gallery" off. **Browser:** a live camera stream (`getUserMedia`) captured to an image; no file input is used. If camera permission is refused, the screen says so and the person cannot check in by photo (HR handles that as an exception).
- **What the server adds** (it cannot prove where a file came from, so it narrows the window):
  - Check-in starts by asking the server for a one-time capture token, valid for 2 minutes and for one photo. A photo without a valid, unused token is refused.
  - The server stamps the photo itself with address, IST time and coordinates; the time is the server's, never the phone's.
  - Image type, size and dimensions are validated; if the image carries a capture time that is more than the window away from now, the record is flagged for HR (flagged, not blocked, because some phones strip it).
  - Location accuracy and age are checked together with the 500 m geofence for PCs.
- Limit, said plainly: no web or app design can make it impossible for a determined person to feed a prepared image to the API. The above removes the normal ways of doing it and leaves a trail (token, server time, flag) for HR.
