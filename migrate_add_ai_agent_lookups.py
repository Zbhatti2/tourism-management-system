"""
Migration: adds the new lookup values needed to start the "Sikh Pilgrimage
Sector" AI Agent / Data Enrichment work (Sept 2026) -- Zeb's request to
identify Gurdwaras + Accommodation + Restaurants + Hospitals + Police
Stations + Tour Guides + Travel Agencies + Tour Operators + Airports +
Rail Systems across Pakistan, starting with one pilot city.

Most of those 10 categories already map onto existing Supplier Types
(Hotel/Resort accommodation, Restaurant, Travel Agent, Tour Operator) or
POI Types (Sikh Gurdwara). This migration adds the handful that had no
home yet:

  - 4 new poi_types: "Airport", "Railway Station", "Hospital",
    "Police Station" (request items #4/#5/#9/#10).
  - 1 new supplier_type: "Tour Guide", plus its sub-types
    (Government-Licensed Guide / Freelance Guide / Multilingual Guide /
    Other) (request item #6).

Idempotent — INSERT OR IGNORE keyed on (tenant_id, code), same as every
other tenant-scoped lookup seed. Safe to re-run.

New installs don't need this: schema.sql's POI_TYPES/SUPPLIER_TYPES lists
in seed_data.py already include these, and seed_first_tenant() already
seeds them for a fresh tenant. Run this once against a database created
before this change:

    flask --app app migrate-add-ai-agent-lookups

or directly:

    python migrate_add_ai_agent_lookups.py
"""
import sqlite3

from config import Config


def migrate():
    db = sqlite3.connect(Config.DATABASE_PATH)
    db.row_factory = sqlite3.Row
    try:
        from seed_data import _seed_simple, POI_TYPES, seed_supplier_lookups

        tenants = db.execute("SELECT tenant_id, tenant_name FROM tenants").fetchall()
        for t in tenants:
            _seed_simple(db, "poi_types", t["tenant_id"], POI_TYPES)
            db.commit()
            seed_supplier_lookups(db, t["tenant_id"])  # re-seeds all Supplier Types/Sub-Types; OR IGNORE keeps existing ones untouched
            db.commit()
            print(f"Seeded new POI Types (Airport/Railway Station/Hospital/Police Station) and the "
                  f"Tour Guide Supplier Type/Sub-Types for '{t['tenant_name']}'.")
    finally:
        db.close()


if __name__ == "__main__":
    migrate()
