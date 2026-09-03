"""
Migration: adds the "Human Resources" module (Human_Resource_Module1a.docx)
— Internal (Employees) and External (Resources) human resources, a shared
Roles lookup, and the Host Organization (the tenant's own company, managed
from System Management — see schema.sql MODULE H for the full picture).
Safe to re-run, and does NOT touch any existing data.

What this does, in order:

  1. Widens the shared `addresses` table's owner_type CHECK constraint to
     allow 'Employee' and 'ExternalResource' alongside the existing
     'Contact' — SQLite has no ALTER TABLE for a CHECK constraint, so this
     rebuilds the table (create new shape, copy every row across
     unchanged, drop the old table, rename) and recreates its two indexes.
     Skipped if the table already has the new shape (fresh installs off
     the current schema.sql, or a database this migration already ran
     against).

  2. Creates the 22 new tables this module needs (host_organizations,
     host_address_types, host_addresses, host_phone_types, host_phones,
     employee_types, departments, job_titles, employees, employee_emails,
     employee_phone_types, employee_phones, employee_education,
     employee_document_links, employees_history, employees_archive,
     resource_types, external_resources, external_resources_history,
     external_resources_archive, hr_roles, hr_role_assignments) if they
     don't already exist — matching schema.sql exactly.

  3. For every existing tenant: seeds the generic starter lookups
     (Employee Types, Departments, Job Titles, Employee Phone Types,
     Resource Types, HR Roles, Host Address Types, Host Phone Types) and
     auto-provisions that tenant's one Host Organization record —
     seed_data.seed_host_organization() is idempotent per tenant, same
     pattern as every other lookup seed in this codebase.

New installs don't need this: schema.sql and seed_data.py already have the
new shape, and `flask --app app seed-tenant` seeds everything as part of
creating a tenant. Run this once against a database created before this
change:

    flask --app app migrate-human-resources

or directly:

    python migrate_human_resources.py
"""
import sqlite3

from config import Config


def _table_exists(db, name):
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def _addresses_needs_widening(db):
    row = db.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'addresses'").fetchone()
    if row is None:
        return False  # addresses doesn't exist yet — nothing for this migration to widen (a much older/incomplete DB)
    return "'Employee'" not in row["sql"]


def _widen_addresses_owner_type(db):
    """Step 1 — see module docstring. Rebuilds `addresses` with the wider
    owner_type CHECK, preserving every existing row and both indexes
    exactly."""
    db.execute("PRAGMA foreign_keys = OFF")
    db.execute("""
        CREATE TABLE addresses_new (
            address_id      INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
            owner_type      TEXT NOT NULL CHECK (owner_type IN ('Contact', 'Employee', 'ExternalResource')),
            owner_id        INTEGER NOT NULL,
            address_type    TEXT,
            street          TEXT,
            unit            TEXT,
            region_id       INTEGER REFERENCES regions(region_id),
            country_id      INTEGER REFERENCES countries(country_id),
            state_id        INTEGER REFERENCES states(state_id),
            state_province_text TEXT,
            city_id         INTEGER REFERENCES cities(city_id),
            city_text       TEXT,
            postal_code     TEXT,
            is_primary      INTEGER NOT NULL DEFAULT 0,
            created_at      TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    db.execute("""
        INSERT INTO addresses_new
        SELECT address_id, tenant_id, owner_type, owner_id, address_type, street, unit, region_id, country_id,
               state_id, state_province_text, city_id, city_text, postal_code, is_primary, created_at, updated_at
        FROM addresses
    """)
    db.execute("DROP TABLE addresses")
    db.execute("ALTER TABLE addresses_new RENAME TO addresses")
    db.execute("CREATE INDEX idx_addresses_tenant ON addresses(tenant_id)")
    db.execute("CREATE INDEX idx_addresses_owner ON addresses(owner_type, owner_id)")
    db.commit()
    db.execute("PRAGMA foreign_keys = ON")
    db.commit()


def _create_hr_tables(db):
    """Step 2 — see module docstring. Matches schema.sql's MODULE H block
    exactly; each CREATE is guarded so a partially-migrated database (e.g.
    an earlier interrupted run) is still safe to re-run."""
    if not _table_exists(db, "host_organizations"):
        db.execute("""
            CREATE TABLE host_organizations (
                host_organization_id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id       INTEGER NOT NULL UNIQUE REFERENCES tenants(tenant_id),
                organization_name TEXT NOT NULL,
                website         TEXT,
                notes           TEXT,
                created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)

    if not _table_exists(db, "host_address_types"):
        db.execute("""
            CREATE TABLE host_address_types (
                address_type_id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                code            TEXT,
                label           TEXT NOT NULL,
                description     TEXT,
                sort_order      INTEGER DEFAULT 0,
                is_active       INTEGER NOT NULL DEFAULT 1,
                is_head_office  INTEGER NOT NULL DEFAULT 0,
                UNIQUE (tenant_id, code)
            )
        """)
        db.execute("CREATE INDEX idx_host_address_types_tenant ON host_address_types(tenant_id)")

    if not _table_exists(db, "host_addresses"):
        db.execute("""
            CREATE TABLE host_addresses (
                host_address_id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                host_organization_id INTEGER NOT NULL REFERENCES host_organizations(host_organization_id),
                address_type_id INTEGER REFERENCES host_address_types(address_type_id),
                street          TEXT,
                unit            TEXT,
                region_id       INTEGER REFERENCES regions(region_id),
                country_id      INTEGER REFERENCES countries(country_id),
                state_id        INTEGER REFERENCES states(state_id),
                state_province_text TEXT,
                city_id         INTEGER REFERENCES cities(city_id),
                city_text       TEXT,
                postal_code     TEXT,
                created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        db.execute("CREATE INDEX idx_host_addresses_tenant ON host_addresses(tenant_id)")
        db.execute("CREATE INDEX idx_host_addresses_host_org ON host_addresses(host_organization_id)")
        db.execute("CREATE INDEX idx_host_addresses_type ON host_addresses(address_type_id)")

    if not _table_exists(db, "host_phone_types"):
        db.execute("""
            CREATE TABLE host_phone_types (
                phone_type_id   INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                code            TEXT,
                label           TEXT NOT NULL,
                description     TEXT,
                sort_order      INTEGER DEFAULT 0,
                is_active       INTEGER NOT NULL DEFAULT 1,
                UNIQUE (tenant_id, code)
            )
        """)
        db.execute("CREATE INDEX idx_host_phone_types_tenant ON host_phone_types(tenant_id)")

    if not _table_exists(db, "host_phones"):
        db.execute("""
            CREATE TABLE host_phones (
                host_phone_id   INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                host_organization_id INTEGER NOT NULL REFERENCES host_organizations(host_organization_id),
                host_address_id INTEGER REFERENCES host_addresses(host_address_id),
                phone_type_id   INTEGER REFERENCES host_phone_types(phone_type_id),
                country_code    TEXT,
                area_code       TEXT,
                number          TEXT NOT NULL,
                extension       TEXT,
                is_primary      INTEGER NOT NULL DEFAULT 0,
                created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        db.execute("CREATE INDEX idx_host_phones_tenant ON host_phones(tenant_id)")
        db.execute("CREATE INDEX idx_host_phones_host_org ON host_phones(host_organization_id)")
        db.execute("CREATE INDEX idx_host_phones_address ON host_phones(host_address_id)")

    for table, pk in [("employee_types", "employee_type_id"), ("departments", "department_id"), ("job_titles", "job_title_id")]:
        if not _table_exists(db, table):
            db.execute(f"""
                CREATE TABLE {table} (
                    {pk}            INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                    code            TEXT,
                    label           TEXT NOT NULL,
                    description     TEXT,
                    sort_order      INTEGER DEFAULT 0,
                    is_active       INTEGER NOT NULL DEFAULT 1,
                    UNIQUE (tenant_id, code)
                )
            """)
            db.execute(f"CREATE INDEX idx_{table}_tenant ON {table}(tenant_id)")

    if not _table_exists(db, "employees"):
        db.execute("""
            CREATE TABLE employees (
                employee_id     INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                full_name       TEXT NOT NULL,
                employee_type_id INTEGER REFERENCES employee_types(employee_type_id),
                department_id   INTEGER REFERENCES departments(department_id),
                job_title_id    INTEGER REFERENCES job_titles(job_title_id),
                job_role        TEXT,
                date_of_birth   TEXT,
                host_address_id INTEGER REFERENCES host_addresses(host_address_id),
                date_of_hire    TEXT,
                monthly_salary  REAL,
                citizenship_country_id INTEGER REFERENCES countries(country_id),
                passport_number TEXT,
                visa_status     TEXT,
                tax_id          TEXT,
                national_id_number TEXT,
                profile_image_path TEXT,
                emergency_contact_name TEXT,
                emergency_contact_address TEXT,
                emergency_contact_phone TEXT,
                emergency_contact_email TEXT,
                notes           TEXT,
                is_deleted      INTEGER NOT NULL DEFAULT 0,
                created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        db.execute("CREATE INDEX idx_employees_tenant ON employees(tenant_id)")
        db.execute("CREATE INDEX idx_employees_department ON employees(department_id)")
        db.execute("CREATE INDEX idx_employees_host_address ON employees(host_address_id)")

    if not _table_exists(db, "employee_emails"):
        db.execute("""
            CREATE TABLE employee_emails (
                employee_email_id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                employee_id     INTEGER NOT NULL REFERENCES employees(employee_id),
                email_address   TEXT NOT NULL,
                is_primary      INTEGER NOT NULL DEFAULT 0,
                created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        db.execute("CREATE INDEX idx_employee_emails_tenant ON employee_emails(tenant_id)")
        db.execute("CREATE INDEX idx_employee_emails_employee ON employee_emails(employee_id)")

    if not _table_exists(db, "employee_phone_types"):
        db.execute("""
            CREATE TABLE employee_phone_types (
                phone_type_id   INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                code            TEXT,
                label           TEXT NOT NULL,
                description     TEXT,
                sort_order      INTEGER DEFAULT 0,
                is_active       INTEGER NOT NULL DEFAULT 1,
                UNIQUE (tenant_id, code)
            )
        """)
        db.execute("CREATE INDEX idx_employee_phone_types_tenant ON employee_phone_types(tenant_id)")

    if not _table_exists(db, "employee_phones"):
        db.execute("""
            CREATE TABLE employee_phones (
                employee_phone_id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                employee_id     INTEGER NOT NULL REFERENCES employees(employee_id),
                phone_type_id   INTEGER REFERENCES employee_phone_types(phone_type_id),
                country_code    TEXT,
                area_code       TEXT,
                number          TEXT NOT NULL,
                extension       TEXT,
                is_primary      INTEGER NOT NULL DEFAULT 0,
                created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        db.execute("CREATE INDEX idx_employee_phones_tenant ON employee_phones(tenant_id)")
        db.execute("CREATE INDEX idx_employee_phones_employee ON employee_phones(employee_id)")
        db.execute("CREATE INDEX idx_employee_phones_type ON employee_phones(phone_type_id)")

    if not _table_exists(db, "employee_education"):
        db.execute("""
            CREATE TABLE employee_education (
                education_id    INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                employee_id     INTEGER NOT NULL REFERENCES employees(employee_id),
                credential_type TEXT NOT NULL CHECK (credential_type IN ('Degree','Certificate')),
                title           TEXT NOT NULL,
                institution     TEXT,
                year_completed  TEXT,
                notes           TEXT,
                created_at      TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        db.execute("CREATE INDEX idx_employee_education_tenant ON employee_education(tenant_id)")
        db.execute("CREATE INDEX idx_employee_education_employee ON employee_education(employee_id)")

    if not _table_exists(db, "employee_document_links"):
        db.execute("""
            CREATE TABLE employee_document_links (
                link_id         INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                employee_id     INTEGER NOT NULL REFERENCES employees(employee_id),
                url             TEXT,
                document_path   TEXT,
                description     TEXT,
                notes           TEXT,
                created_at      TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        db.execute("CREATE INDEX idx_employee_document_links_tenant ON employee_document_links(tenant_id)")
        db.execute("CREATE INDEX idx_employee_document_links_employee ON employee_document_links(employee_id)")

    if not _table_exists(db, "employees_history"):
        db.execute("""
            CREATE TABLE employees_history (
                employee_history_id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                employee_id     INTEGER NOT NULL REFERENCES employees(employee_id),
                field_name      TEXT NOT NULL,
                previous_value  TEXT,
                superseded_at   TEXT NOT NULL DEFAULT (datetime('now')),
                superseded_reason TEXT
            )
        """)
        db.execute("CREATE INDEX idx_employees_history_tenant ON employees_history(tenant_id)")
        db.execute("CREATE INDEX idx_employees_history_employee ON employees_history(employee_id)")

    if not _table_exists(db, "employees_archive"):
        db.execute("""
            CREATE TABLE employees_archive (
                employee_id     INTEGER PRIMARY KEY,
                tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                snapshot        TEXT NOT NULL,
                deleted_at      TEXT NOT NULL DEFAULT (datetime('now')),
                purge_eligible_at TEXT
            )
        """)
        db.execute("CREATE INDEX idx_employees_archive_tenant ON employees_archive(tenant_id)")

    if not _table_exists(db, "resource_types"):
        db.execute("""
            CREATE TABLE resource_types (
                resource_type_id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                code            TEXT,
                label           TEXT NOT NULL,
                description     TEXT,
                sort_order      INTEGER DEFAULT 0,
                is_active       INTEGER NOT NULL DEFAULT 1,
                UNIQUE (tenant_id, code)
            )
        """)
        db.execute("CREATE INDEX idx_resource_types_tenant ON resource_types(tenant_id)")

    if not _table_exists(db, "external_resources"):
        db.execute("""
            CREATE TABLE external_resources (
                resource_id     INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                full_name       TEXT NOT NULL,
                resource_type_id INTEGER REFERENCES resource_types(resource_type_id),
                phone           TEXT,
                email           TEXT,
                tax_id          TEXT,
                national_id_number TEXT,
                hourly_rate     REAL,
                notes           TEXT,
                is_deleted      INTEGER NOT NULL DEFAULT 0,
                created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        db.execute("CREATE INDEX idx_external_resources_tenant ON external_resources(tenant_id)")
        db.execute("CREATE INDEX idx_external_resources_type ON external_resources(resource_type_id)")

    if not _table_exists(db, "external_resources_history"):
        db.execute("""
            CREATE TABLE external_resources_history (
                resource_history_id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                resource_id     INTEGER NOT NULL REFERENCES external_resources(resource_id),
                field_name      TEXT NOT NULL,
                previous_value  TEXT,
                superseded_at   TEXT NOT NULL DEFAULT (datetime('now')),
                superseded_reason TEXT
            )
        """)
        db.execute("CREATE INDEX idx_external_resources_history_tenant ON external_resources_history(tenant_id)")
        db.execute("CREATE INDEX idx_external_resources_history_resource ON external_resources_history(resource_id)")

    if not _table_exists(db, "external_resources_archive"):
        db.execute("""
            CREATE TABLE external_resources_archive (
                resource_id     INTEGER PRIMARY KEY,
                tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                snapshot        TEXT NOT NULL,
                deleted_at      TEXT NOT NULL DEFAULT (datetime('now')),
                purge_eligible_at TEXT
            )
        """)
        db.execute("CREATE INDEX idx_external_resources_archive_tenant ON external_resources_archive(tenant_id)")

    if not _table_exists(db, "hr_roles"):
        db.execute("""
            CREATE TABLE hr_roles (
                role_id         INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                code            TEXT,
                label           TEXT NOT NULL,
                description     TEXT,
                sort_order      INTEGER DEFAULT 0,
                is_active       INTEGER NOT NULL DEFAULT 1,
                UNIQUE (tenant_id, code)
            )
        """)
        db.execute("CREATE INDEX idx_hr_roles_tenant ON hr_roles(tenant_id)")

    if not _table_exists(db, "hr_role_assignments"):
        db.execute("""
            CREATE TABLE hr_role_assignments (
                assignment_id   INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                owner_type      TEXT NOT NULL CHECK (owner_type IN ('Employee','ExternalResource')),
                owner_id        INTEGER NOT NULL,
                role_id         INTEGER NOT NULL REFERENCES hr_roles(role_id),
                created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE (tenant_id, owner_type, owner_id, role_id)
            )
        """)
        db.execute("CREATE INDEX idx_hr_role_assignments_tenant ON hr_role_assignments(tenant_id)")
        db.execute("CREATE INDEX idx_hr_role_assignments_owner ON hr_role_assignments(owner_type, owner_id)")
        db.execute("CREATE INDEX idx_hr_role_assignments_role ON hr_role_assignments(role_id)")

    db.commit()


def migrate():
    db = sqlite3.connect(Config.DATABASE_PATH)
    db.row_factory = sqlite3.Row
    try:
        if _addresses_needs_widening(db):
            _widen_addresses_owner_type(db)
            print("Widened addresses.owner_type to also allow 'Employee'/'ExternalResource'.")
        else:
            print("addresses.owner_type already allows 'Employee'/'ExternalResource' — nothing to widen.")

        before_tables = {r["name"] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        _create_hr_tables(db)
        after_tables = {r["name"] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        created = sorted(after_tables - before_tables)
        if created:
            print(f"Created {len(created)} Human Resources table(s): {', '.join(created)}.")
        else:
            print("All Human Resources tables already exist.")

        # Step 3 — per-tenant lookups + Host Organization.
        from seed_data import (
            seed_host_organization, EMPLOYEE_TYPES, DEPARTMENTS, JOB_TITLES,
            EMPLOYEE_PHONE_TYPES, RESOURCE_TYPES, HR_ROLES, _seed_simple,
        )

        tenants = db.execute("SELECT tenant_id, tenant_name FROM tenants").fetchall()
        for t in tenants:
            seed_host_organization(db, t["tenant_id"])
            _seed_simple(db, "employee_types", t["tenant_id"], EMPLOYEE_TYPES)
            _seed_simple(db, "departments", t["tenant_id"], DEPARTMENTS)
            _seed_simple(db, "job_titles", t["tenant_id"], JOB_TITLES)
            _seed_simple(db, "employee_phone_types", t["tenant_id"], EMPLOYEE_PHONE_TYPES)
            _seed_simple(db, "resource_types", t["tenant_id"], RESOURCE_TYPES)
            _seed_simple(db, "hr_roles", t["tenant_id"], HR_ROLES)
            db.commit()
            print(f"Seeded Human Resources lookups and Host Organization for '{t['tenant_name']}'.")

    finally:
        db.close()


if __name__ == "__main__":
    migrate()
