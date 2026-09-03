"""
Migration: adds suppliers.company_name and three new supplier-level contact
tables (supplier_emails, supplier_phones, supplier_reference_links) to a
database created before the External Resource form/view were redesigned.

Follow-up to migrate_add_supplier_external_resource.py. After that change
shipped, using it revealed two problems with reusing the generic New/Edit
Supplier form for External Resources: it was easy to forget to check the
"External Resource" box (creating an unflagged supplier row instead), and
the form itself carried fields (Web page, Knowledge graph data, multiple
typed addresses, a separate "contact person" sub-record) that don't fit how
a contractor's own record actually works. Per the follow-up request:

    "The 'External Resource' is a Supplier Contractor but can be an
    individual or own a Company for billing purposes only. Leave the
    Original External Form but add a Field for Company and just ONE
    address for billing. Multiple Phone, eMails and links can exist like
    in the Organization Contact tables."

External Resources now get their own dedicated creation route
(suppliers.new_external_resource, /suppliers/resources/new) and their own
form/view templates, styled after the original Human Resources "External
Resource" screens (templates/hr/resource_form.html / resource_view.html --
kept in place, unused, per the standing "never destroy" convention) but
writing into the suppliers table:

- company_name: the billing company, when the contractor bills through one
  rather than as themselves (supplier_name is always their own name in that
  case). Harmless left blank on an ordinary supplier or an individual
  contractor with no company.
- supplier_emails / supplier_phones / supplier_reference_links: mirror
  organization_emails / organization_phones / organization_reference_links
  in shape (supplier_id instead of organization_id; supplier_phones reuses
  the existing `phone_types` lookup, same as supplier_contact_phones).
  These tables exist on every supplier row regardless of
  is_external_resource, but are only exposed in the External Resource
  form/view -- an ordinary supplier keeps using its supplier_contacts
  "contact person" sub-record instead.

A flagged supplier's billing address still lives in the existing
supplier_addresses table -- this migration does not touch that table --
just UI-restricted (blueprints/suppliers.py) to a single row instead of
supplier_addresses' normal multiple-addresses-with-types model.

Safe to re-run -- the column and each table/index are added only if not
already present. Does not touch any existing data.

New installs don't need this: schema.sql already includes these. Run this
once against a database created before this change:

    flask --app app migrate-add-supplier-resource-contact-info

or directly:

    python migrate_add_supplier_resource_contact_info.py
"""
import sqlite3

from config import Config


def migrate():
    db = sqlite3.connect(Config.DATABASE_PATH)
    try:
        columns = [row[1] for row in db.execute("PRAGMA table_info(suppliers)").fetchall()]
        added = []

        if "company_name" not in columns:
            db.execute("ALTER TABLE suppliers ADD COLUMN company_name TEXT")
            added.append("suppliers.company_name")
        db.commit()

        existing_tables = {
            row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }

        if "supplier_emails" not in existing_tables:
            db.execute(
                """CREATE TABLE supplier_emails (
                    supplier_email_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                    supplier_id     INTEGER NOT NULL REFERENCES suppliers(supplier_id),
                    email_address   TEXT NOT NULL,
                    is_primary      INTEGER NOT NULL DEFAULT 0,
                    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
                )"""
            )
            added.append("supplier_emails (table)")
        db.commit()

        if "supplier_phones" not in existing_tables:
            db.execute(
                """CREATE TABLE supplier_phones (
                    supplier_phone_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                    supplier_id     INTEGER NOT NULL REFERENCES suppliers(supplier_id),
                    phone_type_id   INTEGER REFERENCES phone_types(phone_type_id),
                    country_code    TEXT,
                    area_code       TEXT,
                    number          TEXT NOT NULL,
                    extension       TEXT,
                    is_primary      INTEGER NOT NULL DEFAULT 0,
                    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
                )"""
            )
            added.append("supplier_phones (table)")
        db.commit()

        if "supplier_reference_links" not in existing_tables:
            db.execute(
                """CREATE TABLE supplier_reference_links (
                    link_id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                    supplier_id     INTEGER NOT NULL REFERENCES suppliers(supplier_id),
                    url             TEXT,
                    document_path   TEXT,
                    description     TEXT,
                    notes           TEXT,
                    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
                )"""
            )
            added.append("supplier_reference_links (table)")
        db.commit()

        existing_indexes = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
        index_stmts = [
            ("idx_supplier_emails_tenant", "CREATE INDEX idx_supplier_emails_tenant ON supplier_emails(tenant_id)"),
            ("idx_supplier_emails_supplier", "CREATE INDEX idx_supplier_emails_supplier ON supplier_emails(supplier_id)"),
            ("idx_supplier_phones_tenant", "CREATE INDEX idx_supplier_phones_tenant ON supplier_phones(tenant_id)"),
            ("idx_supplier_phones_supplier", "CREATE INDEX idx_supplier_phones_supplier ON supplier_phones(supplier_id)"),
            ("idx_supplier_phones_type", "CREATE INDEX idx_supplier_phones_type ON supplier_phones(phone_type_id)"),
            ("idx_supplier_reference_links_tenant", "CREATE INDEX idx_supplier_reference_links_tenant ON supplier_reference_links(tenant_id)"),
            ("idx_supplier_reference_links_supplier", "CREATE INDEX idx_supplier_reference_links_supplier ON supplier_reference_links(supplier_id)"),
        ]
        for name, stmt in index_stmts:
            if name not in existing_indexes:
                db.execute(stmt)
                added.append(name)
        db.commit()

        if added:
            print(f"Added: {', '.join(added)}.")
        else:
            print("suppliers.company_name / supplier_emails / supplier_phones / supplier_reference_links already exist — nothing to do.")
    finally:
        db.close()


if __name__ == "__main__":
    migrate()
