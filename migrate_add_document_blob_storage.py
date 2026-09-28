"""
Migration: converts Supplier "Images / Photographs" from reference-only
storage (a Local Drive Path or Cloud Link string in
supplier_document_locations.path_or_url) to actually storing the image
bytes in the database.

Zeb, Sept 2026, after seeing 47 real Bulk-Imported images all sitting as
bare Local Drive Path references: "Please convert to the model where
images are stored in the database." Motivation confirmed in the prior
turn's discussion of the old model's downsides -- a stored path is
fragile (breaks if the source file moves, is renamed, or is deleted; a
db backup alone doesn't back up the images) -- so this is a deliberate
move to a real, self-contained BLOB, not the separate "TMS-owned uploads
folder" pattern contacts/employees use for profile photos.

Adds four columns to supplier_document_locations:
  file_data  BLOB -- the raw image bytes.
  file_name  TEXT -- original file name (for troubleshooting / download).
  mime_type  TEXT -- e.g. "image/png", used to serve the right Content-Type.
  file_size  INTEGER -- byte length, shown on the Image Catalog / detail page.

Also widens the location_type CHECK constraint to allow a third value,
'Stored in Database', alongside the existing 'Cloud Link' and 'Local
Drive Path' -- and drops path_or_url's NOT NULL constraint, since a
'Stored in Database' row's bytes live in file_data, not path_or_url (the
column is still populated with the original source path for provenance
when it's known, but it's no longer required).

SQLite can't ALTER a CHECK constraint or a column's NOT NULL-ness in
place, so this rebuilds the table -- same technique as
migrate_fix_history_fk.py / migrate_add_ai_agent_undo.py: create a new
table with the wider shape, copy every existing row's original columns
across unchanged (the 4 new columns land NULL, which is exactly right --
no pre-existing location has stored bytes yet), drop the old table,
rename the new one into place, recreate its indexes.

Existing Cloud Link / Local Drive Path rows are left completely alone --
they keep working exactly as before via image_thumbnail()'s fallback
path. This migration only makes new storage possible; it does not
retroactively pull bytes in for already-imported images (there is no
"go re-fetch every path that's ever been typed in" step -- see
blueprints/suppliers.py's bulk_import_images() for where NEW imports
start writing file_data).

Safe to re-run -- skipped if supplier_document_locations already has a
file_data column (this migration already ran, or the database was
created after this shipped in schema.sql).

New installs don't need this: schema.sql already has the final shape.
Run this once against a database created before this change:

    flask --app app migrate-add-document-blob-storage

or directly:

    python migrate_add_document_blob_storage.py
"""
import sqlite3

from config import Config


def _table_exists(db, name):
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def _has_column(db, table, column):
    return any(r["name"] == column for r in db.execute(f"PRAGMA table_info({table})").fetchall())


def migrate():
    db = sqlite3.connect(Config.DATABASE_PATH)
    db.row_factory = sqlite3.Row
    try:
        if not _table_exists(db, "supplier_document_locations"):
            print("supplier_document_locations table doesn't exist yet -- run migrate-add-suppliers first. Nothing to do.")
            return

        if _has_column(db, "supplier_document_locations", "file_data"):
            print("supplier_document_locations.file_data already exists -- blob storage migration already applied. Nothing to do.")
            return

        db.execute("PRAGMA foreign_keys = OFF")  # table rebuild below

        old_cols = [r["name"] for r in db.execute("PRAGMA table_info(supplier_document_locations)").fetchall()]
        col_list = ", ".join(old_cols)

        db.execute("""
            CREATE TABLE supplier_document_locations__new (
                location_id     INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                supplier_document_id INTEGER NOT NULL REFERENCES supplier_documents(supplier_document_id),
                location_type   TEXT CHECK (location_type IN ('Cloud Link','Local Drive Path','Stored in Database')),
                path_or_url     TEXT,
                file_data       BLOB,
                file_name       TEXT,
                mime_type       TEXT,
                file_size       INTEGER
            )
        """)
        db.execute(f"INSERT INTO supplier_document_locations__new ({col_list}) SELECT {col_list} FROM supplier_document_locations")
        db.execute("DROP TABLE supplier_document_locations")
        db.execute("ALTER TABLE supplier_document_locations__new RENAME TO supplier_document_locations")
        db.execute("CREATE INDEX idx_supplier_document_locations_tenant ON supplier_document_locations(tenant_id)")
        db.execute("CREATE INDEX idx_supplier_document_locations_document ON supplier_document_locations(supplier_document_id)")

        db.execute("PRAGMA foreign_keys = ON")
        db.commit()
        print(
            "Rebuilt supplier_document_locations: added file_data/file_name/mime_type/file_size, widened the "
            "location_type CHECK to allow 'Stored in Database', and made path_or_url optional. "
            "All existing rows preserved unchanged."
        )
    finally:
        db.close()


if __name__ == "__main__":
    migrate()
