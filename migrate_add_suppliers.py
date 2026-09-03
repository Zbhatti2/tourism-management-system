"""
Migration: adds the new "Suppliers" module to an existing database — safe
to re-run, and does NOT touch any existing data.

Suppliers is deliberately kept fully separate from Organizations at every
level (its own type/sub-type tables, its own addresses, its own contacts,
its own phone numbers) — see schema.sql's "MODULE C — SUPPLIERS
MANAGEMENT" comment for the full design note.

What this does, in order:

  1. Creates the 4 new lookup tables (supplier_address_types, phone_types,
     supplier_types, supplier_subtypes) + indexes, if they don't already
     exist — matching schema.sql exactly. (supplier_address_types was
     originally named address_types; renamed once Organizations grew its
     own, separate organization_address_types lookup — see
     migrate_organization_addresses.py.)

  2. Creates the 4 new Suppliers tables (suppliers, supplier_addresses,
     supplier_contacts, supplier_contact_phones) + indexes, if they don't
     already exist — matching schema.sql exactly.

  3. Narrows the pre-existing `addresses` table's owner_type CHECK
     constraint from ('Contact', 'PersonalAccount') down to ('Contact') —
     'PersonalAccount' was dead since the Accounts module was archived in
     an earlier migration, and this keeps a rebuilt table's shape
     identical to a fresh install's. SQLite can't ALTER a CHECK constraint
     in place, so this rebuilds the table (new table + copy rows + drop +
     rename), same technique as migrate_add_organization_notes.py. Skipped
     entirely if the constraint has already been narrowed (e.g. the
     database was already migrated, or was created after this change
     shipped in schema.sql).

  4. For every existing tenant: seeds Address Types (Main Office, Billing
     Address), (Supplier Contact) Phone Types (Cell, Office, Fax), and the
     Supplier Types + Sub-Types (Hotel, Resort, ... Airlines, with their
     nested sub-types) via seed_data.seed_supplier_lookups(). Idempotent —
     INSERT OR IGNORE keyed on (tenant_id, code).

New installs don't need this: schema.sql already has the new shape, and
seed_data.seed_first_tenant() already seeds all of the above for a fresh
tenant. Run this once against a database created before this change:

    flask --app app migrate-add-suppliers

or directly:

    python migrate_add_suppliers.py
"""
import sqlite3

from config import Config


def _table_exists(db, name):
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def _addresses_check_needs_narrowing(db):
    row = db.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'addresses'"
    ).fetchone()
    if row is None or row["sql"] is None:
        return False
    return "PersonalAccount" in row["sql"]


def migrate():
    db = sqlite3.connect(Config.DATABASE_PATH)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA foreign_keys = OFF")  # table rebuild below, re-enabled at the end

        # 1. New lookup tables.
        if not _table_exists(db, "supplier_address_types") and not _table_exists(db, "address_types"):
            db.execute("""
                CREATE TABLE supplier_address_types (
                    address_type_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                    code            TEXT,
                    label           TEXT NOT NULL,
                    description     TEXT,
                    sort_order      INTEGER DEFAULT 0,
                    is_active       INTEGER NOT NULL DEFAULT 1,
                    UNIQUE (tenant_id, code)
                )
            """)
            db.execute("CREATE INDEX idx_supplier_address_types_tenant ON supplier_address_types(tenant_id)")
            print("Created supplier_address_types table.")
        else:
            print("supplier_address_types table already exists (as itself, or under its old name address_types, "
                  "which migrate-organization-addresses will rename).")

        if not _table_exists(db, "phone_types"):
            db.execute("""
                CREATE TABLE phone_types (
                    phone_type_id   INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                    code            TEXT,
                    label           TEXT NOT NULL,
                    description     TEXT,
                    sort_order      INTEGER DEFAULT 0,
                    is_active       INTEGER NOT NULL DEFAULT 1,
                    UNIQUE (tenant_id, code)
                )
            """)
            db.execute("CREATE INDEX idx_phone_types_tenant ON phone_types(tenant_id)")
            print("Created phone_types table.")
        else:
            print("phone_types table already exists.")

        if not _table_exists(db, "supplier_types"):
            db.execute("""
                CREATE TABLE supplier_types (
                    supplier_type_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                    code            TEXT,
                    label           TEXT NOT NULL,
                    description     TEXT,
                    sort_order      INTEGER DEFAULT 0,
                    is_active       INTEGER NOT NULL DEFAULT 1,
                    UNIQUE (tenant_id, code)
                )
            """)
            db.execute("CREATE INDEX idx_supplier_types_tenant ON supplier_types(tenant_id)")
            print("Created supplier_types table.")
        else:
            print("supplier_types table already exists.")

        if not _table_exists(db, "supplier_subtypes"):
            db.execute("""
                CREATE TABLE supplier_subtypes (
                    supplier_subtype_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                    supplier_type_id INTEGER NOT NULL REFERENCES supplier_types(supplier_type_id),
                    code            TEXT,
                    label           TEXT NOT NULL,
                    description     TEXT,
                    sort_order      INTEGER DEFAULT 0,
                    is_active       INTEGER NOT NULL DEFAULT 1,
                    UNIQUE (tenant_id, code)
                )
            """)
            db.execute("CREATE INDEX idx_supplier_subtypes_tenant ON supplier_subtypes(tenant_id)")
            db.execute("CREATE INDEX idx_supplier_subtypes_type ON supplier_subtypes(supplier_type_id)")
            print("Created supplier_subtypes table.")
        else:
            print("supplier_subtypes table already exists.")

        # 2. New Suppliers tables.
        if not _table_exists(db, "suppliers"):
            db.execute("""
                CREATE TABLE suppliers (
                    supplier_id     INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                    supplier_name   TEXT NOT NULL,
                    supplier_type_id INTEGER REFERENCES supplier_types(supplier_type_id),
                    supplier_subtype_id INTEGER REFERENCES supplier_subtypes(supplier_subtype_id),
                    web_page        TEXT,
                    notes           TEXT,
                    knowledge_graph_data TEXT,
                    is_deleted      INTEGER NOT NULL DEFAULT 0,
                    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """)
            db.execute("CREATE INDEX idx_suppliers_tenant ON suppliers(tenant_id)")
            db.execute("CREATE INDEX idx_suppliers_type ON suppliers(supplier_type_id)")
            db.execute("CREATE INDEX idx_suppliers_subtype ON suppliers(supplier_subtype_id)")
            print("Created suppliers table.")
        else:
            print("suppliers table already exists.")

        if not _table_exists(db, "supplier_addresses"):
            db.execute("""
                CREATE TABLE supplier_addresses (
                    supplier_address_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                    supplier_id     INTEGER NOT NULL REFERENCES suppliers(supplier_id),
                    address_type_id INTEGER REFERENCES supplier_address_types(address_type_id),
                    street          TEXT,
                    unit            TEXT,
                    region_id       INTEGER REFERENCES regions(region_id),
                    country_id      INTEGER REFERENCES countries(country_id),
                    state_id        INTEGER REFERENCES states(state_id),
                    state_province_text TEXT,
                    city_id         INTEGER REFERENCES cities(city_id),
                    city_text       TEXT,
                    postal_code     TEXT,
                    is_primary      INTEGER NOT NULL DEFAULT 0,
                    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """)
            db.execute("CREATE INDEX idx_supplier_addresses_tenant ON supplier_addresses(tenant_id)")
            db.execute("CREATE INDEX idx_supplier_addresses_supplier ON supplier_addresses(supplier_id)")
            db.execute("CREATE INDEX idx_supplier_addresses_type ON supplier_addresses(address_type_id)")
            print("Created supplier_addresses table.")
        else:
            print("supplier_addresses table already exists.")

        if not _table_exists(db, "supplier_contacts"):
            db.execute("""
                CREATE TABLE supplier_contacts (
                    supplier_contact_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                    supplier_id     INTEGER NOT NULL REFERENCES suppliers(supplier_id),
                    name            TEXT NOT NULL,
                    title           TEXT,
                    supplier_address_id INTEGER REFERENCES supplier_addresses(supplier_address_id),
                    email           TEXT,
                    notes           TEXT,
                    is_deleted      INTEGER NOT NULL DEFAULT 0,
                    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """)
            db.execute("CREATE INDEX idx_supplier_contacts_tenant ON supplier_contacts(tenant_id)")
            db.execute("CREATE INDEX idx_supplier_contacts_supplier ON supplier_contacts(supplier_id)")
            db.execute("CREATE INDEX idx_supplier_contacts_address ON supplier_contacts(supplier_address_id)")
            print("Created supplier_contacts table.")
        else:
            print("supplier_contacts table already exists.")

        if not _table_exists(db, "supplier_contact_phones"):
            db.execute("""
                CREATE TABLE supplier_contact_phones (
                    phone_id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                    supplier_contact_id INTEGER NOT NULL REFERENCES supplier_contacts(supplier_contact_id),
                    phone_type_id   INTEGER REFERENCES phone_types(phone_type_id),
                    country_code    TEXT,
                    area_code       TEXT,
                    number          TEXT NOT NULL,
                    extension       TEXT,
                    is_primary      INTEGER NOT NULL DEFAULT 0,
                    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """)
            db.execute("CREATE INDEX idx_supplier_contact_phones_tenant ON supplier_contact_phones(tenant_id)")
            db.execute("CREATE INDEX idx_supplier_contact_phones_contact ON supplier_contact_phones(supplier_contact_id)")
            db.execute("CREATE INDEX idx_supplier_contact_phones_type ON supplier_contact_phones(phone_type_id)")
            print("Created supplier_contact_phones table.")
        else:
            print("supplier_contact_phones table already exists.")

        db.commit()

        # 3. Narrow addresses.owner_type's CHECK constraint (drop the dead
        # 'PersonalAccount' branch) — cosmetic/consistency only, rebuilds
        # the table since SQLite can't ALTER a CHECK constraint in place.
        if _addresses_check_needs_narrowing(db):
            db.execute("""
                CREATE TABLE addresses__new (
                    address_id      INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                    owner_type      TEXT NOT NULL CHECK (owner_type IN ('Contact')),
                    owner_id        INTEGER NOT NULL,
                    address_type    TEXT,
                    street          TEXT,
                    unit            TEXT,
                    region_id       INTEGER REFERENCES regions(region_id),
                    country_id      INTEGER REFERENCES countries(country_id),
                    state_id        INTEGER REFERENCES states(state_id),
                    state_province_text TEXT,
                    city_id         INTEGER REFERENCES cities(city_id),
                    city_text       TEXT,
                    postal_code     TEXT,
                    is_primary      INTEGER NOT NULL DEFAULT 0,
                    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """)
            cols = [r["name"] for r in db.execute("PRAGMA table_info(addresses)").fetchall()]
            col_list = ", ".join(cols)
            db.execute(f"INSERT INTO addresses__new ({col_list}) SELECT {col_list} FROM addresses")
            db.execute("DROP TABLE addresses")
            db.execute("ALTER TABLE addresses__new RENAME TO addresses")
            db.execute("CREATE INDEX idx_addresses_owner ON addresses(owner_type, owner_id)")
            db.execute("CREATE INDEX idx_addresses_tenant ON addresses(tenant_id)")
            db.commit()
            print("Narrowed addresses.owner_type CHECK constraint to ('Contact') — 'PersonalAccount' was dead "
                  "since the Accounts module was archived; existing rows were preserved.")
        else:
            print("addresses.owner_type CHECK constraint already narrowed (or table not present) — nothing to do.")

        db.execute("PRAGMA foreign_keys = ON")
        db.commit()

        # 4. Per-tenant: Address Types, (Supplier Contact) Phone Types,
        # Supplier Types + Sub-Types.
        from seed_data import _seed_simple, SUPPLIER_ADDRESS_TYPES, SUPPLIER_PHONE_TYPES, seed_supplier_lookups

        tenants = db.execute("SELECT tenant_id, tenant_name FROM tenants").fetchall()
        for t in tenants:
            _seed_simple(db, "supplier_address_types", t["tenant_id"], SUPPLIER_ADDRESS_TYPES)
            _seed_simple(db, "phone_types", t["tenant_id"], SUPPLIER_PHONE_TYPES)
            db.commit()
            seed_supplier_lookups(db, t["tenant_id"])
            db.commit()
            print(f"Seeded Address Types, (Supplier Contact) Phone Types, and Supplier Types/Sub-Types for '{t['tenant_name']}'.")

    finally:
        db.close()


if __name__ == "__main__":
    migrate()
