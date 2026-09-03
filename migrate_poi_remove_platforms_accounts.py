"""
Migration: removes the "Platforms & Subscriptions" and "Accounts" modules
and adds the new "Points of Interest" module, on an existing database —
safe to re-run, and does NOT drop or lose any existing data.

What this does, in order:

  1. Archive-renames (never DROPs) every table that belonged to the two
     removed modules, prefixing each with "_archived_" so the data is
     still sitting right there in the database file if it's ever needed
     again — just no longer wired up to any blueprint or UI:
       personal_accounts, personal_accounts_history, personal_account_types,
       cloud_platforms, cloud_platform_phones(+history),
       cloud_platform_emails(+history), cloud_service_subscriptions(+history),
       desktop_software_licenses(+history),
       desktop_software_license_phones(+history),
       desktop_software_license_emails(+history),
       desktop_software_license_reference_links, service_types, software_types.
     A table that's already been archived (or never existed on this
     database — e.g. a database created after this migration already
     shipped) is skipped.

  2. Creates the new `poi_types` (tenant-scoped lookup) and
     `points_of_interest` tables + indexes, if they don't already exist —
     matching schema.sql exactly, so a fresh `init-db` and a migrated
     older database end up with an identical shape.

  3. Adds the ~27 new Pakistan cities (Balakot, Bannu, Nankana Sahib, etc.
     — see seed_data.py's PAKISTAN_CITIES for the full list and the notes
     on the handful of province corrections applied) that the Sikh
     Gurdwara seed data needs. Idempotent — only inserts cities that
     aren't already there.

  4. For every existing tenant: seeds the 15 POI Types (Sikh Gurdwara,
     Mosque, Mandir, ... Other). For the Heritage Tours tenant
     specifically (tenant_code = 'HERITAGE'), also seeds the 149 Sikh
     Gurdwara points of interest from LIST OF ALL SIKH GURDWARAS IN
     PAKISTAN1a.docx (Region defaults to Asia, Country defaults to
     Pakistan, per the request) — this is demo data for that one tenant,
     not something sprung on every other tenant's database.

New installs don't need this: schema.sql already has the new shape (no
platforms/accounts tables, poi_types + points_of_interest present), and
seed_data.seed_first_tenant() already seeds the Gurdwara data for a fresh
Heritage Tours tenant. Run this once against a database created before
this change:

    flask --app app migrate-poi-remove-platforms-accounts

or directly:

    python migrate_poi_remove_platforms_accounts.py
"""
import sqlite3

from config import Config

# Every table that belonged to the removed "Platforms & Subscriptions" and
# "Accounts" modules. Archived (renamed), never dropped.
ARCHIVE_TABLES = [
    "personal_accounts",
    "personal_accounts_history",
    "personal_account_types",
    "cloud_platforms",
    "cloud_platform_phones",
    "cloud_platform_phones_history",
    "cloud_platform_emails",
    "cloud_platform_emails_history",
    "cloud_service_subscriptions",
    "cloud_service_subscriptions_history",
    "desktop_software_licenses",
    "desktop_software_licenses_history",
    "desktop_software_license_phones",
    "desktop_software_license_phones_history",
    "desktop_software_license_emails",
    "desktop_software_license_emails_history",
    "desktop_software_license_reference_links",
    "service_types",
    "software_types",
]


def _table_exists(db, name):
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def migrate():
    db = sqlite3.connect(Config.DATABASE_PATH)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA foreign_keys = OFF")  # renames below, re-enabled at the end

        # 1. Archive-rename the removed modules' tables.
        archived, skipped = 0, 0
        for table in ARCHIVE_TABLES:
            archived_name = f"_archived_{table}"
            if _table_exists(db, archived_name):
                skipped += 1
                continue
            if not _table_exists(db, table):
                skipped += 1
                continue
            db.execute(f"ALTER TABLE {table} RENAME TO {archived_name}")
            archived += 1
        db.commit()
        print(f"Archived {archived} table(s) from the removed modules "
              f"({skipped} already archived or not present, left alone).")

        # 2. Create poi_types + points_of_interest if missing.
        if not _table_exists(db, "poi_types"):
            db.execute("""
                CREATE TABLE poi_types (
                    poi_type_id     INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                    code            TEXT,
                    label           TEXT NOT NULL,
                    description     TEXT,
                    sort_order      INTEGER DEFAULT 0,
                    is_active       INTEGER NOT NULL DEFAULT 1,
                    UNIQUE(tenant_id, code)
                )
            """)
            db.execute("CREATE INDEX idx_poi_types_tenant ON poi_types(tenant_id)")
            print("Created poi_types table.")
        else:
            print("poi_types table already exists.")

        if not _table_exists(db, "points_of_interest"):
            db.execute("""
                CREATE TABLE points_of_interest (
                    poi_id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                    name            TEXT NOT NULL,
                    poi_type_id     INTEGER REFERENCES poi_types(poi_type_id),
                    year_established TEXT,
                    region_id       INTEGER REFERENCES regions(region_id),
                    country_id      INTEGER REFERENCES countries(country_id),
                    state_id        INTEGER REFERENCES states(state_id),
                    state_province_text TEXT,
                    city_id         INTEGER REFERENCES cities(city_id),
                    city_text       TEXT,
                    local_location  TEXT,
                    phone           TEXT,
                    fax             TEXT,
                    website         TEXT,
                    historical_significance TEXT,
                    local_contact_id INTEGER REFERENCES contacts(contact_id),
                    governing_authority_id INTEGER REFERENCES organizations(organization_id),
                    directions      TEXT,
                    map_coordinates TEXT,
                    notes           TEXT,
                    links           TEXT,
                    is_deleted      INTEGER NOT NULL DEFAULT 0,
                    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """)
            db.execute("CREATE INDEX idx_poi_tenant ON points_of_interest(tenant_id)")
            db.execute("CREATE INDEX idx_poi_type ON points_of_interest(poi_type_id)")
            db.execute("CREATE INDEX idx_poi_governing_authority ON points_of_interest(governing_authority_id)")
            db.execute("CREATE INDEX idx_poi_local_contact ON points_of_interest(local_contact_id)")
            print("Created points_of_interest table + indexes.")
        else:
            print("points_of_interest table already exists.")

        db.execute("PRAGMA foreign_keys = ON")
        db.commit()

        # 3. Ensure the GLOBAL geography tables (regions, countries, states,
        # cities, incl. the new Pakistan cities the Gurdwara seed data
        # needs) exist and are populated — same call
        # migrate_add_geography_hierarchy.py makes, safe to re-run
        # regardless of whether that migration already ran on this
        # database (every insert here is INSERT OR IGNORE or a manual
        # existence check).
        from seed_data import seed_global_lookups, _seed_simple, seed_gurdwara_pois, POI_TYPES
        seed_global_lookups(db)
        print("Ensured global geography (regions/countries/states/cities), including the "
              "new Pakistan cities the Gurdwara seed data needs, is fully seeded.")

        # 4. Per-tenant: POI Types for everyone, Gurdwara POIs for Heritage
        # Tours only (see module docstring).
        tenants = db.execute("SELECT tenant_id, tenant_code, tenant_name FROM tenants").fetchall()
        for t in tenants:
            _seed_simple(db, "poi_types", t["tenant_id"], POI_TYPES)
            db.commit()
            if t["tenant_code"] == "HERITAGE":
                seed_gurdwara_pois(db, t["tenant_id"])
                db.commit()
                print(f"Seeded POI Types + 149 Sikh Gurdwara points of interest for '{t['tenant_name']}'.")
            else:
                print(f"Seeded POI Types for '{t['tenant_name']}' (no Gurdwara demo data — that's Heritage Tours-specific).")

    finally:
        db.close()


if __name__ == "__main__":
    migrate()
