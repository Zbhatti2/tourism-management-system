"""
Migration: lets the AI Agents Hotel Intelligence agent propose and apply a
structured Address, not just Hotel Name/Company/Website/Notes -- Zeb
noticed that a proposed hotel's whole address had landed as free text in
its Notes field once applied ("The entire address fell into the Notes
field. The address should be parsed and entered into the address field").

Address isn't a plain column on suppliers -- it lives in its own
supplier_addresses table (typed, multiple rows per supplier, with
region/country/state/city lookups), unlike Hotel Name/Company/Website/
Notes, which are simple scalar columns ENTITY_FIELDS/_apply_supplier_hotel
already handled directly. This migration only adds the bookkeeping column
Undo Apply needs to also cover that separate table:

  ai_review_items.applied_address_snapshot -- JSON: the entity's primary
    supplier_addresses row (or JSON null, meaning none existed yet) exactly
    as it was immediately before an 'update' apply touched it -- alongside
    applied_snapshot's existing entity-row snapshot, not replacing it.
    NULL (the column itself) for any entity_type without its own separate
    address table (everything except 'supplier_hotel' today), or any item
    that was never applied.

See blueprints/ai_agents.py for the actual write/undo logic:
ENTITY_FIELDS["supplier_hotel"] now includes an "address" pseudo-field
(not a real suppliers column), parsed via address_parsing.
parse_free_text_address -- the same free-text-address parser the Suppliers
CSV importer already uses -- into supplier_addresses' structured columns
when a reviewer approves it.

A plain additive column (no CHECK constraint changes, so no table rebuild
needed here, unlike migrate_add_ai_agent_undo.py). Safe to re-run --
skipped if ai_review_items already has this column.

New installs don't need this: schema.sql already has the full shape. Run
this once against a database created before this change:

    flask --app app migrate-add-ai-agent-address-field

or directly:

    python migrate_add_ai_agent_address_field.py
"""
import sqlite3

from config import Config


def _has_column(db, table, column):
    return any(r["name"] == column for r in db.execute(f"PRAGMA table_info({table})").fetchall())


def migrate():
    db = sqlite3.connect(Config.DATABASE_PATH)
    db.row_factory = sqlite3.Row
    try:
        if not any(
            r["name"] == "ai_review_items"
            for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        ):
            print("ai_review_items table doesn't exist yet -- run migrate-add-ai-agent-foundations first. Nothing to do.")
            return

        if _has_column(db, "ai_review_items", "applied_address_snapshot"):
            print("ai_review_items.applied_address_snapshot already exists -- nothing to do.")
            return

        db.execute("ALTER TABLE ai_review_items ADD COLUMN applied_address_snapshot TEXT")
        db.commit()
        print(
            "Added ai_review_items.applied_address_snapshot -- the AI Agents Hotel Intelligence agent "
            "can now propose a structured Address (parsed into supplier_addresses), and Undo Apply can "
            "reverse that too, not just the entity's own scalar columns."
        )
    finally:
        db.close()


if __name__ == "__main__":
    migrate()
