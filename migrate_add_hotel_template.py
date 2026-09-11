"""
Migration: adds the "Hotel" Supplier Type Template (Sept 2026,
Amenities_and_Facilities1a.txt) to an existing database — safe to re-run,
and does NOT touch any existing data.

Per Zeb's request: "For Each Supplier Type, there will be a 'Template'
linked to the Supplier" — a Type-specific set of extra attributes shown on
the Supplier form/view. This migration builds the one Template actually
specified this phase: Hotel, with (1) an Amenities & Facilities checklist
and (2) a Room Types / room-count table.

What this does, in order:

  1. Adds supplier_types.template_key (TEXT, nullable) if missing — flags
     which Supplier Types get which specialized UI sections. Only 'Hotel'
     is set to 'hotel' by this migration; every other type stays NULL.

  2. Creates 4 new tables + indexes, if they don't already exist, matching
     schema.sql exactly: hotel_amenity_options (the master Amenities &
     Facilities list, grouped into sub-sections, each with a display
     icon — this is the "Templates table" the request refers to for
     icons), supplier_amenities (which of those a given Hotel Supplier
     actually offers), hotel_room_types (the master Room Types list), and
     supplier_rooms (a Hotel Supplier's own Room Type / room-count rows;
     Total Number of Rooms is computed as SUM(number_of_rooms) at display
     time, never stored).

  3. For every existing tenant: seeds HOTEL_AMENITY_OPTIONS and
     HOTEL_ROOM_TYPES (seed_data.seed_hotel_template()), and flags that
     tenant's 'Hotel' supplier_types row with template_key='hotel'.
     Idempotent — INSERT OR IGNORE keyed on (tenant_id, code); the flag
     UPDATE is itself idempotent.

New installs don't need this: schema.sql already has the new shape, and
seed_data.seed_first_tenant() (via seed_lookup_tables()) already seeds all
of the above for a fresh tenant. Run this once against a database created
before this change:

    flask --app app migrate-add-hotel-template

or directly:

    python migrate_add_hotel_template.py
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
        # 1. supplier_types.template_key
        columns = [row[1] for row in db.execute("PRAGMA table_info(supplier_types)").fetchall()]
        if "template_key" not in columns:
            db.execute("ALTER TABLE supplier_types ADD COLUMN template_key TEXT")
            db.commit()
            print("Added supplier_types.template_key.")
        else:
            print("supplier_types.template_key already exists — nothing to do there.")

        # 2. New tables.
        if not _table_exists(db, "hotel_amenity_options"):
            db.execute("""
                CREATE TABLE hotel_amenity_options (
                    amenity_option_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                    category        TEXT NOT NULL,
                    code            TEXT,
                    label           TEXT NOT NULL,
                    icon            TEXT,
                    sort_order      INTEGER DEFAULT 0,
                    is_active       INTEGER NOT NULL DEFAULT 1,
                    UNIQUE (tenant_id, code)
                )
            """)
            db.execute("CREATE INDEX idx_hotel_amenity_options_tenant ON hotel_amenity_options(tenant_id)")
            print("Created hotel_amenity_options table.")
        else:
            print("hotel_amenity_options table already exists.")

        if not _table_exists(db, "supplier_amenities"):
            db.execute("""
                CREATE TABLE supplier_amenities (
                    supplier_amenity_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                    supplier_id     INTEGER NOT NULL REFERENCES suppliers(supplier_id),
                    amenity_option_id INTEGER NOT NULL REFERENCES hotel_amenity_options(amenity_option_id),
                    UNIQUE (tenant_id, supplier_id, amenity_option_id)
                )
            """)
            db.execute("CREATE INDEX idx_supplier_amenities_supplier ON supplier_amenities(supplier_id)")
            db.execute("CREATE INDEX idx_supplier_amenities_option ON supplier_amenities(amenity_option_id)")
            print("Created supplier_amenities table.")
        else:
            print("supplier_amenities table already exists.")

        if not _table_exists(db, "hotel_room_types"):
            db.execute("""
                CREATE TABLE hotel_room_types (
                    room_type_id    INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                    code            TEXT,
                    label           TEXT NOT NULL,
                    default_description TEXT,
                    sort_order      INTEGER DEFAULT 0,
                    is_active       INTEGER NOT NULL DEFAULT 1,
                    UNIQUE (tenant_id, code)
                )
            """)
            db.execute("CREATE INDEX idx_hotel_room_types_tenant ON hotel_room_types(tenant_id)")
            print("Created hotel_room_types table.")
        else:
            print("hotel_room_types table already exists.")

        if not _table_exists(db, "supplier_rooms"):
            db.execute("""
                CREATE TABLE supplier_rooms (
                    supplier_room_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                    supplier_id     INTEGER NOT NULL REFERENCES suppliers(supplier_id),
                    room_type_id    INTEGER NOT NULL REFERENCES hotel_room_types(room_type_id),
                    description     TEXT,
                    number_of_rooms INTEGER NOT NULL DEFAULT 0,
                    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
                    UNIQUE (tenant_id, supplier_id, room_type_id)
                )
            """)
            db.execute("CREATE INDEX idx_supplier_rooms_supplier ON supplier_rooms(supplier_id)")
            db.execute("CREATE INDEX idx_supplier_rooms_type ON supplier_rooms(room_type_id)")
            print("Created supplier_rooms table.")
        else:
            print("supplier_rooms table already exists.")
        db.commit()

        # 3. Per-tenant: seed the two master lists + flag 'Hotel'.
        from seed_data import seed_hotel_template

        tenants = db.execute("SELECT tenant_id, tenant_name FROM tenants").fetchall()
        for t in tenants:
            seed_hotel_template(db, t["tenant_id"])
            print(f"Seeded the Hotel Template (Amenities & Facilities + Room Types) for '{t['tenant_name']}'.")
    finally:
        db.close()


if __name__ == "__main__":
    migrate()
