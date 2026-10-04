"""
Additional Links for Points of Interest (Zeb, Oct 2026): "one section for
'Additional Links' to type in a URL, and Type (Video / Images /
Documents)" -- replacing the platform POI's single Image URL and Video URL
fields.

platform_poi_links: any number of typed links per platform POI. The old
image_url / video_url values are moved here by the migration (the columns
stay, unused). A tenant's POI keeps its links in poi_reference_links, which
gains the same link_type; catalog sync copies a platform POI's links to
the tenant's copy (adding ones it doesn't have, never removing any).
"""
import re

LINK_TYPES = [("video", "Video"), ("images", "Images"), ("document", "Document"), ("other", "Other")]
LINK_TYPE = dict(LINK_TYPES)

DDL = """
CREATE TABLE IF NOT EXISTS platform_poi_links (
    link_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    poi_id          INTEGER NOT NULL REFERENCES platform_pois(poi_id),
    url             TEXT NOT NULL,
    link_type       TEXT NOT NULL DEFAULT 'other',  -- video / images / document / other
    title           TEXT,
    source          TEXT,                   -- where the link came from (a PDF and page, an import...)
    sort_order      INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_platform_poi_links_poi ON platform_poi_links(poi_id);
"""


def norm_url(url):
    """For spotting the same link twice: no scheme, www, trailing slash or case."""
    u = (url or "").strip().lower()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    return u.rstrip("/")


def clean_url(url):
    u = (url or "").strip()
    if u and not re.match(r"^[a-z][a-z0-9+.-]*://", u, re.I):
        u = "https://" + u
    return u


def guess_type(url, hint=""):
    text = f"{url} {hint}".lower()
    if any(k in text for k in ("youtu", "vimeo", "video", "watch", "tiktok", "facebook.com/watch")):
        return "video"
    if re.search(r"\.(jpe?g|png|webp|gif)(\?|$)", text) or any(k in text for k in ("flickr", "commons.wikimedia",
                                                                                     "photo", "gallery", "image")):
        return "images"
    if re.search(r"\.(pdf|docx?|pptx?)(\?|$)", text) or "document" in text:
        return "document"
    return "other"


def links(db, poi_id):
    return db.execute("SELECT * FROM platform_poi_links WHERE poi_id = ? ORDER BY sort_order, link_id",
                      (poi_id,)).fetchall()


def add(db, poi_id, url, link_type=None, title=None, source=None):
    """Add a link unless the POI already has it. Returns True if added."""
    url = clean_url(url)
    if not url:
        return False
    have = {norm_url(r["url"]) for r in links(db, poi_id)}
    if norm_url(url) in have:
        return False
    n = db.execute("SELECT COALESCE(MAX(sort_order), 0) FROM platform_poi_links WHERE poi_id = ?", (poi_id,)).fetchone()[0]
    db.execute("INSERT INTO platform_poi_links (poi_id, url, link_type, title, source, sort_order) VALUES (?, ?, ?, ?, ?, ?)",
               (poi_id, url, link_type if link_type in LINK_TYPE else guess_type(url, title or ""),
                (title or "").strip() or None, source, n + 1))
    return True


def from_form(form):
    """[(url, type, title)] from the Additional Links rows of a form."""
    out = []
    for url, typ, title in zip(form.getlist("link_url"), form.getlist("link_type"), form.getlist("link_title")):
        url = clean_url(url)
        if url:
            out.append((url, typ if typ in LINK_TYPE else guess_type(url, title), (title or "").strip() or None))
    return out


def save_from_form(db, poi_id, form):
    """Replace a platform POI's links with the form's rows (keeping each
    kept link's source)."""
    sources = {norm_url(r["url"]): r["source"] for r in links(db, poi_id)}
    db.execute("DELETE FROM platform_poi_links WHERE poi_id = ?", (poi_id,))
    seen = set()
    for i, (url, typ, title) in enumerate(from_form(form), 1):
        if norm_url(url) in seen:
            continue
        seen.add(norm_url(url))
        db.execute("INSERT INTO platform_poi_links (poi_id, url, link_type, title, source, sort_order) "
                   "VALUES (?, ?, ?, ?, ?, ?)", (poi_id, url, typ, title, sources.get(norm_url(url)), i))


def migrate_old_columns(db):
    """image_url / video_url -> links."""
    for r in db.execute("SELECT poi_id, image_url, video_url FROM platform_pois "
                        "WHERE COALESCE(image_url, '') <> '' OR COALESCE(video_url, '') <> ''").fetchall():
        if r["image_url"]:
            add(db, r["poi_id"], r["image_url"], "images", None, "Image URL field")
        if r["video_url"]:
            add(db, r["poi_id"], r["video_url"], "video", None, "Video URL field")
    db.execute("UPDATE platform_pois SET image_url = NULL, video_url = NULL "
               "WHERE image_url IS NOT NULL OR video_url IS NOT NULL")


def copy_to_tenant(db, tenant_id, local_poi_id, catalog_id):
    """A tenant's copy of a platform POI gets the platform's links it
    doesn't have yet (the tenant's own links are never touched)."""
    have = {norm_url(r[0]) for r in db.execute(
        "SELECT url FROM poi_reference_links WHERE poi_id = ? AND tenant_id = ?", (local_poi_id, tenant_id))}
    n = db.execute("SELECT COALESCE(MAX(sort_order), 0) FROM poi_reference_links WHERE poi_id = ? AND tenant_id = ?",
                   (local_poi_id, tenant_id)).fetchone()[0]
    added = 0
    for r in links(db, catalog_id):
        if norm_url(r["url"]) in have:
            continue
        n += 1
        db.execute("INSERT INTO poi_reference_links (tenant_id, poi_id, url, description, link_type, sort_order) "
                   "VALUES (?, ?, ?, ?, ?, ?)", (tenant_id, local_poi_id, r["url"], r["title"], r["link_type"], n))
        have.add(norm_url(r["url"]))
        added += 1
    return added
