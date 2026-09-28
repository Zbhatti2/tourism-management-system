"""
Migration: brings Point of Interest "Images / Photographs" up to the same
shape as the Supplier "Images / Photographs" gallery (blueprints/
suppliers.py) -- Zeb, Sept 2026: "Modify the Images Section on 'Points Of
Interest' form with exactly the same format as Images Section of the
Supplier Type Hotels Form. Single and Bulk imports with catalog."

Before this, a POI image (poi_images) was just a bare Cloud Link/Local
Drive Path reference with an optional one-line caption -- no author,
description, notes or keywords, no thumbnail preview, no Catalog page, and
Bulk Import just took a caption per file with no other details. This
migration widens poi_images to carry the same per-image fields Supplier
Images already have, and adds bytes-in-the-database storage the same way
Supplier Images was just converted to (migrate_add_document_blob_storage.py):

  image_name (renamed from the old "caption" column -- same idea, same
    existing values carried over, just promoted to a required, prominent
    field the way Supplier Documents' document_name is) -- REQUIRED
    display name for the image, shown on the card/Catalog/detail page.
  authors, description, notes -- same meaning as supplier_documents'
    matching columns (description capped at 200 characters by the
    application, same as Supplier Documents).
  file_data / file_name / mime_type / file_size -- the image's actual
    bytes, for images added after this ships (Single Add and Bulk Import
    both read the file straight off disk at save time and store it here --
    see new_poi_image()/bulk_import_poi_images() in blueprints/poi.py).
  location_type's CHECK constraint widens to allow 'Stored in Database'
    alongside the existing 'Cloud Link'/'Local Drive Path' (unlike
    Suppliers, one poi_images row IS one location -- there's no separate
    locations table here, so this one row just gains the extra columns).
  path_or_url's NOT NULL is dropped -- a 'Stored in Database' row's bytes
    live in file_data, not path_or_url.

Also creates poi_image_keywords, mirroring supplier_document_keywords
exactly (comma-separated Keywords entered on the image form, one row per
term, searchable from the new Catalog page).

SQLite can't ALTER a CHECK constraint, a column's NOT NULL-ness, or rename
a column while changing its role in one step cleanly for our purposes, so
this rebuilds poi_images -- same table-rebuild technique as
migrate_fix_history_fk.py / migrate_add_ai_agent_undo.py /
migrate_add_document_blob_storage.py: create a new table with the wider
shape, copy every existing row across (with "caption" landing in the new
"image_name" column -- the only column that isn't a straight name-for-name
copy; every existing image's caption becomes its Image Name unchanged),
drop the old table, rename the new one into place, recreate its indexes.
No existing image is deleted, renamed on disk, or has its Cloud Link/Local
Drive Path reference touched -- everything already on file keeps working
exactly as before via image_thumbnail()'s fallback path (same as Supplier
Images' legacy fallback).

Safe to re-run -- skipped if poi_images already has an image_name column
(this migration already ran, or the database was created after this
shipped in schema.sql).

New installs don't need this: schema.sql already has the final shape. Run
this once against a database created before this change:

    flask --app app migrate-add-poi-image-gallery

or directly:

    python migrate_add_poi_image_gallery.py
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
        if not _table_exists(db, "poi_images"):
            print("poi_images table doesn't exist yet -- run migrate-add-poi-images-links first. Nothing to do.")
            return

        if _has_column(db, "poi_images", "image_name"):
            print("poi_images.image_name already exists -- POI image gallery migration already applied. Nothing to do.")
            return

        db.execute("PRAGMA foreign_keys = OFF")  # table rebuild below

        db.execute("""
            CREATE TABLE poi_images__new (
                poi_image_id    INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                poi_id          INTEGER NOT NULL REFERENCES points_of_interest(poi_id),
                location_type   TEXT CHECK (location_type IN ('Cloud Link','Local Drive Path','Stored in Database')),
                path_or_url     TEXT,
                image_name      TEXT,
                authors         TEXT,
                description     TEXT,
                notes           TEXT,
                file_data       BLOB,
                file_name       TEXT,
                mime_type       TEXT,
                file_size       INTEGER,
                sort_order      INTEGER DEFAULT 0,
                created_at      TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        db.execute("""
            INSERT INTO poi_images__new
                (poi_image_id, tenant_id, poi_id, location_type, path_or_url, image_name, sort_order, created_at)
            SELECT poi_image_id, tenant_id, poi_id, location_type, path_or_url, caption, sort_order, created_at
            FROM poi_images
        """)
        db.execute("DROP TABLE poi_images")
        db.execute("ALTER TABLE poi_images__new RENAME TO poi_images")
        db.execute("CREATE INDEX idx_poi_images_tenant ON poi_images(tenant_id)")
        db.execute("CREATE INDEX idx_poi_images_poi ON poi_images(poi_id)")

        if not _table_exists(db, "poi_image_keywords"):
            db.execute("""
                CREATE TABLE poi_image_keywords (
                    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                    poi_image_id    INTEGER NOT NULL REFERENCES poi_images(poi_image_id),
                    term            TEXT NOT NULL,
                    PRIMARY KEY (poi_image_id, term)
                )
            """)
            db.execute("CREATE INDEX idx_poi_image_keywords_tenant ON poi_image_keywords(tenant_id)")

        db.execute("PRAGMA foreign_keys = ON")
        db.commit()
        print(
            "Rebuilt poi_images: renamed caption -> image_name and added authors/description/notes/file_data/"
            "file_name/mime_type/file_size, widened the location_type CHECK to allow 'Stored in Database', and "
            "made path_or_url optional. Created poi_image_keywords. All existing rows preserved (captions carried "
            "over as Image Name)."
        )
    finally:
        db.close()


if __name__ == "__main__":
    migrate()
