"""
Migration: nests hotel_amenity_options and hotel_room_types under
supplier_types (parent = the 'Hotel' row), and brings hotel_room_types'
description column in line with every other Table Maintenance lookup
table — per Zeb's follow-up request (Sept 2026): "Amenities Options and
Room Type will be Lookup tables associated with Supplier Type Hotel."

Follows migrate_add_hotel_template.py, which created these two tables
without a parent link. Safe to re-run, and does NOT touch any Amenities/
Room Types data already on file — every existing row is backfilled to
point at its tenant's own 'Hotel' supplier_types row, not reset.

What this does, in order:

  1. hotel_amenity_options: adds supplier_type_id (nullable FK to
     supplier_types) and description (TEXT) columns, if missing.
  2. hotel_room_types: adds supplier_type_id (nullable FK to
     supplier_types) if missing; renames default_description -> description
     if the old column name is still present (SQLite 3.25+ RENAME COLUMN).
  3. For every existing tenant: backfills supplier_type_id on any row in
     either table that's still NULL, pointing it at that tenant's 'Hotel'
     supplier_types row.
  4. Adds the two new indexes (idx_hotel_amenity_options_supplier_type,
     idx_hotel_room_types_supplier_type), if missing.

New installs don't need this: schema.sql already has the new shape, and
seed_data.seed_hotel_template() already writes supplier_type_id on every
row it seeds. Run this once against a database that already ran the
original migrate-add-hotel-template:

    flask --app app migrate-add-hotel-lookup-parent

or directly:

    python migrate_add_hotel_lookup_parent.py
"""
import sqlite3

from config import Config


def _has_column(db, table, column):
    return column in [row[1] for row in db.execute(f"PRAGMA table_info({table})").fetchall()]


def _has_index(db, name):
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?", (name,)
    ).fetchone() is not None


def migrate():
    db = sqlite3.connect(Config.DATABASE_PATH)
    db.row_factory = sqlite3.Row
    try:
        # 1. hotel_amenity_options
        if not _has_column(db, "hotel_amenity_options", "supplier_type_id"):
            db.execute("ALTER TABLE hotel_amenity_options ADD COLUMN supplier_type_id INTEGER REFERENCES supplier_types(supplier_type_id)")
            db.commit()
            print("Added hotel_amenity_options.supplier_type_id.")
        else:
            print("hotel_amenity_options.supplier_type_id already exists — nothing to do there.")

        if not _has_column(db, "hotel_amenity_options", "description"):
            db.execute("ALTER TABLE hotel_amenity_options ADD COLUMN description TEXT")
            db.commit()
            print("Added hotel_amenity_options.description.")
        else:
            print("hotel_amenity_options.description already exists — nothing to do there.")

        # 2. hotel_room_types
        if not _has_column(db, "hotel_room_types", "supplier_type_id"):
            db.execute("ALTER TABLE hotel_room_types ADD COLUMN supplier_type_id INTEGER REFERENCES supplier_types(supplier_type_id)")
            db.commit()
            print("Added hotel_room_types.supplier_type_id.")
        else:
            print("hotel_room_types.supplier_type_id already exists — nothing to do there.")

        if _has_column(db, "hotel_room_types", "default_description") and not _has_column(db, "hotel_room_types", "description"):
            db.execute("ALTER TABLE hotel_room_types RENAME COLUMN default_description TO description")
            db.commit()
            print("Renamed hotel_room_types.default_description -> description.")
        else:
            print("hotel_room_types.description already in place — nothing to do there.")

        # 3. Per-tenant backfill.
        tenants = db.execute("SELECT tenant_id, tenant_name FROM tenants").fetchall()
        for t in tenants:
            hotel_type = db.execute(
                "SELECT supplier_type_id FROM supplier_types WHERE tenant_id = ? AND label = 'Hotel'", (t["tenant_id"],)
            ).fetchone()
            if not hotel_type:
                continue
            n1 = db.execute(
                "UPDATE hotel_amenity_options SET supplier_type_id = ? WHERE tenant_id = ? AND supplier_type_id IS NULL",
                (hotel_type["supplier_type_id"], t["tenant_id"]),
            ).rowcount
            n2 = db.execute(
                "UPDATE hotel_room_types SET supplier_type_id = ? WHERE tenant_id = ? AND supplier_type_id IS NULL",
                (hotel_type["supplier_type_id"], t["tenant_id"]),
            ).rowcount
            db.commit()
            if n1 or n2:
                print(f"Backfilled supplier_type_id on {n1} Amenity Option(s) and {n2} Room Type(s) for '{t['tenant_name']}'.")

        # 4. Indexes.
        if not _has_index(db, "idx_hotel_amenity_options_supplier_type"):
            db.execute("CREATE INDEX idx_hotel_amenity_options_supplier_type ON hotel_amenity_options(supplier_type_id)")
            db.commit()
            print("Created idx_hotel_amenity_options_supplier_type.")
        if not _has_index(db, "idx_hotel_room_types_supplier_type"):
            db.execute("CREATE INDEX idx_hotel_room_types_supplier_type ON hotel_room_types(supplier_type_id)")
            db.commit()
            print("Created idx_hotel_room_types_supplier_type.")
    finally:
        db.close()


if __name__ == "__main__":
    migrate()
