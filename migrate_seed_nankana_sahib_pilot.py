"""
Migration: populates the Nankana Sahib pilot-city data for the "Sikh
Pilgrimage Sector" AI Agent / Data Enrichment work (Sept 2026) — the
proof-of-pipeline pilot Zeb approved before scaling to all 55 named
cities + the international hub cities.

For every existing tenant that already has Nankana Sahib's 9 seeded
Gurdwara POI records (from seed_gurdwara_pois — Heritage Tours is
currently the only one), this:

  1. Backfills real map_coordinates (town-center reference; see
     seed_data.NANKANA_SAHIB_TOWN_COORDS for the sourcing note) onto those
     9 Gurdwara POI rows, wherever still blank.
  2. Adds the new non-Gurdwara POIs: Quba Masjid, Nankana Lake Resort,
     Nankana Sahib Railway Station, DHQ Hospital, City Police Station,
     and Allama Iqbal International Airport (Lahore) as the pilot's
     reference airport.
  3. Adds the new pilot Suppliers: Hotel One Nankana Sahib, Rana Resort,
     City Family Restaurant, and two Tour Operators (Sikh Tourism
     Pakistan, Trango Adventure) — each with a Main Office address.
  4. Adds the Knowledge Graph edges linking the above (see
     migrate_add_knowledge_graph_edges.py, which must run first).

Every fact was gathered via live web research, never fabricated — see the
`notes` field on each seed_data.py entry for its source and date, and
seed_data.py's NANKANA_SAHIB_* module comment for the full story
(including which coordinates are an honest town-level approximation
rather than an independently verified site-level value).

Requires migrate-add-ai-agent-lookups (poi_types/supplier_types) and
migrate-add-knowledge-graph-edges (the edges table) to have already run.
Idempotent throughout — safe to re-run.

    flask --app app migrate-seed-nankana-sahib-pilot

or directly:

    python migrate_seed_nankana_sahib_pilot.py
"""
import sqlite3

from config import Config


def migrate():
    db = sqlite3.connect(Config.DATABASE_PATH)
    db.row_factory = sqlite3.Row
    try:
        from seed_data import seed_nankana_sahib_pilot

        tenants = db.execute("SELECT tenant_id, tenant_name FROM tenants").fetchall()
        for t in tenants:
            seed_nankana_sahib_pilot(db, t["tenant_id"])
            print(f"Seeded Nankana Sahib pilot data (POIs, Suppliers, Knowledge Graph edges) for '{t['tenant_name']}'.")
    finally:
        db.close()


if __name__ == "__main__":
    migrate()
