"""
Migration: adds the new "Products" sub-module (Inventory Management ->
Products) to an existing database — safe to re-run, and does NOT touch any
existing data.

Built from Claude_Code_Prompt_Products_Module.md and
TTMS_Products_Seed_Data.csv (166 rows, the reference seed data this
migration's taxonomy and catalog are transcribed from — see seed_data.py's
PRODUCT_CATEGORIES/PRODUCT_GROUPS/PRODUCT_SUBGROUPS/PRODUCTS_SEED_DATA).

Products is the second Inventory sub-module, structurally parallel to
Services (migrate_add_services.py above it) but a SEPARATE, INDEPENDENT
registry: product_categories/product_groups/product_subgroups/products/
product_attributes never join against, reference, or share a uniqueness
constraint with service_categories/service_groups/service_subgroups/
services. See schema.sql's MODULE J comment and test_products.py's
explicit proof of this independence.

Deviations from Claude_Code_Prompt_Products_Module.md's generic template,
kept consistent with how Services was actually built in THIS codebase
instead of the prompt-author's assumed shape (the prompt itself says: "If
Services already exists, mirror its exact schema shape"):

  * No separate `product_code` registry table. Services has no separate
    `service_code` table either — the code string is denormalized directly
    onto the tenant-scoped catalog row (products.product_code), computed
    from subgroup_id + sequence_number, with
    UNIQUE(tenant_id, subgroup_id, sequence_number) and
    UNIQUE(tenant_id, product_code) providing the equivalent integrity
    guarantee a separate registry table + FK would have given.
  * The master `products` table is tenant-scoped, not global. The prompt's
    Part 2 doesn't address multi-tenancy (a generic single-tenant
    template), but this app is multi-tenant throughout, and a physical
    product catalog — with its own price/cost/stock_quantity — is exactly
    the kind of thing where two tour operators legitimately keep separate
    inventories, the same reasoning that already made `services`
    tenant-scoped. product_categories/product_groups/product_subgroups
    (the shared coding vocabulary) remain GLOBAL, same split as Services.
  * Attribute storage: a normalized product_attributes key-value table,
    not a JSON/JSONB column (the prompt asked for one or the other,
    explained before implementing — see schema.sql's MODULE J note for the
    full reasoning: this codebase has no JSON column anywhere, and every
    other flexible/multi-value shape here is a normalized child table).

What this does, in order:

  1. Creates the 3 new GLOBAL taxonomy tables (product_categories,
     product_groups, product_subgroups) + indexes, if they don't already
     exist — matching schema.sql's MODULE J exactly. GLOBAL (no
     tenant_id), same treatment as service_categories/service_groups/
     service_subgroups: a fixed, shared coding vocabulary.

  2. Creates the new tenant-scoped `products` table (the actual catalog
     each tour operator builds) + the `product_attributes` key-value table
     + indexes + the two enforcement triggers
     (trg_block_deprecated_product_subgroup on INSERT, ..._update on
     UPDATE OF subgroup_id) — the database-layer backstop that a Product
     Code can never be issued against a 'deprecated' Sub-Group, mirroring
     Services' defense-in-depth even though this taxonomy currently has
     zero deprecated combinations (per the prompt's explicit instruction
     to build the mechanism anyway, for future additions).

  3. Seeds the GLOBAL taxonomy (3 Categories, 15 Groups, 45 Sub-Groups, all
     'active') via seed_data.seed_product_taxonomy() — idempotent,
     INSERT OR IGNORE keyed on each table's UNIQUE constraint.

  4. For every existing tenant: seeds the FULL 166-row Product catalog
     (seed_data.seed_product_catalog()) — unlike Services' demo-services
     seed (a curated handful), this is the complete taxonomy-sourced
     catalog per the prompt's acceptance criteria ("product table contains
     exactly 166 seeded rows"). Idempotent, no-ops for any tenant that
     already has products rows (so it never duplicates or interferes with
     a real user's own catalog edits).

New installs don't need this: schema.sql already has the new shape, and
seed_data.seed_first_tenant() already seeds all of the above for a fresh
tenant. Run this once against a database created before this change:

    flask --app app migrate-add-products

or directly:

    python migrate_add_products.py
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
        if not _table_exists(db, "product_categories"):
            db.execute("""
                CREATE TABLE product_categories (
                    category_id     INTEGER PRIMARY KEY AUTOINCREMENT,
                    category_code   TEXT NOT NULL UNIQUE,
                    category_name   TEXT NOT NULL,
                    description     TEXT,
                    is_active       INTEGER NOT NULL DEFAULT 1,
                    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """)
            print("Created product_categories table.")
        else:
            print("product_categories table already exists.")

        if not _table_exists(db, "product_groups"):
            db.execute("""
                CREATE TABLE product_groups (
                    group_id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    category_id     INTEGER NOT NULL REFERENCES product_categories(category_id),
                    group_code      TEXT NOT NULL,
                    group_name      TEXT NOT NULL,
                    description     TEXT,
                    is_active       INTEGER NOT NULL DEFAULT 1,
                    UNIQUE (category_id, group_code)
                )
            """)
            db.execute("CREATE INDEX idx_product_groups_category ON product_groups(category_id)")
            print("Created product_groups table.")
        else:
            print("product_groups table already exists.")

        if not _table_exists(db, "product_subgroups"):
            db.execute("""
                CREATE TABLE product_subgroups (
                    subgroup_id     INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id        INTEGER NOT NULL REFERENCES product_groups(group_id),
                    subgroup_code   TEXT NOT NULL,
                    subgroup_name   TEXT NOT NULL,
                    description     TEXT,
                    validity_status TEXT NOT NULL DEFAULT 'active' CHECK (validity_status IN ('active','deprecated','under_review')),
                    is_active       INTEGER NOT NULL DEFAULT 1,
                    UNIQUE (group_id, subgroup_code)
                )
            """)
            db.execute("CREATE INDEX idx_product_subgroups_group ON product_subgroups(group_id)")
            db.execute("CREATE INDEX idx_product_subgroups_validity ON product_subgroups(validity_status)")
            print("Created product_subgroups table.")
        else:
            print("product_subgroups table already exists.")

        # 2. Tenant-scoped products table + product_attributes + enforcement triggers.
        if not _table_exists(db, "products"):
            db.execute("""
                CREATE TABLE products (
                    product_id      INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                    subgroup_id     INTEGER NOT NULL REFERENCES product_subgroups(subgroup_id),
                    sequence_number INTEGER NOT NULL CHECK (sequence_number BETWEEN 1 AND 9999),
                    product_code    TEXT NOT NULL,
                    product_name    TEXT NOT NULL,
                    description     TEXT,
                    sku             TEXT,
                    price           NUMERIC,
                    cost            NUMERIC,
                    currency        TEXT,
                    stock_quantity  INTEGER NOT NULL DEFAULT 0,
                    status          TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','active','discontinued')),
                    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
                    UNIQUE (tenant_id, subgroup_id, sequence_number)
                )
            """)
            db.execute("CREATE INDEX idx_products_tenant ON products(tenant_id)")
            db.execute("CREATE INDEX idx_products_subgroup ON products(subgroup_id)")
            db.execute("CREATE UNIQUE INDEX idx_products_tenant_code ON products(tenant_id, product_code)")
            print("Created products table.")
        else:
            print("products table already exists.")

        if not _table_exists(db, "product_attributes"):
            db.execute("""
                CREATE TABLE product_attributes (
                    attribute_id    INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                    product_id      INTEGER NOT NULL REFERENCES products(product_id),
                    attribute_key   TEXT NOT NULL,
                    attribute_value TEXT,
                    UNIQUE (product_id, attribute_key)
                )
            """)
            db.execute("CREATE INDEX idx_product_attributes_tenant ON product_attributes(tenant_id)")
            db.execute("CREATE INDEX idx_product_attributes_product ON product_attributes(product_id)")
            print("Created product_attributes table.")
        else:
            print("product_attributes table already exists.")

        if not _trigger_exists(db, "trg_block_deprecated_product_subgroup"):
            db.execute("""
                CREATE TRIGGER trg_block_deprecated_product_subgroup
                BEFORE INSERT ON products
                FOR EACH ROW
                WHEN (SELECT validity_status FROM product_subgroups WHERE subgroup_id = NEW.subgroup_id) = 'deprecated'
                BEGIN
                    SELECT RAISE(ABORT, 'Cannot issue a Product Code for a deprecated Sub-Group: this combination has been identified as invalid and is blocked.');
                END
            """)
            print("Created trg_block_deprecated_product_subgroup trigger.")
        else:
            print("trg_block_deprecated_product_subgroup trigger already exists.")

        if not _trigger_exists(db, "trg_block_deprecated_product_subgroup_update"):
            db.execute("""
                CREATE TRIGGER trg_block_deprecated_product_subgroup_update
                BEFORE UPDATE OF subgroup_id ON products
                FOR EACH ROW
                WHEN (SELECT validity_status FROM product_subgroups WHERE subgroup_id = NEW.subgroup_id) = 'deprecated'
                BEGIN
                    SELECT RAISE(ABORT, 'Cannot move a Product Code onto a deprecated Sub-Group: this combination has been identified as invalid and is blocked.');
                END
            """)
            print("Created trg_block_deprecated_product_subgroup_update trigger.")
        else:
            print("trg_block_deprecated_product_subgroup_update trigger already exists.")

        db.commit()

        # 3. Seed the GLOBAL taxonomy.
        from seed_data import seed_product_taxonomy, seed_product_catalog

        seed_product_taxonomy(db)
        cat_count = db.execute("SELECT COUNT(*) c FROM product_categories").fetchone()["c"]
        group_count = db.execute("SELECT COUNT(*) c FROM product_groups").fetchone()["c"]
        subgroup_count = db.execute("SELECT COUNT(*) c FROM product_subgroups").fetchone()["c"]
        print(f"Seeded Product Coding System taxonomy: {cat_count} categories, {group_count} groups, {subgroup_count} sub-groups.")

        # 4. Per-tenant: the full 166-row Product catalog.
        tenants = db.execute("SELECT tenant_id, tenant_name FROM tenants").fetchall()
        for t in tenants:
            seed_product_catalog(db, t["tenant_id"])
            print(f"Seeded Product catalog for '{t['tenant_name']}' (no-op if it already had any).")

    finally:
        db.close()


if __name__ == "__main__":
    migrate()
