"""
Migration: adds the new "Services" sub-module (Inventory Management ->
Services) to an existing database — safe to re-run, and does NOT touch any
existing data.

Per the request:

    "We have already created a Placeholder for 'Inventory management' in
    the Blue menu in the System. There are two type of Inventory. The
    first is 'Services' which will have its own table since there are no
    Quantities On hand and Minimum Order Quantity type requirements.
    Services inventory is a Services Code and a Description. The services
    table will primarily be used for planning and formalizing a Tour
    package."

Built from the attached Service_Coding_System_Schema.md (nested
Category -> Group -> Sub-Group -> Sequence code, e.g. 'TP-MN-MC-0001') and
TTMS_Service_Coding_Seed.sql (the reference seed data this migration's
taxonomy is transcribed from — see seed_data.py's SERVICE_CATEGORIES/
SERVICE_GROUPS/SERVICE_SUBGROUP_SETS/SERVICE_SUBGROUP_OVERRIDES).

What this does, in order:

  1. Creates the 3 new GLOBAL taxonomy tables (service_categories,
     service_groups, service_subgroups) + indexes, if they don't already
     exist — matching schema.sql's MODULE I exactly. GLOBAL (no
     tenant_id), same treatment as regions/countries/states/cities: this
     is a fixed, shared coding vocabulary, not each tenant's private
     opinion. See schema.sql's MODULE I comment for the full design note
     and scope decision (the source design doc's Part 2 — per-tenant
     overrides/extensions of this taxonomy — is intentionally not built
     here; there's only one tenant today and the user's own framing of
     this task was the simpler "a Services Code and a Description," not
     the fuller multi-tenant configurability doc).

  2. Creates the new tenant-scoped `services` table (the actual catalog
     each tour operator builds) + indexes + the two enforcement triggers
     (trg_block_deprecated_service_subgroup on INSERT, ..._update on
     UPDATE OF subgroup_id) — the database-layer backstop that a Service
     Code can never be issued against a 'deprecated' Sub-Group, matching
     schema doc S9.2/S9.3's defense-in-depth (layers 1-2 are the New
     Service form's picker and blueprints/services.py's route-level
     check; this is layer 3, the one nothing can bypass).

  3. Seeds the GLOBAL taxonomy (10 active Categories + 5 retired legacy
     Categories, ~51 Groups, ~200 Sub-Groups with every validity_status
     flag from the design doc) via seed_data.seed_service_taxonomy() —
     idempotent, INSERT OR IGNORE keyed on each table's UNIQUE
     constraint.

  4. For every existing tenant: seeds a handful of representative demo
     Services (seed_data.seed_demo_services()) drawn from the design
     doc's own worked examples (the Umrah composite booking, the
     Boston-Malaysia-Pakistan multi-nation tour, ...) — idempotent, no-ops
     for any tenant that already has services rows (so it never
     duplicates or interferes with services a real user has since added).

New installs don't need this: schema.sql already has the new shape, and
seed_data.seed_first_tenant() already seeds all of the above for a fresh
tenant. Run this once against a database created before this change:

    flask --app app migrate-add-services

or directly:

    python migrate_add_services.py
"""
import sqlite3

from config import Config


def _table_exists(db, name):
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def _trigger_exists(db, name):
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND name = ?", (name,)
    ).fetchone() is not None


def migrate():
    db = sqlite3.connect(Config.DATABASE_PATH)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA foreign_keys = ON")

        # 1. GLOBAL taxonomy tables.
        if not _table_exists(db, "service_categories"):
            db.execute("""
                CREATE TABLE service_categories (
                    category_id     INTEGER PRIMARY KEY AUTOINCREMENT,
                    category_code   TEXT NOT NULL UNIQUE,
                    category_name   TEXT NOT NULL,
                    description     TEXT,
                    is_active       INTEGER NOT NULL DEFAULT 1,
                    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """)
            print("Created service_categories table.")
        else:
            print("service_categories table already exists.")

        if not _table_exists(db, "service_groups"):
            db.execute("""
                CREATE TABLE service_groups (
                    group_id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    category_id     INTEGER NOT NULL REFERENCES service_categories(category_id),
                    group_code      TEXT NOT NULL,
                    group_name      TEXT NOT NULL,
                    description     TEXT,
                    is_active       INTEGER NOT NULL DEFAULT 1,
                    UNIQUE (category_id, group_code)
                )
            """)
            db.execute("CREATE INDEX idx_service_groups_category ON service_groups(category_id)")
            print("Created service_groups table.")
        else:
            print("service_groups table already exists.")

        if not _table_exists(db, "service_subgroups"):
            db.execute("""
                CREATE TABLE service_subgroups (
                    subgroup_id     INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id        INTEGER NOT NULL REFERENCES service_groups(group_id),
                    subgroup_code   TEXT NOT NULL,
                    subgroup_name   TEXT NOT NULL,
                    description     TEXT,
                    validity_status TEXT NOT NULL DEFAULT 'active' CHECK (validity_status IN ('active','deprecated','under_review')),
                    is_active       INTEGER NOT NULL DEFAULT 1,
                    UNIQUE (group_id, subgroup_code)
                )
            """)
            db.execute("CREATE INDEX idx_service_subgroups_group ON service_subgroups(group_id)")
            db.execute("CREATE INDEX idx_service_subgroups_validity ON service_subgroups(validity_status)")
            print("Created service_subgroups table.")
        else:
            print("service_subgroups table already exists.")

        # 2. Tenant-scoped services table + enforcement triggers.
        if not _table_exists(db, "services"):
            db.execute("""
                CREATE TABLE services (
                    service_id      INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                    subgroup_id     INTEGER NOT NULL REFERENCES service_subgroups(subgroup_id),
                    sequence_number INTEGER NOT NULL CHECK (sequence_number BETWEEN 1 AND 9999),
                    service_code    TEXT NOT NULL,
                    description     TEXT NOT NULL,
                    status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('draft','active','archived')),
                    notes           TEXT,
                    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
                    UNIQUE (tenant_id, subgroup_id, sequence_number)
                )
            """)
            db.execute("CREATE INDEX idx_services_tenant ON services(tenant_id)")
            db.execute("CREATE INDEX idx_services_subgroup ON services(subgroup_id)")
            db.execute("CREATE UNIQUE INDEX idx_services_tenant_code ON services(tenant_id, service_code)")
            print("Created services table.")
        else:
            print("services table already exists.")

        if not _trigger_exists(db, "trg_block_deprecated_service_subgroup"):
            db.execute("""
                CREATE TRIGGER trg_block_deprecated_service_subgroup
                BEFORE INSERT ON services
                FOR EACH ROW
                WHEN (SELECT validity_status FROM service_subgroups WHERE subgroup_id = NEW.subgroup_id) = 'deprecated'
                BEGIN
                    SELECT RAISE(ABORT, 'Cannot issue a Service Code for a deprecated Sub-Group: this combination has been identified as invalid and is blocked.');
                END
            """)
            print("Created trg_block_deprecated_service_subgroup trigger.")
        else:
            print("trg_block_deprecated_service_subgroup trigger already exists.")

        if not _trigger_exists(db, "trg_block_deprecated_service_subgroup_update"):
            db.execute("""
                CREATE TRIGGER trg_block_deprecated_service_subgroup_update
                BEFORE UPDATE OF subgroup_id ON services
                FOR EACH ROW
                WHEN (SELECT validity_status FROM service_subgroups WHERE subgroup_id = NEW.subgroup_id) = 'deprecated'
                BEGIN
                    SELECT RAISE(ABORT, 'Cannot move a Service Code onto a deprecated Sub-Group: this combination has been identified as invalid and is blocked.');
                END
            """)
            print("Created trg_block_deprecated_service_subgroup_update trigger.")
        else:
            print("trg_block_deprecated_service_subgroup_update trigger already exists.")

        db.commit()

        # 3. Seed the GLOBAL taxonomy.
        from seed_data import seed_service_taxonomy, seed_demo_services

        seed_service_taxonomy(db)
        cat_count = db.execute("SELECT COUNT(*) c FROM service_categories").fetchone()["c"]
        group_count = db.execute("SELECT COUNT(*) c FROM service_groups").fetchone()["c"]
        subgroup_count = db.execute("SELECT COUNT(*) c FROM service_subgroups").fetchone()["c"]
        print(f"Seeded Service Coding System taxonomy: {cat_count} categories, {group_count} groups, {subgroup_count} sub-groups.")

        # 4. Per-tenant: a handful of representative demo Services.
        tenants = db.execute("SELECT tenant_id, tenant_name FROM tenants").fetchall()
        for t in tenants:
            seed_demo_services(db, t["tenant_id"])
            print(f"Seeded demo Services for '{t['tenant_name']}' (no-op if it already had any).")

    finally:
        db.close()


if __name__ == "__main__":
    migrate()
