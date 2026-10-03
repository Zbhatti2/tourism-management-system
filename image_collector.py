"""
AI Image Collector -- a tenant-level agent that looks for photos of the
tenant's Hotels, Resorts and Restaurants on the web and stages them in the
Images Catalog curation step (image_catalog.py). Nothing reaches an album
without the tenant curating it, and every image keeps the page it was found
on. The tenant pays for its runs (ai_usage.py meters every API call).

One run covers one or more suppliers, one after the other, in a background
thread so the app stays responsive (the production server runs a single
worker). For each supplier:

1. Find pages (Claude + the web search tool): the official website and its
   gallery / rooms / dining pages, plus the property's page on its brand
   site or the local tourism board. Booking sites and user-review sites
   are avoided, since their photos are mostly guests' own.
2. Read those pages and collect the image addresses on them (social-share
   image, <img> tags including lazy-loaded and srcset variants, links to
   image files). Logos, icons, maps and tiny images are dropped.
3. Download the candidates; keep real photos (at least 600 x 400 px) that
   aren't already in the album.
4. Review (Claude, vision): keep photos that actually show this property
   (exterior, lobby, rooms, dining, pool, views...) and suggest a Title and
   Description for each.
5. Stage the kept photos in one curation upload (source 'AI Agent'), each
   with its source page.

The same agent also runs at platform level, as a TMS Agent, for the POI
Master Image Catalog (owner kind 'platform_poi'): it prefers the site's
official website, tourism boards and Wikimedia Commons, notes each photo's
licence / credit where the page states it, uses the platform API key and is
metered as a platform cost (tenant NULL). Its photos wait for SystemAdmin
curation like any upload.
"""
import base64
import io
import json
import re
import threading
import traceback
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import agent_runs
import ai_usage
import image_catalog as ic
from image_owners import KINDS

FEATURE = "Image Collector"
AGENT_NAME = "AI Image Collector"
MAX_PAGES = 5
MAX_CANDIDATES = 30          # image addresses tried per supplier
MAX_REVIEW = 16              # photos sent for review per supplier
MIN_W, MIN_H = 600, 400
PAGES_BUDGET_SECONDS = 90       # reading pages, per supplier / POI
DOWNLOAD_BUDGET_SECONDS = 150   # downloading candidate photos, per supplier / POI
MAX_IMAGE_BYTES = 12 * 1024 * 1024
SKIP_WORDS = ("logo", "icon", "sprite", "favicon", "avatar", "badge", "flag", "map", "placeholder", "blank",
              "spinner", "loader", "tripadvisor", "button", "banner-ad", "payment", "qr")
POI_FEATURE = "POI Image Collector"
POI_AGENT_NAME = "TMS POI Image Collector"
AVOID_SITES = ("booking.com", "tripadvisor.", "agoda.", "expedia.", "hotels.com", "trivago.", "kayak.",
               "facebook.com", "instagram.com", "pinterest.", "yelp.")

DDL = """
CREATE TABLE IF NOT EXISTS image_agent_runs (
    run_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    user_id         INTEGER REFERENCES users(user_id),
    scope_label     TEXT NOT NULL,          -- e.g. 'Avari Hotel Lahore' or 'Hotels in Lahore (8)'
    supplier_ids    TEXT NOT NULL,          -- JSON list
    per_supplier    INTEGER NOT NULL DEFAULT 8,
    status          TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','done','failed')),
    progress        TEXT,                   -- what it's doing / did, line per supplier
    batch_id        INTEGER REFERENCES image_import_batches(batch_id),
    images_found    INTEGER NOT NULL DEFAULT 0,
    images_staged   INTEGER NOT NULL DEFAULT 0,
    error           TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    started_at      TEXT,
    finished_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_image_agent_runs_tenant ON image_agent_runs(tenant_id);
"""


class CollectorError(Exception):
    pass


def explain_api_error(e):
    """A plain-language message for API errors that stop the whole run
    (None for anything else, which only affects one supplier)."""
    text = str(e)
    status = getattr(e, "status_code", None)
    if status == 401 or "authentication_error" in text or "x-api-key" in text:
        return ("The Anthropic API key on the server was rejected (invalid API key). Check ANTHROPIC_API_KEY in "
                "the server settings (Coolify > Environment Variables), then restart the app.")
    if status == 403 or "permission_error" in text:
        return "The Anthropic API key isn't allowed to use this model or the web search tool (permission error)."
    if status == 400 and "credit balance" in text.lower():
        return "The Anthropic account behind the API key is out of credit. Add credit in the Anthropic Console."
    if status == 404 or "not_found_error" in text:
        return f"The AI model wasn't found ({text[:160]}). Check ANTHROPIC_MODEL / ANTHROPIC_VISION_MODEL."
    return None


# ---- seams (replaced in tests) ------------------------------------------------------------------

def http_get(url, timeout=15, max_bytes=3 * 1024 * 1024, deadline=40):
    """(content bytes, content-type) or raises. timeout limits connecting and
    each wait for data; deadline limits the whole download, so a site that
    trickles data can't hang the run. The download runs in a helper thread
    because a blocking read can't be interrupted from inside."""
    holder = {}

    def work():
        try:
            holder["result"] = _download(url, timeout, max_bytes, holder)
        except Exception as e:  # noqa: BLE001 -- handed back to the caller
            holder["error"] = e

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(deadline)
    if t.is_alive():
        resp = holder.get("response")
        if resp is not None:
            threading.Thread(target=resp.close, daemon=True).start()
        raise CollectorError(f"took longer than {deadline} seconds")
    if "error" in holder:
        raise holder["error"]
    return holder["result"]


def _download(url, timeout, max_bytes, holder):
    import requests
    r = requests.get(url, timeout=(min(timeout, 10), timeout), stream=True, allow_redirects=True,
                     headers={"User-Agent": "Mozilla/5.0 (compatible; TMS-Image-Collector/1.0)"})
    holder["response"] = r
    try:
        r.raise_for_status()
        data = b""
        for chunk in r.iter_content(65536):
            data += chunk
            if len(data) > max_bytes:
                raise CollectorError("too large")
        return data, r.headers.get("Content-Type", "")
    finally:
        r.close()


def anthropic_client(platform=False):
    """Tenant runs use ANTHROPIC_API_KEY; platform (TMS Agent) runs use
    PLATFORM_ANTHROPIC_API_KEY, falling back to ANTHROPIC_API_KEY."""
    from config import Config
    key = ((getattr(Config, "PLATFORM_ANTHROPIC_API_KEY", None) if platform else None) or Config.ANTHROPIC_API_KEY)
    if not key:
        raise CollectorError("No Anthropic API key is configured on the server"
                             + (" (PLATFORM_ANTHROPIC_API_KEY or ANTHROPIC_API_KEY)." if platform else " (ANTHROPIC_API_KEY)."))
    try:
        import anthropic
    except ImportError as e:
        raise CollectorError("The 'anthropic' package isn't installed on the server.") from e
    return anthropic.Anthropic(api_key=key, timeout=agent_runs.API_TIMEOUT_SECONDS,
                               max_retries=agent_runs.API_MAX_RETRIES)


def models():
    from config import Config
    return Config.ANTHROPIC_MODEL, getattr(Config, "ANTHROPIC_VISION_MODEL", None) or Config.ANTHROPIC_MODEL


# ---- step 1: find pages ---------------------------------------------------------------------------------

def _tool_input(response, name):
    for b in getattr(response, "content", []) or []:
        if getattr(b, "type", None) == "tool_use" and getattr(b, "name", None) == name:
            return b.input or {}
    return None


def _poi_pages_prompt(t, place):
    return (
        f"Find web pages with good photographs of this point of interest ({(t.get('type_label') or 'place').lower()}): "
        f"\"{t['supplier_name']}\"{' in ' + place if place else ''}"
        f"{' (website: ' + t['web_page'] + ')' if t.get('web_page') else ''}.\n\n"
        "Prefer, in this order: the site's own official website or its managing authority; the national, provincial "
        "or city tourism board; Wikimedia Commons (its category or file pages for this place) and Wikipedia; "
        "reputable museums, heritage bodies and archives. Avoid booking and review sites, social media and stock-photo "
        "sellers. Make sure the pages are about this exact place in this city, not a namesake.\n\n"
        f"Use web search, then call report_pages with at most {MAX_PAGES} page URLs taken from the search results "
        "(never invent a URL). For each page, note the licence or credit terms of its photos if the page states them "
        "(e.g. 'CC BY-SA 4.0'). If you can't find this place, report no pages."
    )


def find_pages(client, model, supplier, meter):
    """[(url, why)] of pages likely to have photos of this supplier / POI."""
    place = ", ".join(x for x in (supplier["city"], supplier.get("country")) if x)
    prompt = _poi_pages_prompt(supplier, place) if supplier.get("kind") == "platform_poi" else (
        f"Find web pages with good photographs of this {supplier['type_label'].lower()}: "
        f"\"{supplier['supplier_name']}\"{' in ' + place if place else ''}"
        f"{' (website: ' + supplier['web_page'] + ')' if supplier.get('web_page') else ''}.\n\n"
        "Prefer, in this order: the property's own official website and its photo gallery, rooms, dining or "
        "facilities pages; the property's page on its hotel group / brand website; the local or national "
        "tourism board. Avoid booking and review sites (Booking.com, Tripadvisor, Agoda, Expedia and similar) "
        "and social media. Make sure the pages are about this exact property in this city, not a namesake.\n\n"
        f"Use web search, then call report_pages with at most {MAX_PAGES} page URLs taken from the search results "
        "(never invent a URL). If you can't find this property, report no pages."
    )
    tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3},
             {"name": "report_pages", "description": "Report the pages found that show photos of the property.",
              "input_schema": {"type": "object", "properties": {
                  "official_website": {"type": "string"},
                  "pages": {"type": "array", "items": {"type": "object", "properties": {
                      "url": {"type": "string"}, "why": {"type": "string"}, "licence": {"type": "string"}},
                      "required": ["url"]}},
                  "note": {"type": "string"}}, "required": ["pages"]}}]
    messages = [{"role": "user", "content": prompt}]
    found = None
    for _ in range(4):  # a long search can pause the turn; continue it
        resp = client.messages.create(model=model, max_tokens=2000, tools=tools, messages=messages)
        meter(model, getattr(resp, "usage", None))
        found = _tool_input(resp, "report_pages")
        if found is not None or getattr(resp, "stop_reason", None) != "pause_turn":
            break
        messages = messages + [{"role": "assistant", "content": resp.content}]
    if found is None:
        return [], "The search didn't identify any pages."
    pages, seen = [], set()
    licences = {}
    for p in ([{"url": found.get("official_website"), "why": "Official website"}] if found.get("official_website") else []) + \
            list(found.get("pages") or []):
        url = (p.get("url") or "").strip()
        host = urlparse(url).netloc.lower()
        if not url.startswith(("http://", "https://")) or url in seen or any(a in host for a in AVOID_SITES):
            continue
        seen.add(url)
        pages.append((url, (p.get("why") or "").strip()))
        if (p.get("licence") or "").strip():
            licences[url] = p["licence"].strip()[:200]
    supplier.setdefault("_licences", {}).update(licences)
    return pages[:MAX_PAGES + 1], (found.get("note") or "").strip()


# ---- step 2: image addresses on a page ---------------------------------------------------------------------

class _ImageLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.found = []   # (url, alt)

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta" and a.get("property", a.get("name", "")).lower() in ("og:image", "twitter:image", "og:image:url"):
            self.found.append((a.get("content", ""), "share image"))
        elif tag in ("img", "source"):
            alt = a.get("alt", "") or a.get("title", "")
            for key in ("data-src", "data-lazy-src", "data-original", "src"):
                if a.get(key) and not a[key].startswith("data:"):
                    self.found.append((a[key], alt))
                    break
            srcset = a.get("srcset") or a.get("data-srcset")
            if srcset:
                best = max((part.strip().split(" ") for part in srcset.split(",") if part.strip()),
                           key=lambda x: int(re.sub(r"\D", "", x[1]) or 0) if len(x) > 1 else 0, default=None)
                if best:
                    self.found.append((best[0], alt))
        elif tag == "a" and re.search(r"\.(jpe?g|png|webp)(\?|$)", a.get("href", ""), re.I):
            self.found.append((a["href"], a.get("title", "")))


def image_links(html, base_url):
    p = _ImageLinks()
    try:
        p.feed(html)
    except Exception:
        pass
    out, seen = [], set()
    for src, alt in p.found:
        url = urljoin(base_url, src.strip())
        low = url.lower()
        if not url.startswith(("http://", "https://")) or url in seen or low.split("?")[0].endswith((".svg", ".gif", ".ico")):
            continue
        if any(w in low for w in SKIP_WORDS):
            continue
        seen.add(url)
        out.append((url, alt.strip()))
    return out


# ---- step 3: download --------------------------------------------------------------------------------------------

def download_photos(candidates, known_hashes, log, budget=DOWNLOAD_BUDGET_SECONDS):
    """[(data, url, page, alt, w, h)] of real photos not already known."""
    import time
    from PIL import Image
    photos, hashes = [], set(known_hashes)
    started = time.monotonic()
    for url, page, alt in candidates[:MAX_CANDIDATES]:
        if time.monotonic() - started > budget:
            log(f"stopped downloading after {budget} seconds")
            break
        try:
            data, ctype = http_get(url, max_bytes=MAX_IMAGE_BYTES)
        except Exception:
            continue
        if ctype and "image" not in ctype.lower():
            continue
        try:
            w, h = Image.open(io.BytesIO(data)).size
        except Exception:
            continue
        if max(w, h) < MIN_W or min(w, h) < MIN_H:
            continue
        digest = ic.sha256(data)
        if digest in hashes:
            continue
        hashes.add(digest)
        photos.append((data, url, page, alt, w, h))
    log(f"{len(photos)} usable photo(s) downloaded")
    return photos


# ---- step 4: review ---------------------------------------------------------------------------------------------------

def review_photos(client, model, supplier, photos, meter):
    """{index: {"title", "description"}} for photos worth keeping."""
    if not photos:
        return {}
    if supplier.get("kind") == "platform_poi":
        intro = (
            f"These are candidate photos for the Master Image Catalog of the point of interest "
            f"\"{supplier['supplier_name']}\"{' in ' + supplier['city'] if supplier.get('city') else ''} "
            f"({(supplier.get('type_label') or 'place').lower()}), found on the web, for tour operators' brochures. "
            "Keep a photo only if it plausibly shows this place: its exterior, architecture, interior, grounds, "
            "notable details or the view of it. Reject logos, maps, illustrations, text graphics, generic stock images, "
            "other places, and photos where people are the subject. For each photo you keep, give a short Title "
            "(2-6 words, e.g. 'Main gate at sunset') and a one-sentence Description (under 180 characters) describing "
            "only what is visible. Call review_photos.")
    else:
        intro = None
    content = [{"type": "text", "text": intro or (
        f"These are candidate photos for the Images Catalog of the {supplier['type_label'].lower()} "
        f"\"{supplier['supplier_name']}\"{' in ' + supplier['city'] if supplier.get('city') else ''}, found on the web. "
        "Keep a photo only if it plausibly shows this property: its building, entrance, lobby, rooms, bathrooms, "
        "restaurant or food, pool, spa, gardens, meeting rooms, or the view from it. Reject logos, maps, "
        "illustrations, text graphics, generic stock images, other places, and close-ups of people. "
        "For each photo you keep, give a short Title (2-6 words, e.g. 'Deluxe King Room') and a one-sentence "
        "Description (under 180 characters) describing only what is visible. Call review_photos.")}]
    for i, (data, *_rest) in enumerate(photos):
        thumb = ic.make_thumb(data)  # small JPEG: cheaper to review than the full photo
        if not thumb:
            continue
        content.append({"type": "text", "text": f"Photo {i}:"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                    "data": base64.b64encode(thumb).decode("ascii")}})
    tool = {"name": "review_photos", "description": "Decide which photos to keep, with a title and description.",
            "input_schema": {"type": "object", "properties": {"photos": {"type": "array", "items": {
                "type": "object", "properties": {"index": {"type": "integer"}, "keep": {"type": "boolean"},
                                                 "title": {"type": "string"}, "description": {"type": "string"}},
                "required": ["index", "keep"]}}}, "required": ["photos"]}}
    resp = client.messages.create(model=model, max_tokens=2500, tools=[tool],
                                  tool_choice={"type": "tool", "name": "review_photos"},
                                  messages=[{"role": "user", "content": content}])
    meter(model, getattr(resp, "usage", None))
    result = _tool_input(resp, "review_photos") or {}
    keep = {}
    for p in result.get("photos") or []:
        i = p.get("index")
        if isinstance(i, int) and 0 <= i < len(photos) and p.get("keep"):
            keep[i] = {"title": (p.get("title") or "").strip()[:150] or None,
                       "description": (p.get("description") or "").strip()[:ic.DESCRIPTION_MAX] or None}
    return keep


# ---- one supplier ---------------------------------------------------------------------------------------------------

def _slug(text):
    return re.sub(r"[^A-Za-z0-9]+", " ", text or "").strip()[:60] or "photo"


def collect_for_supplier(db, client, tenant_id, supplier, per_supplier, meter, log):
    """[(file name, data, meta)] staged-ready photos for one supplier (or,
    with supplier["kind"] = 'platform_poi', one platform POI)."""
    search_model, vision_model = models()
    pages, note = find_pages(client, search_model, supplier, meter)
    if not pages:
        log(f"no pages found{': ' + note if note else ''}")
        return []
    log(f"{len(pages)} page(s): " + ", ".join(urlparse(u).netloc + urlparse(u).path[:30] for u, _ in pages))
    import time
    candidates, seen = [], set()
    started = time.monotonic()
    for url, _why in pages:
        if time.monotonic() - started > PAGES_BUDGET_SECONDS:
            log(f"stopped reading pages after {PAGES_BUDGET_SECONDS} seconds")
            break
        try:
            html, ctype = http_get(url)
        except Exception as e:
            log(f"couldn't read {url} ({e.__class__.__name__})")
            continue
        if "html" not in (ctype or "html").lower():
            continue
        for img, alt in image_links(html.decode("utf-8", "replace"), url):
            if img not in seen:
                seen.add(img)
                candidates.append((img, url, alt))
    if not candidates:
        log("no images on those pages")
        return []
    kind = KINDS[supplier.get("kind") or "supplier"]
    oid = supplier["supplier_id"]
    known = {h for (o, h) in kind.hashes(db, tenant_id) if o == oid}
    known |= {r[0] for r in db.execute(
        "SELECT i.content_hash FROM image_import_items i JOIN image_import_batches b ON b.batch_id = i.batch_id "
        "WHERE i.owner_id = ? AND i.tenant_id = ? AND i.status = 'pending' AND b.owner_kind = ?", (oid, tenant_id, kind.key))}
    photos = download_photos(candidates, known, log)
    # Largest first: gallery photos beat thumbnails.
    photos.sort(key=lambda p: -(p[4] * p[5]))
    photos = photos[:MAX_REVIEW]
    keep = review_photos(client, vision_model, supplier, photos, meter)
    out = []
    for n, i in enumerate(sorted(keep)[:per_supplier], start=1):
        data, url, page, alt, _w, _h = photos[i]
        title = keep[i]["title"] or alt or None
        name = f"{_slug(supplier['supplier_name'])}-{_slug(title)}-{n}.jpg"
        if ic.mime_for(url.split("?")[0]) == "image/png":
            name = name[:-4] + ".png"
        elif ic.mime_for(url.split("?")[0]) == "image/webp":
            name = name[:-4] + ".webp"
        out.append((name, data, {"owner_id": oid, "title": title, "description": keep[i]["description"],
                                 "source_url": page if page else url,
                                 "licence": (supplier.get("_licences") or {}).get(page)}))
    log(f"kept {len(out)} of {len(photos)} after review")
    return out


# ---- runs -------------------------------------------------------------------------------------------------------------------

def suppliers_for(db, tenant_id, ids):
    rows = {s["supplier_id"]: s for s in ic.album_suppliers(db, tenant_id)}
    out = []
    for sid in ids:
        s = rows.get(sid)
        if s is None:
            continue
        extra = db.execute("""SELECT s.web_page, co.label AS country FROM suppliers s
                              LEFT JOIN supplier_addresses a ON a.supplier_address_id = (
                                  SELECT x.supplier_address_id FROM supplier_addresses x WHERE x.supplier_id = s.supplier_id
                                  ORDER BY x.is_primary DESC, x.supplier_address_id LIMIT 1)
                              LEFT JOIN countries co ON co.country_id = a.country_id
                              WHERE s.supplier_id = ?""", (sid,)).fetchone()
        out.append(dict(s, web_page=extra["web_page"] if extra else None, country=extra["country"] if extra else None))
    return out


def platform_pois_for(db, ids):
    """Platform POIs in the same shape as suppliers_for() (supplier_id /
    supplier_name are the POI's id and name)."""
    out = []
    for pid in ids:
        r = db.execute("""SELECT p.poi_id, p.name, p.website, COALESCE(c.label, p.city_text) AS city, co.label AS country,
                                 pt.label AS type_label
                          FROM platform_pois p LEFT JOIN cities c ON c.city_id = p.city_id
                          LEFT JOIN countries co ON co.country_id = p.country_id
                          LEFT JOIN poi_types pt ON pt.poi_type_id = p.poi_type_id
                          WHERE p.poi_id = ? AND p.is_active = 1""", (pid,)).fetchone()
        if r:
            out.append({"supplier_id": r["poi_id"], "supplier_name": r["name"], "web_page": r["website"],
                        "city": r["city"], "country": r["country"], "type_label": r["type_label"] or "Point of Interest",
                        "kind": "platform_poi"})
    return out


def targets_for(db, kind, tenant_id, ids):
    return platform_pois_for(db, ids) if kind == "platform_poi" else suppliers_for(db, tenant_id, ids)


def start_run(db, tenant_id, user_id, supplier_ids, per_supplier=8, scope_label=None, background=True, kind="supplier"):
    """Create a run and start it. Returns run_id. Raises CollectorError
    (no API key, allowance used up, nothing to do). kind='platform_poi':
    a TMS Agent run for the POI Master Image Catalog; tenant_id is then the
    TMS Platform tenant and the cost is a platform cost."""
    platform = kind == "platform_poi"
    if not platform:
        over = ai_usage.check_limit(db, tenant_id)
        if over:
            raise CollectorError(over)
    anthropic_client(platform)  # fail now, not in the background, if the API isn't configured
    sups = targets_for(db, kind, tenant_id, supplier_ids)
    if not sups:
        raise CollectorError("Choose at least one Point of Interest." if platform
                             else "Choose at least one Hotel, Resort or Restaurant.")
    label = scope_label or (sups[0]["supplier_name"] if len(sups) == 1 else f"{len(sups)} {'POIs' if platform else 'suppliers'}")
    cur = db.execute("INSERT INTO image_agent_runs (tenant_id, user_id, scope_label, supplier_ids, per_supplier, owner_kind, "
                     "items_total) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (tenant_id, user_id, label, json.dumps([s["supplier_id"] for s in sups]),
                      max(1, min(int(per_supplier or 8), 20)), kind, len(sups)))
    db.commit()
    run_id = cur.lastrowid
    if background:
        threading.Thread(target=_run_in_thread, args=(run_id,), daemon=True).start()
    else:
        _run_in_thread(run_id)
    return run_id


def _connect():
    import sqlite3
    from config import Config
    db = sqlite3.connect(Config.DATABASE_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def _run_in_thread(run_id):
    db = _connect()
    try:
        run_collection(db, run_id)
    finally:
        db.close()


def run_collection(db, run_id):
    run = db.execute("SELECT * FROM image_agent_runs WHERE run_id = ?", (run_id,)).fetchone()
    tenant_id = run["tenant_id"]
    kind = run["owner_kind"] if "owner_kind" in run.keys() and run["owner_kind"] else "supplier"
    platform = kind == "platform_poi"
    lines = []

    def log(text):
        lines.append(text)
        db.execute("UPDATE image_agent_runs SET progress = ?, heartbeat_at = datetime('now') WHERE run_id = ?",
                   ("\n".join(lines)[-6000:], run_id))
        db.commit()

    def meter(model, usage):
        ai_usage.record(db, None if platform else tenant_id, POI_FEATURE if platform else FEATURE, model, usage,
                        user_id=run["user_id"], ref_type="image_agent_runs", ref_id=run_id, note=current["name"])

    current = {"name": run["scope_label"]}
    db.execute("UPDATE image_agent_runs SET status = 'running', started_at = datetime('now'), heartbeat_at = datetime('now') "
               "WHERE run_id = ? AND status = 'queued'", (run_id,))
    db.commit()
    try:
        client = anthropic_client(platform)
        sups = targets_for(db, kind, tenant_id, json.loads(run["supplier_ids"]))
        single = sups[0]["supplier_id"] if len(sups) == 1 else None
        batch_id = None
        staged = failures = 0
        for done, s in enumerate(sups):
            db.execute("UPDATE image_agent_runs SET items_done = ?, heartbeat_at = datetime('now') WHERE run_id = ?",
                       (done, run_id))
            db.commit()
            if agent_runs.cancelled(db, "image_agent_runs", run_id):
                return
            current["name"] = s["supplier_name"]
            over = None if platform else ai_usage.check_limit(db, tenant_id)
            if over:
                log(f"Stopped before {s['supplier_name']}: {over}")
                break
            log(f"— {s['supplier_name']}{' (' + s['city'] + ')' if s.get('city') else ''}")
            try:
                files = collect_for_supplier(db, client, tenant_id, s, run["per_supplier"], meter,
                                             lambda t: log("   " + t))
            except Exception as e:
                fatal = explain_api_error(e)
                if fatal:  # a bad key or empty account fails every supplier: stop and say why
                    raise CollectorError(fatal) from e
                log(f"   failed: {e}")  # one supplier failing doesn't stop the others
                failures += 1
                continue
            if not files:
                continue
            if batch_id is None:
                agent = POI_AGENT_NAME if platform else AGENT_NAME
                batch_id = ic.create_batch(db, tenant_id, [], source="AI Agent", contributor=agent,
                                           owner_id=single, user_id=run["user_id"],
                                           upload_name=f"{agent}: {run['scope_label']}", kind=kind)
                db.execute("UPDATE image_agent_runs SET batch_id = ? WHERE run_id = ?", (batch_id, run_id))
            ic.add_items(db, tenant_id, batch_id, [(n, d) for n, d, _m in files], meta={n: m for n, _d, m in files})
            staged += len(files)
            db.execute("UPDATE image_agent_runs SET images_staged = ? WHERE run_id = ?", (staged, run_id))
            db.commit()
        if failures and failures == len(sups):
            raise CollectorError(f"Every {'POI' if platform else 'supplier'} in this run failed; see Progress for the reasons.")
        if agent_runs.cancelled(db, "image_agent_runs", run_id):
            return
        log(f"Done: {staged} photo(s) ready to curate." if staged else "Done: no new photos found.")
        db.execute("UPDATE image_agent_runs SET status = 'done', items_done = items_total, finished_at = datetime('now') "
                   "WHERE run_id = ? AND status = 'running'", (run_id,))
        db.commit()
    except Exception as e:
        msg = explain_api_error(e) or f"{e}"
        try:
            log(f"Stopped: {msg}")
        except Exception:
            pass
        db.execute("UPDATE image_agent_runs SET status = 'failed', error = ?, finished_at = datetime('now') "
                   "WHERE run_id = ? AND status = 'running'", (msg[:1000] or traceback.format_exc()[-1000:], run_id))
        db.commit()


def mark_stale(db, tenant_id):
    """Runs that stopped responding (a hung request, or a server restart)
    are marked failed -- see agent_runs.py."""
    if tenant_id is None:
        return
    agent_runs.mark_stale(db, "image_agent_runs", " AND tenant_id = ?", (tenant_id,))


def run_cost(db, run_id):
    return db.execute("SELECT COALESCE(SUM(cost_usd), 0), COUNT(*), SUM(web_searches) FROM ai_usage_log "
                      "WHERE ref_type = 'image_agent_runs' AND ref_id = ?", (run_id,)).fetchone()
