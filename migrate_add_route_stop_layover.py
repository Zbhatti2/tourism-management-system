"""
Migration: adds package_route_stops.is_layover and .layover_hours, per
Zeb's request to flag a transit/connection stop on the Route Planning
form so the irrelevant "Nights" field can be masked in favor of an
"Approximate Hrs" field.

What this does, in order:

  1. Adds package_route_stops.is_layover (INTEGER, defaults to 0 for
     every existing row -- nothing already on file is reinterpreted as a
     layover) if missing.
  2. Adds package_route_stops.layover_hours (nullable NUMERIC) if
     missing.

Same idempotent "PRAGMA table_info + ALTER TABLE ADD COLUMN" pattern as
migrate_add_employee_gender.py / migrate_add_package_service_link.py.
Safe to re-run.

New installs don't need this: schema.sql already has this shape. Run
this once against a database created before this change:

    flask --app app migrate-add-route-stop-layover

or directly:

    python migrate_add_route_stop_layover.py
"""
import sqlite3

from config import Config


def migrate():
    db = sqlite3.connect(Config.DATABASE_PATH)
    db.row_factory = sqlite3.Row
    try:
        columns = [row[1] for row in db.execute("PRAGMA table_info(package_route_stops)").fetchall()]
        if "is_layover" not in columns:
            db.execute("ALTER TABLE package_route_stops ADD COLUMN is_layover INTEGER NOT NULL DEFAULT 0")
            db.commit()
            print("Added package_route_stops.is_layover.")
        else:
            print("package_route_stops.is_layover already exists — nothing to do there.")

        columns = [row[1] for row in db.execute("PRAGMA table_info(package_route_stops)").fetchall()]
        if "layover_hours" not in columns:
            db.execute("ALTER TABLE package_route_stops ADD COLUMN layover_hours NUMERIC")
            db.commit()
            print("Added package_route_stops.layover_hours.")
        else:
            print("package_route_stops.layover_hours already exists — nothing to do there.")
    finally:
        db.close()


if __name__ == "__main__":
    migrate()
