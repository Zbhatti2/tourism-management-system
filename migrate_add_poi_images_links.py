"""
Migration: adds Images/Photographs and structured Links to Points of
Interest (Sept 2026) — safe to re-run, and does NOT touch any existing
data.

Per Zeb's request: "Add (1) 'Images/Photographs' to 'Point Of Interest'
form. (2) Bulk import Images. (3) Fix the Links section for Link and
Description as shown in the attached image."

What this does, in order:

  1. Creates 2 new tables + indexes, if they don't already exist, matching
     schema.sql's new MODULE Y exactly: poi_images (one row per image/
     photograph, each with its own Cloud Link or Local Drive Path — same
     shape as supplier_document_locations) and poi_reference_links (the
     structured Link + Description rows shown in Zeb's mockup, replacing
     the single freeform points_of_interest.links textarea — same shape as
     organization_reference_links/supplier_reference_links/
     contact_reference_links).

  2. Backfills: for every existing point of interest with non-blank
     points_of_interest.links text and NO poi_reference_links rows yet,
     splits that text on newlines and inserts one poi_reference_links row
     per non-blank line (url = that line, description = NULL). The
     "NO poi_reference_links rows yet" guard makes this idempotent —
     running the migration twice won't duplicate rows — and the legacy
     points_of_interest.links column itself is left completely untouched
     (not cleared, not dropped) so nothing is ever lost. As of Sept 2026
     zero POIs in the sandbox had any non-blank links text, so in practice
     this step is expected to be a no-op, but it's implemented defensively
     in case the live database has some.

New installs don't need this: schema.sql already has the new shape. Run
this once against a database created before this change:

    flask --app app migrate-add-poi-images-links

or directly:

    python migrate_add_poi_images_links.py
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
        if not _table_exists(db, "poi_images"):
            db.execute("""
                CREATE TABLE poi_images (
                    poi_image_id    INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                    poi_id          INTEGER NOT NULL REFERENCES points_of_interest(poi_id),
                    location_type   TEXT CHECK (location_type IN ('Cloud Link','Local Drive Path')),
                    path_or_url     TEXT NOT NULL,
                    caption         TEXT,
                    sort_order      INTEGER DEFAULT 0,
                    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """)
            db.execute("CREATE INDEX idx_poi_images_tenant ON poi_images(tenant_id)")
            db.execute("CREATE INDEX idx_poi_images_poi ON poi_images(poi_id)")
            print("Created poi_images table.")
        else:
            print("poi_images table already exists.")

        if not _table_exists(db, "poi_reference_links"):
            db.execute("""
                CREATE TABLE poi_reference_links (
                    link_id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                    poi_id          INTEGER NOT NULL REFERENCES points_of_interest(poi_id),
                    url             TEXT,
                    description     TEXT,
                    sort_order      INTEGER DEFAULT 0,
                    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """)
            db.execute("CREATE INDEX idx_poi_reference_links_tenant ON poi_reference_links(tenant_id)")
            db.execute("CREATE INDEX idx_poi_reference_links_poi ON poi_reference_links(poi_id)")
            print("Created poi_reference_links table.")
        else:
            print("poi_reference_links table already exists.")
        db.commit()

        # 2. Backfill legacy freeform links text into the new structured
        #    table, one row per non-blank line, skipping any POI that
        #    already has poi_reference_links rows (idempotency guard).
        pois = db.execute(
            "SELECT poi_id, tenant_id, name, links FROM points_of_interest "
            "WHERE links IS NOT NULL AND TRIM(links) != ''"
        ).fetchall()
        backfilled_pois = 0
        backfilled_rows = 0
        for p in pois:
            existing = db.execute(
                "SELECT 1 FROM poi_reference_links WHERE poi_id = ?", (p["poi_id"],)
            ).fetchone()
            if existing:
                continue
            lines = [ln.strip() for ln in p["links"].splitlines() if ln.strip()]
            if not lines:
                continue
            for i, line in enumerate(lines):
                db.execute(
                    "INSERT INTO poi_reference_links (tenant_id, poi_id, url, sort_order) VALUES (?, ?, ?, ?)",
                    (p["tenant_id"], p["poi_id"], line, i),
                )
                backfilled_rows += 1
            backfilled_pois += 1
            print(f"Backfilled {len(lines)} link(s) from legacy freeform text for POI '{p['name']}'.")
        db.commit()
        if backfilled_pois:
            print(f"Backfilled {backfilled_rows} link row(s) across {backfilled_pois} point(s) of interest.")
        else:
            print("No legacy freeform links text needed backfilling.")
    finally:
        db.close()


if __name__ == "__main__":
    migrate()
