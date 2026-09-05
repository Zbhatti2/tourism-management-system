"""
Migration: adds package_components.quantity and .repeat_for_checkpoint, per
Zeb's format for the Day card's Hotel/Room and Other Service Costing
tables -- QTY x Unit Cost/Unit Price -> Total Cost/Total Price -> Profit
Margin, plus a "Repeat for Checkpoint" flag.

  - quantity (NUMERIC, defaults to 1 for every existing row -- nothing
    already on file changes cost) is the QTY column; Total Cost/Total
    Price/Profit Margin are computed at render time from quantity x
    unit_cost/unit_price, not stored.
  - repeat_for_checkpoint (INTEGER, defaults to 0) marks a line item that
    should show on every Day within its Checkpoint, not just the Day it
    was added to -- e.g. one hotel room entered once instead of once per
    night. Same independent-flag pattern as is_layover/is_checkpoint/
    is_accommodation. Only meaningful for a component whose day_id belongs
    to a Checkpoint-flagged Route Stop; a harmless no-op otherwise. See
    view_package()'s repeat-inheritance pass in blueprints/packages.py.

What this does, in order:

  1. Adds package_components.quantity (NUMERIC, default 1) if missing.
  2. Adds package_components.repeat_for_checkpoint (INTEGER, default 0) if
     missing.

Same idempotent "PRAGMA table_info + ALTER TABLE ADD COLUMN" pattern as
migrate_add_component_day_costing.py. Safe to re-run.

New installs don't need this: schema.sql already has this shape. Run this
once against a database created before this change:

    flask --app app migrate-add-component-quantity-repeat

or directly:

    python migrate_add_component_quantity_repeat.py
"""
import sqlite3

from config import Config


def migrate():
    db = sqlite3.connect(Config.DATABASE_PATH)
    db.row_factory = sqlite3.Row
    try:
        columns = [row[1] for row in db.execute("PRAGMA table_info(package_components)").fetchall()]
        if "quantity" not in columns:
            db.execute("ALTER TABLE package_components ADD COLUMN quantity NUMERIC NOT NULL DEFAULT 1")
            db.commit()
            print("Added package_components.quantity.")
        else:
            print("package_components.quantity already exists — nothing to do there.")

        columns = [row[1] for row in db.execute("PRAGMA table_info(package_components)").fetchall()]
        if "repeat_for_checkpoint" not in columns:
            db.execute("ALTER TABLE package_components ADD COLUMN repeat_for_checkpoint INTEGER NOT NULL DEFAULT 0")
            db.commit()
            print("Added package_components.repeat_for_checkpoint.")
        else:
            print("package_components.repeat_for_checkpoint already exists — nothing to do there.")
    finally:
        db.close()


if __name__ == "__main__":
    migrate()
