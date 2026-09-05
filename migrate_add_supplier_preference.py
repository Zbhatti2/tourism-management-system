"""
Migration: adds suppliers.preference, per Zeb's request to be able to mark
a Top ("Primary") and a Secondary choice among many suppliers of the same
Type in the same City -- e.g. 100+ Hotels in Lahore, and the same need for
Restaurants, Transport Providers, and every other Supplier Type.

  - preference (TEXT, CHECK'd to 'Primary'/'Secondary', defaults to NULL
    for every existing row -- nothing already on file is reinterpreted as
    a preferred supplier) -- shown as a filter and a badge on the
    Suppliers list, the Supplier form, and the Supplier detail page.
    Simple per-supplier flag, not scoped to any one City/Type
    combination -- a supplier that operates in more than one city carries
    one Preference value across all of them.

What this does, in order:

  1. Adds suppliers.preference (TEXT, CHECK'd, default NULL) if missing.

Same idempotent "PRAGMA table_info + ALTER TABLE ADD COLUMN" pattern as
every other additive migration in this project (e.g.
migrate_add_component_airline_ticket.py). Safe to re-run.

New installs don't need this: schema.sql already has this shape. Run this
once against a database created before this change:

    flask --app app migrate-add-supplier-preference

or directly:

    python migrate_add_supplier_preference.py
"""
import sqlite3

from config import Config


def migrate():
    db = sqlite3.connect(Config.DATABASE_PATH)
    db.row_factory = sqlite3.Row
    try:
        columns = [row[1] for row in db.execute("PRAGMA table_info(suppliers)").fetchall()]
        if "preference" not in columns:
            db.execute("ALTER TABLE suppliers ADD COLUMN preference TEXT CHECK (preference IN ('Primary','Secondary'))")
            db.commit()
            print("Added suppliers.preference.")
        else:
            print("suppliers.preference already exists — nothing to do there.")
    finally:
        db.close()


if __name__ == "__main__":
    migrate()
