# Tourism Management System — Phase 1.1

A multi-tenant, multi-user Flask + SQLite app. Phase 1.1 is the skeleton:
the data model, login, and navigation shell, adapted from an existing
single-user project called PIMS (Personal Information Management System),
reworked for multi-tenancy per the Tourism Management System project brief.

The first tenant is **Heritage Tours**, with **Zeb** (password **Zebra**) as
its Tenant Admin.

## Setup

Requires Python 3.10+.

```bash
python -m venv venv
source venv/bin/activate        # venv\Scripts\activate on Windows
pip install -r requirements.txt

flask --app app init-db         # creates instance/tms.db from schema.sql
flask --app app seed-tenant     # creates Heritage Tours + Zeb (TenantAdmin), seeds its lookup tables

flask --app app run --debug     # http://127.0.0.1:5000
```

Then log in at `/login` with User ID `Zeb`, password `Zebra`. Change the
password from System Management once you're in.

A `/setup` wizard also exists for provisioning **additional** tenants later
(Phase 1.3+, when this becomes a real SaaS with more than one tour
operator) — it's not needed for this first run since `seed-tenant` already
creates Heritage Tours directly.

## What changed from PIMS, and why

PIMS was single-user and single-tenant: one master password unlocked the
whole database, and that same password directly derived the key used to
encrypt sensitive fields (subscription passwords, account numbers, CVVs,
PINs, license serials). That's an elegant trick for one person on one
machine, but it doesn't extend to "several employees at Heritage Tours who
all need to read the same company's data" — so Phase 1.1 splits
authentication from field encryption:

- **Authentication** (`users.password_hash`, `security/passwords.py`) is a
  standard salted hash, checked per user, the normal way. Any user of a
  tenant logs in with their own password.
- **Field encryption** (`tenants.dek_wrapped`, `security/crypto.py`) is
  per-**tenant**, not per-user. Each tenant gets its own random Data
  Encryption Key, wrapped under one system-level master key
  (`instance/tenant_master.key`) and unwrapped by the server once a user's
  password check succeeds — never derived from any password. That's what
  lets several different users of Heritage Tours, and a Tenant Admin
  resetting a teammate's password, all read the same encrypted data without
  anything being re-encrypted.

See the "MODULE T" comment block at the top of `schema.sql` for the full
rationale, and the docstring in `security/crypto.py`.

**Every business table now carries its own `tenant_id` column** — Contacts,
Organizations, Platforms/Subscriptions, Accounts, Documents, Data Exchange,
and (per-tenant-customizable) lookup tables all filter and insert on
`tenant_id`, so one tenant can never see or modify another's data, even by
guessing a record ID in a URL. Only three lookup tables stay global, shared
by every tenant: `countries`, `states`, `country_phone_codes` — real-world
geography, not tenant opinion. Every other lookup table (contact
categories, organization types, service types, etc.) is tenant-scoped, so
each tour operator maintains its own picklists via Table Maintenance.

Roles: **SystemAdmin** (not tied to a tenant — provisions new tenants, runs
whole-database backups), **TenantAdmin** (manages users/settings within
their own tenant — this is what Zeb is for Heritage Tours), **User**
(day-to-day). In Phase 1.1's single-tenant desktop deployment, Backup &
Restore and Database Health are gated to TenantAdmin (rather than a
stricter SystemAdmin-only) since there's no separate ops team yet — tighten
that once a real multi-tenant deployment introduces a dedicated SystemAdmin
role.

This was verified end-to-end, not just read over: the app was actually run
(Flask test client), a second tenant was provisioned, and every module —
Contacts, Organizations, Platforms/Subscriptions (including the encrypted
password round-trip), Table Maintenance, System Management/Audit Log — was
confirmed to correctly scope its data to the logged-in tenant, including
rejecting a direct-by-ID request for another tenant's record (404, not an
error page that leaks that the record exists).

## Modules (from PIMS, carried over as-is besides tenant scoping)

| Module | Status |
|---|---|
| E — System Tables (lookups) | Built, seeded per-tenant with PIMS's proposed starter values |
| A — Contacts | Full CRUD, multi-value emails/phones/addresses with history tracking, reference links, quick-add organizations, per-contact history view |
| B — Platforms & Subscriptions | Cloud platforms (with support phones/emails), subscriptions with encrypted password, software licenses with encrypted serial number (plus phones/emails/reference links) |
| C — Accounts | Encrypted account number/CVV/PIN/password, shared `addresses` table, per-account history view |
| D — Documents & Knowledge Base | Multiple locations per item, many-to-many knowledge domains, keyword/hashtag tagging |
| F — Data Exchange | CSV import staging for Contacts (upload → validate → review → commit), JSON export for all four data modules, plus CSV and vCard export for Contacts |
| System Management | Login, per-user password change, password reset via recovery seed phrase, DB integrity/health check, audit log viewer (tenant-scoped), retention setting (per-tenant), backup/restore (whole-database) |

## Known follow-ups (not yet done)

- **Form-submitted foreign keys aren't tenant-validated.** A request that
  references another tenant's `platform_id`, `organization_id`, etc. by ID
  in a form field (as opposed to a URL path segment, which *is* checked)
  isn't currently rejected. Worth a follow-up pass before this is exposed
  beyond a trusted desktop deployment.
- **No user-management screens yet** (add/deactivate a teammate, assign
  role) — Phase 1.1 gets the schema and login working; building the actual
  "Users" admin UI is a natural next step.
- A physical purge job for archived/soft-deleted contacts past their
  retention window exists (`contacts_archive.purge_eligible_at`,
  `system_mgmt._purge_eligible`) but nothing schedules it beyond the
  once-a-day opportunistic check at login.
- PDF export (an option in `schema.sql`'s `export_jobs.format` CHECK
  constraint) isn't implemented.

## Architecture notes

- **No ORM.** `schema.sql` is the single source of truth for structure;
  `db.py` wraps plain `sqlite3` (Flask's own app-context connection
  pattern).
- **CSRF protection** is hand-rolled (`security/csrf.py`) rather than via
  Flask-WTF, to keep the dependency list minimal.
- **Audit log.** Every create/update/delete/login/setup/reset is written to
  `audit_log` via `db.log_action(...)`, tagged with `tenant_id` and
  `user_id` — see System Management → Audit Log in the running app.

## File layout

```
app.py                  Flask app factory, blueprint registration, module nav
config.py                Paths, scrypt params, session timeout, tenant master key
schema.sql                Full DDL — MODULE T at the top covers multi-tenancy
seed_data.py               Lookup table seed values + seed_first_tenant() (Heritage Tours / Zeb)
db.py                       sqlite3 connection handling + audit log helper, CLI commands
security/
  crypto.py                  Per-tenant DEK generation/wrap/unwrap, field encrypt/decrypt
  passwords.py                  Per-user login password hashing (new in Phase 1.1)
  session_keys.py                In-memory per-session tenant-DEK store
  wordlist.py                     Recovery-phrase word list + generator
  csrf.py                           Manual CSRF token issuance/validation
auth/
  routes.py                       Setup wizard (new-tenant provisioning), login, logout, forgot-password
  decorators.py                    @login_required, @tenant_admin_required, @system_admin_required
blueprints/
  contacts.py, organizations.py, platforms.py, accounts.py, documents.py,
  data_exchange.py, table_maintenance.py, system_mgmt.py, dashboard.py
templates/                        Bootstrap 5 (via CDN), Outlook-style left nav
```

## What's next (per the project plan)

Phase 1.1 is the skeleton: multi-tenant schema, login, dashboard, and the
carried-over PIMS modules. Phase 1.2 adds the Tourism-specific tables one
at a time, with seed data: Points of Interest, POI Type, Suppliers,
Supplier Type, Employees, Employee Type, Resources, Resource Type. Phase
1.3 is tour-package planning assisted by AI agents.
