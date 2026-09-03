"""
Migration: adds the new "Organization Intelligence" module — a running,
append-only journal of freeform operational knowledge (see schema.sql's
MODULE G comment, and blueprints/intelligence.py's docstring, for the full
picture). Safe to re-run, and does NOT touch any existing data (it only
ever creates the table if missing, and seeds the Heritage Tours sample
entries if that tenant has none yet).

What this does, in order:

  1. Creates the organization_intelligence table + indexes, if it doesn't
     already exist — matching schema.sql exactly.

  2. Seeds the 7 sample "Organization Intelligence" entries (place-name
     spelling variants, Avari Hotel's pet/childcare policies, Nankana
     Resort and Punja Sahib drive times, and the Murree Hills winter-
     driving note) for the Heritage Tours tenant specifically — the same
     Heritage-Tours-specific demo data seed_data.seed_organization_
     intelligence() adds for a brand-new install, called here so an
     EXISTING database gets it too. Every other tenant is left untouched;
     this is sample data for the one tenant it was written about, not a
     default for every tenant (same reasoning as the Gurdwara POI seed).

     Idempotent: seed_organization_intelligence() itself no-ops if
     Heritage Tours already has any organization_intelligence rows, so
     re-running this migration never creates duplicates and never
     overwrites entries a real user has since added or edited.

New installs don't need this: schema.sql and seed_data.py already have the
new shape, and `flask --app app seed-tenant` seeds the sample entries as
part of creating the Heritage Tours tenant. Run this once against a
database created before this change:

    flask --app app migrate-organization-intelligence

or directly:

    python migrate_organization_intelligence.py
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
        # 1. New table.
        if not _table_exists(db, "organization_intelligence"):
            db.execute("""
                CREATE TABLE organization_intelligence (
                    intelligence_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                    entry_date      TEXT NOT NULL,
                    entered_by_user_id INTEGER REFERENCES users(user_id),
                    entered_by_name TEXT NOT NULL,
                    note            TEXT NOT NULL,
                    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """)
            db.execute("CREATE INDEX idx_organization_intelligence_tenant ON organization_intelligence(tenant_id)")
            db.execute("CREATE INDEX idx_organization_intelligence_date ON organization_intelligence(entry_date)")
            db.commit()
            print("Created organization_intelligence table.")
        else:
            print("organization_intelligence table already exists.")

        # 2. Heritage Tours sample entries (idempotent — see module docstring).
        from seed_data import seed_organization_intelligence

        heritage = db.execute("SELECT tenant_id, tenant_name FROM tenants WHERE tenant_code = 'HERITAGE'").fetchone()
        if heritage:
            before = db.execute(
                "SELECT COUNT(*) c FROM organization_intelligence WHERE tenant_id = ?", (heritage["tenant_id"],)
            ).fetchone()["c"]
            seed_organization_intelligence(db, heritage["tenant_id"])
            after = db.execute(
                "SELECT COUNT(*) c FROM organization_intelligence WHERE tenant_id = ?", (heritage["tenant_id"],)
            ).fetchone()["c"]
            if after > before:
                print(f"Seeded {after - before} sample Organization Intelligence entries for '{heritage['tenant_name']}'.")
            else:
                print(f"'{heritage['tenant_name']}' already has Organization Intelligence entries — nothing seeded.")
        else:
            print("No 'HERITAGE' tenant found — skipping the sample-entry seed (nothing to seed for other tenants).")

    finally:
        db.close()


if __name__ == "__main__":
    migrate()
