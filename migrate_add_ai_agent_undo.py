"""
Migration: adds "Undo Apply" to the AI Agents Review Queue -- Zeb's request
to "reverse the most recent update in case of an error", with "the previous
record ... stored in history".

Scoped, per the plan discussed with Zeb, to AI Agent Approve & Apply
specifically (not a system-wide undo across every TMS module) and to one
level: only the single most recently applied change to a given record can
be reversed, not a full multi-version history.

Adds three columns to ai_review_items:
  applied_snapshot -- JSON: the full entity row (every column) exactly as
    it was immediately before this item's Approve & Apply wrote to it.
    Captured at apply time (not review time) so it's an exact "undo",
    regardless of how long the item sat in the queue first. NULL for
    action_type='create' (nothing existed before that write -- see below)
    or for any item that was never applied.
  undone_at / undone_by -- when/who reversed it, mirroring reviewed_at/
    reviewed_by's existing pattern.

Also widens the status CHECK constraint to allow the new 'undone' value
alongside the existing pending/approved/rejected/needs_changes/applied.
SQLite can't ALTER a CHECK constraint in place, so this rebuilds the table
-- same technique migrate_fix_history_fk.py used for contact_phones_history
etc: create a new table with the wider CHECK + the 3 new columns, copy
every existing row across unchanged (the 3 new columns land NULL, which is
exactly right -- no pre-existing applied item has a captured snapshot),
drop the old table, rename the new one into place, recreate its indexes.

Undo behavior itself (blueprints/ai_agents.py's undo_apply()):
  - action_type='update': restores the entity row from applied_snapshot.
  - action_type='create': there's no "before" row, so undo instead
    soft-deletes (is_deleted=1) the record that apply created -- the same
    convention delete_supplier()/delete_poi() already use elsewhere, so a
    "deleted-by-undo" record disappears from lists exactly like any other
    deleted one, with the row itself still intact if it's ever needed.
  - Only available on the MOST RECENT applied item for a given record --
    if a newer apply has since touched the same Hotel/POI, undo refuses
    rather than risk clobbering that newer change.

Safe to re-run -- skipped if ai_review_items already has an
applied_snapshot column (this migration already ran, or the database was
created after this shipped in schema.sql).

New installs don't need this: schema.sql already has the final shape. Run
this once against a database created before this change:

    flask --app app migrate-add-ai-agent-undo

or directly:

    python migrate_add_ai_agent_undo.py
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
        if not _table_exists(db, "ai_review_items"):
            print("ai_review_items table doesn't exist yet -- run migrate-add-ai-agent-foundations first. Nothing to do.")
            return

        if _has_column(db, "ai_review_items", "applied_snapshot"):
            print("ai_review_items.applied_snapshot already exists -- Undo Apply migration already applied. Nothing to do.")
            return

        db.execute("PRAGMA foreign_keys = OFF")  # table rebuild below

        old_cols = [r["name"] for r in db.execute("PRAGMA table_info(ai_review_items)").fetchall()]
        col_list = ", ".join(old_cols)

        db.execute("""
            CREATE TABLE ai_review_items__new (
                review_id       INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                run_id          INTEGER NOT NULL REFERENCES agent_runs(run_id),
                entity_type     TEXT NOT NULL,
                entity_id       INTEGER,
                entity_label    TEXT NOT NULL,
                action_type     TEXT NOT NULL CHECK (action_type IN ('create','update')),
                status          TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected','needs_changes','applied','undone')),
                reviewer_notes  TEXT,
                reviewed_by     INTEGER REFERENCES users(user_id),
                reviewed_at     TEXT,
                applied_at      TEXT,
                applied_snapshot TEXT,
                undone_at       TEXT,
                undone_by       INTEGER REFERENCES users(user_id),
                created_at      TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        db.execute(f"INSERT INTO ai_review_items__new ({col_list}) SELECT {col_list} FROM ai_review_items")
        db.execute("DROP TABLE ai_review_items")
        db.execute("ALTER TABLE ai_review_items__new RENAME TO ai_review_items")
        db.execute("CREATE INDEX idx_ai_review_items_tenant ON ai_review_items(tenant_id)")
        db.execute("CREATE INDEX idx_ai_review_items_run ON ai_review_items(run_id)")
        db.execute("CREATE INDEX idx_ai_review_items_status ON ai_review_items(tenant_id, status)")

        db.execute("PRAGMA foreign_keys = ON")
        db.commit()
        print(
            "Rebuilt ai_review_items: added applied_snapshot/undone_at/undone_by, and widened the "
            "status CHECK to allow 'undone'. All existing rows preserved unchanged."
        )
    finally:
        db.close()


if __name__ == "__main__":
    migrate()
