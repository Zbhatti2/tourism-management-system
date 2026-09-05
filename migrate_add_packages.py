"""
Migration: adds the new "Package Management" module (Package Management &
Itinerary Builder) to an existing database — safe to re-run, and does NOT
touch any existing data.

Built from the "Next-Phase Blueprint" roadmap (published as an Artifact
alongside this session) — the load-bearing gap the roadmap identified:
nothing downstream (Departures, Bookings, Invoicing) has anything to
attach to until a sellable Package template exists. This migration
absorbs what the source planning docs called "Phase 1: Product Design"
AND "Phase 2: Costing & Pricing" into one module — costing is a property
of each package_components row (unit_cost/unit_price), not a separate
stage, since Products (migrate_add_products.py) already carries
price/cost.

Unlike Services/Products, Package Management has NO global taxonomy layer
— every table here is tenant-scoped (see schema.sql's MODULE K comment
for the full reasoning). Nothing here touches service_* or product_*
tables directly; package_components references services/products by FK
the same way it references suppliers, but creates no rows in those
tables.

What this does, in order:

  1. Creates the 6 new tenant-scoped tables (packages,
     package_route_stops, package_days, package_day_pois,
     package_components, package_price_tiers) + indexes, if they don't
     already exist — matching schema.sql's MODULE K exactly.

  2. Nothing to seed — a package is a tenant's own product, not shared
     coding vocabulary, so there's no taxonomy step here (contrast with
     migrate_add_services.py / migrate_add_products.py, which both seed a
     GLOBAL taxonomy in this step). Existing tenants simply get the new,
     empty tables and can start building packages immediately through the
     UI.

New installs don't need this: schema.sql already has the new shape. Run
this once against a database created before this change:

    flask --app app migrate-add-packages

or directly:

    python migrate_add_packages.py
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
        db.execute("PRAGMA foreign_keys = ON")

        if not _table_exists(db, "packages"):
            db.execute("""
                CREATE TABLE packages (
                    package_id      INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                    package_code    TEXT NOT NULL,
                    package_name    TEXT NOT NULL,
                    package_type    TEXT NOT NULL DEFAULT 'fixed_departure' CHECK (package_type IN ('fixed_departure','custom')),
                    duration_days   INTEGER,
                    duration_nights INTEGER,
                    min_pax         INTEGER,
                    max_pax         INTEGER,
                    difficulty_rating TEXT,
                    minimum_age     INTEGER,
                    status          TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','active','archived')),
                    description     TEXT,
                    inclusions      TEXT,
                    exclusions      TEXT,
                    base_currency   TEXT,
                    notes           TEXT,
                    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
                    UNIQUE (tenant_id, package_code)
                )
            """)
            db.execute("CREATE INDEX idx_packages_tenant ON packages(tenant_id)")
            db.execute("CREATE INDEX idx_packages_status ON packages(status)")
            print("Created packages table.")
        else:
            print("packages table already exists.")

        if not _table_exists(db, "package_route_stops"):
            db.execute("""
                CREATE TABLE package_route_stops (
                    stop_id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                    package_id      INTEGER NOT NULL REFERENCES packages(package_id),
                    sequence_number INTEGER NOT NULL,
                    country_id      INTEGER REFERENCES countries(country_id),
                    city_id         INTEGER REFERENCES cities(city_id),
                    city_text       TEXT,
                    nights          INTEGER,
                    border_crossing_notes TEXT,
                    UNIQUE (package_id, sequence_number)
                )
            """)
            db.execute("CREATE INDEX idx_package_stops_package ON package_route_stops(package_id)")
            db.execute("CREATE INDEX idx_package_stops_tenant ON package_route_stops(tenant_id)")
            print("Created package_route_stops table.")
        else:
            print("package_route_stops table already exists.")

        if not _table_exists(db, "package_days"):
            db.execute("""
                CREATE TABLE package_days (
                    day_id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                    package_id      INTEGER NOT NULL REFERENCES packages(package_id),
                    route_stop_id   INTEGER REFERENCES package_route_stops(stop_id),
                    day_number      INTEGER NOT NULL,
                    title           TEXT,
                    description     TEXT,
                    UNIQUE (package_id, day_number)
                )
            """)
            db.execute("CREATE INDEX idx_package_days_package ON package_days(package_id)")
            db.execute("CREATE INDEX idx_package_days_stop ON package_days(route_stop_id)")
            db.execute("CREATE INDEX idx_package_days_tenant ON package_days(tenant_id)")
            print("Created package_days table.")
        else:
            print("package_days table already exists.")

        if not _table_exists(db, "package_day_pois"):
            db.execute("""
                CREATE TABLE package_day_pois (
                    day_poi_id      INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                    day_id          INTEGER NOT NULL REFERENCES package_days(day_id),
                    poi_id          INTEGER NOT NULL REFERENCES points_of_interest(poi_id),
                    sequence_number INTEGER NOT NULL,
                    visit_notes     TEXT,
                    UNIQUE (day_id, poi_id)
                )
            """)
            db.execute("CREATE INDEX idx_package_day_pois_day ON package_day_pois(day_id)")
            db.execute("CREATE INDEX idx_package_day_pois_poi ON package_day_pois(poi_id)")
            db.execute("CREATE INDEX idx_package_day_pois_tenant ON package_day_pois(tenant_id)")
            print("Created package_day_pois table.")
        else:
            print("package_day_pois table already exists.")

        if not _table_exists(db, "package_components"):
            db.execute("""
                CREATE TABLE package_components (
                    component_id    INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                    package_id      INTEGER NOT NULL REFERENCES packages(package_id),
                    route_stop_id   INTEGER REFERENCES package_route_stops(stop_id),
                    component_type  TEXT NOT NULL CHECK (component_type IN ('service','product','custom')),
                    service_id      INTEGER REFERENCES services(service_id),
                    product_id      INTEGER REFERENCES products(product_id),
                    supplier_id     INTEGER REFERENCES suppliers(supplier_id),
                    description     TEXT,
                    unit            TEXT,
                    unit_cost       NUMERIC,
                    unit_price      NUMERIC,
                    currency        TEXT,
                    notes           TEXT,
                    sequence_number INTEGER NOT NULL DEFAULT 1,
                    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """)
            db.execute("CREATE INDEX idx_package_components_package ON package_components(package_id)")
            db.execute("CREATE INDEX idx_package_components_stop ON package_components(route_stop_id)")
            db.execute("CREATE INDEX idx_package_components_service ON package_components(service_id)")
            db.execute("CREATE INDEX idx_package_components_product ON package_components(product_id)")
            db.execute("CREATE INDEX idx_package_components_supplier ON package_components(supplier_id)")
            db.execute("CREATE INDEX idx_package_components_tenant ON package_components(tenant_id)")
            print("Created package_components table.")
        else:
            print("package_components table already exists.")

        if not _table_exists(db, "package_price_tiers"):
            db.execute("""
                CREATE TABLE package_price_tiers (
                    tier_id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                    package_id      INTEGER NOT NULL REFERENCES packages(package_id),
                    min_pax         INTEGER NOT NULL,
                    max_pax         INTEGER,
                    price_per_pax   NUMERIC NOT NULL,
                    currency        TEXT,
                    notes           TEXT
                )
            """)
            db.execute("CREATE INDEX idx_package_price_tiers_package ON package_price_tiers(package_id)")
            db.execute("CREATE INDEX idx_package_price_tiers_tenant ON package_price_tiers(tenant_id)")
            print("Created package_price_tiers table.")
        else:
            print("package_price_tiers table already exists.")

        db.commit()
        print("No taxonomy to seed — packages are tenant-authored, not shared coding vocabulary.")

    finally:
        db.close()


if __name__ == "__main__":
    migrate()
