"""
Migration: adds the Region -> Country -> Province/State -> City geography
hierarchy to an existing database — safe to re-run, and does not touch or
lose any existing tenant data (contacts, organizations, accounts, etc.).

What this does, in order:
  1. Creates the new GLOBAL `regions` table (Region tier) if missing.
  2. Adds `countries.region_id` if missing.
  3. Creates the new GLOBAL `cities` table (City tier, FK to `states`) if missing.
  4. On `addresses`: renames the old free-text `city` column to `city_text`
     (existing values are preserved, not lost — they just move from an
     unstructured "city" field to the new free-text fallback field, which
     is exactly what they were); adds `city_id` and `region_id` if missing.
  5. Re-labels any existing US/Canada `states` rows that were seeded with
     the bug where `label` was just the 2-letter/short code (e.g. "NY")
     instead of the full name ("New York") — a pre-existing seed_data.py
     bug, fixed going forward, backfilled here for rows seeded before the
     fix.
  6. Calls seed_data.seed_global_lookups() to seed the 6 regions, assign
     every country's region_id, and seed Pakistan's provinces + cities
     (test data from the project brief) — all idempotent, so re-running
     this migration (or seeding a second tenant afterward) is always safe.

New installs don't need this: schema.sql already has the full hierarchy,
so `flask --app app init-db` on a fresh database already has it. Run this
once against a database created before this change:

    flask --app app migrate-add-geography-hierarchy

or directly:

    python migrate_add_geography_hierarchy.py
"""
import sqlite3

from config import Config


# code -> full name, for backfilling states rows seeded with label = code.
US_STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
}
CANADA_PROVINCE_NAMES = {
    "AB": "Alberta", "BC": "British Columbia", "MB": "Manitoba",
    "NB": "New Brunswick", "NL": "Newfoundland and Labrador",
    "NS": "Nova Scotia", "NT": "Northwest Territories", "NU": "Nunavut",
    "ON": "Ontario", "PE": "Prince Edward Island", "QC": "Quebec",
    "SK": "Saskatchewan", "YT": "Yukon",
}


def _columns(db, table):
    return [row[1] for row in db.execute(f"PRAGMA table_info({table})").fetchall()]


def migrate():
    db = sqlite3.connect(Config.DATABASE_PATH)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA foreign_keys = OFF")  # ALTER/rename below, then re-enabled at the end

        # 1. regions
        db.execute("""
            CREATE TABLE IF NOT EXISTS regions (
                region_id       INTEGER PRIMARY KEY AUTOINCREMENT,
                code            TEXT UNIQUE,
                label           TEXT NOT NULL,
                sort_order      INTEGER DEFAULT 0,
                is_active       INTEGER NOT NULL DEFAULT 1
            )
        """)
        print("regions table OK.")

        # 2. countries.region_id
        if "region_id" not in _columns(db, "countries"):
            db.execute("ALTER TABLE countries ADD COLUMN region_id INTEGER REFERENCES regions(region_id)")
            print("Added countries.region_id.")
        else:
            print("countries.region_id already exists.")

        # 3. cities
        db.execute("""
            CREATE TABLE IF NOT EXISTS cities (
                city_id         INTEGER PRIMARY KEY AUTOINCREMENT,
                state_id        INTEGER NOT NULL REFERENCES states(state_id),
                label           TEXT NOT NULL,
                sort_order      INTEGER DEFAULT 0,
                is_active       INTEGER NOT NULL DEFAULT 1
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS idx_cities_state ON cities(state_id)")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_cities_state_label ON cities(state_id, label)")
        print("cities table OK.")

        # states previously had no uniqueness constraint at all, so
        # INSERT OR IGNORE in seed_data._seed_states was a silent no-op —
        # every re-seed (e.g. provisioning a second tenant) would have
        # inserted duplicate US/Canada state rows. Defensively de-duplicate
        # any that already crept in (keeping the lowest state_id — the
        # original row) before adding the constraint that stops it
        # happening again.
        dupe_groups = db.execute("""
            SELECT country_id, code, MIN(state_id) AS keep_id
            FROM states WHERE code IS NOT NULL GROUP BY country_id, code HAVING COUNT(*) > 1
        """).fetchall()
        removed = 0
        for grp in dupe_groups:
            cur = db.execute(
                "DELETE FROM states WHERE country_id = ? AND code = ? AND state_id != ?",
                (grp["country_id"], grp["code"], grp["keep_id"]),
            )
            removed += cur.rowcount
        if removed:
            print(f"Removed {removed} duplicate state row(s) seeded by the pre-fix INSERT OR IGNORE bug.")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_states_country_code ON states(country_id, code) WHERE code IS NOT NULL")
        print("states uniqueness constraint OK.")

        # 4. addresses: city -> city_text (rename, preserves data), + city_id, region_id
        addr_cols = _columns(db, "addresses")
        if "city" in addr_cols and "city_text" not in addr_cols:
            db.execute("ALTER TABLE addresses RENAME COLUMN city TO city_text")
            print("Renamed addresses.city -> addresses.city_text (existing values preserved).")
            addr_cols = _columns(db, "addresses")
        elif "city_text" in addr_cols:
            print("addresses.city_text already exists.")
        if "city_id" not in addr_cols:
            db.execute("ALTER TABLE addresses ADD COLUMN city_id INTEGER REFERENCES cities(city_id)")
            print("Added addresses.city_id.")
        else:
            print("addresses.city_id already exists.")
        if "region_id" not in addr_cols:
            db.execute("ALTER TABLE addresses ADD COLUMN region_id INTEGER REFERENCES regions(region_id)")
            print("Added addresses.region_id.")
        else:
            print("addresses.region_id already exists.")

        db.commit()

        # 5. Backfill any existing US/Canada states rows seeded with label = code.
        relabeled = 0
        for country_code, names in (("US", US_STATE_NAMES), ("CA", CANADA_PROVINCE_NAMES)):
            country = db.execute("SELECT country_id FROM countries WHERE code = ?", (country_code,)).fetchone()
            if not country:
                continue
            for code, full_name in names.items():
                cur = db.execute(
                    "UPDATE states SET label = ? WHERE country_id = ? AND code = ? AND label = ?",
                    (full_name, country["country_id"], code, code),
                )
                relabeled += cur.rowcount
        db.commit()
        print(f"Re-labeled {relabeled} existing US/Canada state row(s) from code to full name.")

        db.execute("PRAGMA foreign_keys = ON")

        # 6. Seed regions, assign country region_id, seed Pakistan geography.
        # (Idempotent — every insert here is INSERT OR IGNORE or a manual
        # existence check; re-running never duplicates or overwrites data.)
        from seed_data import seed_global_lookups
        seed_global_lookups(db)
        print("Seeded regions, assigned country region_id, seeded Pakistan provinces + cities.")

    finally:
        db.close()


if __name__ == "__main__":
    migrate()
