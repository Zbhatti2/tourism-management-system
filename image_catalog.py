"""
Images Catalog -- curated photo albums for Hotels, Resorts and Restaurants
(tenant level).

An album is the supplier's existing "Images / Photographs" (supplier_documents
rows of the 'Image / Photograph' Document Type, bytes in
supplier_document_locations), now with curation fields: Title (the
document name), Description, Date and Sort order, plus where the image came
from (source, contributor, source URL) and a content hash for duplicate
checks.

Nothing reaches an album without being curated first:

1. Images arrive in an import batch (image_import_batches / _items):
   * a Zip (or several image files) from Data Exchange, for many hotels and
     restaurants at once -- each file name must start with the exact
     Hotel / Restaurant name and a hyphen ("Avari Hotel Lahore-Lobby.jpg");
   * images added from one supplier's own page (no naming rule needed);
   * later, the AI Agent that collects images from the web (queue_image()).
2. Each image is matched to its supplier by the file name, checked for
   duplicates (byte-identical to an album image or to another file in the
   batch), and given a suggested Title, Date (the date the photo was taken,
   if the file records it, otherwise the batch's date) and Sort order.
3. On the curation screen the user fixes the supplier where needed, edits
   Title / Description / Date / Sort order, ticks what to keep, and clicks
   Add to albums.
"""
import hashlib
import io
import mimetypes
import os
import re
import unicodedata
import zipfile
from datetime import date

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".heic"}
IMAGE_TYPE_LABEL = "Image / Photograph"
ALBUM_TYPE_CODES = ("HOTEL", "RESORT", "RESTAURANT")
DESCRIPTION_MAX = 200
THUMB_PX = 480
MAX_FILES = 1000
MAX_TOTAL_BYTES = 400 * 1024 * 1024   # uncompressed, per batch
SOURCES = ("Individual", "AI Agent")

DDL = """
CREATE TABLE IF NOT EXISTS image_import_batches (
    batch_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    source          TEXT NOT NULL DEFAULT 'Individual' CHECK (source IN ('Individual','AI Agent')),
    contributor     TEXT,                   -- who supplied the photos (a person, or the agent's name)
    file_name       TEXT,                   -- uploaded zip / first file name
    default_date    TEXT,                   -- YYYY-MM-DD used when a photo doesn't record its own date
    supplier_id     INTEGER REFERENCES suppliers(supplier_id),  -- set when added from one supplier's page
    status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','done','discarded')),
    created_by      INTEGER REFERENCES users(user_id),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_image_import_batches_tenant ON image_import_batches(tenant_id);
CREATE TABLE IF NOT EXISTS image_import_items (
    item_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    batch_id        INTEGER NOT NULL REFERENCES image_import_batches(batch_id),
    file_name       TEXT NOT NULL,
    file_data       BLOB NOT NULL,
    mime_type       TEXT,
    file_size       INTEGER,
    content_hash    TEXT,
    thumb_data      BLOB,
    supplier_id     INTEGER REFERENCES suppliers(supplier_id),
    match_how       TEXT,                   -- exact / similar / ambiguous / entity / manual / NULL (no match)
    match_note      TEXT,
    duplicate_note  TEXT,                   -- set when the same image is already in the album or earlier in the batch
    title           TEXT,
    description     TEXT,
    image_date      TEXT,
    sort_order      INTEGER,
    include         INTEGER NOT NULL DEFAULT 1,
    source_url      TEXT,                   -- where an AI Agent found it
    status          TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','added','rejected')),
    document_id     INTEGER REFERENCES supplier_documents(supplier_document_id),
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_image_import_items_batch ON image_import_items(batch_id);
CREATE INDEX IF NOT EXISTS idx_image_import_items_tenant ON image_import_items(tenant_id);
"""

# Columns added to supplier_documents (used by image rows only).
DOCUMENT_COLUMNS = [("image_date", "TEXT"), ("sort_order", "INTEGER"), ("source", "TEXT"), ("source_url", "TEXT"),
                    ("content_hash", "TEXT"), ("thumb_data", "BLOB")]


# ---- small helpers -------------------------------------------------------------------------

def sha256(data):
    return hashlib.sha256(data).hexdigest()


def is_image_name(name):
    base = os.path.basename(name)
    return (os.path.splitext(base)[1].lower() in IMAGE_EXTENSIONS and not base.startswith(".")
            and "__MACOSX" not in name)


def mime_for(name):
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def make_thumb(data):
    """A JPEG thumbnail (longest side THUMB_PX), or None when Pillow can't
    read the file (e.g. HEIC) -- callers then fall back to the original."""
    try:
        from PIL import Image, ImageOps
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img)
        img.thumbnail((THUMB_PX, THUMB_PX))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        out = io.BytesIO()
        img.save(out, "JPEG", quality=82)
        return out.getvalue()
    except Exception:
        return None


def taken_date(data):
    """The date the photo was taken, from its EXIF data (YYYY-MM-DD), or None."""
    try:
        from PIL import Image
        exif = Image.open(io.BytesIO(data)).getexif()
        raw = exif.get_ifd(0x8769).get(36867) or exif.get(306)  # DateTimeOriginal, else DateTime
        m = re.match(r"(\d{4}):(\d{2}):(\d{2})", raw or "")
        if m and m.group(1) != "0000":
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    except Exception:
        pass
    return None


def _norm(text):
    """Comparable form of a name or file name: accents, case, underscores and
    spacing around hyphens don't matter."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    text = text.replace("_", " ").replace("–", "-").replace("—", "-")
    text = re.sub(r"\s*-\s*", "-", text)
    return re.sub(r"\s+", " ", text).strip()


def image_type_id(db, tenant_id):
    row = db.execute("SELECT document_type_id FROM supplier_document_types WHERE tenant_id = ? AND label = ?",
                     (tenant_id, IMAGE_TYPE_LABEL)).fetchone()
    if row:
        return row[0]
    cur = db.execute("INSERT INTO supplier_document_types (tenant_id, code, label, sort_order, is_active) "
                     "VALUES (?, 'IMAGE_PHOTOGRAPH', ?, 0, 1)", (tenant_id, IMAGE_TYPE_LABEL))
    return cur.lastrowid


# ---- suppliers that have albums ---------------------------------------------------------------

def album_suppliers(db, tenant_id):
    """Hotels, Resorts and Restaurants, with their city, for matching and pickers."""
    return [dict(r) for r in db.execute(
        f"""SELECT s.supplier_id, s.supplier_name, t.label AS type_label, COALESCE(c.label, a.city_text) AS city
            FROM suppliers s
            JOIN supplier_types t ON t.supplier_type_id = s.supplier_type_id
            LEFT JOIN supplier_addresses a ON a.supplier_address_id = (
                SELECT x.supplier_address_id FROM supplier_addresses x WHERE x.supplier_id = s.supplier_id
                ORDER BY x.is_primary DESC, x.supplier_address_id LIMIT 1)
            LEFT JOIN cities c ON c.city_id = a.city_id
            WHERE s.tenant_id = ? AND s.is_deleted = 0 AND s.is_external_resource = 0
              AND t.code IN ({','.join('?' * len(ALBUM_TYPE_CODES))})
            ORDER BY s.supplier_name COLLATE NOCASE""", (tenant_id,) + ALBUM_TYPE_CODES)]


def has_album(db, tenant_id, supplier_id):
    return any(s["supplier_id"] == supplier_id for s in album_suppliers(db, tenant_id))


# ---- matching a file name to a supplier ---------------------------------------------------------

def match_file(stem, suppliers):
    """(supplier_id or None, how, note, remainder) for a file name without
    its extension. how: 'exact' (starts with the exact name and a hyphen, or
    is exactly the name), 'ambiguous' (several suppliers share that name),
    'similar' (a near spelling -- needs a check), or None."""
    s = _norm(stem)
    by_name = {}
    for sup in suppliers:
        by_name.setdefault(_norm(sup["supplier_name"]), []).append(sup)
    best = None
    for name, sups in by_name.items():
        if s == name or s.startswith(name + "-"):
            if best is None or len(name) > len(best[0]):
                best = (name, sups)
    if best:
        name, sups = best
        remainder = s[len(name):].lstrip("-").strip()
        if len(sups) > 1:
            cities = ", ".join(f"{x['supplier_name']} ({x['city'] or 'no city'})" for x in sups)
            return None, "ambiguous", f"{len(sups)} suppliers have this name: {cities}. Choose one.", remainder
        return sups[0]["supplier_id"], "exact", None, remainder
    # No exact name: try the text before each hyphen as a near spelling.
    from fuzzy import compare
    rank = {"same": 3, "sounds": 2, "similar": 1}
    cut_points = [m.start() for m in re.finditer("-", s)] + [len(s)]
    found = None
    for cut in cut_points:
        head = s[:cut].strip()
        if len(head) < 3:
            continue
        for sup in suppliers:
            how = compare(head, sup["supplier_name"])
            if how and (found is None or rank[how] > rank[found[1]] or
                        (rank[how] == rank[found[1]] and cut > found[3])):
                found = (sup, how, head, cut)
    if found:
        sup, how, head, cut = found
        return sup["supplier_id"], "similar", f"File name starts “{head}”; is this {sup['supplier_name']}?", \
            s[cut:].lstrip("-").strip()
    return None, None, "No Hotel / Restaurant name at the start of the file name.", s


def suggest_title(remainder, supplier_name, n):
    """A Title from what follows the name in the file name ("Avari Hotel
    Lahore-Lobby at night" -> "Lobby at night"); camera-style leftovers
    (IMG_2034, 1, WhatsApp Image ...) become "<Name> — photo n"."""
    text = re.sub(r"[-]+", " ", remainder or "").strip()
    camera = re.match(r"^(img|dsc|dscn|pxl|photo|image|whatsapp image|screenshot)?[\s\d:.()x-]*$", text, re.I)
    if not text or camera or not re.search(r"[a-z]{2}", text, re.I):
        return f"{supplier_name} — photo {n}" if supplier_name else f"Photo {n}"
    return text[:1].upper() + text[1:]


# ---- building a batch ------------------------------------------------------------------------

def read_upload(files):
    """[(name, bytes)] from uploaded FileStorage objects: zip files are
    opened (any folder depth), image files are taken as they are. Raises
    ValueError with a user-facing message."""
    out, total = [], 0
    for f in files:
        if not f or not f.filename:
            continue
        data = f.read()
        if f.filename.lower().endswith(".zip"):
            try:
                zf = zipfile.ZipFile(io.BytesIO(data))
            except zipfile.BadZipFile:
                raise ValueError(f"“{f.filename}” isn't a readable Zip file.")
            for info in zf.infolist():
                if info.is_dir() or not is_image_name(info.filename):
                    continue
                total += info.file_size
                if total > MAX_TOTAL_BYTES or len(out) >= MAX_FILES:
                    raise ValueError(f"Too much at once: split it into Zips of at most {MAX_FILES} images "
                                     f"and {MAX_TOTAL_BYTES // (1024 * 1024)} MB.")
                out.append((os.path.basename(info.filename), zf.read(info)))
        elif is_image_name(f.filename):
            total += len(data)
            out.append((os.path.basename(f.filename), data))
    if not out:
        raise ValueError("No images found. Upload a Zip of photos, or .jpg / .png / .webp / .heic files.")
    return out


def create_batch(db, tenant_id, files, source="Individual", contributor=None, default_date=None, supplier_id=None,
                 user_id=None, upload_name=None, source_urls=None):
    """Stage the images for curation. Returns the batch_id."""
    default_date = default_date or date.today().isoformat()
    cur = db.execute("INSERT INTO image_import_batches (tenant_id, source, contributor, file_name, default_date, "
                     "supplier_id, created_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (tenant_id, source if source in SOURCES else "Individual", contributor, upload_name,
                      default_date, supplier_id, user_id))
    batch_id = cur.lastrowid
    add_items(db, tenant_id, batch_id, files, source_urls=source_urls)
    db.commit()
    return batch_id


def add_items(db, tenant_id, batch_id, files, source_urls=None):
    batch = db.execute("SELECT * FROM image_import_batches WHERE batch_id = ?", (batch_id,)).fetchone()
    suppliers = album_suppliers(db, tenant_id)
    names = {s["supplier_id"]: s["supplier_name"] for s in suppliers}
    if batch["supplier_id"] and batch["supplier_id"] not in names:
        r = db.execute("SELECT supplier_name FROM suppliers WHERE supplier_id = ? AND tenant_id = ?",
                       (batch["supplier_id"], tenant_id)).fetchone()
        names[batch["supplier_id"]] = r[0] if r else ""
    album_hashes = {}
    for r in db.execute("SELECT supplier_id, content_hash, document_name FROM supplier_documents "
                        "WHERE tenant_id = ? AND is_deleted = 0 AND content_hash IS NOT NULL", (tenant_id,)):
        album_hashes[(r[0], r[1])] = r[2]
    seen = {(r[0], r[1]): r[2] for r in db.execute(
        "SELECT supplier_id, content_hash, file_name FROM image_import_items WHERE batch_id = ?", (batch_id,))}
    next_sort, counts = {}, {}
    for i, (name, data) in enumerate(sorted(files, key=lambda x: x[0].lower())):
        stem = os.path.splitext(name)[0]
        if batch["supplier_id"]:
            sid, how, note = batch["supplier_id"], "entity", None
            remainder = _norm(stem)
            n_ = _norm(names.get(sid, ""))
            if n_ and remainder.startswith(n_):
                remainder = remainder[len(n_):].lstrip("-").strip()
        else:
            sid, how, note, remainder = match_file(stem, suppliers)
        h = sha256(data)
        dup = None
        if sid and (sid, h) in album_hashes:
            dup = f"Already in the album as “{album_hashes[(sid, h)]}”."
        elif (sid, h) in seen:
            dup = f"Same image as {seen[(sid, h)]} in this upload."
        seen[(sid, h)] = name
        if sid not in next_sort:
            next_sort[sid] = (db.execute("SELECT COALESCE(MAX(sort_order), 0) FROM supplier_documents "
                                         "WHERE supplier_id = ? AND tenant_id = ? AND is_deleted = 0",
                                         (sid, tenant_id)).fetchone()[0] if sid else 0)
        next_sort[sid] += 1
        counts[sid] = counts.get(sid, 0) + 1
        include = 1 if (sid and how in ("exact", "entity") and not dup) else 0
        db.execute(
            """INSERT INTO image_import_items (tenant_id, batch_id, file_name, file_data, mime_type, file_size,
                   content_hash, thumb_data, supplier_id, match_how, match_note, duplicate_note, title, image_date,
                   sort_order, include, source_url)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (tenant_id, batch_id, name, data, mime_for(name), len(data), h, make_thumb(data), sid, how, note, dup,
             suggest_title(remainder, names.get(sid), counts[sid]), taken_date(data) or batch["default_date"],
             next_sort[sid], include, (source_urls or {}).get(name)))


def queue_image(db, tenant_id, supplier_id, data, file_name, title=None, description=None, source_url=None,
                batch_id=None, agent_name="AI Agent", user_id=None):
    """Entry point for the AI Agent that collects images from the web: puts
    one found image into an open 'AI Agent' batch for the supplier, to be
    curated like any other upload. Returns the batch_id."""
    if batch_id is None:
        batch_id = create_batch(db, tenant_id, [], source="AI Agent", contributor=agent_name,
                                supplier_id=supplier_id, user_id=user_id, upload_name=f"{agent_name} search")
    add_items(db, tenant_id, batch_id, [(file_name, data)], source_urls={file_name: source_url})
    if title or description:
        db.execute("UPDATE image_import_items SET title = COALESCE(?, title), description = ? "
                   "WHERE item_id = (SELECT MAX(item_id) FROM image_import_items WHERE batch_id = ?)",
                   (title, (description or "")[:DESCRIPTION_MAX] or None, batch_id))
    db.commit()
    return batch_id


# ---- curation -------------------------------------------------------------------------------------

def batch_items(db, tenant_id, batch_id):
    return db.execute(
        """SELECT i.item_id, i.file_name, i.file_size, i.supplier_id, i.match_how, i.match_note, i.duplicate_note,
                  i.title, i.description, i.image_date, i.sort_order, i.include, i.status, i.source_url,
                  i.document_id, s.supplier_name
           FROM image_import_items i LEFT JOIN suppliers s ON s.supplier_id = i.supplier_id
           WHERE i.batch_id = ? AND i.tenant_id = ? ORDER BY COALESCE(s.supplier_name, 'zzz'), i.sort_order, i.item_id""",
        (batch_id, tenant_id)).fetchall()


def item_state(item):
    """Which filter tab an item belongs to."""
    if item["status"] == "added":
        return "added"
    if item["status"] == "rejected":
        return "rejected"
    if item["duplicate_note"]:
        return "duplicate"
    if not item["supplier_id"]:
        return "unmatched"
    if item["match_how"] == "similar":
        return "check"
    return "ready"


def save_edits(db, tenant_id, batch_id, form, allowed_suppliers):
    """Store the curation form's values on the batch's pending items."""
    for item in db.execute("SELECT item_id, supplier_id, match_how FROM image_import_items "
                           "WHERE batch_id = ? AND tenant_id = ? AND status = 'pending'", (batch_id, tenant_id)).fetchall():
        k = item["item_id"]
        if f"title_{k}" not in form:
            continue
        sid = form.get(f"supplier_{k}")
        sid = int(sid) if sid and sid.isdigit() and int(sid) in allowed_suppliers else None
        how = item["match_how"]
        if sid != item["supplier_id"] or (how == "similar" and sid):
            how = "manual" if sid else None
        try:
            sort = int(form.get(f"sort_{k}") or 0)
        except ValueError:
            sort = 0
        img_date = (form.get(f"date_{k}") or "").strip() or None
        if img_date and not re.match(r"^\d{4}-\d{2}-\d{2}$", img_date):
            img_date = None
        db.execute(
            """UPDATE image_import_items SET supplier_id = ?, match_how = ?, title = ?, description = ?, image_date = ?,
                   sort_order = ?, include = ?, match_note = CASE WHEN ? IN ('manual','exact','entity') THEN NULL ELSE match_note END
               WHERE item_id = ?""",
            (sid, how, (form.get(f"title_{k}") or "").strip() or None,
             (form.get(f"description_{k}") or "").strip()[:DESCRIPTION_MAX] or None, img_date, sort,
             1 if form.get(f"include_{k}") else 0, how, k))
    db.commit()


def approve(db, tenant_id, batch_id):
    """Add every pending, ticked item that has a supplier to that supplier's
    album. Returns (added, problems)."""
    batch = db.execute("SELECT * FROM image_import_batches WHERE batch_id = ? AND tenant_id = ?",
                       (batch_id, tenant_id)).fetchone()
    type_id = image_type_id(db, tenant_id)
    added, problems = 0, []
    for it in db.execute("SELECT * FROM image_import_items WHERE batch_id = ? AND tenant_id = ? AND status = 'pending' "
                         "AND include = 1", (batch_id, tenant_id)).fetchall():
        if not it["supplier_id"]:
            problems.append(f"{it['file_name']}: choose a Hotel / Restaurant first")
            continue
        title = it["title"] or os.path.splitext(it["file_name"])[0]
        cur = db.execute(
            """INSERT INTO supplier_documents (tenant_id, supplier_id, document_name, document_type_id, authors,
                   description, image_date, sort_order, source, source_url, content_hash, thumb_data)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (tenant_id, it["supplier_id"], title, type_id, batch["contributor"], it["description"], it["image_date"],
             it["sort_order"], batch["source"], it["source_url"], it["content_hash"], it["thumb_data"]))
        doc_id = cur.lastrowid
        db.execute("""INSERT INTO supplier_document_locations (tenant_id, supplier_document_id, location_type,
                          path_or_url, file_data, file_name, mime_type, file_size)
                      VALUES (?, ?, 'Stored in Database', ?, ?, ?, ?, ?)""",
                   (tenant_id, doc_id, it["file_name"], it["file_data"], it["file_name"], it["mime_type"], it["file_size"]))
        # The bytes now live in the album; drop the staged copy.
        db.execute("UPDATE image_import_items SET status = 'added', document_id = ?, file_data = X'', thumb_data = NULL "
                   "WHERE item_id = ?", (doc_id, it["item_id"]))
        added += 1
    _close_if_done(db, batch_id)
    db.commit()
    return added, problems


def reject_unticked(db, tenant_id, batch_id):
    n = db.execute("UPDATE image_import_items SET status = 'rejected', file_data = X'', thumb_data = NULL "
                   "WHERE batch_id = ? AND tenant_id = ? AND status = 'pending' AND include = 0",
                   (batch_id, tenant_id)).rowcount
    _close_if_done(db, batch_id)
    db.commit()
    return n


def discard(db, tenant_id, batch_id):
    db.execute("UPDATE image_import_items SET status = 'rejected', file_data = X'', thumb_data = NULL "
               "WHERE batch_id = ? AND tenant_id = ? AND status = 'pending'", (batch_id, tenant_id))
    db.execute("UPDATE image_import_batches SET status = 'discarded', finished_at = datetime('now') "
               "WHERE batch_id = ? AND tenant_id = ?", (batch_id, tenant_id))
    db.commit()


def _close_if_done(db, batch_id):
    left = db.execute("SELECT COUNT(*) FROM image_import_items WHERE batch_id = ? AND status = 'pending'",
                      (batch_id,)).fetchone()[0]
    if not left:
        db.execute("UPDATE image_import_batches SET status = 'done', finished_at = datetime('now') "
                   "WHERE batch_id = ? AND status = 'open'", (batch_id,))


def open_batches(db, tenant_id):
    return db.execute(
        """SELECT b.*, s.supplier_name,
                  (SELECT COUNT(*) FROM image_import_items i WHERE i.batch_id = b.batch_id) AS total,
                  (SELECT COUNT(*) FROM image_import_items i WHERE i.batch_id = b.batch_id AND i.status = 'pending') AS pending,
                  (SELECT COUNT(*) FROM image_import_items i WHERE i.batch_id = b.batch_id AND i.status = 'added') AS added
           FROM image_import_batches b LEFT JOIN suppliers s ON s.supplier_id = b.supplier_id
           WHERE b.tenant_id = ? ORDER BY (b.status = 'open') DESC, b.batch_id DESC LIMIT 30""", (tenant_id,)).fetchall()


# ---- albums -----------------------------------------------------------------------------------------

ALBUM_SORTS = {
    "album": "COALESCE(d.sort_order, 999999), COALESCE(d.image_date, substr(d.created_at, 1, 10)) DESC, d.supplier_document_id",
    "newest": "COALESCE(d.image_date, substr(d.created_at, 1, 10)) DESC, d.supplier_document_id DESC",
    "oldest": "COALESCE(d.image_date, substr(d.created_at, 1, 10)) ASC, d.supplier_document_id",
    "title": "d.document_name COLLATE NOCASE, d.supplier_document_id",
}
SORT_LABELS = {"album": "Album order", "newest": "Date — newest first", "oldest": "Date — oldest first",
               "title": "Title A–Z"}


def album(db, tenant_id, supplier_id, q="", sort="album", date_from=None, date_to=None):
    type_id = image_type_id(db, tenant_id)
    sql = ("SELECT d.supplier_document_id, d.document_name, d.description, d.authors, d.notes, d.sort_order, d.source, "
           "d.source_url, COALESCE(d.image_date, substr(d.created_at, 1, 10)) AS shown_date, d.image_date, d.created_at, "
           "(SELECT GROUP_CONCAT(term, ', ') FROM supplier_document_keywords k "
           " WHERE k.supplier_document_id = d.supplier_document_id) AS keywords "
           "FROM supplier_documents d WHERE d.supplier_id = ? AND d.tenant_id = ? AND d.is_deleted = 0 "
           "AND d.document_type_id = ?")
    params = [supplier_id, tenant_id, type_id]
    if q:
        sql += (" AND (d.document_name LIKE ? OR d.description LIKE ? OR d.notes LIKE ? OR d.authors LIKE ? "
                "OR COALESCE(d.image_date, substr(d.created_at, 1, 10)) LIKE ? "
                "OR EXISTS (SELECT 1 FROM supplier_document_keywords k WHERE k.supplier_document_id = d.supplier_document_id AND k.term LIKE ?))")
        params += [f"%{q}%"] * 6
    if date_from:
        sql += " AND COALESCE(d.image_date, substr(d.created_at, 1, 10)) >= ?"
        params.append(date_from)
    if date_to:
        sql += " AND COALESCE(d.image_date, substr(d.created_at, 1, 10)) <= ?"
        params.append(date_to)
    return db.execute(sql + " ORDER BY " + ALBUM_SORTS.get(sort, ALBUM_SORTS["album"]), params).fetchall()


def document_thumb(db, tenant_id, document_id):
    """(bytes, mime) of an album image's thumbnail, making and caching it
    the first time (images added before the Images Catalog have none)."""
    row = db.execute("SELECT thumb_data FROM supplier_documents WHERE supplier_document_id = ? AND tenant_id = ?",
                     (document_id, tenant_id)).fetchone()
    if row is None:
        return None, None
    if row[0]:
        return row[0], "image/jpeg"
    loc = db.execute("SELECT file_data, mime_type FROM supplier_document_locations WHERE supplier_document_id = ? "
                     "AND tenant_id = ? AND location_type = 'Stored in Database' ORDER BY location_id LIMIT 1",
                     (document_id, tenant_id)).fetchone()
    if loc is None or not loc[0]:
        return None, None
    thumb = make_thumb(loc[0])
    if thumb:
        db.execute("UPDATE supplier_documents SET thumb_data = ? WHERE supplier_document_id = ?", (thumb, document_id))
        db.commit()
        return thumb, "image/jpeg"
    return loc[0], loc[1]


def backfill_hashes(db):
    """Content hashes for images stored before the Images Catalog, so their
    duplicates are recognised on import."""
    for r in db.execute("""SELECT d.supplier_document_id, l.file_data FROM supplier_documents d
                           JOIN supplier_document_locations l ON l.supplier_document_id = d.supplier_document_id
                           WHERE d.content_hash IS NULL AND l.location_type = 'Stored in Database'
                             AND l.file_data IS NOT NULL""").fetchall():
        db.execute("UPDATE supplier_documents SET content_hash = ? WHERE supplier_document_id = ?",
                   (sha256(r[1]), r[0]))
