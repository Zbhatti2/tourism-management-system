# Tourism Management System (TMS)

A multi-tenant, multi-user Flask + SQLite app for tour operators, run as a
SaaS platform: each tour operator (tenant) has its own isolated data, its
own users and its own encryption key.

| Tenant | Account # | Code | Website |
|---|---|---|---|
| TMS Platform (reserved, no logins) | 10000001 | TMS_PLATFORM | — |
| **Ma Vie Tours** (first tenant; Tenant Admin **Zeb**) | 10000002 | MAVIE | www.mavietours.com |

Hosted at **https://tms.ipromise.com** (Hostinger VPS2, Docker via Coolify —
same setup as the GSS app).

## Roles

- **SystemAdmin** — the platform ("TMS") level. Belongs to no tenant.
  Creates, suspends and reactivates tenants (Tenant Management), and runs
  whole-database health checks and backups. Created once per database with
  `flask --app app create-system-admin`.
- **TenantAdmin** — manages their own organization: its users (Manage
  Users), settings and data.
- **User** — day-to-day work inside their organization.

Usernames are unique across the whole system, so login needs no tenant
picker.

## Running locally

Double-click `Start_TMS.bat` (http://127.0.0.1:5050). Database changes are
applied automatically when the app starts (`db.py` → `MIGRATIONS`) — there
is no separate migration step any more.

Only for a brand-new, empty database:

```bash
flask --app app init-db        # creates instance/tms.db from schema.sql
flask --app app seed-tenant    # creates Ma Vie Tours + Zeb (TenantAdmin)
flask --app app create-system-admin
```

Other useful commands: `flask --app app set-password` (reset any user's
password), `flask --app app count-rows counts.txt` (row count of every
table — used to verify a database move).

## Hosting (Hostinger VPS2 / Coolify)

`Dockerfile` + `entrypoint.sh` build the production image (gunicorn, one
worker). In Coolify:

1. Application from this GitHub repo, branch `main`, build pack Dockerfile, port 5000.
2. Persistent volume mounted at `/app/instance` (holds `tms.db`,
   `tenant_master.key`, `secret_key`, uploads and backups).
3. Environment variables: `SESSION_COOKIE_SECURE=true`, `ANTHROPIC_API_KEY`.
4. Domain `https://tms.ipromise.com`.

The container never creates an empty database on its own: upload the real
`tms.db` **and its `tenant_master.key`** into the volume. (Set
`TMS_ALLOW_INIT=true` only to start a brand-new, empty system.) The
database cannot be read without the key it was created with.

## Nightly backups

`flask --app app nightly-backup` makes a consistent, integrity-checked copy
of the whole database into `instance/backups/` (newest 14 kept) and lists it
on System Management → Backup & Restore, where the System Admin can download
any copy to a PC. On the VPS, cron runs `deploy/tms-nightly-backup.sh` every
night; it also copies the backups and both key files to `/root/tms-backups`
(kept 30 days), outside the folder Coolify manages.

A backup can only be read with the `tenant_master.key` it was made with —
keep a copy of that key somewhere safe off the server too.

## Adding files and images

Images (POI and supplier) and documents (supplier documents, Documents &
Knowledge Base) are uploaded from the browser and stored in the database,
up to 25 MB each — this works the same on your PC and on the hosted app.



Only the code needed to build and update TMS. `.gitignore` keeps secrets
(`.env`, keys), the database, uploads, backups, notes, Word/PDF documents
and one-off data scripts on the PC.

## What changed from PIMS, and why

PIMS was single-user and single-tenant: one master password unlocked the
whole database, and that same password directly derived the key used to
encrypt sensitive fields (subscription passwords, account numbers, CVVs,
PINs, license serials). That's an elegant trick for one person on one
machine, but it doesn't extend to "several employees at Ma Vie Tours who
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
  lets several different users of Ma Vie Tours, and a Tenant Admin
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
their own tenant — this is what Zeb is for Ma Vie Tours), **User**
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
| B — Platforms & Subscriptions | **Removed.** This was carried over from the PIMS skeleton (personal cloud accounts, subscriptions, software licenses) along with Contacts and Organizations, but it isn't a fit for a Tourism/Travel Management System — no blueprint, route, or menu item references it any more. Superseded by the Points of Interest module. See `migrate_poi_remove_platforms_accounts.py` (disconnected it) and `migrate_drop_platforms_subscriptions_module.py` (permanently dropped its tables — every one was confirmed empty or unused seed data, nothing lost). |
| C — Accounts | Encrypted account number/CVV/PIN/password, shared `addresses` table, per-account history view — also currently disconnected from the app's navigation (not requested for removal; noted here for awareness) |
| D — Documents & Knowledge Base | Multiple locations per item, many-to-many knowledge domains, keyword/hashtag tagging |
| F — Data Exchange | CSV import staging for Contacts (upload → validate → review → commit), JSON export for all four data modules, plus CSV and vCard export for Contacts |
| System Management | Login, per-user password change, password reset via recovery seed phrase, DB integrity/health check, audit log viewer (tenant-scoped), retention setting (per-tenant), backup/restore (whole-database) |

## Known follow-ups (not yet done)

- A physical purge job for archived/soft-deleted contacts past their
  retention window exists but nothing schedules it beyond the
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
seed_data.py               Lookup table seed values + seed_first_tenant() (Ma Vie Tours / Zeb)
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
