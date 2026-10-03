"""
TMS Agent: Points of Interest from a PDF (Zeb, Oct 2026) -- "extract images
and text from PDF files to add / update POIs at the platform level, similar
to AI searching the web and populating the table".

The SystemAdmin uploads a PDF (a brochure, guidebook, gazetteer, heritage
report...). The run:

1. Splits it into parts of up to PART_PAGES pages and sends each part to
   Claude as a PDF document (Claude reads the text and the page images, so
   scanned PDFs work too). Claude lists every Point of Interest the part
   describes, with the fields it states (description, year founded, hours,
   entry fee, website, phone, address, coordinates), its city and type, and
   the pages where it is the main subject.
2. Matches each place to the platform POI catalog (name, other spellings,
   city). For a known POI, each value that is new or different becomes a
   proposal in the TMS Agents review queue; for a new place, its fields are
   proposed as a *new* POI, created when its values are approved.
3. Extracts the photos embedded in the PDF and puts them in one Master Image
   Catalog upload for curation, each assigned to the POI whose pages it is
   on (photos on pages about a new POI can be assigned once it exists).

Every proposal's source is the uploaded PDF and its page, viewable from the
review screen. The cost is a platform cost like the other TMS Agents.
"""
import base64
import io
import json
import re

import agent_runs
import platform_agents as pa

PART_PAGES = 30
MAX_PAGES = 300
MAX_BYTES = 40 * 1024 * 1024
MIN_IMAGE_W, MIN_IMAGE_H = 400, 300
MAX_IMAGES = 300
AGENT_NAME = "TMS Agent: POIs from PDF"

DDL = """
CREATE TABLE IF NOT EXISTS platform_agent_uploads (
    upload_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER NOT NULL REFERENCES platform_agent_runs(run_id),
    file_name       TEXT NOT NULL,
    mime_type       TEXT,
    file_size       INTEGER,
    page_count      INTEGER,
    file_data       BLOB NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_platform_agent_uploads_run ON platform_agent_uploads(run_id);
"""

# Fields a new POI gets proposed with, on top of FIELDS["platform_pois"].
NEW_FIELDS = {"name": ("Name", "text"), "city": ("City", "text"), "poi_type": ("POI Type", "text")}
NEW_ENTITY = "platform_pois_new"


class PdfError(Exception):
    pass


# ---- reading the PDF ---------------------------------------------------------------------------------

def inspect(data):
    """Page count of a PDF; raises PdfError if it can't be read."""
    if b"%PDF" not in data[:1024]:
        raise PdfError("That file isn't a PDF.")
    try:
        import logging
        logging.getLogger("pypdf").setLevel(logging.ERROR)
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:
                raise PdfError("The PDF is password-protected.")
        n = len(reader.pages)
        if not n:
            raise PdfError("That PDF has no pages.")
        return n
    except PdfError:
        raise
    except Exception as e:
        raise PdfError(f"That file couldn't be read as a PDF ({e.__class__.__name__}).")


def parts(data, size=PART_PAGES):
    """[(first page number (1-based), last page number, pdf bytes)]."""
    from pypdf import PdfReader, PdfWriter
    reader = PdfReader(io.BytesIO(data))
    n = min(len(reader.pages), MAX_PAGES)
    out = []
    for start in range(0, n, size):
        w = PdfWriter()
        for i in range(start, min(start + size, n)):
            w.add_page(reader.pages[i])
        buf = io.BytesIO()
        w.write(buf)
        out.append((start + 1, min(start + size, n), buf.getvalue()))
    return out


def page_text(data, page_no):
    try:
        from pypdf import PdfReader
        return PdfReader(io.BytesIO(data)).pages[page_no - 1].extract_text() or ""
    except Exception:
        return ""


def extract_images(data, log=lambda t: None):
    """[(page number, file name, bytes, width, height)] of real photos in the
    PDF: big enough, not repeated (logos and page decorations repeat)."""
    from PIL import Image
    from pypdf import PdfReader
    import image_catalog as ic
    reader = PdfReader(io.BytesIO(data))
    found, seen, counts = [], set(), {}
    for pno, page in enumerate(reader.pages[:MAX_PAGES], start=1):
        try:
            images = list(page.images)
        except Exception:
            continue
        for k, img in enumerate(images, start=1):
            try:
                raw = img.data
                im = Image.open(io.BytesIO(raw))
                w, h = im.size
            except Exception:
                continue
            if max(w, h) < MIN_IMAGE_W or min(w, h) < MIN_IMAGE_H:
                continue
            digest = ic.sha256(raw)
            counts[digest] = counts.get(digest, 0) + 1
            if digest in seen:
                continue
            seen.add(digest)
            fmt = (im.format or "JPEG").upper()
            if fmt not in ("JPEG", "PNG", "WEBP"):
                buf = io.BytesIO()
                im.convert("RGB").save(buf, "JPEG", quality=88)
                raw, fmt = buf.getvalue(), "JPEG"
            ext = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}[fmt]
            found.append((pno, f"page-{pno:03d}-{k}.{ext}", raw, w, h, digest))
            if len(found) >= MAX_IMAGES:
                break
        if len(found) >= MAX_IMAGES:
            break
    # An image that appears on 3+ pages is a logo / decoration, not a photo of a place.
    photos = [(p, n, d, w, h) for p, n, d, w, h, dig in found if counts[dig] < 3]
    log(f"{len(photos)} photo(s) found in the PDF")
    return photos


# ---- asking Claude ---------------------------------------------------------------------------------------

def _tool():
    props = {f: {"type": "string"} for f in pa.FIELDS["platform_pois"]}
    props.update({
        "name": {"type": "string"}, "alt_names": {"type": "array", "items": {"type": "string"}},
        "city": {"type": "string"}, "country": {"type": "string"}, "poi_type": {"type": "string"},
        "main_pages": {"type": "array", "items": {"type": "integer"},
                       "description": "Page numbers (as printed in the page list) where this place is the main subject"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    })
    return {"name": "report_places", "description": "Report the points of interest described in this part of the PDF.",
            "input_schema": {"type": "object", "properties": {"places": {"type": "array", "items": {
                "type": "object", "properties": props, "required": ["name", "main_pages"]}}}, "required": ["places"]}}


def read_part(client, part_pdf, first, last, file_name, poi_types, meter):
    """[place dicts] for one part of the PDF; page numbers in the result are
    the original PDF's."""
    prompt = (
        f"This is pages {first}-{last} of the PDF \"{file_name}\". It is being used to build a tourism platform's "
        "catalog of Points of Interest (monuments, religious sites, museums, forts, parks, natural sites, markets, "
        "landmarks...).\n\n"
        "List every point of interest that these pages DESCRIBE (not ones only mentioned in passing). For each, give "
        "only what the document states: its name (as the document spells it, plus other spellings in alt_names), the "
        "city or nearest town, the country, its type (one of: " + ", ".join(poi_types[:40]) + ", or your own short "
        "type), a factual description of 2-4 sentences in your own words, year founded or era, opening days / hours, "
        "entry fee (with currency), website, phone, street address or landmark, and latitude / longitude in decimal "
        "degrees only if the document gives them. Leave out anything the document doesn't state.\n\n"
        f"main_pages: the page numbers ({first}-{last}, counting the first page of this part as {first}) where the "
        "place is the main subject -- photos on those pages will be attached to it. Give a confidence (0-1) that the "
        "details are stated clearly. Call report_places once.")
    content = [{"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                               "data": base64.b64encode(part_pdf).decode("ascii")}},
               {"type": "text", "text": prompt}]
    resp = agent_runs.create_message(client, model=pa.model(), max_tokens=8000, tools=[_tool()],
                                  tool_choice={"type": "tool", "name": "report_places"},
                                  messages=[{"role": "user", "content": content}])
    meter(getattr(resp, "usage", None))
    places = []
    for b in getattr(resp, "content", []) or []:
        if getattr(b, "type", None) == "tool_use" and getattr(b, "name", None) == "report_places":
            places = (b.input or {}).get("places") or []
    out = []
    for p in places:
        name = (p.get("name") or "").strip()
        if not name:
            continue
        pages = sorted({int(x) for x in (p.get("main_pages") or []) if str(x).lstrip("-").isdigit()
                        and first <= int(x) <= last})
        out.append(dict(p, name=name[:200], main_pages=pages))
    return out


# ---- matching to the catalog -------------------------------------------------------------------------------------

def catalog(db):
    from fuzzy import split_alt_names
    return [{"id": r["poi_id"], "name": r["name"], "alt": split_alt_names(r["alt_names"]),
             "city": (r["city_label"] or r["city_text"] or ""), "row": r}
            for r in db.execute("""SELECT p.*, ci.label AS city_label FROM platform_pois p
                                   LEFT JOIN cities ci ON ci.city_id = p.city_id""")]


def match(place, pois):
    """The platform POI this place is, or None."""
    from fuzzy import compare, normalize
    best, rank = None, 0
    city = normalize(place.get("city") or "")
    for p in pois:
        how = compare(place["name"], p["name"], p["alt"], ignore=[place.get("city") or "", p["city"]])
        for alt in place.get("alt_names") or []:
            if how != "same":
                how = compare(alt, p["name"], p["alt"]) or how
        r = {"same": 3, "sounds": 2}.get(how, 0)
        if not r:
            continue
        pc = normalize(p["city"])
        if city and pc and city != pc and compare(place.get("city"), p["city"]) not in ("same", "sounds"):
            continue  # same name, different city: a different place
        if r > rank:
            best, rank = p, r
    return best


def merge_places(places):
    """The same place reported by two parts (or twice) becomes one."""
    from fuzzy import compare
    out = []
    for p in places:
        twin = next((q for q in out if compare(p["name"], q["name"], q.get("alt_names") or []) == "same"
                     and (not p.get("city") or not q.get("city") or compare(p["city"], q["city"]) in ("same", "sounds"))),
                    None)
        if twin is None:
            out.append(dict(p))
            continue
        twin["main_pages"] = sorted(set(twin["main_pages"]) | set(p["main_pages"]))
        for k, v in p.items():
            if v not in (None, "", []) and twin.get(k) in (None, "", []):
                twin[k] = v
    return out


# ---- the run -------------------------------------------------------------------------------------------------------

def start(db, file_name, data, params, user_id):
    """Store the upload and start the run. Returns run_id."""
    if not data:
        raise PdfError("Choose a PDF file.")
    if len(data) > MAX_BYTES:
        raise PdfError(f"That PDF is larger than {MAX_BYTES // (1024 * 1024)} MB. Split it and upload the parts.")
    pages = inspect(data)
    pa.anthropic_client()
    n_parts = (min(pages, MAX_PAGES) + PART_PAGES - 1) // PART_PAGES
    label = f"{file_name} ({pages} page{'s' if pages != 1 else ''})"
    cur = db.execute("INSERT INTO platform_agent_runs (agent_key, scope_label, params, created_by, items_total) "
                     "VALUES ('pdf', ?, ?, ?, ?)", (label, json.dumps(params), user_id, n_parts + 1))
    run_id = cur.lastrowid
    db.execute("INSERT INTO platform_agent_uploads (run_id, file_name, mime_type, file_size, page_count, file_data) "
               "VALUES (?, ?, 'application/pdf', ?, ?, ?)", (run_id, file_name, len(data), pages, data))
    db.commit()
    return run_id


def upload(db, run_id):
    return db.execute("SELECT * FROM platform_agent_uploads WHERE run_id = ? ORDER BY upload_id LIMIT 1",
                      (run_id,)).fetchone()


def source_ref(run_id, pages):
    """The 'source' of a proposal: the uploaded PDF at the place's first page."""
    first = pages[0] if pages else 1
    return f"/platform/agents/run/{run_id}/pdf#page={first}"


def run(db, run_id, client, log, meter, cancelled):
    """Called by platform_agents.run for agent 'pdf'. Returns the number of proposals."""
    up = upload(db, run_id)
    if up is None:
        raise PdfError("The uploaded PDF is missing.")
    data, file_name = up["file_data"], up["file_name"]
    params = json.loads(db.execute("SELECT params FROM platform_agent_runs WHERE run_id = ?", (run_id,)).fetchone()[0] or "{}")
    poi_types = [r[0] for r in db.execute(
        "SELECT pt.label FROM poi_types pt JOIN tenants t ON t.tenant_id = pt.tenant_id WHERE t.is_platform = 1 "
        "AND pt.is_active = 1 ORDER BY pt.sort_order, pt.label")]
    places = []
    chunks = parts(data)
    log(f"{file_name}: {up['page_count']} page(s), read in {len(chunks)} part(s)"
        + (f" (only the first {MAX_PAGES} pages)" if up["page_count"] > MAX_PAGES else ""))
    for i, (first, last, pdf) in enumerate(chunks):
        db.execute("UPDATE platform_agent_runs SET items_done = ?, heartbeat_at = datetime('now') WHERE run_id = ?", (i, run_id))
        db.commit()
        if cancelled():
            return 0
        log(f"Pages {first}-{last}: reading…")
        try:
            found = read_part(client, pdf, first, last, file_name, poi_types, meter)
        except Exception as e:
            fatal = pa.explain_api_error(e)
            if fatal:
                raise pa.AgentError(fatal) from e
            log(f"   failed: {e}")
            continue
        log(f"   {len(found)} place(s): " + "; ".join(p["name"] for p in found[:12]) + ("…" if len(found) > 12 else ""))
        places += found
    places = merge_places(places)
    total = propose(db, run_id, places, params)
    log(f"{total} value(s) proposed for {len(places)} place(s)")
    db.execute("UPDATE platform_agent_runs SET items_done = ?, proposals = ?, heartbeat_at = datetime('now') WHERE run_id = ?",
               (len(chunks), total, run_id))
    db.commit()
    if cancelled():
        return total
    stage_images(db, run_id, data, file_name, places, log)
    return total


def propose(db, run_id, places, params):
    """Write the proposals: changes to known POIs, and new POIs."""
    pois = catalog(db)
    fields = pa.FIELDS["platform_pois"]
    total, new_n = 0, 0
    for p in places:
        known = match(p, pois)
        conf = p.get("confidence")
        conf = float(conf) if isinstance(conf, (int, float)) else None
        src = source_ref(run_id, p["main_pages"])
        pages_note = f"PDF page{'s' if len(p['main_pages']) != 1 else ''} {', '.join(map(str, p['main_pages'][:8]))}" \
            if p["main_pages"] else None
        rows = []
        if known:
            p["poi_id"] = known["id"]
            key, label, entity = str(known["id"]), known["name"], "platform_pois"
            for f, (_lbl, kind) in fields.items():
                value, err = pa.validate(kind, p.get(f))
                if err:
                    continue
                cur = known["row"][f]
                if cur not in (None, "") and str(cur).strip().lower() == str(value).strip().lower():
                    continue
                rows.append((f, None if cur in (None, "") else str(cur), value))
        else:
            new_n += 1
            key, entity = f"new:{run_id}:{new_n}", NEW_ENTITY
            city = (p.get("city") or "").strip()
            label = f"{p['name']} (new{', ' + city if city else ''})"
            p["new_key"] = key
            rows.append(("name", None, p["name"]))
            if city:
                rows.append(("city", None, city + (f", {p['country']}" if p.get("country") else "")))
            ptype = (p.get("poi_type") or params.get("poi_type_label") or "").strip()
            if ptype:
                rows.append(("poi_type", None, ptype))
            for f, (_lbl, kind) in fields.items():
                value, err = pa.validate(kind, p.get(f))
                if not err:
                    rows.append((f, None, value))
        for f, cur, value in rows:
            db.execute("""INSERT INTO platform_agent_proposals (run_id, entity, record_key, record_label, field,
                              current_value, proposed_value, confidence, source_url, note)
                          VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                       (run_id, entity, key, label, f, cur, str(value), conf, src, pages_note))
            total += 1
    db.commit()
    return total


def stage_images(db, run_id, data, file_name, places, log):
    """Put the PDF's photos in one Master Image Catalog upload for curation."""
    import image_catalog as ic
    from platform_lookups import platform_tenant_id
    photos = extract_images(data, log)
    if not photos:
        return None
    by_page = {}
    for p in places:
        for pg in p["main_pages"]:
            by_page.setdefault(pg, []).append(p)
    files, meta = [], {}
    for pno, name, raw, _w, _h in photos:
        owners = [p for p in by_page.get(pno, [])]
        known = [p for p in owners if p.get("poi_id")]
        m = {"source_url": source_ref(run_id, [pno]), "licence": f"From “{file_name}”, page {pno}"}
        if len(owners) == 1:
            p = owners[0]
            m["title"] = f"{p['name']} — page {pno}"
            if known:
                m["owner_id"] = p["poi_id"]
            elif p.get("new_key"):
                m["new_key"], m["new_name"] = p["new_key"], p["name"]
        files.append((name, raw))
        meta[name] = m
    pid = platform_tenant_id(db)
    batch_id = ic.create_batch(db, pid, [], source="AI Agent", contributor=AGENT_NAME, kind="platform_poi",
                               upload_name=f"{file_name} (photos)")
    ic.add_items(db, pid, batch_id, files, meta=meta)
    for name, m in meta.items():
        if m.get("new_key"):
            db.execute("UPDATE image_import_items SET match_note = ? WHERE batch_id = ? AND file_name = ?",
                       (f"For the new POI “{m['new_name']}”: approve it in the TMS Agents review queue and this photo "
                        f"is assigned to it. [{m['new_key']}]", batch_id, name))
    db.execute("UPDATE platform_agent_runs SET batch_id = ? WHERE run_id = ?", (batch_id, run_id))
    db.commit()
    matched = sum(1 for m in meta.values() if m.get("owner_id"))
    log(f"{len(files)} photo(s) staged for curation ({matched} matched to a POI; the rest: choose the POI while curating)")
    return batch_id


# ---- approving a new POI --------------------------------------------------------------------------------------------

def create_new_poi(db, record_key, user_id=None):
    """Create the platform POI behind a 'new:' proposal group (on the first
    approval of any of its values) and point the group's proposals at it.
    Returns the new poi_id."""
    from geo_resolve import Resolver
    rows = {r["field"]: r for r in db.execute(
        "SELECT * FROM platform_agent_proposals WHERE record_key = ? AND entity = ?", (record_key, NEW_ENTITY))}
    name = rows.get("name")
    if name is None or name["status"] == "rejected":
        raise pa.AgentError("approve its Name first (a new POI needs one)")
    city_id = country_id = state_id = region_id = None
    city_text = None
    if rows.get("city") and rows["city"]["status"] != "rejected":
        text = rows["city"]["proposed_value"]
        city_name, _, country_name = text.partition(",")
        r = Resolver(db)
        country = r.country(country_name.strip())[0] if country_name.strip() else None
        if country is None:  # no country stated: the platform's home country first
            country = next((c for c in r.countries if c["label"] == "Pakistan"), None)
        if country is not None:
            row, how = r.city(country["country_id"], None, city_name.strip())
            country_id, region_id = country["country_id"], r.region_for(country["country_id"])
            if row and how in ("same", "sounds"):
                city_id, state_id = row["city_id"], row["state_id"]
        if city_id is None:
            city_text = city_name.strip() or None
    poi_type_id = None
    if rows.get("poi_type") and rows["poi_type"]["status"] != "rejected":
        poi_type_id = poi_type_for(db, rows["poi_type"]["proposed_value"])
    run = db.execute("SELECT r.params, u.file_name FROM platform_agent_runs r LEFT JOIN platform_agent_uploads u "
                     "ON u.run_id = r.run_id WHERE r.run_id = ?", (int(record_key.split(":")[1]),)).fetchone()
    params = json.loads((run["params"] if run else None) or "{}")
    if poi_type_id is None and params.get("poi_type_id"):
        poi_type_id = int(params["poi_type_id"])
    from datetime import date
    cur = db.execute("""INSERT INTO platform_pois (name, poi_type_id, region_id, country_id, state_id, city_id, city_text,
                            source, checked_on, is_active) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
                     (name["proposed_value"], poi_type_id, region_id, country_id, state_id, city_id, city_text,
                      f"PDF: {run['file_name'] if run else 'upload'}", date.today().isoformat()))
    poi_id = cur.lastrowid
    db.execute("UPDATE platform_agent_proposals SET record_key = ? WHERE record_key = ? AND entity = ?",
               (str(poi_id), record_key, NEW_ENTITY))
    # Photos from its pages, waiting in the run's upload, now have their POI.
    batch = db.execute("SELECT batch_id FROM platform_agent_runs WHERE run_id = ?", (int(record_key.split(":")[1]),)).fetchone()
    if batch and batch[0]:
        db.execute("""UPDATE image_import_items SET owner_id = ?, match_how = 'entity', include = 1, match_note = NULL
                      WHERE batch_id = ? AND status = 'pending' AND match_note LIKE ?""",
                   (poi_id, batch[0], f"%[{record_key}]%"))
    # The structural fields went into the new record: mark them applied.
    for f in ("name", "city", "poi_type"):
        if f in rows and rows[f]["status"] == "pending":
            db.execute("UPDATE platform_agent_proposals SET status = 'approved', decided_by = ?, decided_at = datetime('now') "
                       "WHERE proposal_id = ?", (user_id, rows[f]["proposal_id"]))
    return poi_id


def poi_type_for(db, text):
    from fuzzy import compare
    t = (text or "").strip()
    if not t:
        return None
    rows = db.execute("SELECT pt.poi_type_id, pt.label, pt.code FROM poi_types pt JOIN tenants tn ON tn.tenant_id = pt.tenant_id "
                      "WHERE tn.is_platform = 1 AND pt.is_active = 1").fetchall()
    for r in rows:
        if compare(t, r["label"]) == "same" or (r["code"] and r["code"].lower() == re.sub(r"\W+", "_", t).lower()):
            return r["poi_type_id"]
    for r in rows:  # "Mosque" finds "Mosque / Masjid"
        if t.lower() in r["label"].lower() or r["label"].lower().split(" /")[0] in t.lower():
            return r["poi_type_id"]
    return None
