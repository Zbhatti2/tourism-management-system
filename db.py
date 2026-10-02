"""
SQLite connection management (Flask application-context pattern) and audit
logging helper. Deliberately plain sqlite3 — no ORM — schema.sql is the
single source of truth for structure.
"""
import sqlite3

import click
from flask import current_app, g, has_request_context, session

from config import Config


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(Config.DATABASE_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = get_db()
    with open(Config.SCHEMA_PATH, "r") as f:
        db.executescript(f.read())
    db.commit()


def log_action(action: str, entity_type: str = None, entity_id: int = None, detail: str = None,
                tenant_id: int = None, user_id: int = None):
    """Write one row to audit_log. Call this after any create/update/delete/
    import/export/login/purge — commits immediately so the audit trail
    survives even if the surrounding request later fails.

    tenant_id/user_id default to the current session's values when omitted,
    so most call sites inside a logged-in request don't need to pass them
    explicitly. Pass them explicitly for pre-login events (e.g. a failed
    login attempt, where the tenant may or may not be known yet).
    """
    # has_request_context(): also callable from a `flask` CLI command (e.g.
    # create-system-admin) or a migration, where no session exists at all.
    if tenant_id is None and has_request_context():
        tenant_id = session.get("tenant_id")
    if user_id is None and has_request_context():
        user_id = session.get("user_id")
    db = get_db()
    db.execute(
        "INSERT INTO audit_log (tenant_id, user_id, action, entity_type, entity_id, detail) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (tenant_id, user_id, action, entity_type, entity_id, detail),
    )
    db.commit()


def _table_exists(db, table: str) -> bool:
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (table,)
    ).fetchone() is not None


def _column_exists(db, table: str, column: str) -> bool:
    return any(row[1] == column for row in db.execute(f'PRAGMA table_info("{table}")').fetchall())


def is_configured() -> bool:
    """Has at least one real (non-platform) tenant been provisioned? The
    first tenant (Ma Vie Tours) is seeded directly via seed_data.py, so in
    normal use this is already true by the time anyone hits the app; the
    /setup wizard (auth/routes.py) exists as a fallback if the database was
    created without seeding. The reserved TMS Platform row (is_platform = 1)
    doesn't count -- nobody can log into it."""
    db = get_db()
    if not _table_exists(db, "tenants"):
        return False
    if _column_exists(db, "tenants", "is_platform"):
        row = db.execute("SELECT 1 FROM tenants WHERE is_platform = 0 LIMIT 1").fetchone()
    else:
        row = db.execute("SELECT 1 FROM tenants LIMIT 1").fetchone()
    return row is not None


# =============================================================================
# Schema migrations -- additive, automatic, run once per schema change.
#
# schema.sql is the source of truth for a BRAND NEW database (flask init-db).
# A database already in use (your real TMS data) picks up later changes
# through MIGRATIONS below: run_pending_migrations() is called once at app
# startup (app.py's create_app()), applies whichever entries this database
# hasn't recorded yet, in order, and records each one in schema_migrations
# so it is never applied twice. Every migration function must be safe to run
# against a database that already has the change (it checks before it
# CREATEs/ALTERs), since a freshly init-db'd database runs them too.
#
# The ~40 older `flask migrate-*` commands further down are NOT in this list:
# every database that exists today has already had them applied by hand, and
# schema.sql already includes everything they added. They stay available as
# CLI commands for reference only.
# =============================================================================

def next_account_number(db) -> int:
    """Consumes and returns the next 8-digit tenant account number from the
    tenant_account_number_seq counter. This is the ONLY way an account number
    is ever assigned -- never MAX()+1 or tenant_id -- so a number is never
    reused, even if the tenant that held it is later removed. The reserved
    TMS Platform row takes 10000001; the first real tenant (Ma Vie Tours)
    takes 10000002; every tenant created after that gets the next one."""
    row = db.execute("SELECT next_value FROM tenant_account_number_seq WHERE id = 1").fetchone()
    value = row["next_value"]
    db.execute("UPDATE tenant_account_number_seq SET next_value = next_value + 1 WHERE id = 1")
    db.commit()
    return value


def _migration_account_number_infra(db):
    """tenants.account_number + tenants.is_platform + the counter table.
    ALTER TABLE ADD COLUMN can't carry UNIQUE in SQLite, so uniqueness is a
    separate index (NULLs don't collide, which covers the moment between this
    migration and the backfill below)."""
    if not _column_exists(db, "tenants", "account_number"):
        db.execute("ALTER TABLE tenants ADD COLUMN account_number INTEGER")
    if not _column_exists(db, "tenants", "is_platform"):
        db.execute("ALTER TABLE tenants ADD COLUMN is_platform INTEGER NOT NULL DEFAULT 0")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_tenants_account_number ON tenants(account_number)")
    db.execute(
        """CREATE TABLE IF NOT EXISTS tenant_account_number_seq (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            next_value INTEGER NOT NULL
        )"""
    )
    db.execute("INSERT OR IGNORE INTO tenant_account_number_seq (id, next_value) VALUES (1, 10000001)")
    db.commit()


def _migration_create_platform_tenant(db):
    """Creates the single reserved "TMS Platform" row (tenant_code
    TMS_PLATFORM, is_platform = 1). It MUST run right after the infra
    migration and right before the backfill, so it is always the first
    consumer of the counter (10000001) and the first real tenant lands on
    10000002. Nobody logs into it; it's hidden from Tenant Management and
    exists to hold platform-level master data for future tenants. It needs a
    dek_wrapped only because that column is NOT NULL."""
    if db.execute("SELECT 1 FROM tenants WHERE tenant_code = 'TMS_PLATFORM'").fetchone():
        return
    from security import crypto

    dek_wrapped = crypto.wrap_tenant_dek(crypto.new_tenant_dek())
    account_number = next_account_number(db)
    db.execute(
        "INSERT INTO tenants (tenant_code, account_number, tenant_name, is_platform, dek_wrapped) "
        "VALUES ('TMS_PLATFORM', ?, 'TMS Platform', 1, ?)",
        (account_number, dek_wrapped),
    )
    db.commit()


def _migration_rename_first_tenant_ma_vie_tours(db):
    """The first tenant was built and populated as "Heritage Tours"
    (code HERITAGE). It is renamed IN PLACE to "Ma Vie Tours" (code MAVIE):
    same tenant_id, so every row of existing data stays exactly where it is.
    Only touches the row still carrying the original seeded code AND name,
    so it can never overwrite a name someone changes later. The tenant's own
    Host Organization record gets the same rename (again only if it still
    has the original name) and the tenant's website."""
    row = db.execute(
        "SELECT tenant_id FROM tenants WHERE tenant_code = 'HERITAGE' AND tenant_name = 'Heritage Tours'"
    ).fetchone()
    if row is None:
        return
    tenant_id = row["tenant_id"]
    db.execute(
        "UPDATE tenants SET tenant_code = 'MAVIE', tenant_name = 'Ma Vie Tours', updated_at = datetime('now') "
        "WHERE tenant_id = ?",
        (tenant_id,),
    )
    if _table_exists(db, "host_organizations"):
        db.execute(
            "UPDATE host_organizations SET organization_name = 'Ma Vie Tours', website = 'https://www.mavietours.com', "
            "updated_at = datetime('now') WHERE tenant_id = ? AND organization_name = 'Heritage Tours'",
            (tenant_id,),
        )
    db.execute(
        "INSERT INTO audit_log (tenant_id, action, entity_type, entity_id, detail) VALUES (?, 'Update', 'tenants', ?, ?)",
        (tenant_id, tenant_id, "Tenant renamed from 'Heritage Tours' (HERITAGE) to 'Ma Vie Tours' (MAVIE)"),
    )
    db.commit()


def _migration_backfill_tenant_account_numbers(db):
    """Gives every tenant without an account number one, in creation order
    (tenant_id). Runs after the platform row took 10000001, so the oldest
    real tenant -- Ma Vie Tours, tenant_id 1 -- gets 10000002."""
    rows = db.execute("SELECT tenant_id FROM tenants WHERE account_number IS NULL ORDER BY tenant_id").fetchall()
    for row in rows:
        db.execute(
            "UPDATE tenants SET account_number = ?, updated_at = datetime('now') WHERE tenant_id = ?",
            (next_account_number(db), row["tenant_id"]),
        )
    db.commit()


def _migration_tenant_website_domain(db):
    """tenants.website_domain -- the tenant's own public website (Step 2:
    each tenant sells its tours from its own domain). Ma Vie Tours gets
    www.mavietours.com."""
    if not _column_exists(db, "tenants", "website_domain"):
        db.execute("ALTER TABLE tenants ADD COLUMN website_domain TEXT")
    db.execute(
        "UPDATE tenants SET website_domain = 'www.mavietours.com' WHERE tenant_code = 'MAVIE' AND website_domain IS NULL"
    )
    db.commit()


def _migration_content_locations_file_storage(db):
    """Documents & Knowledge Base: lets a content location hold the file
    itself ('Stored in Database'), uploaded from the browser -- the same
    model supplier documents and POI images use -- so documents can be
    added on the hosted app, where a 'Local Drive Path' on someone's PC
    means nothing. SQLite can't alter a CHECK constraint in place, so the
    table is rebuilt with its rows copied across unchanged."""
    if _column_exists(db, "content_locations", "file_data"):
        return
    db.executescript("""
        CREATE TABLE content_locations_new (
            location_id     INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
            content_id      INTEGER NOT NULL REFERENCES content(content_id),
            location_type   TEXT CHECK (location_type IN ('Cloud Link','Local Drive Path','Stored in Database')),
            path_or_url     TEXT NOT NULL,
            file_data       BLOB,
            file_name       TEXT,
            mime_type       TEXT,
            file_size       INTEGER
        );
        INSERT INTO content_locations_new (location_id, tenant_id, content_id, location_type, path_or_url)
            SELECT location_id, tenant_id, content_id, location_type, path_or_url FROM content_locations;
        DROP TABLE content_locations;
        ALTER TABLE content_locations_new RENAME TO content_locations;
        CREATE INDEX IF NOT EXISTS idx_content_locations_tenant ON content_locations(tenant_id);
        CREATE INDEX IF NOT EXISTS idx_content_locations_content ON content_locations(content_id);
    """)
    db.commit()


def _migration_backups_nightly_type(db):
    """Allows backups.backup_type = 'Nightly' (the scheduled backups made by
    `flask --app app nightly-backup`). SQLite can't alter a CHECK in place,
    so the small backups log table is rebuilt with its rows copied across."""
    sql = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='backups'").fetchone()
    if sql is None or "'Nightly'" in sql[0]:
        return
    db.executescript("""
        CREATE TABLE backups_new (
            backup_id       INTEGER PRIMARY KEY AUTOINCREMENT,
            file_name       TEXT NOT NULL,
            file_path       TEXT NOT NULL,
            size_bytes      INTEGER NOT NULL,
            table_count     INTEGER,
            total_rows      INTEGER,
            integrity_ok    INTEGER,
            backup_type     TEXT NOT NULL DEFAULT 'Manual'
                            CHECK (backup_type IN ('Manual','Pre-restore safety','Restore point','Nightly')),
            created_at      TEXT NOT NULL DEFAULT (datetime('now')),
            notes           TEXT
        );
        INSERT INTO backups_new SELECT backup_id, file_name, file_path, size_bytes, table_count, total_rows,
                                       integrity_ok, backup_type, created_at, notes FROM backups;
        DROP TABLE backups;
        ALTER TABLE backups_new RENAME TO backups;
    """)
    db.commit()


def _migration_platform_lookups(db):
    """Platform Lookups: adds is_system to supplier_types, supplier_subtypes
    and poi_types, seeds the platform tenant's starter list of locked rows
    (Hotel/Resort/Restaurant, the Five Star/4-Star sub-types, the
    sightseeing POI Types), then locks the matching rows in every tenant --
    matched by code, then label, so existing suppliers/POIs keep their
    links. See platform_lookups.py."""
    import platform_lookups

    platform_lookups.add_is_system_columns(db)
    platform_lookups.seed_starter_platform_lookups(db)
    platform_lookups.sync_platform_lookups(db)


TRANSPORT_HUBS_DDL = """
CREATE TABLE IF NOT EXISTS hub_types (
    hub_type_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    code            TEXT NOT NULL UNIQUE,
    label           TEXT NOT NULL,
    icon            TEXT,                   -- Bootstrap icon name, e.g. 'airplane'
    sort_order      INTEGER DEFAULT 0,
    is_active       INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS transport_hubs (
    hub_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    hub_type_id     INTEGER NOT NULL REFERENCES hub_types(hub_type_id),
    name            TEXT NOT NULL,
    code            TEXT,                   -- IATA for airports (LHE); station code where one exists
    icao_code       TEXT,                   -- airports only (OPLA)
    region_id       INTEGER REFERENCES regions(region_id),
    country_id      INTEGER REFERENCES countries(country_id),
    state_id        INTEGER REFERENCES states(state_id),
    state_province_text TEXT,
    city_id         INTEGER REFERENCES cities(city_id),
    city_text       TEXT,
    latitude        REAL,
    longitude       REAL,
    operator        TEXT,                   -- e.g. Pakistan Railways, Daewoo Express, a port authority
    scope           TEXT CHECK (scope IN ('International','Domestic','Regional')),
    address         TEXT,
    phone           TEXT,
    website         TEXT,
    notes           TEXT,
    is_major        INTEGER NOT NULL DEFAULT 1,
    is_active       INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_transport_hubs_type ON transport_hubs(hub_type_id);
CREATE INDEX IF NOT EXISTS idx_transport_hubs_country ON transport_hubs(country_id);
CREATE INDEX IF NOT EXISTS idx_transport_hubs_city ON transport_hubs(city_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_transport_hubs_type_code ON transport_hubs(hub_type_id, code) WHERE code IS NOT NULL;
"""

HUB_TYPES = [
    ("AIRPORT", "Airport", "airplane"),
    ("RAILWAY_STATION", "Railway Station", "train-front"),
    ("BUS_TERMINAL", "Bus Terminal", "bus-front"),
    ("SEAPORT", "Seaport / Ferry Terminal", "water"),
]


def _migration_transport_hubs(db):
    """Transport Hubs (Group A, shared by every tenant -- no tenant_id):
    hub_types + transport_hubs, seeded with Airport / Railway Station /
    Bus Terminal / Seaport. Then copies every tenant POI of type Airport
    or Railway Station into transport_hubs (one hub per name+type, so two
    tenants holding the same airport give one hub). The POI rows themselves
    are left untouched -- they may carry images, links and knowledge-graph
    edges -- so nothing is lost; retiring them is a separate decision."""
    from utils import parse_coordinates

    db.executescript(TRANSPORT_HUBS_DDL)
    for i, (code, label, icon) in enumerate(HUB_TYPES):
        db.execute("INSERT OR IGNORE INTO hub_types (code, label, icon, sort_order) VALUES (?, ?, ?, ?)",
                   (code, label, icon, i))
    type_ids = {r["code"]: r["hub_type_id"] for r in db.execute("SELECT hub_type_id, code FROM hub_types")}

    pois = db.execute(
        """SELECT p.*, pt.code AS type_code, t.tenant_name FROM points_of_interest p
           JOIN poi_types pt ON pt.poi_type_id = p.poi_type_id
           JOIN tenants t ON t.tenant_id = p.tenant_id
           WHERE p.is_deleted = 0 AND pt.code IN ('AIRPORT', 'RAILWAY_STATION')
           ORDER BY p.poi_id"""
    ).fetchall()
    for p in pois:
        hub_type_id = type_ids[p["type_code"]]
        if db.execute("SELECT 1 FROM transport_hubs WHERE hub_type_id = ? AND lower(name) = lower(?)",
                      (hub_type_id, p["name"])).fetchone():
            continue
        coords = parse_coordinates(p["map_coordinates"]) if p["map_coordinates"] else None
        # Fill state/country/region from the city when the POI only had a city.
        state_id, country_id, region_id = p["state_id"], p["country_id"], p["region_id"]
        if p["city_id"]:
            geo = db.execute(
                """SELECT s.state_id, co.country_id, co.region_id FROM cities ci
                   JOIN states s ON s.state_id = ci.state_id JOIN countries co ON co.country_id = s.country_id
                   WHERE ci.city_id = ?""", (p["city_id"],)).fetchone()
            if geo:
                state_id = state_id or geo["state_id"]
                country_id = country_id or geo["country_id"]
                region_id = region_id or geo["region_id"]
        notes = "\n\n".join(x for x in (p["notes"], p["historical_significance"],
                                         f"Copied from {p['tenant_name']}'s Points of Interest (POI #{p['poi_id']}).")
                             if x)
        db.execute(
            """INSERT INTO transport_hubs (hub_type_id, name, region_id, country_id, state_id, state_province_text,
                   city_id, city_text, latitude, longitude, scope, address, phone, website, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (hub_type_id, p["name"], region_id, country_id, state_id, p["state_province_text"],
             p["city_id"], p["city_text"], coords[0] if coords else None, coords[1] if coords else None,
             "International" if "international" in p["name"].lower() else None,
             p["local_location"], p["phone"], p["website"], notes),
        )
    db.commit()


def _migration_load_major_airports(db):
    """Loads platform_seed_data/airports_ourairports.csv (every large or
    medium airport with scheduled service and an IATA code, ~3,200, from
    OurAirports -- public domain) into transport_hubs. Country comes from
    the ISO code; province/state and city are matched to TMS geography by
    code or name and otherwise kept as text. An airport already on file
    (same IATA code, or same name when it has no code yet) only has its EMPTY fields filled in, so
    nothing entered in TMS is overwritten. Large airports are flagged
    Major."""
    import csv
    from pathlib import Path

    path = Path(__file__).resolve().parent / "platform_seed_data" / "airports_ourairports.csv"
    if not path.exists():
        return
    airport_type = db.execute("SELECT hub_type_id FROM hub_types WHERE code = 'AIRPORT'").fetchone()
    if airport_type is None:
        return
    airport_type_id = airport_type[0]

    countries = {r["code"]: (r["country_id"], r["region_id"])
                 for r in db.execute("SELECT country_id, code, region_id FROM countries WHERE code IS NOT NULL")}
    states_by_code, states_by_name = {}, {}
    for r in db.execute("SELECT state_id, country_id, code, label FROM states"):
        if r["code"]:
            states_by_code[(r["country_id"], r["code"].upper())] = r["state_id"]
        states_by_name[(r["country_id"], r["label"].lower())] = r["state_id"]
    cities = {}
    for r in db.execute("""SELECT ci.city_id, ci.state_id, s.country_id, ci.label FROM cities ci
                           JOIN states s ON s.state_id = ci.state_id"""):
        cities.setdefault((r["country_id"], r["label"].lower()), (r["city_id"], r["state_id"]))

    source_note = "Source: OurAirports (ourairports.com, public domain), Oct 2026."
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            country_id, region_id = countries.get(row["iso_country"], (None, None))
            state_id = state_text = city_id = city_text = None
            if country_id:
                sub = row["iso_region"].split("-", 1)[-1].upper()
                state_id = (states_by_code.get((country_id, sub))
                            or states_by_name.get((country_id, (row["region_name"] or "").lower())))
                match = cities.get((country_id, (row["municipality"] or "").lower()))
                if match and (state_id is None or match[1] == state_id):
                    city_id, state_id = match[0], match[1]
            if state_id is None:
                state_text = row["region_name"] or None
            if city_id is None:
                city_text = row["municipality"] or None
            name = row["name"]
            values = {
                "code": row["iata"], "icao_code": row["icao"] or None, "name": name,
                "region_id": region_id, "country_id": country_id, "state_id": state_id,
                "state_province_text": state_text, "city_id": city_id, "city_text": city_text,
                "latitude": float(row["latitude"]), "longitude": float(row["longitude"]),
                "scope": "International" if "international" in name.lower() else None,
                "website": row["website"] or row["wikipedia"] or None,
                "is_major": 1 if row["size"] == "large" else 0,
            }
            existing = db.execute(
                "SELECT * FROM transport_hubs WHERE hub_type_id = ? "
                "AND (code = ? OR (code IS NULL AND lower(name) = lower(?))) ORDER BY code IS NULL LIMIT 1",
                (airport_type_id, row["iata"], name),
            ).fetchone()
            if existing:
                fill = {k: v for k, v in values.items() if v is not None and existing[k] in (None, "")}
                if "code" in fill and db.execute(
                        "SELECT 1 FROM transport_hubs WHERE hub_type_id = ? AND code = ? AND hub_id != ?",
                        (airport_type_id, fill["code"], existing["hub_id"])).fetchone():
                    del fill["code"]
                if fill:
                    db.execute(f"UPDATE transport_hubs SET {', '.join(f'{k} = ?' for k in fill)}, "
                               f"updated_at = datetime('now') WHERE hub_id = ?",
                               tuple(fill.values()) + (existing["hub_id"],))
                continue
            values.update({"hub_type_id": airport_type_id, "notes": source_note, "is_active": 1})
            db.execute(f"INSERT INTO transport_hubs ({', '.join(values)}) VALUES ({', '.join('?' * len(values))})",
                       tuple(values.values()))
    db.commit()


def _migration_package_hubs(db):
    """Links Transport Hubs into Packages and retires the Airport /
    Railway Station POIs they replace.

    1. package_route_stops gains arrival_hub_id / departure_hub_id, and
       package_components gains from_hub_id / to_hub_id (used by Airline
       Tickets) -- all pointing at the shared transport_hubs table.
    2. knowledge_graph_edges is rebuilt so its subject/object type CHECK
       also allows 'TransportHub' (SQLite can't alter a CHECK in place).
    3. Every tenant POI of type Airport or Railway Station that has a
       matching hub (same type, same name) is retired: its knowledge-graph
       links are moved to the hub, and the POI is soft-deleted
       (is_deleted = 1), so its images and links stay on file. A POI still
       used on a package Day is left alone. Those two POI Types are then
       deactivated in every tenant that no longer has live POIs of them, so
       nobody adds new airport/station POIs by mistake."""
    for table, cols in (("package_route_stops", ("arrival_hub_id", "departure_hub_id")),
                        ("package_components", ("from_hub_id", "to_hub_id"))):
        for col in cols:
            if not _column_exists(db, table, col):
                db.execute(f"ALTER TABLE {table} ADD COLUMN {col} INTEGER REFERENCES transport_hubs(hub_id)")
    db.commit()

    sql = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='knowledge_graph_edges'").fetchone()
    if sql and "'TransportHub'" not in sql[0]:
        types = "('Supplier','PointOfInterest','City','Organization','Contact','TransportHub')"
        db.executescript(f"""
            CREATE TABLE knowledge_graph_edges_new (
                edge_id         INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                subject_type    TEXT NOT NULL CHECK (subject_type IN {types}),
                subject_id      INTEGER NOT NULL,
                relationship    TEXT NOT NULL,
                object_type     TEXT NOT NULL CHECK (object_type IN {types}),
                object_id       INTEGER NOT NULL,
                notes           TEXT,
                created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE (tenant_id, subject_type, subject_id, relationship, object_type, object_id)
            );
            INSERT INTO knowledge_graph_edges_new SELECT edge_id, tenant_id, subject_type, subject_id, relationship,
                   object_type, object_id, notes, created_at, updated_at FROM knowledge_graph_edges;
            DROP TABLE knowledge_graph_edges;
            ALTER TABLE knowledge_graph_edges_new RENAME TO knowledge_graph_edges;
            CREATE INDEX IF NOT EXISTS idx_kg_edges_tenant ON knowledge_graph_edges(tenant_id);
            CREATE INDEX IF NOT EXISTS idx_kg_edges_subject ON knowledge_graph_edges(tenant_id, subject_type, subject_id);
            CREATE INDEX IF NOT EXISTS idx_kg_edges_object ON knowledge_graph_edges(tenant_id, object_type, object_id);
        """)

    if not _table_exists(db, "transport_hubs"):
        return
    pois = db.execute(
        """SELECT p.poi_id, p.tenant_id, p.name, pt.code AS type_code FROM points_of_interest p
           JOIN poi_types pt ON pt.poi_type_id = p.poi_type_id
           WHERE p.is_deleted = 0 AND pt.code IN ('AIRPORT', 'RAILWAY_STATION')"""
    ).fetchall()
    for p in pois:
        hub = db.execute(
            """SELECT h.hub_id FROM transport_hubs h JOIN hub_types ht ON ht.hub_type_id = h.hub_type_id
               WHERE ht.code = ? AND lower(h.name) = lower(?) ORDER BY h.hub_id LIMIT 1""",
            (p["type_code"], p["name"]),
        ).fetchone()
        if hub is None:
            continue
        if db.execute("SELECT 1 FROM package_day_pois WHERE poi_id = ?", (p["poi_id"],)).fetchone():
            continue
        for side in ("subject", "object"):
            # OR IGNORE: if the same edge already exists against the hub, the
            # POI copy is simply dropped below instead of duplicated.
            db.execute(
                f"""UPDATE OR IGNORE knowledge_graph_edges SET {side}_type = 'TransportHub', {side}_id = ?,
                        updated_at = datetime('now')
                    WHERE tenant_id = ? AND {side}_type = 'PointOfInterest' AND {side}_id = ?""",
                (hub["hub_id"], p["tenant_id"], p["poi_id"]),
            )
            db.execute(
                f"DELETE FROM knowledge_graph_edges WHERE tenant_id = ? AND {side}_type = 'PointOfInterest' AND {side}_id = ?",
                (p["tenant_id"], p["poi_id"]),
            )
        db.execute(
            "UPDATE points_of_interest SET is_deleted = 1, updated_at = datetime('now'), "
            "notes = COALESCE(notes || char(10) || char(10), '') || ? WHERE poi_id = ?",
            (f"Retired Oct 2026: now a shared Transport Hub (hub #{hub['hub_id']}).", p["poi_id"]),
        )

    db.execute(
        """UPDATE poi_types SET is_active = 0
           WHERE code IN ('AIRPORT', 'RAILWAY_STATION') AND is_system = 0
             AND NOT EXISTS (SELECT 1 FROM points_of_interest p
                             WHERE p.poi_type_id = poi_types.poi_type_id AND p.is_deleted = 0)"""
    )
    db.commit()


CITY_DISTANCES_DDL = """
CREATE TABLE IF NOT EXISTS city_distances (
    distance_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    city_a_id       INTEGER NOT NULL REFERENCES cities(city_id),
    city_b_id       INTEGER NOT NULL REFERENCES cities(city_id),
    road_km         REAL,
    drive_minutes   INTEGER,                -- typical driving time
    rail_available  TEXT CHECK (rail_available IN ('Yes','No')),
    source          TEXT,                   -- where the figures came from
    verified_on     TEXT,                   -- date last checked (YYYY-MM-DD)
    notes           TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (city_a_id < city_b_id),          -- one row per pair, stored in id order
    UNIQUE (city_a_id, city_b_id)
);
CREATE INDEX IF NOT EXISTS idx_city_distances_b ON city_distances(city_b_id);
"""


def _migration_geography_distances(db):
    """Geography & Distances (Group A, shared by every tenant):
    cities gain latitude, longitude and timezone, filled from
    platform_seed_data/city_coordinates_geonames.csv (GeoNames, CC BY 4.0)
    -- only where empty. A city with no match gets its country's time zone
    when every matched city in that country shares one. Then creates
    city_distances: one row per city pair with road km, drive time, rail
    and source; the straight-line distance is always calculated from the
    coordinates, never stored."""
    import csv
    from pathlib import Path

    for col, typ in (("latitude", "REAL"), ("longitude", "REAL"), ("timezone", "TEXT")):
        if not _column_exists(db, "cities", col):
            db.execute(f"ALTER TABLE cities ADD COLUMN {col} {typ}")
    db.executescript(CITY_DISTANCES_DDL)

    path = Path(__file__).resolve().parent / "platform_seed_data" / "city_coordinates_geonames.csv"
    if path.exists():
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                city = db.execute(
                    """SELECT ci.city_id, ci.latitude, ci.timezone FROM cities ci
                       JOIN states s ON s.state_id = ci.state_id JOIN countries co ON co.country_id = s.country_id
                       WHERE co.code = ? AND lower(s.label) = lower(?) AND lower(ci.label) = lower(?)""",
                    (row["iso_country"], row["state"], row["city"]),
                ).fetchone()
                if city is None:
                    continue
                if city["latitude"] is None:
                    db.execute("UPDATE cities SET latitude = ?, longitude = ? WHERE city_id = ?",
                               (float(row["latitude"]), float(row["longitude"]), city["city_id"]))
                if not city["timezone"]:
                    db.execute("UPDATE cities SET timezone = ? WHERE city_id = ?", (row["timezone"], city["city_id"]))

    for country in db.execute(
        """SELECT s.country_id, MIN(ci.timezone) AS tz FROM cities ci JOIN states s ON s.state_id = ci.state_id
           WHERE ci.timezone IS NOT NULL GROUP BY s.country_id HAVING COUNT(DISTINCT ci.timezone) = 1"""
    ).fetchall():
        db.execute(
            """UPDATE cities SET timezone = ? WHERE timezone IS NULL
               AND state_id IN (SELECT state_id FROM states WHERE country_id = ?)""",
            (country["tz"], country["country_id"]),
        )
    db.commit()


def _migration_platform_catalogs(db):
    """Platform catalogs + import/merge support (Oct 2026), all GLOBAL:
    platform_pois, platform_accommodation, platform_restaurants, embassies
    (defined in platform_catalog.py), the merge log, and the import wizard's
    staging tables. Also: alternate names / source / checked-on for
    transport_hubs, altitude and alternate names for cities, an upper drive
    time and route name for city_distances, and the 15 new POI Types added
    to the platform's locked list (pushed to every tenant)."""
    import platform_catalog as pc
    import platform_lookups
    from seed_data import _slug

    for spec in pc.CATALOGS.values():
        db.executescript(pc.ddl(spec))
    db.executescript(pc.SUPPORT_DDL)
    for table, col, typ in (("transport_hubs", "alt_names", "TEXT"), ("transport_hubs", "source", "TEXT"),
                            ("transport_hubs", "checked_on", "TEXT"), ("cities", "altitude_m", "INTEGER"),
                            ("cities", "alt_names", "TEXT"), ("city_distances", "drive_minutes_max", "INTEGER"),
                            ("city_distances", "route_name", "TEXT")):
        if _table_exists(db, table) and not _column_exists(db, table, col):
            db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    db.commit()

    pid = platform_lookups.platform_tenant_id(db)
    if pid is not None and _column_exists(db, "poi_types", "is_system"):
        start = db.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 FROM poi_types WHERE tenant_id = ?", (pid,)).fetchone()[0]
        for i, label in enumerate(pc.NEW_POI_TYPES):
            db.execute("INSERT OR IGNORE INTO poi_types (tenant_id, code, label, sort_order, is_active, is_system) "
                       "VALUES (?, ?, ?, ?, 1, 1)", (pid, _slug(label), label, start + i))
        db.commit()
        platform_lookups.sync_platform_lookups(db)


def _migration_catalog_sync(db):
    """Catalog Sync (Oct 2026): tenant_catalog_links ties each tenant's POIs
    and Suppliers to the platform catalog records they came from (or were
    matched to), and tenants.catalog_synced_at marks tenants whose first
    sync is done -- from then on platform changes reach them automatically.
    See catalog_sync.py. Existing tenants start unsynced: the SystemAdmin
    runs their first sync from the Catalog Sync screen, with a preview."""
    import catalog_sync
    db.executescript(catalog_sync.LINKS_DDL)
    if not _column_exists(db, "tenants", "catalog_synced_at"):
        db.execute("ALTER TABLE tenants ADD COLUMN catalog_synced_at TEXT")
    db.commit()


def _migration_image_catalog(db):
    """Images Catalog (Oct 2026): curation fields on supplier images (date,
    sort order, source, source URL, content hash, cached thumbnail) and the
    import batches images wait in until they're curated. See
    image_catalog.py. Existing stored images get a content hash so a
    re-sent copy is recognised as a duplicate."""
    import image_catalog
    for col, typ in image_catalog.DOCUMENT_COLUMNS:
        if not _column_exists(db, "supplier_documents", col):
            db.execute(f"ALTER TABLE supplier_documents ADD COLUMN {col} {typ}")
    db.executescript(image_catalog.DDL)
    image_catalog.backfill_hashes(db)
    db.commit()


def _migration_ai_metering_image_collector(db):
    """AI usage metering (ai_usage.py: every Claude API call logged against
    its tenant, with cost; tenants.ai_monthly_limit_usd = optional monthly
    allowance) and the AI Image Collector's runs (image_collector.py)."""
    import ai_usage
    import image_collector
    db.executescript(ai_usage.DDL)
    db.executescript(image_collector.DDL)
    if not _column_exists(db, "tenants", "ai_monthly_limit_usd"):
        db.execute("ALTER TABLE tenants ADD COLUMN ai_monthly_limit_usd REAL")
    db.commit()


# Append-only. Each entry is (unique_name, function(db)). Never edit or remove
# a shipped entry -- add a new one for any further change.
MIGRATIONS = [
    # Order matters for the first four: infra -> platform row (10000001) ->
    # rename -> backfill (Ma Vie Tours = 10000002).
    ("2026_09_account_number_infra", _migration_account_number_infra),
    ("2026_09_create_platform_tenant", _migration_create_platform_tenant),
    ("2026_09_rename_first_tenant_ma_vie_tours", _migration_rename_first_tenant_ma_vie_tours),
    ("2026_09_backfill_tenant_account_numbers", _migration_backfill_tenant_account_numbers),
    ("2026_09_tenant_website_domain", _migration_tenant_website_domain),
    ("2026_09_content_locations_file_storage", _migration_content_locations_file_storage),
    ("2026_09_backups_nightly_type", _migration_backups_nightly_type),
    ("2026_10_platform_lookups", _migration_platform_lookups),
    ("2026_10_transport_hubs", _migration_transport_hubs),
    ("2026_10_load_major_airports", _migration_load_major_airports),
    ("2026_10_package_hubs", _migration_package_hubs),
    ("2026_10_geography_distances", _migration_geography_distances),
    ("2026_10_platform_catalogs", _migration_platform_catalogs),
    ("2026_10_catalog_sync", _migration_catalog_sync),
    ("2026_10_image_catalog", _migration_image_catalog),
    ("2026_10_ai_metering_image_collector", _migration_ai_metering_image_collector),
]


# =============================================================================
# Tenant isolation -- cross-tenant foreign-key guard triggers.
#
# PRAGMA foreign_keys = ON guarantees a referenced row EXISTS, but not that it
# belongs to the SAME tenant as the row pointing at it. Every screen builds
# its dropdowns from tenant-scoped queries, so these should never fire in
# normal use; they are a hard backstop against a form-submitted id for
# another tenant's record (a bug, a hand-crafted request, or a future route
# that forgets its WHERE tenant_id = ?). URL-path ids are already checked by
# each route.
#
# Generated from the live schema rather than a hand-kept list, and re-checked
# at every startup (CREATE TRIGGER IF NOT EXISTS), so tables added later are
# covered automatically. A relationship is guarded when BOTH tables carry a
# tenant_id; references to tenants/users (SystemAdmin has no tenant) and the
# audit log are skipped. As a safety net, a relationship whose EXISTING data
# would already fail the check is skipped and reported instead of guarded,
# so a trigger can never block an ordinary edit of an existing record.
# =============================================================================

TENANT_FK_SKIP_TABLES = {"audit_log", "tenants", "users", "schema_migrations"}


def tenant_fk_relationships(db):
    """[(table, fk_column, referenced_table, referenced_column), ...] for
    every foreign key where both sides are tenant-scoped."""
    tables = [r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()]
    has_tenant = {t for t in tables if _column_exists(db, t, "tenant_id")}
    rels = []
    for t in tables:
        if t not in has_tenant or t in TENANT_FK_SKIP_TABLES or t.startswith("_archived"):
            continue
        for fk in db.execute(f'PRAGMA foreign_key_list("{t}")').fetchall():
            ref_table, from_col, to_col = fk[2], fk[3], fk[4]
            if ref_table in TENANT_FK_SKIP_TABLES or ref_table not in has_tenant or from_col == "tenant_id":
                continue
            if not to_col:
                pk = [r[1] for r in db.execute(f'PRAGMA table_info("{ref_table}")').fetchall() if r[5] == 1]
                if len(pk) != 1:
                    continue
                to_col = pk[0]
            rels.append((t, from_col, ref_table, to_col))
    return rels


def ensure_tenant_fk_triggers(db):
    """Creates any missing cross-tenant guard triggers (see above). Returns
    the list of relationships skipped because existing data already mixes
    tenants (empty in a healthy database)."""
    skipped = []
    for table, col, ref_table, ref_col in tenant_fk_relationships(db):
        name = f"trg_tenant_fk_{table}_{col}"
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name = ?", (name + "_ins",)).fetchone():
            continue
        bad = db.execute(
            f'SELECT COUNT(*) FROM "{table}" x WHERE x."{col}" IS NOT NULL AND NOT EXISTS '
            f'(SELECT 1 FROM "{ref_table}" r WHERE r."{ref_col}" = x."{col}" AND r.tenant_id = x.tenant_id)'
        ).fetchone()[0]
        if bad:
            skipped.append((table, col, ref_table, bad))
            continue
        msg = f"{table}.{col}: cross-tenant reference not allowed"
        for suffix, event in (("_ins", "INSERT"), ("_upd", "UPDATE")):
            db.execute(f'''CREATE TRIGGER IF NOT EXISTS {name}{suffix}
                BEFORE {event} ON "{table}"
                WHEN NEW."{col}" IS NOT NULL
                BEGIN
                    SELECT RAISE(ABORT, '{msg}')
                    WHERE NOT EXISTS (
                        SELECT 1 FROM "{ref_table}"
                        WHERE "{ref_col}" = NEW."{col}" AND tenant_id = NEW.tenant_id
                    );
                END''')
    db.commit()
    for table, col, ref_table, bad in skipped:
        print(f"[tenant-isolation] NOT guarding {table}.{col} -> {ref_table}: "
              f"{bad} existing row(s) already reference another tenant's record.")
    return skipped


def run_pending_migrations():
    """Applies whichever MIGRATIONS this database hasn't recorded yet, in
    order, then makes sure every tenant-isolation trigger exists. Called
    once at app startup, so any database -- your local copy or the one on
    the server -- is upgraded automatically the next time the app starts.
    A no-op on a database that isn't initialized yet."""
    db = get_db()
    if not _table_exists(db, "tenants"):
        return
    db.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
        name TEXT PRIMARY KEY,
        applied_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""")
    db.commit()
    applied = {row["name"] for row in db.execute("SELECT name FROM schema_migrations").fetchall()}
    for name, fn in MIGRATIONS:
        if name in applied:
            continue
        fn(db)
        db.execute("INSERT INTO schema_migrations (name) VALUES (?)", (name,))
        db.commit()
    ensure_tenant_fk_triggers(db)


def init_app(app):
    app.teardown_appcontext(close_db)

    @app.cli.command("init-db")
    def init_db_command():
        """Flask CLI: `flask --app app init-db` — (re)creates all tables from schema.sql."""
        init_db()
        click.echo("Initialized the database from schema.sql.")

    @app.cli.command("seed-lookups")
    @click.argument("tenant_code")
    def seed_lookups_command(tenant_code):
        """Flask CLI: `flask --app app seed-lookups HERITAGE` — populates
        Module E lookup tables for one tenant."""
        from seed_data import seed_lookup_tables

        db = get_db()
        row = db.execute("SELECT tenant_id FROM tenants WHERE tenant_code = ?", (tenant_code,)).fetchone()
        if row is None:
            click.echo(f"No tenant with code {tenant_code!r}. Run `flask --app app seed-tenant` first.")
            return
        seed_lookup_tables(db, row["tenant_id"])
        click.echo(f"Seeded lookup tables for tenant {tenant_code}.")

    @app.cli.command("migrate-add-organization-notes")
    def migrate_add_organization_notes_command():
        """Flask CLI: `flask --app app migrate-add-organization-notes` — adds
        organizations.notes to a database created before that column
        existed. Safe to re-run; a no-op if already present. See
        migrate_add_organization_notes.py for details."""
        from migrate_add_organization_notes import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-add-geography-hierarchy")
    def migrate_add_geography_hierarchy_command():
        """Flask CLI: `flask --app app migrate-add-geography-hierarchy` —
        adds the Region -> Country -> Province/State -> City hierarchy
        (regions/cities tables, countries.region_id, addresses.region_id/
        city_id, renames addresses.city -> addresses.city_text) to a
        database created before that change, and seeds the 6 regions +
        Pakistan provinces/cities. Safe to re-run; does not touch existing
        tenant data. See migrate_add_geography_hierarchy.py for details."""
        from migrate_add_geography_hierarchy import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-poi-remove-platforms-accounts")
    def migrate_poi_remove_platforms_accounts_command():
        """Flask CLI: `flask --app app migrate-poi-remove-platforms-accounts`
        — archive-renames (never drops) the removed "Platforms &
        Subscriptions" and "Accounts" modules' tables, creates the new
        poi_types/points_of_interest tables, adds the Pakistan cities the
        Sikh Gurdwara seed data needs, and seeds POI Types (+ the 149
        Gurdwara POIs for Heritage Tours specifically) for every existing
        tenant. Safe to re-run. See migrate_poi_remove_platforms_accounts.py
        for details."""
        from migrate_poi_remove_platforms_accounts import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-drop-platforms-subscriptions")
    def migrate_drop_platforms_subscriptions_command():
        """Flask CLI: `flask --app app migrate-drop-platforms-subscriptions`
        — per Zeb's request to remove all traces of the "Platforms &
        Subscriptions" module (confirmed not needed for a Tourism/Travel
        Management System), permanently DROPs the 16 _archived_* tables
        that migrate-poi-remove-platforms-accounts left behind for that
        module specifically (every one confirmed empty or holding only
        unused lookup seed values -- no real data lost). Does NOT touch
        the separate Accounts module's archived tables. Safe to re-run.
        See migrate_drop_platforms_subscriptions_module.py for details."""
        from migrate_drop_platforms_subscriptions_module import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-add-suppliers")
    def migrate_add_suppliers_command():
        """Flask CLI: `flask --app app migrate-add-suppliers` — adds the new
        "Suppliers" module (suppliers, supplier_addresses, supplier_contacts,
        supplier_contact_phones, plus the address_types/phone_types/
        supplier_types/supplier_subtypes lookups — address_types was later
        renamed to supplier_address_types, see migrate-organization-addresses
        below) to a database created before that change, seeds those lookups
        for every existing tenant, and narrows addresses.owner_type's CHECK
        constraint (dropping the already-dead 'PersonalAccount' branch).
        Safe to re-run; does not touch existing tenant data. See
        migrate_add_suppliers.py for details."""
        from migrate_add_suppliers import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-add-supplier-preference")
    def migrate_add_supplier_preference_command():
        """Flask CLI: `flask --app app migrate-add-supplier-preference` —
        adds suppliers.preference ('Primary'/'Secondary', NULL by default)
        so a Top and a Secondary choice can be marked among many suppliers
        of the same Type in the same City (e.g. 100+ Hotels in Lahore).
        Safe to re-run; does not touch existing tenant data. See
        migrate_add_supplier_preference.py for details."""
        from migrate_add_supplier_preference import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-organization-addresses")
    def migrate_organization_addresses_command():
        """Flask CLI: `flask --app app migrate-organization-addresses` —
        renames the address_types table to supplier_address_types (if still
        present under its old name), adds the new organization_address_types
        lookup and organization_addresses table, seeds organization_address_
        types for every existing tenant, and backfills one organization_
        addresses row per existing organization that has a non-blank
        full_address, parsed into Street/City/Province/Country via
        address_parsing.py (recovering any phone numbers found embedded in
        that text into organizations.phone when it's currently blank).
        organizations.full_address itself is left in place, untouched, as a
        legacy fallback. Safe to re-run — organizations that already have an
        organization_addresses row are skipped. See
        migrate_organization_addresses.py for details."""
        from migrate_organization_addresses import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-organization-contact-info")
    def migrate_organization_contact_info_command():
        """Flask CLI: `flask --app app migrate-organization-contact-info` —
        adds the "Multiple Email and Phone" and "Documents Link" features to
        the Organizations module: creates the organization_phone_types
        lookup and the organization_emails/organization_phones/
        organization_reference_links tables (its own, separate phone-type
        lookup from Suppliers'), seeds organization_phone_types for every
        existing tenant, and backfills organization_emails/organization_
        phones rows from each organization's existing single-value email/
        phone columns (splitting on ";" where more than one value was
        jammed into one column). organizations.email/.phone themselves are
        left in place, untouched, as a legacy fallback.
        organization_reference_links has nothing to backfill — it starts
        empty, same as a fresh install. Safe to re-run — organizations that
        already have rows in a given new table are skipped for that table.
        See migrate_organization_contact_info.py for details."""
        from migrate_organization_contact_info import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-organization-intelligence")
    def migrate_organization_intelligence_command():
        """Flask CLI: `flask --app app migrate-organization-intelligence` —
        adds the new "Organization Intelligence" module: creates the
        organization_intelligence table (a freeform, dated, append-only
        journal — see schema.sql's MODULE G comment), and seeds the 7
        sample entries for the Heritage Tours tenant specifically (not for
        every tenant — this is Heritage Tours' own demo data, same as the
        Gurdwara POI seed). Safe to re-run — no-ops on the table if it
        already exists, and no-ops on the seed if Heritage Tours already
        has any Organization Intelligence entries. See
        migrate_organization_intelligence.py for details."""
        from migrate_organization_intelligence import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-human-resources")
    def migrate_human_resources_command():
        """Flask CLI: `flask --app app migrate-human-resources` — adds the
        Human Resources module (Employees, External Resources, shared HR
        Roles, and the Host Organization) to a database created before that
        change: widens addresses.owner_type to also allow 'Employee'/
        'ExternalResource', creates the 22 new tables, and seeds the
        generic starter lookups + one Host Organization record per existing
        tenant. Safe to re-run; does not touch any existing data. See
        migrate_human_resources.py for details."""
        from migrate_human_resources import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-fix-history-fk")
    def migrate_fix_history_fk_command():
        """Flask CLI: `flask --app app migrate-fix-history-fk` — drops the
        FOREIGN KEY constraint on contact_phones_history.phone_id,
        contact_emails_history.email_id, and addresses_history.address_id.
        Those FKs made it impossible to ever finish deleting a contact
        phone/email/address: the "Deleted" history row inserted just before
        the live row is removed pointed at that same row, so the DELETE
        that followed always failed with "FOREIGN KEY constraint failed".
        Safe to re-run; does not touch any existing data. See
        migrate_fix_history_fk.py for the full story."""
        from migrate_fix_history_fk import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-add-employee-role-manager")
    def migrate_add_employee_role_manager_command():
        """Flask CLI: `flask --app app migrate-add-employee-role-manager` —
        adds employees.primary_role_id, employees.secondary_role_id, and
        employees.manager_id (the Employee form's Primary Role, Secondary
        Role, and Reports To fields) to a database created before they
        existed. Safe to re-run; does not touch any existing data or assign
        a role to any existing employee. See
        migrate_add_employee_role_manager.py for the full story."""
        from migrate_add_employee_role_manager import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-add-employee-gender")
    def migrate_add_employee_gender_command():
        """Flask CLI: `flask --app app migrate-add-employee-gender` — adds
        the genders lookup table and employees.gender_id (the Employee
        form's Gender field), and seeds a starter Genders list (Male /
        Female / Other) for every existing tenant. Safe to re-run; does not
        touch any existing data. Also see migrate_add_employee_gender.py's
        docstring for the removal of the redundant freeform "Job role"
        field from the Employee form — the employees.job_role column
        itself is left untouched. See migrate_add_employee_gender.py for
        the full story."""
        from migrate_add_employee_gender import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-add-resource-organization")
    def migrate_add_resource_organization_command():
        """Flask CLI: `flask --app app migrate-add-resource-organization` —
        RETIRED: adds external_resources.organization_id, a column on a
        table the app no longer reads or writes (see
        migrate-add-supplier-external-resource below, and blueprints/hr.py's
        retirement note). Kept only so a database that already ran this
        once stays consistent; there's no reason to run it against a new
        database. Safe to re-run; does not touch any existing data. See
        migrate_add_resource_organization.py for the full story."""
        from migrate_add_resource_organization import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-add-supplier-external-resource")
    def migrate_add_supplier_external_resource_command():
        """Flask CLI: `flask --app app migrate-add-supplier-external-resource`
        — adds suppliers.is_external_resource, suppliers.tax_id,
        suppliers.national_id_number, and suppliers.hourly_rate (the
        Supplier form's "External Resource" flag and the fields that go
        with it) to a database created before External Resources were
        retired from Human Resources in favor of Suppliers. Safe to
        re-run; does not touch any existing data, and does not migrate
        anything out of the old external_resources table (which was
        confirmed empty). See migrate_add_supplier_external_resource.py
        for the full story."""
        from migrate_add_supplier_external_resource import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-add-supplier-resource-contact-info")
    def migrate_add_supplier_resource_contact_info_command():
        """Flask CLI: `flask --app app migrate-add-supplier-resource-contact-info`
        — adds suppliers.company_name and three new tables (supplier_emails,
        supplier_phones, supplier_reference_links) backing the redesigned,
        dedicated External Resource form/view (blueprints/suppliers.py's
        new_external_resource()) — a Company field, multiple phones/emails/
        reference links directly on the resource's own record, and (via app
        code, not a schema change) a single billing address instead of the
        generic Supplier form's multi-address/contact-person model. Safe to
        re-run; does not touch any existing data. See
        migrate_add_supplier_resource_contact_info.py for the full story."""
        from migrate_add_supplier_resource_contact_info import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("import-external-resources")
    @click.argument("tenant_code")
    @click.argument("csv_path")
    def import_external_resources_command(tenant_code, csv_path):
        """Flask CLI: `flask --app app import-external-resources TENANT_CODE
        path/to/file.csv` — bulk-creates External Resources
        (suppliers.is_external_resource=1) from a "Non-Employee Resources
        Contact" CSV export (Title/First Name/Last Name/Company/Job Title/
        Street/City/State/Postal Code/Country/Fax/Phone 1/Mobile Phone/
        E-mail 1/E-mail 2/Web Page columns) — one resource per row, with a
        billing address, up to three phones (Office/Cell/Fax), up to two
        emails, and a Web Page reference link, matching the redesigned
        External Resource model. Safe to re-run against the same file or a
        later batch that repeats a row — see import_external_resources.py
        for the full story and the skip-if-already-imported rule."""
        from import_external_resources import import_csv

        import_csv(csv_path, tenant_code)

    @app.cli.command("migrate-normalize-poi-coordinates")
    def migrate_normalize_poi_coordinates_command():
        """Flask CLI: `flask --app app migrate-normalize-poi-coordinates` —
        rewrites every existing Points of Interest "Map coordinates" value
        (points_of_interest.map_coordinates) into the canonical DMS display
        format ('31°35′17″N 74°18′34″E'), matching what new/edited POIs are
        normalized to automatically at save time (see utils.normalize_map_
        coordinates). Only touches rows that parse as real coordinates; a
        row that doesn't is left completely untouched. Safe to re-run —
        idempotent. See migrate_normalize_poi_coordinates.py for the full
        story."""
        from migrate_normalize_poi_coordinates import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-add-poi-knowledge-graph")
    def migrate_add_poi_knowledge_graph_command():
        """Flask CLI: `flask --app app migrate-add-poi-knowledge-graph` —
        adds points_of_interest.knowledge_graph_data (a freeform, ';'-
        delimited field, same convention as contacts.knowledge_graph_data)
        to a database created before this column existed. Safe to re-run;
        does not touch any existing data. See
        migrate_add_poi_knowledge_graph.py for the full story."""
        from migrate_add_poi_knowledge_graph import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-add-services")
    def migrate_add_services_command():
        """Flask CLI: `flask --app app migrate-add-services` — adds the new
        "Services" sub-module (Inventory Management -> Services): the 3
        GLOBAL Service Coding System taxonomy tables (service_categories/
        service_groups/service_subgroups, seeded with the full Category ->
        Group -> Sub-Group vocabulary and validity flags from
        Service_Coding_System_Schema.md), the tenant-scoped `services`
        catalog table, its two deprecated-Sub-Group enforcement triggers,
        and a handful of representative demo Services per existing tenant.
        Safe to re-run; does not touch any existing data. See
        migrate_add_services.py for the full story."""
        from migrate_add_services import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-add-products")
    def migrate_add_products_command():
        """Flask CLI: `flask --app app migrate-add-products` — adds the new
        "Products" sub-module (Inventory Management -> Products): the 3
        GLOBAL Product Coding System taxonomy tables (product_categories/
        product_groups/product_subgroups, seeded with the full Category ->
        Group -> Sub-Group vocabulary from TTMS_Products_Seed_Data.csv), the
        tenant-scoped `products` catalog table (166 seeded rows per
        tenant), the `product_attributes` key-value table, its two
        deprecated-Sub-Group enforcement triggers, and the full Product
        catalog per existing tenant. A separate, independent registry from
        Services -- never joined against or constrained with
        service_categories/service_groups/service_subgroups/services. Safe
        to re-run; does not touch any existing data. See
        migrate_add_products.py for the full story, including the
        deviations from the original prompt's generic template."""
        from migrate_add_products import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-add-packages")
    def migrate_add_packages_command():
        """Flask CLI: `flask --app app migrate-add-packages` — adds the new
        "Package Management" module (Package Management & Itinerary
        Builder): packages, package_route_stops, package_days,
        package_day_pois, package_components, and package_price_tiers.
        Unlike Services/Products, all six tables are tenant-scoped — no
        GLOBAL taxonomy to seed, since a package is one operator's own
        product, not shared coding vocabulary. package_components
        references services/products/suppliers by FK but writes no rows
        to those tables. Safe to re-run; does not touch any existing data.
        See migrate_add_packages.py for the full story."""
        from migrate_add_packages import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-add-package-service-link")
    def migrate_add_package_service_link_command():
        """Flask CLI: `flask --app app migrate-add-package-service-link` —
        adds packages.service_id, linking every Package back to the 'TP'
        (Tour Package) Category Services Inventory row it was created
        from, so the New Package form can auto-fill Package Number,
        Package Description, and Package Name from an existing Service
        instead of free-typing them. Safe to re-run; does not touch any
        existing data. See migrate_add_package_service_link.py for the
        full story."""
        from migrate_add_package_service_link import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-add-route-stop-layover")
    def migrate_add_route_stop_layover_command():
        """Flask CLI: `flask --app app migrate-add-route-stop-layover` —
        adds package_route_stops.is_layover and .layover_hours, so a
        transit/connection stop on the Route Planning form can be flagged
        and its irrelevant "Nights" field masked in favor of an
        "Approximate Hrs" field. Safe to re-run; does not touch any
        existing data. See migrate_add_route_stop_layover.py for the
        full story."""
        from migrate_add_route_stop_layover import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-add-route-stop-geography")
    def migrate_add_route_stop_geography_command():
        """Flask CLI: `flask --app app migrate-add-route-stop-geography` —
        adds package_route_stops.region_id, .state_id, and
        .state_province_text, so the Route Stop form's Country/City picker
        becomes the same Region -> Country -> Province/State -> City
        cascade used everywhere else in the app (see
        templates/_geography_fields.html), instead of skipping the
        Province/State level entirely. Safe to re-run; does not touch any
        existing data. See migrate_add_route_stop_geography.py for the
        full story."""
        from migrate_add_route_stop_geography import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-add-route-stop-checkpoint")
    def migrate_add_route_stop_checkpoint_command():
        """Flask CLI: `flask --app app migrate-add-route-stop-checkpoint` —
        adds package_route_stops.is_checkpoint, flagging a Route Stop as a
        place the group needs accommodations arranged for -- a planning
        datapoint independent of is_layover -- shown as its own Checkpoint
        column/badge on the Route table. Safe to re-run; does not touch
        any existing data. See migrate_add_route_stop_checkpoint.py for
        the full story."""
        from migrate_add_route_stop_checkpoint import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-add-component-day-costing")
    def migrate_add_component_day_costing_command():
        """Flask CLI: `flask --app app migrate-add-component-day-costing` —
        adds package_components.day_id and .is_accommodation, so a Costing
        line can be itemized against one specific Day (Hotel/Room vs Other
        Service, the two Costing sub-parts shown on each Day's card
        alongside its POI's). Safe to re-run; does not touch any existing
        data. See migrate_add_component_day_costing.py for the full
        story."""
        from migrate_add_component_day_costing import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-add-component-quantity-repeat")
    def migrate_add_component_quantity_repeat_command():
        """Flask CLI: `flask --app app migrate-add-component-quantity-repeat`
        — adds package_components.quantity and .repeat_for_checkpoint, for
        the Day card's Hotel/Room and Other Service Costing tables (QTY x
        Unit Cost/Unit Price -> Total Cost/Total Price/Profit Margin, plus
        a flag that repeats a line across every Day in its Checkpoint).
        Safe to re-run; does not touch any existing data. See
        migrate_add_component_quantity_repeat.py for the full story."""
        from migrate_add_component_quantity_repeat import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-add-component-airline-ticket")
    def migrate_add_component_airline_ticket_command():
        """Flask CLI: `flask --app app migrate-add-component-airline-ticket`
        — adds package_components.is_airline_ticket, so a whole-trip
        component can be flagged an Airline Ticket and shown in its own
        section on the package page (between Route and Entire Tour
        Services) instead of folded into the generic Entire Tour Services
        list. Safe to re-run; does not touch any existing data. See
        migrate_add_component_airline_ticket.py for the full story."""
        from migrate_add_component_airline_ticket import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-add-ai-agent-lookups")
    def migrate_add_ai_agent_lookups_command():
        """Flask CLI: `flask --app app migrate-add-ai-agent-lookups` — adds
        the new lookup values for the "Sikh Pilgrimage Sector" AI Agent /
        Data Enrichment pilot: 4 new POI Types (Airport, Railway Station,
        Hospital, Police Station) and 1 new Supplier Type (Tour Guide,
        with sub-types), for every existing tenant. Safe to re-run; does
        not touch any existing data. See migrate_add_ai_agent_lookups.py
        for the full story."""
        from migrate_add_ai_agent_lookups import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-add-knowledge-graph-edges")
    def migrate_add_knowledge_graph_edges_command():
        """Flask CLI: `flask --app app migrate-add-knowledge-graph-edges` —
        creates the new knowledge_graph_edges table, the structured
        replacement for the freeform knowledge_graph_data text field (see
        knowledge_graph.py). Safe to re-run — CREATE TABLE IF NOT EXISTS.
        See migrate_add_knowledge_graph_edges.py for the full story."""
        from migrate_add_knowledge_graph_edges import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-seed-nankana-sahib-pilot")
    def migrate_seed_nankana_sahib_pilot_command():
        """Flask CLI: `flask --app app migrate-seed-nankana-sahib-pilot` —
        populates the Nankana Sahib pilot-city data (real map coordinates
        on the 9 existing Gurdwara POIs, new non-Gurdwara POIs, new
        Suppliers, and Knowledge Graph edges linking them) for every
        existing tenant. Requires migrate-add-ai-agent-lookups and
        migrate-add-knowledge-graph-edges to have already run. Safe to
        re-run — idempotent. See migrate_seed_nankana_sahib_pilot.py for
        the full story."""
        from migrate_seed_nankana_sahib_pilot import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-add-hotel-template")
    def migrate_add_hotel_template_command():
        """Flask CLI: `flask --app app migrate-add-hotel-template` — adds
        the "Hotel" Supplier Type Template: supplier_types.template_key,
        the hotel_amenity_options / supplier_amenities / hotel_room_types /
        supplier_rooms tables, and seeds the Amenities & Facilities +
        Room Types master lists for every existing tenant, flagging their
        'Hotel' Supplier Type. Safe to re-run; does not touch any existing
        data. See migrate_add_hotel_template.py for the full story."""
        from migrate_add_hotel_template import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-add-hotel-lookup-parent")
    def migrate_add_hotel_lookup_parent_command():
        """Flask CLI: `flask --app app migrate-add-hotel-lookup-parent` —
        nests hotel_amenity_options and hotel_room_types under
        supplier_types (parent = 'Hotel'), so both are manageable from
        Table Maintenance, and renames hotel_room_types.default_description
        to description to match the standard lookup-table shape. Safe to
        re-run; does not touch any Amenities/Room Types data already on
        file. See migrate_add_hotel_lookup_parent.py for the full story."""
        from migrate_add_hotel_lookup_parent import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-add-supplier-documents")
    def migrate_add_supplier_documents_command():
        """Flask CLI: `flask --app app migrate-add-supplier-documents` —
        adds the Suppliers "Documents, Links and Images" sub-module
        (supplier_document_types, supplier_documents,
        supplier_document_locations, supplier_document_keywords,
        supplier_document_hashtags) and seeds the starter document-type
        lookup for every tenant. Safe to re-run; does not touch any
        documents already on file. See migrate_add_supplier_documents.py
        for the full story."""
        from migrate_add_supplier_documents import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-add-poi-images-links")
    def migrate_add_poi_images_links_command():
        """Flask CLI: `flask --app app migrate-add-poi-images-links` — adds
        Images/Photographs (poi_images) and structured Link + Description
        rows (poi_reference_links, replacing the old freeform
        points_of_interest.links textarea) to Points of Interest, and
        backfills any existing freeform links text into the new structured
        table. Safe to re-run; does not touch any images or links already
        on file. See migrate_add_poi_images_links.py for the full story."""
        from migrate_add_poi_images_links import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-add-room-price-history")
    def migrate_add_room_price_history_command():
        """Flask CLI: `flask --app app migrate-add-room-price-history` —
        adds price tracking (supplier_rooms.price_per_night/price_as_of)
        and a full append-only price history log
        (supplier_room_price_history) to Hotel Room Types. Safe to re-run;
        does not touch any Room Type rows already on file. See
        migrate_add_room_price_history.py for the full story."""
        from migrate_add_room_price_history import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-add-ai-agent-foundations")
    def migrate_add_ai_agent_foundations_command():
        """Flask CLI: `flask --app app migrate-add-ai-agent-foundations` —
        creates the AI Agents foundations: agent_runs (one row per
        narrowly-scoped agent invocation), ai_review_items and
        ai_review_fields (the Human Review Queue). Purely additive — no
        existing table is touched. Safe to re-run. See
        migrate_add_ai_agent_foundations.py for the full story."""
        from migrate_add_ai_agent_foundations import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-add-agent-run-sources")
    def migrate_add_agent_run_sources_command():
        """Flask CLI: `flask --app app migrate-add-agent-run-sources` —
        adds agent_run_sources (URLs/documents entered on the New Agent
        Run form). Purely additive. Safe to re-run. See
        migrate_add_agent_run_sources.py for the full story."""
        from migrate_add_agent_run_sources import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-add-ai-agent-undo")
    def migrate_add_ai_agent_undo_command():
        """Flask CLI: `flask --app app migrate-add-ai-agent-undo` — adds
        "Undo Apply" to the Review Queue: applied_snapshot/undone_at/
        undone_by columns on ai_review_items, plus widening its status
        CHECK to allow 'undone' (a table rebuild — SQLite can't alter a
        CHECK constraint in place). Every existing row is preserved
        unchanged. Safe to re-run. See migrate_add_ai_agent_undo.py for
        the full story."""
        from migrate_add_ai_agent_undo import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-add-ai-agent-address-field")
    def migrate_add_ai_agent_address_field_command():
        """Flask CLI: `flask --app app migrate-add-ai-agent-address-field` —
        lets the AI Agents Hotel Intelligence agent propose and apply a
        structured Address (parsed into supplier_addresses, not dumped as
        free text into Notes): adds ai_review_items.applied_address_snapshot,
        a plain additive column so Undo Apply can also reverse an address
        write, alongside applied_snapshot's existing entity-row snapshot.
        Safe to re-run. See migrate_add_ai_agent_address_field.py for the
        full story."""
        from migrate_add_ai_agent_address_field import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-add-document-blob-storage")
    def migrate_add_document_blob_storage_command():
        """Flask CLI: `flask --app app migrate-add-document-blob-storage` —
        converts Supplier Images/Photographs from reference-only storage
        (a Local Drive Path/Cloud Link string) to actually storing the
        image bytes in the database: adds file_data/file_name/mime_type/
        file_size columns to supplier_document_locations and widens its
        location_type CHECK to allow 'Stored in Database' (a table
        rebuild — SQLite can't alter a CHECK constraint in place). Every
        existing location row is preserved unchanged. Safe to re-run. See
        migrate_add_document_blob_storage.py for the full story."""
        from migrate_add_document_blob_storage import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-add-poi-image-gallery")
    def migrate_add_poi_image_gallery_command():
        """Flask CLI: `flask --app app migrate-add-poi-image-gallery` —
        brings Point of Interest Images/Photographs up to the same shape
        as Supplier Images: renames poi_images.caption to image_name and
        adds authors/description/notes/file_data/file_name/mime_type/
        file_size, widens location_type's CHECK to allow 'Stored in
        Database', makes path_or_url optional, and creates
        poi_image_keywords (a table rebuild — SQLite can't alter a CHECK
        constraint in place). Every existing image is preserved unchanged
        (its caption becomes its Image Name). Safe to re-run. See
        migrate_add_poi_image_gallery.py for the full story."""
        from migrate_add_poi_image_gallery import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-add-currency-exchange-rates")
    def migrate_add_currency_exchange_rates_command():
        """Flask CLI: `flask --app app migrate-add-currency-exchange-rates` —
        adds Currency Rates & Exchange Rates: the GLOBAL currencies/
        exchange_rates/exchange_rate_history tables, countries.currency_code
        (Destination Currency) and tenants.host_currency_code (Host
        Currency), then seeds ~154 ISO 4217 currencies, backfills every
        seeded country's currency_code, and seeds USD's exchange rate at
        1.0 (the base). Every existing row is preserved unchanged. Safe to
        re-run. See migrate_add_currency_exchange_rates.py for the full
        story."""
        from migrate_add_currency_exchange_rates import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-add-agent-run-image-sources")
    def migrate_add_agent_run_image_sources_command():
        """Flask CLI: `flask --app app migrate-add-agent-run-image-sources` —
        lets an Agent Run Source be an uploaded image (bytes stored in the
        row, same convention as Supplier/POI Images), not just a URL, and
        widens agent_run_sources' shape to allow it (a table rebuild —
        SQLite can't add a CHECK constraint in place). Every existing
        source row is preserved unchanged as source_type='url'. Safe to
        re-run. See migrate_add_agent_run_image_sources.py for the full
        story."""
        from migrate_add_agent_run_image_sources import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("seed-tenant")
    def seed_tenant_command():
        """Flask CLI: `flask --app app seed-tenant` — creates the first
        tenant (Ma Vie Tours) with its Tenant Admin (Zeb), plus that
        tenant's lookup tables. Only used for a brand-new, empty database;
        safe to re-run (does nothing if the tenant already exists)."""
        from seed_data import seed_first_tenant

        db = get_db()
        seed_first_tenant(db)
        click.echo("Seeded the Ma Vie Tours tenant and its admin user.")

    @app.cli.command("create-system-admin")
    @click.option("--username", prompt=True, help="Login id for the new SystemAdmin.")
    @click.option("--display-name", prompt="Display name", help="Shown in the UI (e.g. the person's real name).")
    @click.option("--password", prompt=True, hide_input=True, confirmation_prompt=True,
                  help="Prompted (hidden) if omitted -- don't pass it on the command line, where it lands in shell history.")
    def create_system_admin_command(username, display_name, password):
        """Flask CLI: `flask --app app create-system-admin` — creates a
        SystemAdmin login: the platform-level ("TMS") role that provisions
        and suspends tenants and runs whole-database health checks and
        backups. A SystemAdmin belongs to no tenant (tenant_id is NULL) and
        can't be created from inside the app, so this command is the
        one-time bootstrap for that account."""
        from security.passwords import hash_password

        db = get_db()
        username = username.strip()
        if not username:
            click.echo("Username is required.")
            return
        if db.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
            click.echo(f"A user with username {username!r} already exists (usernames are unique across the whole system).")
            return
        if len(password) < 8:
            click.echo("Password must be at least 8 characters.")
            return
        db.execute(
            "INSERT INTO users (tenant_id, username, display_name, password_hash, role) "
            "VALUES (NULL, ?, ?, ?, 'SystemAdmin')",
            (username, display_name.strip() or username, hash_password(password)),
        )
        db.commit()
        log_action("Create", "users", None, f"Created SystemAdmin {username!r} via CLI", tenant_id=None)
        click.echo(f"Created SystemAdmin {username!r}. Log in at /login with this username and password.")

    @app.cli.command("set-password")
    @click.option("--username", prompt=True, help="An existing user's login id (any role, any tenant).")
    @click.option("--password", prompt=True, hide_input=True, confirmation_prompt=True,
                  help="Prompted (hidden) if omitted.")
    def set_password_command(username, password):
        """Flask CLI: `flask --app app set-password` — (re)sets an existing
        user's password directly, e.g. to recover a locked-out account on
        the server. Clears must_change_password."""
        from security.passwords import hash_password

        db = get_db()
        username = username.strip()
        user = db.execute("SELECT user_id, tenant_id FROM users WHERE username = ?", (username,)).fetchone()
        if user is None:
            click.echo(f"No user with username {username!r}.")
            return
        if len(password) < 8:
            click.echo("Password must be at least 8 characters.")
            return
        db.execute(
            "UPDATE users SET password_hash = ?, must_change_password = 0, updated_at = datetime('now') WHERE user_id = ?",
            (hash_password(password), user["user_id"]),
        )
        db.commit()
        log_action("Update", "users", user["user_id"], f"Password set via CLI for {username!r}",
                   tenant_id=user["tenant_id"])
        click.echo(f"Password set for {username!r}.")

    @app.cli.command("count-rows")
    @click.argument("output_file", required=False)
    def count_rows_command(output_file):
        """Flask CLI: `flask --app app count-rows counts.txt` — writes the
        row count of every table (sorted by name) to a text file, or prints
        it. Run it before and after moving the database to the server and
        compare the two files: they must be identical."""
        db = get_db()
        tables = [r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()]
        lines = []
        for t in tables:
            count = db.execute('SELECT COUNT(*) FROM "' + t + '"').fetchone()[0]
            lines.append(f"{t}\t{count}")
        text = "\n".join(lines) + "\n"
        if output_file:
            with open(output_file, "w", encoding="utf-8") as f:
                f.write(text)
            click.echo(f"Wrote row counts for {len(tables)} tables to {output_file}.")
        else:
            click.echo(text)

    @app.cli.command("nightly-backup")
    @click.option("--keep", default=14, show_default=True, help="How many nightly backups to keep.")
    def nightly_backup_command(keep):
        """Flask CLI: `flask --app app nightly-backup` — makes a consistent
        copy of the whole database (SQLite online backup, safe while the app
        is running) into instance/backups/, records it in the Backup &
        Restore log as 'Nightly', and deletes nightly backups beyond the
        newest --keep. Run once a day by the VPS's cron job (see README,
        "Nightly backups"). Manual and restore-point backups are never
        touched."""
        import os
        from datetime import datetime

        from blueprints.system_mgmt import _integrity_ok, _table_stats, _write_backup_file

        db = get_db()
        file_name = f"tms_nightly_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
        file_path, size_bytes = _write_backup_file(db, file_name)
        table_count, total_rows = _table_stats(db)
        integrity_ok = _integrity_ok(db)
        db.execute(
            """INSERT INTO backups (file_name, file_path, size_bytes, table_count, total_rows, integrity_ok, backup_type)
               VALUES (?, ?, ?, ?, ?, ?, 'Nightly')""",
            (os.path.basename(file_path), file_path, size_bytes, table_count, total_rows, integrity_ok),
        )
        db.commit()

        old = db.execute(
            "SELECT backup_id, file_path FROM backups WHERE backup_type = 'Nightly' ORDER BY backup_id DESC LIMIT -1 OFFSET ?",
            (max(keep, 1),),
        ).fetchall()
        for row in old:
            try:
                if os.path.exists(row["file_path"]):
                    os.remove(row["file_path"])
            except OSError:
                pass
            db.execute("DELETE FROM backups WHERE backup_id = ?", (row["backup_id"],))
        db.commit()
        log_action("Backup", "backup", None,
                   f"Nightly backup {os.path.basename(file_path)} ({size_bytes} bytes, integrity {'ok' if integrity_ok else 'FAILED'}); "
                   f"removed {len(old)} older nightly backup(s)", tenant_id=None)
        click.echo(f"{datetime.now():%Y-%m-%d %H:%M:%S} backup {os.path.basename(file_path)} "
                   f"{size_bytes} bytes integrity={'ok' if integrity_ok else 'FAILED'} removed_old={len(old)}")

