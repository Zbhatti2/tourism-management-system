"""
POI Master Image Catalog -> tenants.

The platform keeps a Master Image Catalog per Point of Interest
(platform_poi_images, curated by the SystemAdmin). A tenant's POI that came
from, or was linked to, a platform POI (tenant_catalog_links, entity 'pois')
inherits those images: each becomes a poi_images row with platform_image_id
set. The image bytes stay in platform_poi_images (one copy for all tenants);
the tenant's row carries its own Title / Description / Date / Sort order, so
a tenant can re-caption, reorder or delete inherited images and add its own.

When a POI is first linked, all its master images are inherited, whatever
the tenant's setting. After that, changes to the master catalog reach each
tenant according to tenants.poi_image_updates (System Management):

* 'auto'    -- new images are added, changed captions are updated (unless the
               tenant edited that image's caption), removed images are removed.
* 'review'  -- (default) each change waits on the tenant's "Platform image
               updates" screen for Accept or Skip.
* 'none'    -- nothing changes after the first inheritance.

A tenant that deleted an inherited image never gets it back automatically
(its row stays, is_deleted = 1).
"""
POLICIES = {"auto": "Update automatically", "review": "Review before updating", "none": "No updates"}
DEFAULT_POLICY = "review"

DDL = """
CREATE TABLE IF NOT EXISTS poi_image_updates (
    update_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    poi_id          INTEGER NOT NULL REFERENCES points_of_interest(poi_id),  -- the tenant's POI
    platform_image_id INTEGER NOT NULL,     -- platform_poi_images.image_id
    change          TEXT NOT NULL CHECK (change IN ('new','changed','removed')),
    version         INTEGER NOT NULL,       -- the master image's version this update brings
    status          TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','accepted','skipped')),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    decided_by      INTEGER REFERENCES users(user_id),
    decided_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_poi_image_updates_tenant ON poi_image_updates(tenant_id, status);
"""

# Columns added to poi_images.
POI_IMAGE_COLUMNS = [("platform_image_id", "INTEGER"), ("platform_version", "INTEGER"),
                     ("local_edited", "INTEGER NOT NULL DEFAULT 0"), ("is_deleted", "INTEGER NOT NULL DEFAULT 0"),
                     ("removed_by_platform", "INTEGER NOT NULL DEFAULT 0"), ("image_date", "TEXT"),
                     ("source", "TEXT"), ("source_url", "TEXT"), ("content_hash", "TEXT"), ("thumb_data", "BLOB")]


def policy(db, tenant_id):
    r = db.execute("SELECT poi_image_updates FROM tenants WHERE tenant_id = ?", (tenant_id,)).fetchone()
    return (r[0] if r and r[0] in POLICIES else DEFAULT_POLICY)


def set_policy(db, tenant_id, value):
    if value not in POLICIES:
        raise ValueError("Unknown setting")
    db.execute("UPDATE tenants SET poi_image_updates = ? WHERE tenant_id = ?", (value, tenant_id))
    db.commit()


def _master(db, platform_poi_id, active_only=True):
    return db.execute("SELECT image_id, title, description, image_date, sort_order, source, contributor, source_url, "
                      "licence, version, is_active FROM platform_poi_images WHERE poi_id = ?"
                      + (" AND is_active = 1" if active_only else "") + " ORDER BY COALESCE(sort_order, 999999), image_id",
                      (platform_poi_id,)).fetchall()


def _local_rows(db, tenant_id, local_poi_id):
    """{platform_image_id: poi_images row} including deleted rows."""
    return {r["platform_image_id"]: r for r in db.execute(
        "SELECT * FROM poi_images WHERE tenant_id = ? AND poi_id = ? AND platform_image_id IS NOT NULL",
        (tenant_id, local_poi_id))}


def _add(db, tenant_id, local_poi_id, m):
    nxt = db.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 FROM poi_images WHERE tenant_id = ? AND poi_id = ? "
                     "AND COALESCE(is_deleted, 0) = 0", (tenant_id, local_poi_id)).fetchone()[0]
    db.execute("""INSERT INTO poi_images (tenant_id, poi_id, location_type, image_name, authors, description,
                      sort_order, image_date, source, source_url, platform_image_id, platform_version)
                  VALUES (?, ?, 'Stored in Database', ?, ?, ?, ?, ?, 'Platform', ?, ?, ?)""",
               (tenant_id, local_poi_id, m["title"], m["licence"] or m["contributor"], m["description"],
                m["sort_order"] or nxt, m["image_date"], m["source_url"], m["image_id"], m["version"]))


def _update(db, row, m):
    if row["local_edited"]:
        db.execute("UPDATE poi_images SET platform_version = ? WHERE poi_image_id = ?", (m["version"], row["poi_image_id"]))
    else:
        db.execute("UPDATE poi_images SET image_name = ?, description = ?, image_date = ?, authors = ?, "
                   "source_url = ?, platform_version = ? WHERE poi_image_id = ?",
                   (m["title"], m["description"], m["image_date"], m["licence"] or m["contributor"], m["source_url"],
                    m["version"], row["poi_image_id"]))


def _remove(db, row, version):
    db.execute("UPDATE poi_images SET is_deleted = 1, removed_by_platform = 1, platform_version = ? "
               "WHERE poi_image_id = ?", (version, row["poi_image_id"]))


def inherit_all(db, tenant_id, local_poi_id, platform_poi_id):
    """A tenant POI was just added from / linked to a platform POI: take every
    master image it doesn't have yet. Returns how many were added."""
    have = _local_rows(db, tenant_id, local_poi_id)
    n = 0
    for m in _master(db, platform_poi_id):
        if m["image_id"] not in have:
            _add(db, tenant_id, local_poi_id, m)
            n += 1
    return n


def changes_for(db, tenant_id, local_poi_id, platform_poi_id):
    """[(change, master row, local row or None)] where the tenant's POI is
    behind the master catalog."""
    have = _local_rows(db, tenant_id, local_poi_id)
    out = []
    for m in _master(db, platform_poi_id, active_only=False):
        row = have.get(m["image_id"])
        if m["is_active"]:
            if row is None:
                out.append(("new", m, None))
            elif not row["is_deleted"] and (row["platform_version"] or 0) < m["version"]:
                out.append(("changed", m, row))
        elif row is not None and not row["is_deleted"]:
            out.append(("removed", m, row))
    return out


def _apply_change(db, tenant_id, local_poi_id, change, m, row):
    if change == "new":
        _add(db, tenant_id, local_poi_id, m)
    elif change == "changed" and row is not None:
        _update(db, row, m)
    elif change == "removed" and row is not None:
        _remove(db, row, m["version"])


def links_for(db, platform_poi_ids=None, tenant_id=None):
    sql = ("SELECT l.tenant_id, l.catalog_id, l.local_id FROM tenant_catalog_links l "
           "JOIN points_of_interest p ON p.poi_id = l.local_id AND p.is_deleted = 0 "
           "WHERE l.entity = 'pois' AND l.how != 'skipped' AND l.local_id IS NOT NULL")
    params = []
    if platform_poi_ids is not None:
        ids = list(platform_poi_ids)
        sql += f" AND l.catalog_id IN ({','.join('?' * len(ids)) or 'NULL'})"
        params += ids
    if tenant_id is not None:
        sql += " AND l.tenant_id = ?"
        params.append(tenant_id)
    return db.execute(sql, params).fetchall()


def push(db, platform_poi_ids):
    """After the master catalog of these platform POIs changed: bring every
    linked tenant POI up to date according to its tenant's setting.
    Returns {tenant_id: {"applied": n, "queued": n}}."""
    out = {}
    policies = {}
    for link in links_for(db, platform_poi_ids):
        tid = link["tenant_id"]
        pol = policies.setdefault(tid, policy(db, tid))
        c = out.setdefault(tid, {"applied": 0, "queued": 0})
        if pol == "none":
            continue
        for change, m, row in changes_for(db, tid, link["local_id"], link["catalog_id"]):
            if pol == "auto":
                _apply_change(db, tid, link["local_id"], change, m, row)
                c["applied"] += 1
            else:
                _queue(db, tid, link["local_id"], m["image_id"], change, m["version"])
                c["queued"] += 1
    db.commit()
    return out


def _queue(db, tenant_id, local_poi_id, image_id, change, version):
    """One pending update per tenant POI and master image: a newer change
    replaces an older pending one."""
    existing = db.execute("SELECT update_id FROM poi_image_updates WHERE tenant_id = ? AND poi_id = ? "
                          "AND platform_image_id = ? AND status = 'pending'", (tenant_id, local_poi_id, image_id)).fetchone()
    if existing:
        db.execute("UPDATE poi_image_updates SET change = ?, version = ?, created_at = datetime('now') WHERE update_id = ?",
                   (change, version, existing[0]))
        return
    skipped = db.execute("SELECT 1 FROM poi_image_updates WHERE tenant_id = ? AND poi_id = ? AND platform_image_id = ? "
                         "AND status = 'skipped' AND version >= ?", (tenant_id, local_poi_id, image_id, version)).fetchone()
    if skipped:  # the tenant already said no to this version
        return
    db.execute("INSERT INTO poi_image_updates (tenant_id, poi_id, platform_image_id, change, version) VALUES (?, ?, ?, ?, ?)",
               (tenant_id, local_poi_id, image_id, change, version))


# ---- the tenant's review screen ---------------------------------------------------------------

def pending_count(db, tenant_id):
    if tenant_id is None:
        return 0
    try:
        return db.execute("SELECT COUNT(*) FROM poi_image_updates WHERE tenant_id = ? AND status = 'pending'",
                          (tenant_id,)).fetchone()[0]
    except Exception:
        return 0


def pending(db, tenant_id, poi_id=None):
    sql = """SELECT u.*, p.name AS poi_name, m.title AS master_title, m.description AS master_description,
                    m.image_date AS master_date, m.licence, m.source_url, i.poi_image_id, i.image_name AS my_title,
                    i.description AS my_description, i.local_edited
             FROM poi_image_updates u
             JOIN points_of_interest p ON p.poi_id = u.poi_id
             JOIN platform_poi_images m ON m.image_id = u.platform_image_id
             LEFT JOIN poi_images i ON i.tenant_id = u.tenant_id AND i.poi_id = u.poi_id
                                    AND i.platform_image_id = u.platform_image_id
             WHERE u.tenant_id = ? AND u.status = 'pending'"""
    params = [tenant_id]
    if poi_id:
        sql += " AND u.poi_id = ?"
        params.append(poi_id)
    return db.execute(sql + " ORDER BY p.name COLLATE NOCASE, u.change, u.update_id", params).fetchall()


def decide(db, tenant_id, update_ids, accept, user_id=None):
    """Accept (apply) or skip pending updates. Returns how many."""
    n = 0
    for uid in update_ids:
        u = db.execute("SELECT * FROM poi_image_updates WHERE update_id = ? AND tenant_id = ? AND status = 'pending'",
                       (uid, tenant_id)).fetchone()
        if u is None:
            continue
        if accept:
            m = db.execute("SELECT * FROM platform_poi_images WHERE image_id = ?", (u["platform_image_id"],)).fetchone()
            row = _local_rows(db, tenant_id, u["poi_id"]).get(u["platform_image_id"])
            change = u["change"]
            # Re-check against the master as it is now.
            if m is not None:
                if not m["is_active"]:
                    change = "removed"
                if change == "new" and row is not None:
                    change = None if row["is_deleted"] else "changed"
                if change:
                    _apply_change(db, tenant_id, u["poi_id"], change, m, row)
        db.execute("UPDATE poi_image_updates SET status = ?, decided_by = ?, decided_at = datetime('now') WHERE update_id = ?",
                   ("accepted" if accept else "skipped", user_id, uid))
        n += 1
    db.commit()
    return n
