"""
Migration: adds the "Documents, Links and Images" sub-module to Suppliers
(Sept 2026) — safe to re-run, and does NOT touch any existing data.

Per Zeb's request: "In the Suppliers Form and Table, please add a
sub-module called 'Documents, Links and Images'. Each supplier's copies of
business licenses, permissions, rules and regulations, agreements and
Images / photographs will be stored in this sub-module. The example form
from 'Organization Intelligence/Documents' can be used as reference.
Notice I have removed 'Knowledge domains' as that is not required for
Suppliers."

What this does, in order:

  1. Creates 5 new tables + indexes, if they don't already exist, matching
     schema.sql's new MODULE X exactly: supplier_document_types (the
     Table Maintenance-managed lookup for what KIND of document — Business
     License, Permit, Agreement, ...), supplier_documents (one row per
     document/link/image on a Supplier's own "Documents, Links and Images"
     card), supplier_document_locations (one or more Local Drive Path /
     Cloud Link copies per document), supplier_document_keywords and
     supplier_document_hashtags (comma-separated tag lists, same shape as
     Module D's content_keywords/content_hashtags).

  2. For every existing tenant: seeds SUPPLIER_DOCUMENT_TYPES
     (seed_data.SUPPLIER_DOCUMENT_TYPES via _seed_simple) — idempotent,
     INSERT OR IGNORE keyed on (tenant_id, code).

New installs don't need this: schema.sql already has the new shape, and
seed_data.seed_first_tenant() (via seed_lookup_tables()) already seeds
SUPPLIER_DOCUMENT_TYPES for a fresh tenant. Run this once against a
database created before this change:

    flask --app app migrate-add-supplier-documents

or directly:

    python migrate_add_supplier_documents.py
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
        # 1. New tables.
        if not _table_exists(db, "supplier_document_types"):
            db.execute("""
                CREATE TABLE supplier_document_types (
                    document_type_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                    code            TEXT,
                    label           TEXT NOT NULL,
                    description     TEXT,
                    sort_order      INTEGER DEFAULT 0,
                    is_active       INTEGER NOT NULL DEFAULT 1,
                    UNIQUE (tenant_id, code)
                )
            """)
            db.execute("CREATE INDEX idx_supplier_document_types_tenant ON supplier_document_types(tenant_id)")
            print("Created supplier_document_types table.")
        else:
            print("supplier_document_types table already exists.")

        if not _table_exists(db, "supplier_documents"):
            db.execute("""
                CREATE TABLE supplier_documents (
                    supplier_document_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                    supplier_id     INTEGER NOT NULL REFERENCES suppliers(supplier_id),
                    document_name   TEXT NOT NULL,
                    document_type_id INTEGER REFERENCES supplier_document_types(document_type_id),
                    authors         TEXT,
                    description     TEXT,
                    notes           TEXT,
                    is_deleted      INTEGER NOT NULL DEFAULT 0,
                    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """)
            db.execute("CREATE INDEX idx_supplier_documents_tenant ON supplier_documents(tenant_id)")
            db.execute("CREATE INDEX idx_supplier_documents_supplier ON supplier_documents(supplier_id)")
            db.execute("CREATE INDEX idx_supplier_documents_type ON supplier_documents(document_type_id)")
            print("Created supplier_documents table.")
        else:
            print("supplier_documents table already exists.")

        if not _table_exists(db, "supplier_document_locations"):
            db.execute("""
                CREATE TABLE supplier_document_locations (
                    location_id     INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                    supplier_document_id INTEGER NOT NULL REFERENCES supplier_documents(supplier_document_id),
                    location_type   TEXT CHECK (location_type IN ('Cloud Link','Local Drive Path')),
                    path_or_url     TEXT NOT NULL
                )
            """)
            db.execute("CREATE INDEX idx_supplier_document_locations_tenant ON supplier_document_locations(tenant_id)")
            db.execute("CREATE INDEX idx_supplier_document_locations_document ON supplier_document_locations(supplier_document_id)")
            print("Created supplier_document_locations table.")
        else:
            print("supplier_document_locations table already exists.")

        if not _table_exists(db, "supplier_document_keywords"):
            db.execute("""
                CREATE TABLE supplier_document_keywords (
                    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                    supplier_document_id INTEGER NOT NULL REFERENCES supplier_documents(supplier_document_id),
                    term            TEXT NOT NULL,
                    PRIMARY KEY (supplier_document_id, term)
                )
            """)
            db.execute("CREATE INDEX idx_supplier_document_keywords_tenant ON supplier_document_keywords(tenant_id)")
            print("Created supplier_document_keywords table.")
        else:
            print("supplier_document_keywords table already exists.")

        if not _table_exists(db, "supplier_document_hashtags"):
            db.execute("""
                CREATE TABLE supplier_document_hashtags (
                    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                    supplier_document_id INTEGER NOT NULL REFERENCES supplier_documents(supplier_document_id),
                    term            TEXT NOT NULL,
                    PRIMARY KEY (supplier_document_id, term)
                )
            """)
            db.execute("CREATE INDEX idx_supplier_document_hashtags_tenant ON supplier_document_hashtags(tenant_id)")
            print("Created supplier_document_hashtags table.")
        else:
            print("supplier_document_hashtags table already exists.")
        db.commit()

        # 2. Per-tenant: seed the document-type lookup.
        from seed_data import SUPPLIER_DOCUMENT_TYPES, _seed_simple

        tenants = db.execute("SELECT tenant_id, tenant_name FROM tenants").fetchall()
        for t in tenants:
            _seed_simple(db, "supplier_document_types", t["tenant_id"], SUPPLIER_DOCUMENT_TYPES)
            print(f"Seeded Supplier Document Types for '{t['tenant_name']}'.")
        db.commit()
    finally:
        db.close()


if __name__ == "__main__":
    migrate()
