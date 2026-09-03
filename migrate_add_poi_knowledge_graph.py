"""
Migration: adds points_of_interest.knowledge_graph_data to a database
created before this column existed.

Per the request:

    "Please add a 'Knowledge graph data' freeform field in the Points of
    Interest Table (just like in the Contacts table). Update the form
    Accordingly."

Same convention as contacts.knowledge_graph_data (and
suppliers.knowledge_graph_data, added earlier for the same reason): a
freeform, ';'-delimited list of short facts about the POI (e.g. "Sister
site of Wazir Khan Mosque; Managed by the Auqaf Department"), rendered as
a bulleted list on the view page — Phase 2 may turn this into structured
triples, same as the other two.

New installs don't need this: schema.sql already includes the column. Run
this once against a database created before this change:

    flask --app app migrate-add-poi-knowledge-graph

or directly:

    python migrate_add_poi_knowledge_graph.py

Safe to re-run — the column is added only if not already present. Does
not touch any existing data.
"""
import sqlite3

from config import Config


def migrate():
    db = sqlite3.connect(Config.DATABASE_PATH)
    try:
        columns = [row[1] for row in db.execute("PRAGMA table_info(points_of_interest)").fetchall()]
        if "knowledge_graph_data" not in columns:
            db.execute("ALTER TABLE points_of_interest ADD COLUMN knowledge_graph_data TEXT")
            db.commit()
            print("Added: points_of_interest.knowledge_graph_data.")
        else:
            print("points_of_interest.knowledge_graph_data already exists — nothing to do.")
    finally:
        db.close()


if __name__ == "__main__":
    migrate()
