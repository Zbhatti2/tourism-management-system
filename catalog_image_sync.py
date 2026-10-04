"""
Master Image Catalog of platform Accommodation and Restaurants -> tenants'
Suppliers (Zeb, Oct 2026: "add an Image to each entity ... just like the
Points of Interest feature. Also add the Sync feature for Tenants. Same
concept as POIs. The Tenants can add additional photos or delete the ones
they don't like").

Same rules as poi_image_sync.py, for Suppliers:

* A tenant's Supplier that came from, or is linked to, a platform
  Accommodation / Restaurant (tenant_catalog_links, entity 'accommodation'
  or 'restaurants') inherits its master images when it is linked: each
  becomes an image in the supplier's Images Catalog (a supplier_documents
  row with platform_image_id set, with its own copy of the image).
* After that, master changes reach the tenant by its "Platform image
  updates" setting (tenants.poi_image_updates, shared with POIs):
  automatically, after review (the default), or not at all.
* The tenant can re-caption, reorder or delete inherited images and add its
  own; a deleted inherited image is never offered again automatically, and
  a caption the tenant edited is kept when the platform changes it.
"""
import poi_image_sync

ENTITIES = ("accommodation", "restaurants")

DDL = """
CREATE TABLE IF NOT EXISTS supplier_image_updates (
    update_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    supplier_id     INTEGER NOT NULL REFERENCES suppliers(supplier_id),
    platform_image_id INTEGER NOT NULL,     -- platform_catalog_images.image_id
    change          TEXT NOT NULL CHECK (change IN ('new','changed','removed')),
    version         INTEGER NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','accepted','skipped')),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    decided_by      INTEGER REFERENCES users(user_id),
    decided_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_supplier_image_updates_tenant ON supplier_image_updates(tenant_id, status);
"""

# Columns added to supplier_documents.
DOC_COLUMNS = [("platform_image_id", "INTEGER"), ("platform_version", "INTEGER"),
               ("local_edited", "INTEGER NOT NULL DEFAULT 0"), ("removed_by_platform", "INTEGER NOT NULL DEFAULT 0")]


def policy(db, tenant_id):
    return poi_image_sync.policy(db, tenant_id)


def _master(db, entity, record_id, active_only=True):
    return db.execute("SELECT * FROM platform_catalog_images WHERE entity = ? AND record_id = ?"
                      + (" AND is_active = 1" if active_only else "") + " ORDER BY COALESCE(sort_order, 999999), image_id",
                      (entity, record_id)).fetchall()


def _local_rows(db, tenant_id, supplier_id):
    """{platform_image_id: supplier_documents row} including deleted rows."""
    return {r["platform_image_id"]: r for r in db.execute(
        "SELECT * FROM supplier_documents WHERE tenant_id = ? AND supplier_id = ? AND platform_image_id IS NOT NULL",
        (tenant_id, supplier_id))}


def _add(db, tenant_id, supplier_id, m):
    from image_catalog import image_type_id
    nxt = db.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 FROM supplier_documents WHERE tenant_id = ? AND supplier_id = ? "
                     "AND is_deleted = 0", (tenant_id, supplier_id)).fetchone()[0]
    cur = db.execute(
        """INSERT INTO supplier_documents (tenant_id, supplier_id, document_name, document_type_id, authors, description,
               image_date, sort_order, source, source_url, content_hash, thumb_data, platform_image_id, platform_version)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'Platform', ?, ?, ?, ?, ?)""",
        (tenant_id, supplier_id, m["title"], image_type_id(db, tenant_id), m["licence"] or m["contributor"],
         m["description"], m["image_date"], m["sort_order"] or nxt, m["source_url"], m["content_hash"], m["thumb_data"],
         m["image_id"], m["version"]))
    db.execute("""INSERT INTO supplier_document_locations (tenant_id, supplier_document_id, location_type, path_or_url,
                      file_data, file_name, mime_type, file_size)
                  VALUES (?, ?, 'Stored in Database', ?, ?, ?, ?, ?)""",
               (tenant_id, cur.lastrowid, m["file_name"] or f"image-{m['image_id']}", m["file_data"],
                m["file_name"] or f"image-{m['image_id']}.jpg", m["mime_type"], m["file_size"]))


def _update(db, row, m):
    if row["local_edited"]:
        db.execute("UPDATE supplier_documents SET platform_version = ? WHERE supplier_document_id = ?",
                   (m["version"], row["supplier_document_id"]))
    else:
        db.execute("UPDATE supplier_documents SET document_name = ?, description = ?, image_date = ?, authors = ?, "
                   "source_url = ?, platform_version = ?, updated_at = datetime('now') WHERE supplier_document_id = ?",
                   (m["title"], m["description"], m["image_date"], m["licence"] or m["contributor"], m["source_url"],
                    m["version"], row["supplier_document_id"]))


def _remove(db, row, version):
    db.execute("UPDATE supplier_documents SET is_deleted = 1, removed_by_platform = 1, platform_version = ?, "
               "updated_at = datetime('now') WHERE supplier_document_id = ?", (version, row["supplier_document_id"]))


def inherit_all(db, tenant_id, supplier_id, entity, record_id):
    """A Supplier was just added from / linked to a platform record: take
    every master image it doesn't have yet. Returns how many were added."""
    have = _local_rows(db, tenant_id, supplier_id)
    n = 0
    for m in _master(db, entity, record_id):
        if m["image_id"] not in have:
            _add(db, tenant_id, supplier_id, m)
            n += 1
    return n


def changes_for(db, tenant_id, supplier_id, entity, record_id):
    have = _local_rows(db, tenant_id, supplier_id)
    out = []
    for m in _master(db, entity, record_id, active_only=False):
        row = have.get(m["image_id"])
        if m["is_active"]:
            if row is None:
                out.append(("new", m, None))
            elif not row["is_deleted"] and (row["platform_version"] or 0) < m["version"]:
                out.append(("changed", m, row))
        elif row is not None and not row["is_deleted"]:
            out.append(("removed", m, row))
    return out


def _apply_change(db, tenant_id, supplier_id, change, m, row):
    if change == "new":
        _add(db, tenant_id, supplier_id, m)
    elif change == "changed" and row is not None:
        _update(db, row, m)
    elif change == "removed" and row is not None:
        _remove(db, row, m["version"])


def links_for(db, entity, record_ids=None, tenant_id=None):
    sql = ("SELECT l.tenant_id, l.catalog_id, l.local_id, l.entity FROM tenant_catalog_links l "
           "JOIN suppliers s ON s.supplier_id = l.local_id AND s.is_deleted = 0 "
           "WHERE l.entity = ? AND l.how != 'skipped' AND l.local_id IS NOT NULL")
    params = [entity]
    if record_ids is not None:
        ids = list(record_ids)
        sql += f" AND l.catalog_id IN ({','.join('?' * len(ids)) or 'NULL'})"
        params += ids
    if tenant_id is not None:
        sql += " AND l.tenant_id = ?"
        params.append(tenant_id)
    return db.execute(sql, params).fetchall()


def push(db, entity, record_ids):
    """After the master catalog of these platform records changed: bring each
    linked Supplier up to date by its tenant's setting."""
    out, policies = {}, {}
    for link in links_for(db, entity, record_ids):
        tid = link["tenant_id"]
        pol = policies.setdefault(tid, policy(db, tid))
        c = out.setdefault(tid, {"applied": 0, "queued": 0})
        if pol == "none":
            continue
        for change, m, row in changes_for(db, tid, link["local_id"], entity, link["catalog_id"]):
            if pol == "auto":
                _apply_change(db, tid, link["local_id"], change, m, row)
                c["applied"] += 1
            else:
                _queue(db, tid, link["local_id"], m["image_id"], change, m["version"])
                c["queued"] += 1
    db.commit()
    return out


def _queue(db, tenant_id, supplier_id, image_id, change, version):
    existing = db.execute("SELECT update_id FROM supplier_image_updates WHERE tenant_id = ? AND supplier_id = ? "
                          "AND platform_image_id = ? AND status = 'pending'", (tenant_id, supplier_id, image_id)).fetchone()
    if existing:
        db.execute("UPDATE supplier_image_updates SET change = ?, version = ?, created_at = datetime('now') "
                   "WHERE update_id = ?", (change, version, existing[0]))
        return
    if db.execute("SELECT 1 FROM supplier_image_updates WHERE tenant_id = ? AND supplier_id = ? AND platform_image_id = ? "
                  "AND status = 'skipped' AND version >= ?", (tenant_id, supplier_id, image_id, version)).fetchone():
        return
    db.execute("INSERT INTO supplier_image_updates (tenant_id, supplier_id, platform_image_id, change, version) "
               "VALUES (?, ?, ?, ?, ?)", (tenant_id, supplier_id, image_id, change, version))


# ---- the tenant's review screen ---------------------------------------------------------------

def pending_count(db, tenant_id):
    if tenant_id is None:
        return 0
    try:
        return db.execute("SELECT COUNT(*) FROM supplier_image_updates WHERE tenant_id = ? AND status = 'pending'",
                          (tenant_id,)).fetchone()[0]
    except Exception:
        return 0


def pending(db, tenant_id, supplier_id=None):
    sql = """SELECT u.*, s.supplier_name AS owner_name, m.title AS master_title, m.description AS master_description,
                    m.image_date AS master_date, m.licence, m.source_url, d.supplier_document_id AS my_id,
                    d.document_name AS my_title, d.local_edited
             FROM supplier_image_updates u
             JOIN suppliers s ON s.supplier_id = u.supplier_id
             JOIN platform_catalog_images m ON m.image_id = u.platform_image_id
             LEFT JOIN supplier_documents d ON d.tenant_id = u.tenant_id AND d.supplier_id = u.supplier_id
                                           AND d.platform_image_id = u.platform_image_id
             WHERE u.tenant_id = ? AND u.status = 'pending'"""
    params = [tenant_id]
    if supplier_id:
        sql += " AND u.supplier_id = ?"
        params.append(supplier_id)
    return db.execute(sql + " ORDER BY s.supplier_name COLLATE NOCASE, u.change, u.update_id", params).fetchall()


def decide(db, tenant_id, update_ids, accept, user_id=None):
    n = 0
    for uid in update_ids:
        u = db.execute("SELECT * FROM supplier_image_updates WHERE update_id = ? AND tenant_id = ? AND status = 'pending'",
                       (uid, tenant_id)).fetchone()
        if u is None:
            continue
        if accept:
            m = db.execute("SELECT * FROM platform_catalog_images WHERE image_id = ?", (u["platform_image_id"],)).fetchone()
            row = _local_rows(db, tenant_id, u["supplier_id"]).get(u["platform_image_id"])
            change = u["change"]
            if m is not None:
                if not m["is_active"]:
                    change = "removed"
                if change == "new" and row is not None:
                    change = None if row["is_deleted"] else "changed"
                if change:
                    _apply_change(db, tenant_id, u["supplier_id"], change, m, row)
        db.execute("UPDATE supplier_image_updates SET status = ?, decided_by = ?, decided_at = datetime('now') "
                   "WHERE update_id = ?", ("accepted" if accept else "skipped", user_id, uid))
        n += 1
    db.commit()
    return n


def migrate(db, column_exists):
    import image_owners
    db.executescript(image_owners.PLATFORM_CATALOG_DDL)
    db.executescript(DDL)
    for col, decl in DOC_COLUMNS:
        if not column_exists(db, "supplier_documents", col):
            db.execute(f"ALTER TABLE supplier_documents ADD COLUMN {col} {decl}")
    db.commit()
