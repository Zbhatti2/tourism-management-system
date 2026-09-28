"""
Migration: Agent Run Sources (Sept 2026).

Adds agent_run_sources -- optional URLs/documents entered on the New Agent
Run form, per Zeb's request: "The user should also be able to enter
specific urls and sources." Purely additive: no existing table is touched,
safe to run against a live database, and safe to re-run.
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
        if not _table_exists(db, "agent_run_sources"):
            db.execute("""
                CREATE TABLE agent_run_sources (
                    source_id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                    run_id          INTEGER NOT NULL REFERENCES agent_runs(run_id),
                    url             TEXT NOT NULL,
                    note            TEXT,
                    sort_order      INTEGER DEFAULT 0,
                    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """)
            db.execute("CREATE INDEX idx_agent_run_sources_tenant ON agent_run_sources(tenant_id)")
            db.execute("CREATE INDEX idx_agent_run_sources_run ON agent_run_sources(run_id)")
            db.commit()
            print("Created table: agent_run_sources.")
        else:
            print("agent_run_sources already exists -- nothing to do.")
    finally:
        db.close()


if __name__ == "__main__":
    migrate()
