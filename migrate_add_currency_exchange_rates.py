"""
Migration: adds Currency Rates & Exchange Rates — safe to re-run, and does
not touch or lose any existing tenant data.

Zeb: "I need a currency rates and exchange rate table with US dollar as
the base rate. This is something I overlooked. When Planning Tours, it
will be important to show prices in both the Host Currency (the Tenants
Currency) as well as the Destination Currency. And in case of Multi-country
tours, all the countries that the Tour will encounter (except for the
Layovers). For now we are focusing on a Single country tour but the design
should provide for multi country tours."

Design decisions (confirmed with Zeb before building — see schema.sql's
"MODULE E -- Currencies & Exchange Rates" comment for the full story):
  * GLOBAL, not per-tenant — one shared rate table every tenant reads.
  * Manual rate entry for now (no automated fetch).
  * "Foundation only" for this phase: the data model, country -> currency
    links, a tenant's Host Currency setting, and the manual rate entry/
    history screen — NOT the dual-currency display on Package/Tour costing
    screens (Route Stops, Day-by-Day costing, Price Tiers), which is a
    deliberately deferred follow-on phase.
  * Multi-country tours need no new schema — package_route_stops already
    has country_id + is_layover.

What this does, in order:
  1. Creates the new GLOBAL `currencies` table (ISO 4217) if missing.
  2. Creates the new GLOBAL `exchange_rates` table (current-rate cache,
     one row per currency) if missing.
  3. Creates the new GLOBAL `exchange_rate_history` table (append-only
     log of every rate ever entered) if missing.
  4. Adds `countries.currency_code` (a country's own Destination Currency)
     if missing.
  5. Adds `tenants.host_currency_code` (the Host Currency setting) if
     missing.
  6. Calls seed_data.seed_global_lookups() to seed the ~154 currencies,
     backfill every seeded country's currency_code, and seed USD's
     exchange rate at 1.0 (the base) — idempotent, so re-running this
     migration (or seeding a second tenant afterward) is always safe.

New installs don't need this: schema.sql already has the full shape, so
`flask --app app init-db` on a fresh database already has it. Run this
once against a database created before this change:

    flask --app app migrate-add-currency-exchange-rates

or directly:

    python migrate_add_currency_exchange_rates.py
"""
import sqlite3

from config import Config


def _columns(db, table):
    return [row[1] for row in db.execute(f"PRAGMA table_info({table})").fetchall()]


def migrate():
    db = sqlite3.connect(Config.DATABASE_PATH)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA foreign_keys = OFF")  # ALTER below, then re-enabled at the end

        # 1. currencies
        db.execute("""
            CREATE TABLE IF NOT EXISTS currencies (
                currency_id     INTEGER PRIMARY KEY AUTOINCREMENT,
                code            TEXT NOT NULL UNIQUE,
                label           TEXT NOT NULL,
                symbol          TEXT,
                sort_order      INTEGER DEFAULT 0,
                is_active       INTEGER NOT NULL DEFAULT 1
            )
        """)
        print("currencies table OK.")

        # 2. exchange_rates (current-value cache)
        db.execute("""
            CREATE TABLE IF NOT EXISTS exchange_rates (
                exchange_rate_id INTEGER PRIMARY KEY AUTOINCREMENT,
                currency_code   TEXT NOT NULL UNIQUE REFERENCES currencies(code),
                rate_to_usd     REAL NOT NULL,
                rate_as_of      TEXT NOT NULL,
                created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        print("exchange_rates table OK.")

        # 3. exchange_rate_history (append-only)
        db.execute("""
            CREATE TABLE IF NOT EXISTS exchange_rate_history (
                exchange_rate_history_id INTEGER PRIMARY KEY AUTOINCREMENT,
                currency_code   TEXT NOT NULL REFERENCES currencies(code),
                rate_to_usd     REAL NOT NULL,
                rate_as_of      TEXT NOT NULL,
                created_at      TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS idx_exchange_rate_history_currency ON exchange_rate_history(currency_code)")
        print("exchange_rate_history table OK.")

        # 4. countries.currency_code
        if "currency_code" not in _columns(db, "countries"):
            db.execute("ALTER TABLE countries ADD COLUMN currency_code TEXT REFERENCES currencies(code)")
            print("Added countries.currency_code.")
        else:
            print("countries.currency_code already exists.")

        # 5. tenants.host_currency_code
        if "host_currency_code" not in _columns(db, "tenants"):
            db.execute("ALTER TABLE tenants ADD COLUMN host_currency_code TEXT REFERENCES currencies(code)")
            print("Added tenants.host_currency_code.")
        else:
            print("tenants.host_currency_code already exists.")

        db.commit()
        db.execute("PRAGMA foreign_keys = ON")

        # 6. Seed currencies, backfill countries.currency_code, seed USD @ 1.0.
        # (Idempotent — every insert here is INSERT OR IGNORE or a manual
        # existence check; re-running never duplicates or overwrites data.)
        from seed_data import seed_global_lookups
        seed_global_lookups(db)
        print("Seeded currencies, backfilled country currency_code, seeded USD exchange rate at 1.0.")

    finally:
        db.close()


if __name__ == "__main__":
    migrate()
