"""
Migration: adds suppliers.is_external_resource, suppliers.tax_id,
suppliers.national_id_number, and suppliers.hourly_rate to a database
created before External Resources (Human Resources' old non-salaried/
contract-person record type) were retired in favor of Suppliers.

Per the request: "an external resource is also like a supplier ... it
makes sense to have their organization in the Supplier table. The only
caveat is that they must be flagged in the table as an external resource
since the contacts for Suppliers is a different table." A contractor is
now just a Supplier row with is_external_resource=1 -- getting Suppliers'
own address (supplier_addresses -- full Country/Province/City cascade,
multiple locations) and contact/phone handling (supplier_contacts /
supplier_contact_phones) instead of a separate HR record.

- is_external_resource: 1 = a contracted person/resource, 0 (default) = an
  ordinary vendor company. Every existing supplier is left at 0 -- this
  migration does not try to guess which existing suppliers, if any, should
  be reclassified.
- tax_id / national_id_number / hourly_rate: mainly relevant when
  is_external_resource=1, but harmless left blank on an ordinary supplier.

The old external_resources / external_resources_history /
external_resources_archive tables (and the resource_types lookup) are left
exactly as they are -- this migration does not touch or migrate any data
out of them. They were confirmed empty in the live database before this
change, so there was nothing to move; see blueprints/hr.py's retirement
note for the full story. If a database DOES have real rows in
external_resources that predate this migration, they are NOT automatically
copied into suppliers -- do that by hand (or ask for a one-off script)
before relying on this migration alone.

Safe to re-run -- each column and the index are each added only if not
already present.

New installs don't need this: schema.sql already includes these columns
and the index, so `flask --app app init-db` on a fresh database already
has them. Run this once against a database created before this change:

    flask --app app migrate-add-supplier-external-resource

or directly:

    python migrate_add_supplier_external_resource.py
"""
import sqlite3

from config import Config


def migrate():
    db = sqlite3.connect(Config.DATABASE_PATH)
    try:
        columns = [row[1] for row in db.execute("PRAGMA table_info(suppliers)").fetchall()]
        added = []

        if "is_external_resource" not in columns:
            db.execute("ALTER TABLE suppliers ADD COLUMN is_external_resource INTEGER NOT NULL DEFAULT 0")
            added.append("is_external_resource")
        if "tax_id" not in columns:
            db.execute("ALTER TABLE suppliers ADD COLUMN tax_id TEXT")
            added.append("tax_id")
        if "national_id_number" not in columns:
            db.execute("ALTER TABLE suppliers ADD COLUMN national_id_number TEXT")
            added.append("national_id_number")
        if "hourly_rate" not in columns:
            db.execute("ALTER TABLE suppliers ADD COLUMN hourly_rate REAL")
            added.append("hourly_rate")
        db.commit()

        existing_indexes = {row[1] for row in db.execute("PRAGMA index_list(suppliers)").fetchall()}
        if "idx_suppliers_is_external_resource" not in existing_indexes:
            db.execute("CREATE INDEX idx_suppliers_is_external_resource ON suppliers(is_external_resource)")
            db.commit()

        if added:
            print(f"Added suppliers columns: {', '.join(added)} (plus the is_external_resource index).")
        else:
            print("suppliers.is_external_resource / tax_id / national_id_number / hourly_rate already exist — nothing to do.")
    finally:
        db.close()


if __name__ == "__main__":
    migrate()
