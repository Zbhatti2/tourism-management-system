"""
Migration: adds the "Multiple Email and Phone" and "Documents Link"
features to the Organizations module — lets an organization have more
than one email address and phone number (instead of the single phone/
email text columns it had before), plus a per-organization list of
reference links (a web URL and/or local document path, each with a
short description/notes) for quickly attaching things like a brochure
or MOU. Safe to re-run, and does NOT touch any existing data it has
already migrated (it only ever adds tables/rows, never drops or
overwrites anything).

Organizations keeps its own, separate phone-type lookup from Suppliers,
per the standing rule that the two modules share no type/sub-type/
address/contact/phone data — see schema.sql's "MODULE A" and "MODULE C"
comments, and migrate_organization_addresses.py, which this migration
follows the exact same shape as.

What this does, in order:

  1. Creates the organization_phone_types lookup + index, and the
     organization_emails / organization_phones / organization_reference_
     links tables + indexes, if they don't already exist — matching
     schema.sql exactly. None of these get history tables: Organizations
     is treated as shared/reference data rather than an owned record
     with its own audit trail (see schema.sql's note on this), unlike
     Contacts' equivalent tables.

  2. For every existing tenant: seeds Organization Phone Types (Office,
     Mobile, Fax) via seed_data._seed_simple(). Idempotent — INSERT OR
     IGNORE keyed on (tenant_id, code).

  3. Backfills organization_emails/organization_phones rows for every
     existing organization that has a non-blank legacy email/phone value
     AND doesn't already have any rows in the matching new table — so
     this step is itself idempotent and safe to re-run, including
     against a database this migration has already been run against
     once (nothing double-inserts). A legacy value that has more than
     one email/phone jammed into it separated by ";" (the same
     convention used elsewhere in this app, e.g. the CSV importers) is
     split into one row per value; phones are tagged phone type
     "Office" since that's the best default for a company-level number
     with no further information. organizations.email/.phone themselves
     are never modified or cleared by this step; they stay in place as
     a legacy fallback/audit trail alongside the new structured rows.

     organization_reference_links has nothing to backfill from — there
     was no prior "documents link" data anywhere in the schema for
     Organizations, so this migration only needs to create that table;
     it starts out empty for every organization, same as a fresh install.

New installs don't need this: schema.sql already has the new shape (no
organizations have any legacy email/phone to backfill from yet on a
fresh install, so step 3 is a no-op there). Run this once against a
database created before this change:

    flask --app app migrate-organization-contact-info

or directly:

    python migrate_organization_contact_info.py
"""
import sqlite3

from config import Config


def _table_exists(db, name):
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def migrate():
    db = sqlite3.connect(Config.DATABASE_PATH)
    db.row_factory = sqlite3.Row
    try:
        # 1. New Organizations contact-info tables.
        if not _table_exists(db, "organization_phone_types"):
            db.execute("""
                CREATE TABLE organization_phone_types (
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
            db.execute("CREATE INDEX idx_organization_phone_types_tenant ON organization_phone_types(tenant_id)")
            print("Created organization_phone_types table.")
        else:
            print("organization_phone_types table already exists.")

        if not _table_exists(db, "organization_emails"):
            db.execute("""
                CREATE TABLE organization_emails (
                    organization_email_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                    organization_id INTEGER NOT NULL REFERENCES organizations(organization_id),
                    email_address   TEXT NOT NULL,
                    is_primary      INTEGER NOT NULL DEFAULT 0,
                    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """)
            db.execute("CREATE INDEX idx_organization_emails_tenant ON organization_emails(tenant_id)")
            db.execute("CREATE INDEX idx_organization_emails_org ON organization_emails(organization_id)")
            print("Created organization_emails table.")
        else:
            print("organization_emails table already exists.")

        if not _table_exists(db, "organization_phones"):
            db.execute("""
                CREATE TABLE organization_phones (
                    organization_phone_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                    organization_id INTEGER NOT NULL REFERENCES organizations(organization_id),
                    phone_type_id   INTEGER REFERENCES organization_phone_types(phone_type_id),
                    country_code    TEXT,
                    area_code       TEXT,
                    number          TEXT NOT NULL,
                    extension       TEXT,
                    is_primary      INTEGER NOT NULL DEFAULT 0,
                    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """)
            db.execute("CREATE INDEX idx_organization_phones_tenant ON organization_phones(tenant_id)")
            db.execute("CREATE INDEX idx_organization_phones_org ON organization_phones(organization_id)")
            db.execute("CREATE INDEX idx_organization_phones_type ON organization_phones(phone_type_id)")
            print("Created organization_phones table.")
        else:
            print("organization_phones table already exists.")

        if not _table_exists(db, "organization_reference_links"):
            db.execute("""
                CREATE TABLE organization_reference_links (
                    link_id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                    organization_id INTEGER NOT NULL REFERENCES organizations(organization_id),
                    url             TEXT,
                    document_path   TEXT,
                    description     TEXT,
                    notes           TEXT,
                    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """)
            db.execute("CREATE INDEX idx_organization_reference_links_tenant ON organization_reference_links(tenant_id)")
            db.execute("CREATE INDEX idx_organization_reference_links_org ON organization_reference_links(organization_id)")
            print("Created organization_reference_links table.")
        else:
            print("organization_reference_links table already exists.")

        db.commit()

        # 2. Per-tenant: seed Organization Phone Types.
        from seed_data import _seed_simple, ORGANIZATION_PHONE_TYPES

        tenants = db.execute("SELECT tenant_id, tenant_name FROM tenants").fetchall()
        for t in tenants:
            _seed_simple(db, "organization_phone_types", t["tenant_id"], ORGANIZATION_PHONE_TYPES)
            db.commit()
            print(f"Seeded Organization Phone Types for '{t['tenant_name']}'.")

        # 3. Backfill organization_emails/organization_phones from the
        # legacy email/phone columns, per tenant, for organizations that
        # don't already have rows in the matching new table.
        total_emails = 0
        total_phones = 0
        for t in tenants:
            tenant_id = t["tenant_id"]
            office_type_id = db.execute(
                "SELECT phone_type_id FROM organization_phone_types WHERE tenant_id = ? AND lower(label) = 'office'",
                (tenant_id,),
            ).fetchone()
            office_type_id = office_type_id["phone_type_id"] if office_type_id else None

            orgs_needing_emails = db.execute(
                """SELECT organization_id, email FROM organizations
                   WHERE tenant_id = ? AND email IS NOT NULL AND trim(email) != ''
                   AND organization_id NOT IN (
                       SELECT organization_id FROM organization_emails WHERE tenant_id = ?
                   )""",
                (tenant_id, tenant_id),
            ).fetchall()
            for org in orgs_needing_emails:
                addresses = [e.strip() for e in org["email"].split(";") if e.strip()]
                for i, addr in enumerate(addresses):
                    db.execute(
                        "INSERT INTO organization_emails (tenant_id, organization_id, email_address, is_primary) VALUES (?, ?, ?, ?)",
                        (tenant_id, org["organization_id"], addr, 1 if i == 0 else 0),
                    )
                    total_emails += 1

            orgs_needing_phones = db.execute(
                """SELECT organization_id, phone FROM organizations
                   WHERE tenant_id = ? AND phone IS NOT NULL AND trim(phone) != ''
                   AND organization_id NOT IN (
                       SELECT organization_id FROM organization_phones WHERE tenant_id = ?
                   )""",
                (tenant_id, tenant_id),
            ).fetchall()
            for org in orgs_needing_phones:
                numbers = [p.strip() for p in org["phone"].split(";") if p.strip()]
                for i, num in enumerate(numbers):
                    db.execute(
                        "INSERT INTO organization_phones (tenant_id, organization_id, phone_type_id, number, is_primary) VALUES (?, ?, ?, ?, ?)",
                        (tenant_id, org["organization_id"], office_type_id, num, 1 if i == 0 else 0),
                    )
                    total_phones += 1

            db.commit()

        print(f"Backfilled {total_emails} organization_emails row(s) and {total_phones} organization_phones "
              f"row(s) from existing organizations.email/.phone data. organizations.email/.phone themselves "
              f"were left untouched.")

    finally:
        db.close()


if __name__ == "__main__":
    migrate()
