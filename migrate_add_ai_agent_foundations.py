"""
Migration: AI Agents -- Agent Runs & Human Review Queue (Sept 2026).

Adds three brand-new tables -- agent_runs, ai_review_items, ai_review_fields
-- the foundations for Zeb's "First Agents" plan (see schema.sql's MODULE Z
comment for the full design note). Purely additive: no existing table is
touched, so this is safe to run against a live database with real data on
file, and safe to re-run (idempotent -- every CREATE is guarded).
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
        created = []

        if not _table_exists(db, "agent_runs"):
            db.execute("""
                CREATE TABLE agent_runs (
                    run_id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                    agent_type      TEXT NOT NULL,
                    task_type       TEXT NOT NULL CHECK (task_type IN ('discover_new','update_existing','refresh_volatile')),
                    scope_label     TEXT NOT NULL,
                    scope_params    TEXT,
                    status          TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','completed','failed')),
                    summary         TEXT,
                    started_at      TEXT,
                    completed_at    TEXT,
                    created_by      INTEGER REFERENCES users(user_id),
                    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """)
            db.execute("CREATE INDEX idx_agent_runs_tenant ON agent_runs(tenant_id)")
            db.execute("CREATE INDEX idx_agent_runs_status ON agent_runs(tenant_id, status)")
            created.append("agent_runs")

        if not _table_exists(db, "ai_review_items"):
            db.execute("""
                CREATE TABLE ai_review_items (
                    review_id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                    run_id          INTEGER NOT NULL REFERENCES agent_runs(run_id),
                    entity_type     TEXT NOT NULL,
                    entity_id       INTEGER,
                    entity_label    TEXT NOT NULL,
                    action_type     TEXT NOT NULL CHECK (action_type IN ('create','update')),
                    status          TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected','needs_changes','applied')),
                    reviewer_notes  TEXT,
                    reviewed_by     INTEGER REFERENCES users(user_id),
                    reviewed_at     TEXT,
                    applied_at      TEXT,
                    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """)
            db.execute("CREATE INDEX idx_ai_review_items_tenant ON ai_review_items(tenant_id)")
            db.execute("CREATE INDEX idx_ai_review_items_run ON ai_review_items(run_id)")
            db.execute("CREATE INDEX idx_ai_review_items_status ON ai_review_items(tenant_id, status)")
            created.append("ai_review_items")

        if not _table_exists(db, "ai_review_fields"):
            db.execute("""
                CREATE TABLE ai_review_fields (
                    field_id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                    review_id       INTEGER NOT NULL REFERENCES ai_review_items(review_id),
                    field_name      TEXT NOT NULL,
                    field_label     TEXT NOT NULL,
                    current_value   TEXT,
                    proposed_value  TEXT,
                    field_status    TEXT NOT NULL DEFAULT 'pending' CHECK (field_status IN ('pending','approved','rejected')),
                    confidence      REAL,
                    source_citation TEXT,
                    sort_order      INTEGER NOT NULL DEFAULT 0
                )
            """)
            db.execute("CREATE INDEX idx_ai_review_fields_tenant ON ai_review_fields(tenant_id)")
            db.execute("CREATE INDEX idx_ai_review_fields_review ON ai_review_fields(review_id)")
            created.append("ai_review_fields")

        db.commit()
        if created:
            print(f"Created tables: {', '.join(created)}.")
        else:
            print("agent_runs / ai_review_items / ai_review_fields already exist -- nothing to do.")
    finally:
        db.close()


if __name__ == "__main__":
    migrate()
