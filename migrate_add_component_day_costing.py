"""
Migration: adds package_components.day_id and .is_accommodation, so a
Costing line can be itemized against one specific Day, not just a whole
Route Stop -- per Zeb's request to split each Day's card in the Day-by-Day
Itinerary into three sub-parts: Hotel/Room, POI's, and Other Service.

  - day_id (nullable INTEGER FK -> package_days) ties a component to one
    Day. Independent of route_stop_id -- a component can carry either,
    both, or neither (a whole-checkpoint item like a multi-night hotel
    block might stay on the Route Stop; a specific night's room, or a
    specific day's guided tour, goes on the Day).
  - is_accommodation (INTEGER, defaults to 0) marks the "Hotel/Room"
    sub-part vs "Other Service" -- same independent-flag pattern as
    is_layover/is_checkpoint on package_route_stops. The Day card (see
    view.html's day_card macro) shows the two lists separately; POI's
    stay their own thing via the pre-existing package_day_pois table.

Nothing already on file is reinterpreted: every existing component keeps
day_id NULL (still shown only in the package-wide Components table, same
as before this change) and is_accommodation 0 until edited and re-saved
through the new form.

What this does, in order:

  1. Adds package_components.day_id (nullable INTEGER FK) if missing.
  2. Adds package_components.is_accommodation (INTEGER, default 0) if
     missing.
  3. Creates idx_package_components_day on day_id if missing.

Same idempotent "PRAGMA table_info + ALTER TABLE ADD COLUMN" pattern as
migrate_add_route_stop_geography.py / migrate_add_route_stop_checkpoint.py.
Safe to re-run.

New installs don't need this: schema.sql already has this shape. Run this
once against a database created before this change:

    flask --app app migrate-add-component-day-costing

or directly:

    python migrate_add_component_day_costing.py
"""
import sqlite3

from config import Config


def migrate():
    db = sqlite3.connect(Config.DATABASE_PATH)
    db.row_factory = sqlite3.Row
    try:
        columns = [row[1] for row in db.execute("PRAGMA table_info(package_components)").fetchall()]
        if "day_id" not in columns:
            db.execute("ALTER TABLE package_components ADD COLUMN day_id INTEGER REFERENCES package_days(day_id)")
            db.commit()
            print("Added package_components.day_id.")
        else:
            print("package_components.day_id already exists — nothing to do there.")

        columns = [row[1] for row in db.execute("PRAGMA table_info(package_components)").fetchall()]
        if "is_accommodation" not in columns:
            db.execute("ALTER TABLE package_components ADD COLUMN is_accommodation INTEGER NOT NULL DEFAULT 0")
            db.commit()
            print("Added package_components.is_accommodation.")
        else:
            print("package_components.is_accommodation already exists — nothing to do there.")

        indexes = [row[1] for row in db.execute("PRAGMA index_list(package_components)").fetchall()]
        if "idx_package_components_day" not in indexes:
            db.execute("CREATE INDEX idx_package_components_day ON package_components(day_id)")
            db.commit()
            print("Created idx_package_components_day.")
        else:
            print("idx_package_components_day already exists — nothing to do there.")
    finally:
        db.close()


if __name__ == "__main__":
    migrate()
