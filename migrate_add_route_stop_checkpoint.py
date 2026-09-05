"""
Migration: adds package_route_stops.is_checkpoint, per Zeb's request to
flag a Route Stop as a "Checkpoint" -- a place the group needs
accommodations arranged for, i.e. a real planning stop -- as its own
tracked datapoint instead of a hand-typed "Checkpoint 1/2/3..." note
buried in Border Crossing / Handoff Notes.

Independent of is_layover: a stop can be a Checkpoint, a Layover,
neither, or (in principle) both -- is_layover marks a transit/connection
stop that masks Nights in favor of Approximate Hrs; is_checkpoint marks
one that needs lodging arranged, unrelated to whether it's a transit stop
or an overnight stay. The Route table shows it as its own "Checkpoint"
column/badge, same display pattern as the existing Layover badge (see
stop_form.html / view.html).

What this does, in order:

  1. Adds package_route_stops.is_checkpoint (INTEGER, defaults to 0 for
     every existing row -- nothing already on file is reinterpreted as a
     checkpoint) if missing.

Same idempotent "PRAGMA table_info + ALTER TABLE ADD COLUMN" pattern as
migrate_add_route_stop_layover.py / migrate_add_route_stop_geography.py.
Safe to re-run.

New installs don't need this: schema.sql already has this shape. Run
this once against a database created before this change:

    flask --app app migrate-add-route-stop-checkpoint

or directly:

    python migrate_add_route_stop_checkpoint.py
"""
import sqlite3

from config import Config


def migrate():
    db = sqlite3.connect(Config.DATABASE_PATH)
    db.row_factory = sqlite3.Row
    try:
        columns = [row[1] for row in db.execute("PRAGMA table_info(package_route_stops)").fetchall()]
        if "is_checkpoint" not in columns:
            db.execute("ALTER TABLE package_route_stops ADD COLUMN is_checkpoint INTEGER NOT NULL DEFAULT 0")
            db.commit()
            print("Added package_route_stops.is_checkpoint.")
        else:
            print("package_route_stops.is_checkpoint already exists — nothing to do there.")
    finally:
        db.close()


if __name__ == "__main__":
    migrate()
