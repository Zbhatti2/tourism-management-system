"""
Knowledge Graph edges -- structured replacement for the freeform
';'-delimited knowledge_graph_data text column that already sits on
contacts/points_of_interest/suppliers/organizations (each explicitly
commented in schema.sql "Phase 2: structured triples"). This module is
that Phase 2, scoped to what the "Sikh Pilgrimage Sector" AI Agent / Data
Enrichment pilot (Sept 2026) actually needs: a small, generic directed-edge
table (knowledge_graph_edges) plus the two helpers below.

The existing freeform text fields are left in place (still shown in the
UI) -- this is additive, not a replacement migration. See
migrate_add_knowledge_graph_edges.py for the table's creation on an
existing live database.
"""

ENTITY_TYPES = ("Supplier", "PointOfInterest", "City", "Organization", "Contact")

# entity_type -> (table, id_column, label expression)
_ENTITY_LABEL_QUERIES = {
    "Supplier": ("SELECT supplier_name AS label FROM suppliers WHERE supplier_id = ?", None),
    "PointOfInterest": ("SELECT name AS label FROM points_of_interest WHERE poi_id = ?", None),
    "City": ("SELECT label FROM cities WHERE city_id = ?", None),
    "Organization": ("SELECT organization_name AS label FROM organizations WHERE organization_id = ?", None),
    "Contact": ("SELECT full_name AS label FROM contacts WHERE contact_id = ?", None),
}

# entity_type -> (blueprint.endpoint, url_for kwarg name) for a clickable
# link next to the label -- None where no view route applies (e.g. City,
# which has no dedicated detail page in this app).
_ENTITY_VIEW_ROUTES = {
    "Supplier": ("suppliers.view_supplier", "supplier_id"),
    "PointOfInterest": ("poi.view_poi", "poi_id"),
    "Organization": ("organizations.view_organization", "org_id"),
    "Contact": ("contacts.view_contact", "contact_id"),
    "City": (None, None),
}


def add_edge(db, tenant_id, subject_type, subject_id, relationship, object_type, object_id, notes=None):
    """Idempotent insert -- the UNIQUE constraint on
    (tenant_id, subject_type, subject_id, relationship, object_type,
    object_id) makes INSERT OR IGNORE safe to call repeatedly (e.g. from a
    re-run seed/migration script)."""
    if subject_type not in ENTITY_TYPES or object_type not in ENTITY_TYPES:
        raise ValueError(f"Unknown entity type: {subject_type!r} / {object_type!r}")
    db.execute(
        "INSERT OR IGNORE INTO knowledge_graph_edges "
        "(tenant_id, subject_type, subject_id, relationship, object_type, object_id, notes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (tenant_id, subject_type, subject_id, relationship, object_type, object_id, notes),
    )


def _label_for(db, entity_type, entity_id):
    query, _ = _ENTITY_LABEL_QUERIES.get(entity_type, (None, None))
    if not query:
        return None
    row = db.execute(query, (entity_id,)).fetchone()
    return row["label"] if row else None


def get_edges_for(db, tenant_id, entity_type, entity_id):
    """Returns every edge touching one entity, in both directions, as a
    list of dicts: {relationship, direction ('out'|'in'), other_type,
    other_id, other_label, other_endpoint, other_url_kwarg, notes}.
    `other_label` is None if the linked row no longer exists (e.g. it was
    since deleted) -- callers should skip those rather than error."""
    results = []
    outgoing = db.execute(
        "SELECT edge_id, relationship, object_type AS other_type, object_id AS other_id, notes "
        "FROM knowledge_graph_edges WHERE tenant_id = ? AND subject_type = ? AND subject_id = ? "
        "ORDER BY relationship",
        (tenant_id, entity_type, entity_id),
    ).fetchall()
    incoming = db.execute(
        "SELECT edge_id, relationship, subject_type AS other_type, subject_id AS other_id, notes "
        "FROM knowledge_graph_edges WHERE tenant_id = ? AND object_type = ? AND object_id = ? "
        "ORDER BY relationship",
        (tenant_id, entity_type, entity_id),
    ).fetchall()
    for row, direction in [(r, "out") for r in outgoing] + [(r, "in") for r in incoming]:
        label = _label_for(db, row["other_type"], row["other_id"])
        if not label:
            continue
        endpoint, url_kwarg = _ENTITY_VIEW_ROUTES.get(row["other_type"], (None, None))
        results.append({
            "edge_id": row["edge_id"],
            "relationship": row["relationship"],
            "direction": direction,
            "other_type": row["other_type"],
            "other_id": row["other_id"],
            "other_label": label,
            "other_endpoint": endpoint,
            "other_url_kwarg": url_kwarg,
            "notes": row["notes"],
        })
    return results
