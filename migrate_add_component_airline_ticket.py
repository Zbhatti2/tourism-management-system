"""
Migration: adds package_components.is_airline_ticket, per Zeb's request for
a dedicated "Airline Tickets" section on the package page -- a cost item he
had overlooked -- shown between Route (Country -> City) and Entire Tour
Services, rather than airline tickets getting folded anonymously into the
generic Entire Tour Services list.

  - is_airline_ticket (INTEGER, defaults to 0 for every existing row --
    nothing already on file is reinterpreted as an Airline Ticket) flags a
    whole-trip component (no route_stop_id, no day_id) as an Airline
    Ticket. Same independent-flag pattern as is_accommodation; only
    meaningful for a component with no day_id -- a Day-tied item still
    lives on its own Day's Accommodation/Other Service table regardless of
    this flag.

What this does, in order:

  1. Adds package_components.is_airline_ticket (INTEGER, default 0) if
     missing.

Same idempotent "PRAGMA table_info + ALTER TABLE ADD COLUMN" pattern as
migrate_add_component_quantity_repeat.py / migrate_add_component_day_costing.py.
Safe to re-run.

New installs don't need this: schema.sql already has this shape. Run this
once against a database created before this change:

    flask --app app migrate-add-component-airline-ticket

or directly:

    python migrate_add_component_airline_ticket.py
"""
import sqlite3

from config import Config


def migrate():
    db = sqlite3.connect(Config.DATABASE_PATH)
    db.row_factory = sqlite3.Row
    try:
        columns = [row[1] for row in db.execute("PRAGMA table_info(package_components)").fetchall()]
        if "is_airline_ticket" not in columns:
            db.execute("ALTER TABLE package_components ADD COLUMN is_airline_ticket INTEGER NOT NULL DEFAULT 0")
            db.commit()
            print("Added package_components.is_airline_ticket.")
        else:
            print("package_components.is_airline_ticket already exists — nothing to do there.")
    finally:
        db.close()


if __name__ == "__main__":
    migrate()
