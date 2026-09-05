"""
Migration: creates the new knowledge_graph_edges table — the structured
replacement for the freeform ';'-delimited knowledge_graph_data text
column already sitting on contacts/points_of_interest/suppliers/
organizations (each explicitly commented in schema.sql "Phase 2:
structured triples"). Built for the "Sikh Pilgrimage Sector" AI Agent /
Data Enrichment pilot (Sept 2026), which asked for "a Comprehensive
Knowledge Graph" linking all relevant entities.

One row = one directed edge between two entities, e.g.
    (PointOfInterest, <Gurdwara Janam Asthan>)
        --[near]--> (Supplier, <Hotel One Nankana Sahib>)

See knowledge_graph.py for the add_edge()/get_edges_for() helpers that
read and write this table.

This is additive — the existing freeform text fields are left in place
(still shown in the UI), not replaced. Safe to re-run: CREATE TABLE IF NOT
EXISTS.

New installs don't need this: schema.sql already has this table. Run this
once against a database created before this change:

    flask --app app migrate-add-knowledge-graph-edges

or directly:

    python migrate_add_knowledge_graph_edges.py
"""
import sqlite3

from config import Config


def migrate():
    db = sqlite3.connect(Config.DATABASE_PATH)
    db.row_factory = sqlite3.Row
    try:
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'knowledge_graph_edges'"
        ).fetchone()
        if exists:
            print("knowledge_graph_edges table already exists — nothing to do.")
            return

        db.execute("""
            CREATE TABLE knowledge_graph_edges (
                edge_id         INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                subject_type    TEXT NOT NULL CHECK (subject_type IN ('Supplier','PointOfInterest','City','Organization','Contact')),
                subject_id      INTEGER NOT NULL,
                relationship    TEXT NOT NULL,
                object_type     TEXT NOT NULL CHECK (object_type IN ('Supplier','PointOfInterest','City','Organization','Contact')),
                object_id       INTEGER NOT NULL,
                notes           TEXT,
                created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE (tenant_id, subject_type, subject_id, relationship, object_type, object_id)
            )
        """)
        db.execute("CREATE INDEX idx_kg_edges_tenant ON knowledge_graph_edges(tenant_id)")
        db.execute("CREATE INDEX idx_kg_edges_subject ON knowledge_graph_edges(tenant_id, subject_type, subject_id)")
        db.execute("CREATE INDEX idx_kg_edges_object ON knowledge_graph_edges(tenant_id, object_type, object_id)")
        db.commit()
        print("Created knowledge_graph_edges table + indexes.")
    finally:
        db.close()


if __name__ == "__main__":
    migrate()
