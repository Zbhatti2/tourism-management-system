"""
Migration: adds packages.service_id -- links every Package back to the
'TP' (Tour Package) Category Services Inventory row it was created from,
per Zeb's request to auto-fill the New Package form (Package Number,
Package Description, Package Name) from an existing Service rather than
free-typing them.

What this does, in order:

  1. Adds packages.service_id (nullable FK into services) if missing,
     plus its index -- same idempotent "PRAGMA table_info + ALTER TABLE
     ADD COLUMN" pattern as migrate_add_employee_gender.py.

Nullable at the database level (existing packages created before this
change, if any, simply have service_id = NULL and keep whatever
package_code/package_name they already had -- nothing is renumbered or
backfilled, since there is no reliable way to infer which Service a
pre-existing free-typed package_code was meant to correspond to). The
application layer (blueprints/packages.py's new_package()) requires a
selection going forward, so every package created from here on has one.

Safe to re-run -- the column and index are added only if not already
present.

New installs don't need this: schema.sql already has this shape. Run
this once against a database created before this change:

    flask --app app migrate-add-package-service-link

or directly:

    python migrate_add_package_service_link.py
"""
import sqlite3

from config import Config


def migrate():
    db = sqlite3.connect(Config.DATABASE_PATH)
    db.row_factory = sqlite3.Row
    try:
        columns = [row[1] for row in db.execute("PRAGMA table_info(packages)").fetchall()]
        if "service_id" not in columns:
            db.execute("ALTER TABLE packages ADD COLUMN service_id INTEGER REFERENCES services(service_id)")
            db.commit()
            print("Added packages.service_id.")
        else:
            print("packages.service_id already exists — nothing to do there.")

        indexes = {row[1] for row in db.execute("PRAGMA index_list(packages)").fetchall()}
        if "idx_packages_service" not in indexes:
            db.execute("CREATE INDEX idx_packages_service ON packages(service_id)")
            db.commit()
            print("Created idx_packages_service index.")
        else:
            print("idx_packages_service already exists — nothing to do there.")
    finally:
        db.close()


if __name__ == "__main__":
    migrate()
