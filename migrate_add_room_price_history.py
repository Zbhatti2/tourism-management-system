"""
Migration: adds price tracking (with a full history log) to Hotel Room
Types -- safe to re-run, and does NOT touch any existing Room Type rows.

Per Zeb's request: "For Resource type 'Hotel', I need to track rooms
pricing for each 'Room type'. Pricing will change over time so I need to
track history prices in a price history log as well. A price history
table will track Room type, price/night (in USD), date updated."

What this does, in order:

  1. Adds supplier_rooms.price_per_night (REAL, NULL until a price is
     first entered) and supplier_rooms.price_as_of (TEXT, ISO
     'YYYY-MM-DD') if missing -- the "current price" cache shown on the
     Rooms list/form without a join.

  2. Creates supplier_room_price_history (if it doesn't already exist) --
     one append-only row per price change (or explicit reconfirmation of
     the same price on a later date), keyed to a supplier_rooms row.
     Reached by clicking "History" on the Edit Room Type form.

Nothing is backfilled into the history table: no existing supplier_rooms
row has a price on file yet (there was no price field before this
change), so there is nothing to build a history entry from. The very
first price entered for a room, going forward, becomes that room's first
history row -- see blueprints/suppliers.py's _save_room_price.

New installs don't need this: schema.sql already has this shape. Run this
once against a database created before this change:

    flask --app app migrate-add-room-price-history

or directly:

    python migrate_add_room_price_history.py
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
        # 1. supplier_rooms.price_per_night / price_as_of.
        columns = [row[1] for row in db.execute("PRAGMA table_info(supplier_rooms)").fetchall()]
        added = []
        if "price_per_night" not in columns:
            db.execute("ALTER TABLE supplier_rooms ADD COLUMN price_per_night REAL")
            added.append("price_per_night")
        if "price_as_of" not in columns:
            db.execute("ALTER TABLE supplier_rooms ADD COLUMN price_as_of TEXT")
            added.append("price_as_of")
        db.commit()
        if added:
            print(f"Added supplier_rooms columns: {', '.join(added)}.")
        else:
            print("supplier_rooms.price_per_night / price_as_of already exist — nothing to do there.")

        # 2. supplier_room_price_history table.
        if not _table_exists(db, "supplier_room_price_history"):
            db.execute("""
                CREATE TABLE supplier_room_price_history (
                    price_history_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                    supplier_room_id INTEGER NOT NULL REFERENCES supplier_rooms(supplier_room_id),
                    price_per_night REAL NOT NULL,
                    price_as_of     TEXT NOT NULL,
                    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """)
            db.execute("CREATE INDEX idx_supplier_room_price_history_tenant ON supplier_room_price_history(tenant_id)")
            db.execute("CREATE INDEX idx_supplier_room_price_history_room ON supplier_room_price_history(supplier_room_id)")
            db.commit()
            print("Created supplier_room_price_history table.")
        else:
            print("supplier_room_price_history table already exists.")
    finally:
        db.close()


if __name__ == "__main__":
    migrate()
