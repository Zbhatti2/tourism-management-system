"""
Platform Lookups -- the shared vocabulary the platform catalog depends on.

Some lookup codes have to mean the same thing in every tenant, because
platform data will later be copied into tenant tables by matching on them:
a platform Accommodation record lands in the tenant's Supplier Type "Hotel"
(or "Resort") with Sub-Type "Five Star"/"4-Star", a platform Restaurant in
Supplier Type "Restaurant", and a platform Point of Interest in the matching
POI Type. If a tenant could rename, deactivate or delete those rows, the
copy would have nowhere to land. So they are LOCKED (is_system = 1) in
every tenant and maintained only by the SystemAdmin, from the Platform
Lookups screen (blueprints/platform_lookups.py).

Where the master list lives
---------------------------
As ordinary rows in supplier_types / supplier_subtypes / poi_types owned by
the reserved TMS Platform tenant (tenants.is_platform = 1), each flagged
is_system = 1. Nobody logs into that tenant, so its rows are only ever
seen here.

How tenants get them -- sync_platform_lookups()
-----------------------------------------------
For every platform row, each real tenant gets a matching row, found by
`code` first and then by label (case-insensitive, and under the same parent
for sub-types). A match is updated in place -- label, parent, template_key
and code follow the platform row, it is re-activated and flagged
is_system = 1 -- so the tenant's existing suppliers/POIs that already point
at it keep their links and nothing is renamed out from under them except
the locked text itself. No match means a new row is inserted. A tenant row
still flagged is_system = 1 whose code is no longer on the platform list is
unlocked again (is_system = 0) and becomes an ordinary tenant entry --
never deleted.

Called by: the 2026_10_platform_lookups migration (db.py), every Platform
Lookups add/edit/remove, and seed_lookup_tables() (seed_data.py), so a
newly provisioned tenant starts with the locked rows already in place.
"""
from seed_data import _slug

# Lookup tables that can hold platform-locked rows. "parent" (sub-types
# only) names the parent lookup and the FK column pointing at it; the
# parent link is carried across tenants by the parent's CODE, since ids
# differ per tenant.
LOCKABLE_TABLES = {
    "supplier_types": {
        "label": "Supplier Types",
        "table": "supplier_types",
        "pk": "supplier_type_id",
        "has_template_key": True,
    },
    "supplier_subtypes": {
        "label": "Supplier Sub-Types",
        "table": "supplier_subtypes",
        "pk": "supplier_subtype_id",
        "parent": {"key": "supplier_types", "table": "supplier_types", "pk": "supplier_type_id",
                   "fk": "supplier_type_id", "nav_label": "Supplier Type"},
    },
    "poi_types": {
        "label": "POI Types",
        "table": "poi_types",
        "pk": "poi_type_id",
    },
}

# Starter platform list, seeded once by the migration. After that the
# SystemAdmin maintains it from the Platform Lookups screen; this list is
# not re-applied.
#
# Supplier Types: (code, label, template_key). Hotel keeps its 'hotel'
# template (Amenities & Facilities / Rooms sections on the supplier page).
STARTER_SUPPLIER_TYPES = [
    ("HOTEL", "Hotel", "hotel"),
    ("RESORT", "Resort", None),
    ("RESTAURANT", "Restaurant", None),
]
# Supplier Sub-Types: (parent_code, code, label) -- the star ratings a
# platform Accommodation record maps onto.
STARTER_SUPPLIER_SUBTYPES = [
    ("HOTEL", "HOTEL_FIVE_STAR", "Five Star"),
    ("HOTEL", "HOTEL_4_STAR", "4-Star"),
    ("RESORT", "RESORT_FIVE_STAR", "Five Star"),
    ("RESORT", "RESORT_4_STAR", "4-Star"),
]
# POI Types: the sightseeing types the platform POI catalog will use, plus
# the new Group B categories (bridges, dams, lakes, malls, stadiums,
# arenas). Deliberately NOT locked: Airport and Railway Station (moving to
# Transport Hubs), Hospital, Police Station, Fish Farm, Agri-Farm and Other
# -- those stay ordinary, tenant-editable POI Types.
STARTER_POI_TYPES = [
    "Sikh Gurdwara", "Mosque", "Mandir", "Museum", "Zoo", "Botonical Garden",
    "Historic Castle", "Historical Architecture", "Historical Fort",
    "Amusement Park", "Ski Site", "Historic Library",
    "Bridge", "Dam", "Lake", "Shopping Mall", "Stadium", "Sports Arena",
]


def _column_exists(db, table, column):
    return any(r[1] == column for r in db.execute(f'PRAGMA table_info("{table}")').fetchall())


def platform_tenant_id(db):
    row = db.execute("SELECT tenant_id FROM tenants WHERE is_platform = 1 LIMIT 1").fetchone()
    return row[0] if row else None


def is_ready(db):
    """True once the is_system columns and the platform tenant exist."""
    return (_column_exists(db, "supplier_types", "is_system")
            and _column_exists(db, "poi_types", "is_system")
            and platform_tenant_id(db) is not None)


def add_is_system_columns(db):
    for table in ("supplier_types", "supplier_subtypes", "poi_types"):
        if not _column_exists(db, table, "is_system"):
            db.execute(f"ALTER TABLE {table} ADD COLUMN is_system INTEGER NOT NULL DEFAULT 0")
    db.commit()


def seed_starter_platform_lookups(db):
    """Writes the STARTER_* lists as the platform tenant's locked rows.
    Idempotent: skips a code the platform already has."""
    pid = platform_tenant_id(db)
    if pid is None:
        return
    for i, (code, label, template_key) in enumerate(STARTER_SUPPLIER_TYPES):
        db.execute(
            "INSERT OR IGNORE INTO supplier_types (tenant_id, code, label, sort_order, is_active, template_key, is_system) "
            "VALUES (?, ?, ?, ?, 1, ?, 1)",
            (pid, code, label, i, template_key),
        )
    for i, (parent_code, code, label) in enumerate(STARTER_SUPPLIER_SUBTYPES):
        parent = db.execute(
            "SELECT supplier_type_id FROM supplier_types WHERE tenant_id = ? AND code = ?", (pid, parent_code)
        ).fetchone()
        if parent:
            db.execute(
                "INSERT OR IGNORE INTO supplier_subtypes (tenant_id, supplier_type_id, code, label, sort_order, is_active, is_system) "
                "VALUES (?, ?, ?, ?, ?, 1, 1)",
                (pid, parent[0], code, label, i),
            )
    for i, label in enumerate(STARTER_POI_TYPES):
        db.execute(
            "INSERT OR IGNORE INTO poi_types (tenant_id, code, label, sort_order, is_active, is_system) "
            "VALUES (?, ?, ?, ?, 1, 1)",
            (pid, _slug(label), label, i),
        )
    db.commit()


def platform_rows(db, key):
    """The platform tenant's locked rows for one lookup table, with the
    parent's code/label attached for sub-types."""
    cfg = LOCKABLE_TABLES[key]
    pid = platform_tenant_id(db)
    p = cfg.get("parent")
    if p:
        sql = (f"SELECT t.*, par.code AS parent_code, par.label AS parent_label FROM {cfg['table']} t "
               f"LEFT JOIN {p['table']} par ON par.{p['pk']} = t.{p['fk']} "
               f"WHERE t.tenant_id = ? AND t.is_system = 1 ORDER BY par.sort_order, par.label, t.sort_order, t.label")
    else:
        sql = f"SELECT t.* FROM {cfg['table']} t WHERE t.tenant_id = ? AND t.is_system = 1 ORDER BY t.sort_order, t.label"
    return db.execute(sql, (pid,)).fetchall()


def _real_tenant_ids(db, tenant_id=None):
    if tenant_id is not None:
        return [tenant_id]
    return [r[0] for r in db.execute("SELECT tenant_id FROM tenants WHERE is_platform = 0").fetchall()]


def _sync_table(db, key, tid):
    cfg = LOCKABLE_TABLES[key]
    table, pk, p = cfg["table"], cfg["pk"], cfg.get("parent")
    platform = platform_rows(db, key)
    platform_codes = {r["code"] for r in platform}
    changed = 0

    for prow in platform:
        parent_id = None
        if p:
            parent = db.execute(
                f"SELECT {p['pk']} FROM {p['table']} WHERE tenant_id = ? AND code = ?", (tid, prow["parent_code"])
            ).fetchone()
            if parent is None:
                continue  # parent type not synced (shouldn't happen: types sync first)
            parent_id = parent[0]

        match = db.execute(f"SELECT * FROM {table} WHERE tenant_id = ? AND code = ?", (tid, prow["code"])).fetchone()
        if match is None:
            if p:
                match = db.execute(
                    f"SELECT * FROM {table} WHERE tenant_id = ? AND lower(label) = lower(?) AND {p['fk']} = ?",
                    (tid, prow["label"], parent_id),
                ).fetchone()
            else:
                match = db.execute(
                    f"SELECT * FROM {table} WHERE tenant_id = ? AND lower(label) = lower(?)", (tid, prow["label"])
                ).fetchone()

        if match is None:
            cols = {"tenant_id": tid, "code": prow["code"], "label": prow["label"],
                    "description": prow["description"], "sort_order": prow["sort_order"],
                    "is_active": 1, "is_system": 1}
            if p:
                cols[p["fk"]] = parent_id
            if cfg.get("has_template_key"):
                cols["template_key"] = prow["template_key"]
            db.execute(
                f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                tuple(cols.values()),
            )
            changed += 1
            continue

        sets = {"label": prow["label"], "is_active": 1, "is_system": 1}
        if prow["description"]:
            sets["description"] = prow["description"]
        if p:
            sets[p["fk"]] = parent_id
        if cfg.get("has_template_key") and prow["template_key"]:
            sets["template_key"] = prow["template_key"]
        if match["code"] != prow["code"]:
            # Matched by label under a different code: adopt the platform
            # code, unless some other row of this tenant already holds it.
            taken = db.execute(
                f"SELECT 1 FROM {table} WHERE tenant_id = ? AND code = ? AND {pk} != ?",
                (tid, prow["code"], match[pk]),
            ).fetchone()
            if not taken:
                sets["code"] = prow["code"]
        if any(match[col] != val for col, val in sets.items()):
            db.execute(
                f"UPDATE {table} SET {', '.join(f'{c} = ?' for c in sets)} WHERE {pk} = ?",
                tuple(sets.values()) + (match[pk],),
            )
            changed += 1

    # Unlock rows the platform no longer lists.
    for row in db.execute(f"SELECT {pk}, code FROM {table} WHERE tenant_id = ? AND is_system = 1", (tid,)).fetchall():
        if row["code"] not in platform_codes:
            db.execute(f"UPDATE {table} SET is_system = 0 WHERE {pk} = ?", (row[pk],))
            changed += 1
    return changed


def sync_platform_lookups(db, tenant_id=None):
    """Brings one tenant (or every real tenant, when tenant_id is None) in
    line with the platform's locked rows. Returns (tenants_touched,
    rows_changed). A no-op until the migration has run."""
    if not is_ready(db):
        return 0, 0
    tenants = _real_tenant_ids(db, tenant_id)
    total = 0
    for tid in tenants:
        # Parents before children, so sub-types can find their type.
        for key in ("supplier_types", "supplier_subtypes", "poi_types"):
            total += _sync_table(db, key, tid)
    db.commit()
    return len(tenants), total


def tenant_count_locked(db, key, code):
    """How many real tenants currently hold this code as a locked row."""
    table = LOCKABLE_TABLES[key]["table"]
    return db.execute(
        f"SELECT COUNT(*) FROM {table} t JOIN tenants tn ON tn.tenant_id = t.tenant_id "
        f"WHERE tn.is_platform = 0 AND t.code = ? AND t.is_system = 1",
        (code,),
    ).fetchone()[0]
