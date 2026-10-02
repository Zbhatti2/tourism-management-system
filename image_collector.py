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
"""
import base64
import io
import json
import re
import threading
import traceback
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import ai_usage
import image_catalog as ic

FEATURE = "Image Collector"
AGENT_NAME = "AI Image Collector"
MAX_PAGES = 5
MAX_CANDIDATES = 30          # image addresses tried per supplier
MAX_REVIEW = 16              # photos sent for review per supplier
MIN_W, MIN_H = 600, 400
MAX_IMAGE_BYTES = 12 * 1024 * 1024
SKIP_WORDS = ("logo", "icon", "sprite", "favicon", "avatar", "badge", "flag", "map", "placeholder", "blank",
              "spinner", "loader", "tripadvisor", "button", "banner-ad", "payment", "qr")
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

def http_get(url, timeout=15, max_bytes=3 * 1024 * 1024):
    """(content bytes, content-type) or raises."""
    import requests
    with requests.get(url, timeout=timeout, stream=True, allow_redirects=True,
                      headers={"User-Agent": "Mozilla/5.0 (compatible; TMS-Image-Collector/1.0)"}) as r:
        r.raise_for_status()
        data = b""
        for chunk in r.iter_content(65536):
            data += chunk
            if len(data) > max_bytes:
                raise CollectorError("too large")
        return data, r.headers.get("Content-Type", "")


def anthropic_client():
    from config import Config
    if not Config.ANTHROPIC_API_KEY:
        raise CollectorError("No ANTHROPIC_API_KEY is configured on the server.")
    try:
        import anthropic
    except ImportError as e:
        raise CollectorError("The 'anthropic' package isn't installed on the server.") from e
    return anthropic.Anthropic(api_key=Config.ANTHROPIC_API_KEY)


def models():
    from config import Config
    return Config.ANTHROPIC_MODEL, getattr(Config, "ANTHROPIC_VISION_MODEL", None) or Config.ANTHROPIC_MODEL


# ---- step 1: find pages ---------------------------------------------------------------------------------

def _tool_input(response, name):
    for b in getattr(response, "content", []) or []:
        if getattr(b, "type", None) == "tool_use" and getattr(b, "name", None) == name:
            return b.input or {}
    return None


def find_pages(client, model, supplier, meter):
    """[(url, why)] of pages likely to have photos of this supplier."""
    place = ", ".join(x for x in (supplier["city"], supplier.get("country")) if x)
    prompt = (
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
                      "url": {"type": "string"}, "why": {"type": "string"}}, "required": ["url"]}},
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
    for p in ([{"url": found.get("official_website"), "why": "Official website"}] if found.get("official_website") else []) + \
            list(found.get("pages") or []):
        url = (p.get("url") or "").strip()
        host = urlparse(url).netloc.lower()
        if not url.startswith(("http://", "https://")) or url in seen or any(a in host for a in AVOID_SITES):
            continue
        seen.add(url)
        pages.append((url, (p.get("why") or "").strip()))
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

def download_photos(candidates, known_hashes, log):
    """[(data, url, page, alt, w, h)] of real photos not already known."""
    from PIL import Image
    photos, hashes = [], set(known_hashes)
    for url, page, alt in candidates[:MAX_CANDIDATES]:
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
    content = [{"type": "text", "text": (
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
    """[(file name, data, meta)] staged-ready photos for one supplier."""
    search_model, vision_model = models()
    pages, note = find_pages(client, search_model, supplier, meter)
    if not pages:
        log(f"no pages found{': ' + note if note else ''}")
        return []
    log(f"{len(pages)} page(s): " + ", ".join(urlparse(u).netloc + urlparse(u).path[:30] for u, _ in pages))
    candidates, seen = [], set()
    for url, _why in pages:
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
    known = {r[0] for r in db.execute(
        "SELECT content_hash FROM supplier_documents WHERE supplier_id = ? AND tenant_id = ? AND is_deleted = 0 "
        "AND content_hash IS NOT NULL UNION SELECT content_hash FROM image_import_items WHERE supplier_id = ? "
        "AND tenant_id = ? AND status = 'pending'", (supplier["supplier_id"], tenant_id, supplier["supplier_id"], tenant_id))}
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
        out.append((name, data, {"supplier_id": supplier["supplier_id"], "title": title,
                                 "description": keep[i]["description"], "source_url": page if page else url}))
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


def start_run(db, tenant_id, user_id, supplier_ids, per_supplier=8, scope_label=None, background=True):
    """Create a run and start it. Returns run_id. Raises CollectorError
    (no API key, allowance used up, nothing to do)."""
    over = ai_usage.check_limit(db, tenant_id)
    if over:
        raise CollectorError(over)
    anthropic_client()  # fail now, not in the background, if the API isn't configured
    sups = suppliers_for(db, tenant_id, supplier_ids)
    if not sups:
        raise CollectorError("Choose at least one Hotel, Resort or Restaurant.")
    label = scope_label or (sups[0]["supplier_name"] if len(sups) == 1 else f"{len(sups)} suppliers")
    cur = db.execute("INSERT INTO image_agent_runs (tenant_id, user_id, scope_label, supplier_ids, per_supplier) "
                     "VALUES (?, ?, ?, ?, ?)", (tenant_id, user_id, label, json.dumps([s["supplier_id"] for s in sups]),
                                                max(1, min(int(per_supplier or 8), 20))))
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
    lines = []

    def log(text):
        lines.append(text)
        db.execute("UPDATE image_agent_runs SET progress = ? WHERE run_id = ?", ("\n".join(lines)[-6000:], run_id))
        db.commit()

    def meter(model, usage):
        ai_usage.record(db, tenant_id, FEATURE, model, usage, user_id=run["user_id"], ref_type="image_agent_runs",
                        ref_id=run_id, note=current["name"])

    current = {"name": run["scope_label"]}
    db.execute("UPDATE image_agent_runs SET status = 'running', started_at = datetime('now') WHERE run_id = ?", (run_id,))
    db.commit()
    try:
        client = anthropic_client()
        sups = suppliers_for(db, tenant_id, json.loads(run["supplier_ids"]))
        single = sups[0]["supplier_id"] if len(sups) == 1 else None
        batch_id = None
        staged = failures = 0
        for s in sups:
            current["name"] = s["supplier_name"]
            over = ai_usage.check_limit(db, tenant_id)
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
                batch_id = ic.create_batch(db, tenant_id, [], source="AI Agent", contributor=AGENT_NAME,
                                           supplier_id=single, user_id=run["user_id"],
                                           upload_name=f"{AGENT_NAME}: {run['scope_label']}")
                db.execute("UPDATE image_agent_runs SET batch_id = ? WHERE run_id = ?", (batch_id, run_id))
            ic.add_items(db, tenant_id, batch_id, [(n, d) for n, d, _m in files], meta={n: m for n, _d, m in files})
            staged += len(files)
            db.execute("UPDATE image_agent_runs SET images_staged = ? WHERE run_id = ?", (staged, run_id))
            db.commit()
        if failures and failures == len(sups):
            raise CollectorError("Every supplier in this run failed; see Progress for the reasons.")
        log(f"Done: {staged} photo(s) ready to curate." if staged else "Done: no new photos found.")
        db.execute("UPDATE image_agent_runs SET status = 'done', finished_at = datetime('now') WHERE run_id = ?", (run_id,))
        db.commit()
    except Exception as e:
        msg = explain_api_error(e) or f"{e}"
        try:
            log(f"Stopped: {msg}")
        except Exception:
            pass
        db.execute("UPDATE image_agent_runs SET status = 'failed', error = ?, finished_at = datetime('now') WHERE run_id = ?",
                   (msg[:1000] or traceback.format_exc()[-1000:], run_id))
        db.commit()


def mark_stale(db, tenant_id):
    """Runs cut off by a server restart: anything 'running' or 'queued' for
    over 30 minutes is marked failed."""
    db.execute("UPDATE image_agent_runs SET status = 'failed', error = 'Stopped (the server restarted).', "
               "finished_at = datetime('now') WHERE tenant_id = ? AND status IN ('queued','running') "
               "AND created_at < datetime('now', '-30 minutes')", (tenant_id,))
    db.commit()


def run_cost(db, run_id):
    return db.execute("SELECT COALESCE(SUM(cost_usd), 0), COUNT(*), SUM(web_searches) FROM ai_usage_log "
                      "WHERE ref_type = 'image_agent_runs' AND ref_id = ?", (run_id,)).fetchone()
