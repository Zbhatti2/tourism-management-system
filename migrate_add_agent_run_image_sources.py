"""
Migration: lets an Agent Run Source be an uploaded image, not just a URL —
safe to re-run, and does not touch or lose any existing source rows.

Zeb shared a hotel-booking-listing screenshot and asked: "Can the Agent be
trained to pick relevant information from this image?" This adds a second
source shape to agent_run_sources (source_type='image', with the image's
bytes stored right in the row — same "stored in the database" convention
already used for supplier_document_locations/poi_images) alongside the
existing source_type='url' shape, and run_agent() now sends any image
sources to the extraction model as vision input, in addition to whatever
text it fetches from URL sources. See schema.sql's agent_run_sources
comment for the full design note.

SQLite can't add a CHECK constraint or relax a NOT NULL in place, so this
is a table rebuild: every existing agent_run_sources row is preserved
unchanged (source_type set to 'url', file_data/file_name/mime_type/
file_size left NULL — exactly what a pre-existing URL-only row already
was).

New installs don't need this: schema.sql already has the full shape, so
`flask --app app init-db` on a fresh database already has it. Run this
once against a database created before this change:

    flask --app app migrate-add-agent-run-image-sources

or directly:

    python migrate_add_agent_run_image_sources.py
"""
import sqlite3

from config import Config


def _columns(db, table):
    return [row[1] for row in db.execute(f"PRAGMA table_info({table})").fetchall()]


def migrate():
    db = sqlite3.connect(Config.DATABASE_PATH)
    db.row_factory = sqlite3.Row
    try:
        cols = _columns(db, "agent_run_sources")
        if "source_type" in cols and "file_data" in cols:
            print("agent_run_sources already has source_type/file_data -- nothing to do.")
            return

        db.execute("PRAGMA foreign_keys = OFF")

        db.execute("""
            CREATE TABLE agent_run_sources__new (
                source_id       INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                run_id          INTEGER NOT NULL REFERENCES agent_runs(run_id),
                source_type     TEXT NOT NULL DEFAULT 'url' CHECK (source_type IN ('url', 'image')),
                url             TEXT,
                file_data       BLOB,
                file_name       TEXT,
                mime_type       TEXT,
                file_size       INTEGER,
                note            TEXT,
                sort_order      INTEGER DEFAULT 0,
                created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                CHECK (
                    (source_type = 'url' AND url IS NOT NULL AND file_data IS NULL)
                    OR (source_type = 'image' AND file_data IS NOT NULL AND url IS NULL)
                )
            )
        """)
        db.execute("""
            INSERT INTO agent_run_sources__new
                (source_id, tenant_id, run_id, source_type, url, note, sort_order, created_at)
            SELECT source_id, tenant_id, run_id, 'url', url, note, sort_order, created_at
            FROM agent_run_sources
        """)
        moved = db.execute("SELECT COUNT(*) c FROM agent_run_sources__new").fetchone()["c"]
        db.execute("DROP TABLE agent_run_sources")
        db.execute("ALTER TABLE agent_run_sources__new RENAME TO agent_run_sources")
        db.execute("CREATE INDEX idx_agent_run_sources_tenant ON agent_run_sources(tenant_id)")
        db.execute("CREATE INDEX idx_agent_run_sources_run ON agent_run_sources(run_id)")
        db.commit()
        db.execute("PRAGMA foreign_keys = ON")
        print(f"agent_run_sources rebuilt with source_type/file_data/file_name/mime_type/file_size "
              f"({moved} existing row(s) preserved as source_type='url').")

    finally:
        db.close()


if __name__ == "__main__":
    migrate()
