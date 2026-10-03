"""
What an Images Catalog album can belong to ("owner kinds"), so the same
upload -> curate -> album screens (blueprints/images.py, image_catalog.py)
serve all of them:

* supplier      -- a tenant's Hotel / Resort / Restaurant
                   (supplier_documents of the 'Image / Photograph' type).
* poi           -- a tenant's Point of Interest (poi_images). Its album holds
                   the tenant's own images plus images inherited from the
                   platform's Master Image Catalog (rows with platform_image_id;
                   their bytes stay in platform_poi_images -- see
                   poi_image_sync.py).
* platform_poi  -- a platform Point of Interest's Master Image Catalog
                   (platform_poi_images), curated by the SystemAdmin. Platform
                   uploads are staged under the reserved TMS Platform tenant.

Every album row is returned with the same keys: image_id, title, description,
authors, notes, sort_order, source, source_url, shown_date, image_date,
keywords, inherited, licence.
"""
from image_catalog import (ALBUM_TYPE_CODES, DESCRIPTION_MAX, IMAGE_TYPE_LABEL, album_suppliers, image_type_id,
                           make_thumb)

PLATFORM_POI_DDL = """
CREATE TABLE IF NOT EXISTS platform_poi_images (
    image_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    poi_id          INTEGER NOT NULL,       -- platform_pois.poi_id (no FK: images outlive a deleted POI for tenants still using them)
    title           TEXT NOT NULL,
    description     TEXT,
    image_date      TEXT,
    sort_order      INTEGER,
    source          TEXT,                   -- 'Individual' / 'AI Agent'
    contributor     TEXT,
    source_url      TEXT,
    licence         TEXT,                   -- licence / credit line, e.g. 'CC BY-SA 4.0, photo by …'
    content_hash    TEXT,
    file_name       TEXT,
    mime_type       TEXT,
    file_size       INTEGER,
    file_data       BLOB NOT NULL,
    thumb_data      BLOB,
    is_active       INTEGER NOT NULL DEFAULT 1,   -- 0 = removed from the master catalog (bytes kept for tenants)
    version         INTEGER NOT NULL DEFAULT 1,   -- bumped when title / description / date / licence change
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_platform_poi_images_poi ON platform_poi_images(poi_id);
"""


def _base_where(q, date_from, date_to):
    where, params = [], []
    if q:
        where.append("(title LIKE ? OR description LIKE ? OR notes LIKE ? OR authors LIKE ? OR shown_date LIKE ? "
                     "OR keywords LIKE ? OR licence LIKE ?)")
        params += [f"%{q}%"] * 7
    if date_from:
        where.append("shown_date >= ?")
        params.append(date_from)
    if date_to:
        where.append("shown_date <= ?")
        params.append(date_to)
    return (" WHERE " + " AND ".join(where)) if where else "", params


SORTS = {
    "album": "COALESCE(NULLIF(sort_order, 0), 999999), shown_date DESC, image_id",
    "newest": "shown_date DESC, image_id DESC",
    "oldest": "shown_date ASC, image_id",
    "title": "title COLLATE NOCASE, image_id",
}


def _kept_date(row, new_date):
    """The list view shows the date added when an image has no date of its
    own; saving that shown date back isn't a change."""
    if not row["image_date"] and new_date and new_date == (row["created_at"] or "")[:10]:
        return None
    return new_date


class Kind:
    key = None
    platform = False
    singular = plural = ""
    owner_word = ""           # used in sentences: "this hotel", "this point of interest"

    def owners(self, db, scope):
        raise NotImplementedError

    def owner(self, db, scope, owner_id):
        return next((o for o in self.owners(db, scope) if o["id"] == owner_id), None)

    def _album_sql(self):
        raise NotImplementedError

    def album(self, db, scope, owner_id, q="", sort="album", date_from=None, date_to=None):
        base, params = self._album_sql(db, scope, owner_id)
        where, wparams = _base_where(q, date_from, date_to)
        return [dict(r) for r in db.execute(f"SELECT * FROM ({base}){where} ORDER BY {SORTS.get(sort, SORTS['album'])}",
                                            params + wparams)]

    def after_change(self, db, owner_ids):
        """Hook after images were added, edited or removed."""


class SupplierKind(Kind):
    key = "supplier"
    singular, plural, owner_word = "Hotel / Restaurant", "Hotels, Resorts & Restaurants", "hotel or restaurant"

    def owners(self, db, scope):
        return [{"id": s["supplier_id"], "name": s["supplier_name"], "city": s["city"], "type_label": s["type_label"]}
                for s in album_suppliers(db, scope)]

    def owner(self, db, scope, owner_id):
        s = db.execute("""SELECT s.supplier_id, s.supplier_name, t.label AS type_label, t.code AS type_code FROM suppliers s
                          LEFT JOIN supplier_types t ON t.supplier_type_id = s.supplier_type_id
                          WHERE s.supplier_id = ? AND s.tenant_id = ? AND s.is_deleted = 0""", (owner_id, scope)).fetchone()
        if s is None:
            return None
        return {"id": s[0], "name": s[1], "type_label": s[2], "type_code": s[3], "city": None,
                "has_collector": s[3] in ALBUM_TYPE_CODES}

    def page_url(self, url_for, owner_id):
        return url_for("suppliers.view_supplier", supplier_id=owner_id)

    def hashes(self, db, scope):
        return {(r[0], r[1]): r[2] for r in db.execute(
            "SELECT supplier_id, content_hash, document_name FROM supplier_documents WHERE tenant_id = ? AND is_deleted = 0 "
            "AND content_hash IS NOT NULL", (scope,))}

    def max_sort(self, db, scope, owner_id):
        return db.execute("SELECT COALESCE(MAX(sort_order), 0) FROM supplier_documents WHERE supplier_id = ? "
                          "AND tenant_id = ? AND is_deleted = 0", (owner_id, scope)).fetchone()[0]

    def insert(self, db, scope, owner_id, it, batch):
        cur = db.execute(
            """INSERT INTO supplier_documents (tenant_id, supplier_id, document_name, document_type_id, authors,
                   description, image_date, sort_order, source, source_url, content_hash, thumb_data)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (scope, owner_id, it["title"], image_type_id(db, scope), batch["contributor"], it["description"],
             it["image_date"], it["sort_order"], batch["source"], it["source_url"], it["content_hash"], it["thumb_data"]))
        doc_id = cur.lastrowid
        db.execute("""INSERT INTO supplier_document_locations (tenant_id, supplier_document_id, location_type,
                          path_or_url, file_data, file_name, mime_type, file_size)
                      VALUES (?, ?, 'Stored in Database', ?, ?, ?, ?, ?)""",
                   (scope, doc_id, it["file_name"], it["file_data"], it["file_name"], it["mime_type"], it["file_size"]))
        return doc_id

    def _album_sql(self, db, scope, owner_id):
        return ("""SELECT d.supplier_document_id AS image_id, d.document_name AS title, d.description, d.authors, d.notes,
                          d.sort_order, d.source, d.source_url, COALESCE(d.image_date, substr(d.created_at, 1, 10)) AS shown_date,
                          d.image_date, (SELECT GROUP_CONCAT(term, ', ') FROM supplier_document_keywords k
                                         WHERE k.supplier_document_id = d.supplier_document_id) AS keywords,
                          0 AS inherited, NULL AS licence
                   FROM supplier_documents d WHERE d.supplier_id = ? AND d.tenant_id = ? AND d.is_deleted = 0
                     AND d.document_type_id = ?""", [owner_id, scope, image_type_id(db, scope)])

    def image(self, db, scope, owner_id, image_id, size):
        """(bytes, mime) or ('redirect', url-args) for a thumbnail / full image."""
        row = db.execute("SELECT thumb_data FROM supplier_documents WHERE supplier_document_id = ? AND supplier_id = ? "
                         "AND tenant_id = ? AND is_deleted = 0", (image_id, owner_id, scope)).fetchone()
        if row is None:
            return None, None
        if size == "thumb":
            from image_catalog import document_thumb
            data, mime = document_thumb(db, scope, image_id)
            if data:
                return data, mime
        return "redirect", ("suppliers.image_thumbnail", {"supplier_id": owner_id, "document_id": image_id})

    def owns(self, db, scope, owner_id, ids):
        ids = list(ids)
        return {r[0] for r in db.execute(
            f"SELECT supplier_document_id FROM supplier_documents WHERE supplier_id = ? AND tenant_id = ? AND is_deleted = 0 "
            f"AND supplier_document_id IN ({','.join('?' * len(ids)) or 'NULL'})", [owner_id, scope] + ids)}

    def update(self, db, scope, owner_id, image_id, title, description, image_date, sort_order, licence=None):
        db.execute("UPDATE supplier_documents SET document_name = ?, description = ?, image_date = ?, sort_order = ?, "
                   "updated_at = datetime('now') WHERE supplier_document_id = ? AND tenant_id = ?",
                   (title, description, image_date, sort_order, image_id, scope))

    def delete(self, db, scope, owner_id, image_id):
        db.execute("UPDATE supplier_documents SET is_deleted = 1, updated_at = datetime('now') "
                   "WHERE supplier_document_id = ? AND tenant_id = ?", (image_id, scope))

    def has_images(self, db, scope, owner_id):
        return db.execute("SELECT 1 FROM supplier_documents WHERE supplier_id = ? AND tenant_id = ? AND is_deleted = 0 "
                          "AND document_type_id = ? LIMIT 1", (owner_id, scope, image_type_id(db, scope))).fetchone() is not None


class PoiKind(Kind):
    key = "poi"
    singular, plural, owner_word = "Point of Interest", "Points of Interest", "point of interest"

    def owners(self, db, scope):
        return [dict(r) for r in db.execute(
            """SELECT p.poi_id AS id, p.name, COALESCE(c.label, p.city_text) AS city, pt.label AS type_label
               FROM points_of_interest p LEFT JOIN cities c ON c.city_id = p.city_id
               LEFT JOIN poi_types pt ON pt.poi_type_id = p.poi_type_id
               WHERE p.tenant_id = ? AND p.is_deleted = 0 ORDER BY p.name COLLATE NOCASE""", (scope,))]

    def owner(self, db, scope, owner_id):
        r = db.execute("""SELECT p.poi_id AS id, p.name, pt.label AS type_label FROM points_of_interest p
                          LEFT JOIN poi_types pt ON pt.poi_type_id = p.poi_type_id
                          WHERE p.poi_id = ? AND p.tenant_id = ? AND p.is_deleted = 0""", (owner_id, scope)).fetchone()
        return dict(r, city=None, type_code=None, has_collector=False) if r else None

    def page_url(self, url_for, owner_id):
        return url_for("poi.view_poi", poi_id=owner_id)

    def hashes(self, db, scope):
        return {(r[0], r[1]): r[2] for r in db.execute(
            """SELECT i.poi_id, COALESCE(i.content_hash, m.content_hash), COALESCE(i.image_name, i.file_name)
               FROM poi_images i LEFT JOIN platform_poi_images m ON m.image_id = i.platform_image_id
               WHERE i.tenant_id = ? AND COALESCE(i.is_deleted, 0) = 0""", (scope,)) if r[1]}

    def max_sort(self, db, scope, owner_id):
        return db.execute("SELECT COALESCE(MAX(sort_order), 0) FROM poi_images WHERE poi_id = ? AND tenant_id = ? "
                          "AND COALESCE(is_deleted, 0) = 0", (owner_id, scope)).fetchone()[0]

    def insert(self, db, scope, owner_id, it, batch):
        cur = db.execute(
            """INSERT INTO poi_images (tenant_id, poi_id, location_type, path_or_url, image_name, authors, description,
                   file_data, file_name, mime_type, file_size, sort_order, image_date, source, source_url, content_hash,
                   thumb_data)
               VALUES (?, ?, 'Stored in Database', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (scope, owner_id, it["file_name"], it["title"], batch["contributor"], it["description"], it["file_data"],
             it["file_name"], it["mime_type"], it["file_size"], it["sort_order"], it["image_date"], batch["source"],
             it["source_url"], it["content_hash"], it["thumb_data"]))
        return cur.lastrowid

    def _album_sql(self, db, scope, owner_id):
        return ("""SELECT i.poi_image_id AS image_id, COALESCE(i.image_name, i.file_name, 'Image') AS title, i.description,
                          i.authors, i.notes, i.sort_order, i.source, COALESCE(i.source_url, m.source_url) AS source_url,
                          COALESCE(i.image_date, substr(i.created_at, 1, 10)) AS shown_date, i.image_date,
                          (SELECT GROUP_CONCAT(term, ', ') FROM poi_image_keywords k WHERE k.poi_image_id = i.poi_image_id) AS keywords,
                          (i.platform_image_id IS NOT NULL) AS inherited, m.licence
                   FROM poi_images i LEFT JOIN platform_poi_images m ON m.image_id = i.platform_image_id
                   WHERE i.poi_id = ? AND i.tenant_id = ? AND COALESCE(i.is_deleted, 0) = 0""", [owner_id, scope])

    def image(self, db, scope, owner_id, image_id, size):
        r = db.execute("SELECT * FROM poi_images WHERE poi_image_id = ? AND poi_id = ? AND tenant_id = ? "
                       "AND COALESCE(is_deleted, 0) = 0", (image_id, owner_id, scope)).fetchone()
        if r is None:
            return None, None
        if r["platform_image_id"]:
            return platform_image(db, r["platform_image_id"], size)
        if r["location_type"] == "Stored in Database" and r["file_data"]:
            if size == "thumb":
                if r["thumb_data"]:
                    return r["thumb_data"], "image/jpeg"
                thumb = make_thumb(r["file_data"])
                if thumb:
                    db.execute("UPDATE poi_images SET thumb_data = ? WHERE poi_image_id = ?", (thumb, image_id))
                    db.commit()
                    return thumb, "image/jpeg"
            return r["file_data"], r["mime_type"] or "application/octet-stream"
        return "redirect", ("poi.poi_image_thumbnail", {"poi_id": owner_id, "image_id": image_id})

    def owns(self, db, scope, owner_id, ids):
        ids = list(ids)
        return {r[0] for r in db.execute(
            f"SELECT poi_image_id FROM poi_images WHERE poi_id = ? AND tenant_id = ? AND COALESCE(is_deleted, 0) = 0 "
            f"AND poi_image_id IN ({','.join('?' * len(ids)) or 'NULL'})", [owner_id, scope] + ids)}

    def update(self, db, scope, owner_id, image_id, title, description, image_date, sort_order, licence=None):
        r = db.execute("SELECT image_name, description, image_date, platform_image_id, created_at FROM poi_images "
                       "WHERE poi_image_id = ? AND tenant_id = ?", (image_id, scope)).fetchone()
        if r is None:
            return
        image_date = _kept_date(r, image_date)
        edited = r["platform_image_id"] is not None and (
            (r["image_name"] or "") != (title or "") or (r["description"] or "") != (description or "") or
            (r["image_date"] or "") != (image_date or ""))
        db.execute("UPDATE poi_images SET image_name = ?, description = ?, image_date = ?, sort_order = ?, "
                   "local_edited = CASE WHEN ? THEN 1 ELSE COALESCE(local_edited, 0) END "
                   "WHERE poi_image_id = ? AND tenant_id = ?",
                   (title, description, image_date, sort_order, 1 if edited else 0, image_id, scope))

    def delete(self, db, scope, owner_id, image_id):
        # Soft delete: an inherited image the tenant removed is remembered, so it isn't offered again.
        db.execute("UPDATE poi_images SET is_deleted = 1 WHERE poi_image_id = ? AND tenant_id = ?", (image_id, scope))

    def has_images(self, db, scope, owner_id):
        return db.execute("SELECT 1 FROM poi_images WHERE poi_id = ? AND tenant_id = ? AND COALESCE(is_deleted, 0) = 0 "
                          "LIMIT 1", (owner_id, scope)).fetchone() is not None


class PlatformPoiKind(Kind):
    key = "platform_poi"
    platform = True
    singular, plural, owner_word = "Point of Interest", "Points of Interest (platform)", "point of interest"

    def owners(self, db, scope):
        return [dict(r) for r in db.execute(
            """SELECT p.poi_id AS id, p.name, COALESCE(c.label, p.city_text) AS city, pt.label AS type_label
               FROM platform_pois p LEFT JOIN cities c ON c.city_id = p.city_id
               LEFT JOIN poi_types pt ON pt.poi_type_id = p.poi_type_id
               WHERE p.is_active = 1 ORDER BY p.name COLLATE NOCASE""")]

    def owner(self, db, scope, owner_id):
        r = db.execute("""SELECT p.poi_id AS id, p.name, pt.label AS type_label, COALESCE(c.label, p.city_text) AS city,
                                 p.website FROM platform_pois p LEFT JOIN poi_types pt ON pt.poi_type_id = p.poi_type_id
                          LEFT JOIN cities c ON c.city_id = p.city_id WHERE p.poi_id = ?""", (owner_id,)).fetchone()
        return dict(r, type_code=None, has_collector=True) if r else None

    def page_url(self, url_for, owner_id):
        return url_for("platform_catalog.view_record", entity="pois", rid=owner_id)

    def hashes(self, db, scope):
        return {(r[0], r[1]): r[2] for r in db.execute(
            "SELECT poi_id, content_hash, title FROM platform_poi_images WHERE is_active = 1 AND content_hash IS NOT NULL")}

    def max_sort(self, db, scope, owner_id):
        return db.execute("SELECT COALESCE(MAX(sort_order), 0) FROM platform_poi_images WHERE poi_id = ? AND is_active = 1",
                          (owner_id,)).fetchone()[0]

    def insert(self, db, scope, owner_id, it, batch):
        cur = db.execute(
            """INSERT INTO platform_poi_images (poi_id, title, description, image_date, sort_order, source, contributor,
                   source_url, licence, content_hash, file_name, mime_type, file_size, file_data, thumb_data)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (owner_id, it["title"], it["description"], it["image_date"], it["sort_order"], batch["source"],
             batch["contributor"], it["source_url"], it["licence"] if "licence" in it.keys() else None,
             it["content_hash"], it["file_name"], it["mime_type"], it["file_size"], it["file_data"], it["thumb_data"]))
        return cur.lastrowid

    def _album_sql(self, db, scope, owner_id):
        return ("""SELECT image_id, title, description, contributor AS authors, NULL AS notes, sort_order, source, source_url,
                          COALESCE(image_date, substr(created_at, 1, 10)) AS shown_date, image_date, NULL AS keywords,
                          0 AS inherited, licence
                   FROM platform_poi_images WHERE poi_id = ? AND is_active = 1""", [owner_id])

    def image(self, db, scope, owner_id, image_id, size):
        r = db.execute("SELECT poi_id FROM platform_poi_images WHERE image_id = ? AND poi_id = ?", (image_id, owner_id)).fetchone()
        return platform_image(db, image_id, size) if r else (None, None)

    def owns(self, db, scope, owner_id, ids):
        ids = list(ids)
        return {r[0] for r in db.execute(
            f"SELECT image_id FROM platform_poi_images WHERE poi_id = ? AND is_active = 1 "
            f"AND image_id IN ({','.join('?' * len(ids)) or 'NULL'})", [owner_id] + ids)}

    def update(self, db, scope, owner_id, image_id, title, description, image_date, sort_order, licence=None):
        r = db.execute("SELECT title, description, image_date, licence, created_at FROM platform_poi_images WHERE image_id = ?",
                       (image_id,)).fetchone()
        if r is None:
            return
        image_date = _kept_date(r, image_date)
        changed = ((r["title"] or "") != (title or "") or (r["description"] or "") != (description or "") or
                   (r["image_date"] or "") != (image_date or "") or (r["licence"] or "") != (licence or ""))
        db.execute("UPDATE platform_poi_images SET title = ?, description = ?, image_date = ?, sort_order = ?, licence = ?, "
                   "version = version + ?, updated_at = datetime('now') WHERE image_id = ?",
                   (title, description, image_date, sort_order, licence, 1 if changed else 0, image_id))

    def delete(self, db, scope, owner_id, image_id):
        # Retired, not erased: tenants that inherited it keep it unless their update setting removes it.
        db.execute("UPDATE platform_poi_images SET is_active = 0, version = version + 1, updated_at = datetime('now') "
                   "WHERE image_id = ?", (image_id,))

    def has_images(self, db, scope, owner_id):
        return db.execute("SELECT 1 FROM platform_poi_images WHERE poi_id = ? AND is_active = 1 LIMIT 1",
                          (owner_id,)).fetchone() is not None

    def after_change(self, db, owner_ids):
        import poi_image_sync
        poi_image_sync.push(db, owner_ids)


def platform_image(db, image_id, size):
    r = db.execute("SELECT file_data, mime_type, thumb_data FROM platform_poi_images WHERE image_id = ?", (image_id,)).fetchone()
    if r is None:
        return None, None
    if size == "thumb":
        if r["thumb_data"]:
            return r["thumb_data"], "image/jpeg"
        thumb = make_thumb(r["file_data"])
        if thumb:
            db.execute("UPDATE platform_poi_images SET thumb_data = ? WHERE image_id = ?", (thumb, image_id))
            db.commit()
            return thumb, "image/jpeg"
    return r["file_data"], r["mime_type"] or "application/octet-stream"


KINDS = {k.key: k for k in (SupplierKind(), PoiKind(), PlatformPoiKind())}

__all__ = ["KINDS", "PLATFORM_POI_DDL", "DESCRIPTION_MAX", "IMAGE_TYPE_LABEL", "platform_image"]
