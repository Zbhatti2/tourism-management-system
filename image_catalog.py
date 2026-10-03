"""
Images Catalog -- curated photo albums. Owners ("kinds", see image_owners.py):
a tenant's Hotels, Resorts and Restaurants, a tenant's Points of Interest, and
the platform's Points of Interest (the Master Image Catalog tenants inherit).

The text below describes the supplier albums; POI albums work the same way.

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

# Owner columns (Oct 2026, POI Master Image Catalog): a batch / item belongs to
# an owner of some kind; supplier_id / document_id are kept for older rows.
BATCH_COLUMNS = [("owner_kind", "TEXT NOT NULL DEFAULT 'supplier'"), ("owner_id", "INTEGER")]
ITEM_COLUMNS = [("owner_id", "INTEGER"), ("image_ref", "INTEGER"), ("licence", "TEXT")]

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


def _kind(kind):
    from image_owners import KINDS
    return KINDS[kind] if isinstance(kind, str) else kind


# ---- matching a file name to an owner ---------------------------------------------------------

def match_file(stem, owners, what="Hotel / Restaurant"):
    """(owner id or None, how, note, remainder) for a file name without its
    extension. owners: [{id, name, city}]. how: 'exact' (starts with the
    exact name and a hyphen, or is exactly the name), 'ambiguous' (several
    owners share that name), 'similar' (a near spelling -- needs a check),
    or None."""
    s = _norm(stem)
    by_name = {}
    for o in owners:
        by_name.setdefault(_norm(o["name"]), []).append(o)
    best = None
    for name, os_ in by_name.items():
        if s == name or s.startswith(name + "-"):
            if best is None or len(name) > len(best[0]):
                best = (name, os_)
    if best:
        name, os_ = best
        remainder = s[len(name):].lstrip("-").strip()
        if len(os_) > 1:
            cities = ", ".join(f"{x['name']} ({x.get('city') or 'no city'})" for x in os_)
            return None, "ambiguous", f"{len(os_)} records have this name: {cities}. Choose one.", remainder
        return os_[0]["id"], "exact", None, remainder
    # No exact name: try the text before each hyphen as a near spelling.
    from fuzzy import compare
    rank = {"same": 3, "sounds": 2, "similar": 1}
    cut_points = [m.start() for m in re.finditer("-", s)] + [len(s)]
    found = None
    for cut in cut_points:
        head = s[:cut].strip()
        if len(head) < 3:
            continue
        for o in owners:
            how = compare(head, o["name"])
            if how and (found is None or rank[how] > rank[found[1]] or
                        (rank[how] == rank[found[1]] and cut > found[3])):
                found = (o, how, head, cut)
    if found:
        o, how, head, cut = found
        return o["id"], "similar", f"File name starts “{head}”; is this {o['name']}?", s[cut:].lstrip("-").strip()
    return None, None, f"No {what} name at the start of the file name.", s


def suggest_title(remainder, owner_name, n):
    """A Title from what follows the name in the file name ("Avari Hotel
    Lahore-Lobby at night" -> "Lobby at night"); camera-style leftovers
    (IMG_2034, 1, WhatsApp Image ...) become "<Name> — photo n"."""
    text = re.sub(r"[-]+", " ", remainder or "").strip()
    camera = re.match(r"^(img|dsc|dscn|pxl|photo|image|whatsapp image|screenshot)?[\s\d:.()x-]*$", text, re.I)
    if not text or camera or not re.search(r"[a-z]{2}", text, re.I):
        return f"{owner_name} — photo {n}" if owner_name else f"Photo {n}"
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


def create_batch(db, tenant_id, files, source="Individual", contributor=None, default_date=None, owner_id=None,
                 user_id=None, upload_name=None, source_urls=None, kind="supplier"):
    """Stage the images for curation. tenant_id is the batch's scope (the
    TMS Platform tenant for platform albums). Returns the batch_id."""
    kind = _kind(kind)
    default_date = default_date or date.today().isoformat()
    cur = db.execute("INSERT INTO image_import_batches (tenant_id, source, contributor, file_name, default_date, "
                     "owner_kind, owner_id, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                     (tenant_id, source if source in SOURCES else "Individual", contributor, upload_name,
                      default_date, kind.key, owner_id, user_id))
    batch_id = cur.lastrowid
    add_items(db, tenant_id, batch_id, files, source_urls=source_urls)
    db.commit()
    return batch_id


def batch_kind(batch):
    return _kind((batch["owner_kind"] if "owner_kind" in batch.keys() else None) or "supplier")


def add_items(db, tenant_id, batch_id, files, source_urls=None, meta=None):
    """Stage files in a batch. meta: optional {file name: {owner_id, title,
    description, source_url, licence}} from an AI collector, which already
    knows which owner each image is for."""
    meta = meta or {}
    batch = db.execute("SELECT * FROM image_import_batches WHERE batch_id = ?", (batch_id,)).fetchone()
    kind = batch_kind(batch)
    owners = kind.owners(db, tenant_id) if not batch["owner_id"] else []
    names = {o["id"]: o["name"] for o in owners}

    def name_of(oid):
        if oid not in names:
            o = kind.owner(db, tenant_id, oid)
            names[oid] = o["name"] if o else ""
        return names[oid]

    album_hashes = kind.hashes(db, tenant_id)
    seen = {(r[0], r[1]): r[2] for r in db.execute(
        "SELECT owner_id, content_hash, file_name FROM image_import_items WHERE batch_id = ?", (batch_id,))}
    next_sort, counts = {}, {}
    for name, data in sorted(files, key=lambda x: x[0].lower()):
        stem = os.path.splitext(name)[0]
        m = meta.get(name, {})
        given = m.get("owner_id") or m.get("supplier_id")
        if given:
            oid, how, note, remainder = given, "entity", None, ""
        elif batch["owner_id"]:
            oid, how, note = batch["owner_id"], "entity", None
            remainder = _norm(stem)
            n_ = _norm(name_of(oid))
            if n_ and remainder.startswith(n_):
                remainder = remainder[len(n_):].lstrip("-").strip()
        else:
            oid, how, note, remainder = match_file(stem, owners, kind.singular)
        h = sha256(data)
        dup = None
        if oid and (oid, h) in album_hashes:
            dup = f"Already in the album as “{album_hashes[(oid, h)]}”."
        elif (oid, h) in seen:
            dup = f"Same image as {seen[(oid, h)]} in this upload."
        seen[(oid, h)] = name
        if oid not in next_sort:
            next_sort[oid] = kind.max_sort(db, tenant_id, oid) if oid else 0
        next_sort[oid] = (next_sort[oid] or 0) + 1
        counts[oid] = counts.get(oid, 0) + 1
        include = 1 if (oid and how in ("exact", "entity") and not dup) else 0
        db.execute(
            """INSERT INTO image_import_items (tenant_id, batch_id, file_name, file_data, mime_type, file_size,
                   content_hash, thumb_data, owner_id, match_how, match_note, duplicate_note, title, description,
                   image_date, sort_order, include, source_url, licence)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (tenant_id, batch_id, name, data, mime_for(name), len(data), h, make_thumb(data), oid, how, note, dup,
             m.get("title") or suggest_title(remainder, name_of(oid) if oid else None, counts[oid]),
             (m.get("description") or "")[:DESCRIPTION_MAX] or None,
             taken_date(data) or batch["default_date"], next_sort[oid], include,
             m.get("source_url") or (source_urls or {}).get(name), m.get("licence")))


def queue_image(db, tenant_id, owner_id, data, file_name, title=None, description=None, source_url=None,
                batch_id=None, agent_name="AI Agent", user_id=None, kind="supplier"):
    """Puts one found image into an open 'AI Agent' batch for the owner, to
    be curated like any other upload. Returns the batch_id."""
    if batch_id is None:
        batch_id = create_batch(db, tenant_id, [], source="AI Agent", contributor=agent_name, owner_id=owner_id,
                                user_id=user_id, upload_name=f"{agent_name} search", kind=kind)
    add_items(db, tenant_id, batch_id, [(file_name, data)],
              meta={file_name: {"owner_id": owner_id, "title": title, "description": description,
                                "source_url": source_url}})
    db.commit()
    return batch_id


# ---- curation -------------------------------------------------------------------------------------

def batch_items(db, tenant_id, batch_id):
    """The batch's items as dicts, each with owner_name."""
    batch = db.execute("SELECT * FROM image_import_batches WHERE batch_id = ?", (batch_id,)).fetchone()
    kind = batch_kind(batch)
    rows = [dict(r) for r in db.execute(
        """SELECT item_id, file_name, file_size, owner_id, match_how, match_note, duplicate_note, title, description,
                  image_date, sort_order, include, status, source_url, licence, image_ref
           FROM image_import_items WHERE batch_id = ? AND tenant_id = ?""", (batch_id, tenant_id))]
    names = {}
    for r in rows:
        oid = r["owner_id"]
        if oid and oid not in names:
            o = kind.owner(db, tenant_id, oid)
            names[oid] = o["name"] if o else None
        r["owner_name"] = names.get(oid)
    rows.sort(key=lambda r: ((r["owner_name"] or "zzz").lower(), r["sort_order"] or 0, r["item_id"]))
    return rows


def item_state(item):
    """Which filter tab an item belongs to."""
    if item["status"] == "added":
        return "added"
    if item["status"] == "rejected":
        return "rejected"
    if item["duplicate_note"]:
        return "duplicate"
    if not item["owner_id"]:
        return "unmatched"
    if item["match_how"] == "similar":
        return "check"
    return "ready"


def save_edits(db, tenant_id, batch_id, form, allowed_owners, with_licence=False):
    """Store the curation form's values on the batch's pending items."""
    for item in db.execute("SELECT item_id, owner_id, match_how FROM image_import_items "
                           "WHERE batch_id = ? AND tenant_id = ? AND status = 'pending'", (batch_id, tenant_id)).fetchall():
        k = item["item_id"]
        if f"title_{k}" not in form:
            continue
        oid = form.get(f"owner_{k}")
        oid = int(oid) if oid and oid.isdigit() and int(oid) in allowed_owners else None
        how = item["match_how"]
        if oid != item["owner_id"] or (how == "similar" and oid):
            how = "manual" if oid else None
        try:
            sort = int(form.get(f"sort_{k}") or 0)
        except ValueError:
            sort = 0
        img_date = (form.get(f"date_{k}") or "").strip() or None
        if img_date and not re.match(r"^\d{4}-\d{2}-\d{2}$", img_date):
            img_date = None
        db.execute(
            """UPDATE image_import_items SET owner_id = ?, match_how = ?, title = ?, description = ?, image_date = ?,
                   sort_order = ?, include = ?, match_note = CASE WHEN ? IN ('manual','exact','entity') THEN NULL ELSE match_note END
               WHERE item_id = ?""",
            (oid, how, (form.get(f"title_{k}") or "").strip() or None,
             (form.get(f"description_{k}") or "").strip()[:DESCRIPTION_MAX] or None, img_date, sort,
             1 if form.get(f"include_{k}") else 0, how, k))
        if with_licence:
            db.execute("UPDATE image_import_items SET licence = ? WHERE item_id = ?",
                       ((form.get(f"licence_{k}") or "").strip()[:200] or None, k))
    db.commit()


def approve(db, tenant_id, batch_id):
    """Add every pending, ticked item that has an owner to that owner's
    album. Returns (added, problems)."""
    batch = db.execute("SELECT * FROM image_import_batches WHERE batch_id = ? AND tenant_id = ?",
                       (batch_id, tenant_id)).fetchone()
    kind = batch_kind(batch)
    added, problems, touched = 0, [], set()
    for it in db.execute("SELECT * FROM image_import_items WHERE batch_id = ? AND tenant_id = ? AND status = 'pending' "
                         "AND include = 1", (batch_id, tenant_id)).fetchall():
        if not it["owner_id"]:
            problems.append(f"{it['file_name']}: choose a {kind.singular} first")
            continue
        it = dict(it)
        it["title"] = it["title"] or os.path.splitext(it["file_name"])[0]
        ref = kind.insert(db, tenant_id, it["owner_id"], it, batch)
        # The bytes now live in the album; drop the staged copy.
        db.execute("UPDATE image_import_items SET status = 'added', image_ref = ?, file_data = X'', thumb_data = NULL "
                   "WHERE item_id = ?", (ref, it["item_id"]))
        touched.add(it["owner_id"])
        added += 1
    _close_if_done(db, batch_id)
    if touched:
        kind.after_change(db, sorted(touched))
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


def open_batches(db, tenant_id, kind="supplier"):
    kind = _kind(kind)
    rows = [dict(r) for r in db.execute(
        """SELECT b.*,
                  (SELECT COUNT(*) FROM image_import_items i WHERE i.batch_id = b.batch_id) AS total,
                  (SELECT COUNT(*) FROM image_import_items i WHERE i.batch_id = b.batch_id AND i.status = 'pending') AS pending,
                  (SELECT COUNT(*) FROM image_import_items i WHERE i.batch_id = b.batch_id AND i.status = 'added') AS added
           FROM image_import_batches b WHERE b.tenant_id = ? AND b.owner_kind = ?
           ORDER BY (b.status = 'open') DESC, b.batch_id DESC LIMIT 30""", (tenant_id, kind.key))]
    for r in rows:
        o = kind.owner(db, tenant_id, r["owner_id"]) if r["owner_id"] else None
        r["owner_name"] = o["name"] if o else None
    return rows


def pending_for_owner(db, tenant_id, kind, owner_id):
    """[(batch_id, n)] open uploads with images waiting for this owner."""
    return db.execute("""SELECT b.batch_id, COUNT(*) AS n FROM image_import_items i
                         JOIN image_import_batches b ON b.batch_id = i.batch_id
                         WHERE i.tenant_id = ? AND i.status = 'pending' AND i.owner_id = ? AND b.owner_kind = ?
                           AND b.status = 'open' GROUP BY b.batch_id ORDER BY b.batch_id DESC""",
                      (tenant_id, owner_id, _kind(kind).key)).fetchall()


# ---- albums -----------------------------------------------------------------------------------------

SORT_LABELS = {"album": "Album order", "newest": "Date — newest first", "oldest": "Date — oldest first",
               "title": "Title A–Z"}


def album(db, tenant_id, owner_id, q="", sort="album", date_from=None, date_to=None, kind="supplier"):
    """The owner's album rows (see image_owners.py for the keys)."""
    return _kind(kind).album(db, tenant_id, owner_id, q=q, sort=sort, date_from=date_from, date_to=date_to)


def document_thumb(db, tenant_id, document_id):
    """(bytes, mime) of a supplier album image's thumbnail, making and
    caching it the first time (images added before the Images Catalog have
    none)."""
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


def backfill_poi_hashes(db):
    """The same for POI images stored in the database."""
    for r in db.execute("SELECT poi_image_id, file_data FROM poi_images WHERE content_hash IS NULL "
                        "AND file_data IS NOT NULL AND length(file_data) > 0").fetchall():
        db.execute("UPDATE poi_images SET content_hash = ? WHERE poi_image_id = ?", (sha256(r[1]), r[0]))
