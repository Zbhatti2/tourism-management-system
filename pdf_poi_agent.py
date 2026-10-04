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

Version 2 (Zeb, Oct 2026, from "Lahore Gurdwaras" -- 40 Gurdwaras on 8
pages, 3-6 per page, each with history, coordinates, a video link and a
photo or two; text drawn as shapes, so only readable as page images):

- Two modes. "Several POIs per PDF" (a guidebook, a list) and "One PDF per
  POI" (several files at once; each file name is matched to a POI by the
  image naming convention, e.g. "Gurdwara Rori Sahib - 2.pdf").
- Photos: small ones count (shortest side 100 px), photos the PDF split
  into strips are put back together, slivers and repeated logos dropped.
  Each photo is shown to Claude with its page and position, and Claude
  says which place it belongs to -- several places per page work.
- Text: a short Description (in Claude's words) and the place's history
  and details as the document gives them, for Notes / History; video,
  photo and document links (Additional Links); coordinates in any format.
- Duplicate text (text_dedup.py): what a POI already has -- its
  description, notes, and pending proposals -- is compared sentence by
  sentence, then by meaning; only new facts are proposed, appended to Notes
  / History, never replacing it. Disagreements are listed for the reviewer.
- The same PDF uploaded again is recognised (its fingerprint) and refused
  unless "read it again" is ticked.
"""
import base64
import io
import json
import re

import agent_runs
import platform_agents as pa

PART_PAGES = 20            # pages per request at most
PART_PHOTOS = 40           # photos shown per request at most
MAX_PAGES = 300
MAX_BYTES = 40 * 1024 * 1024
MAX_FILES = 25
MIN_SIDE, MIN_LONG = 100, 160   # photo size in pixels (shortest side, longest side)
MIN_PT = 20                     # drawn size on the page in points (smaller: a sliver)
MAX_IMAGES = 400
THUMB = 320
AGENT_NAME = "TMS Agent: POIs from PDF"
MODES = {"multi": "Several POIs per PDF", "per_poi": "One PDF per POI"}

DDL = """
CREATE TABLE IF NOT EXISTS platform_agent_uploads (
    upload_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER NOT NULL REFERENCES platform_agent_runs(run_id),
    file_name       TEXT NOT NULL,
    mime_type       TEXT,
    file_size       INTEGER,
    page_count      INTEGER,
    file_data       BLOB NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    file_hash       TEXT
);
CREATE INDEX IF NOT EXISTS idx_platform_agent_uploads_run ON platform_agent_uploads(run_id);
"""

# Fields a new POI gets proposed with, on top of FIELDS["platform_pois"].
NEW_FIELDS = {"name": ("Name", "text"), "city": ("City", "text"), "poi_type": ("POI Type", "text")}
NEW_ENTITY = "platform_pois_new"
SCALARS = ["year_founded", "opening_hours", "entry_fee", "website", "phone", "address"]


class PdfError(Exception):
    pass


# ---- reading the PDF ---------------------------------------------------------------------------------

def _reader(data):
    import logging
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception:
            raise PdfError("The PDF is password-protected.")
    return reader


def inspect(data):
    """Page count of a PDF; raises PdfError if it can't be read."""
    if b"%PDF" not in data[:1024]:
        raise PdfError("That file isn't a PDF.")
    try:
        n = len(_reader(data).pages)
        if not n:
            raise PdfError("That PDF has no pages.")
        return n
    except PdfError:
        raise
    except Exception as e:
        raise PdfError(f"That file couldn't be read as a PDF ({e.__class__.__name__}).")


def fingerprint(data):
    import hashlib
    return hashlib.sha256(data).hexdigest()


def sub_pdf(data, first, last):
    """Pages first..last (1-based) as a PDF."""
    from pypdf import PdfWriter
    reader = _reader(data)
    w = PdfWriter()
    for i in range(first - 1, last):
        w.add_page(reader.pages[i])
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def plan_parts(n_pages, photos_per_page):
    """[(first, last)] so that no part has more than PART_PAGES pages or
    PART_PHOTOS photos (a page with more photos is a part of its own)."""
    out, start, count = [], 1, 0
    for p in range(1, n_pages + 1):
        k = photos_per_page.get(p, 0)
        if p > start and (p - start >= PART_PAGES or count + k > PART_PHOTOS):
            out.append((start, p - 1))
            start, count = p, 0
        count += k
    if start <= n_pages:
        out.append((start, n_pages))
    return out


def page_text(data, page_no):
    try:
        return _reader(data).pages[page_no - 1].extract_text() or ""
    except Exception:
        return ""


def _mult(a, b):
    return [a[0] * b[0] + a[1] * b[2], a[0] * b[1] + a[1] * b[3], a[2] * b[0] + a[3] * b[2],
            a[2] * b[1] + a[3] * b[3], a[4] * b[0] + a[5] * b[2] + b[4], a[4] * b[1] + a[5] * b[3] + b[5]]


def placements(reader, page):
    """{XObject name without '/': (x0, y0, x1, y1)} where each image is
    drawn on the page, in points, y up."""
    from pypdf.generic import ContentStream
    out = {}
    try:
        cs = ContentStream(page.get_contents(), reader)
    except Exception:
        return out
    ctm, stack = [1, 0, 0, 1, 0, 0], []
    for operands, op in cs.operations:
        try:
            if op == b"q":
                stack.append(ctm[:])
            elif op == b"Q":
                ctm = stack.pop() if stack else [1, 0, 0, 1, 0, 0]
            elif op == b"cm":
                ctm = _mult([float(x) for x in operands], ctm)
            elif op == b"Do":
                xs = [ctm[4], ctm[0] + ctm[4], ctm[2] + ctm[4], ctm[0] + ctm[2] + ctm[4]]
                ys = [ctm[5], ctm[1] + ctm[5], ctm[3] + ctm[5], ctm[1] + ctm[3] + ctm[5]]
                out.setdefault(str(operands[0]).lstrip("/"), (min(xs), min(ys), max(xs), max(ys)))
        except Exception:
            continue
    return out


def _stack(pieces):
    """Image strips drawn one above the other (top first) as one image."""
    from PIL import Image
    ims = [p.convert("RGB") for p in pieces]
    w = max(i.width for i in ims)
    ims = [i if i.width == w else i.resize((w, max(1, round(i.height * w / i.width)))) for i in ims]
    out = Image.new("RGB", (w, sum(i.height for i in ims)), "white")
    y = 0
    for i in ims:
        out.paste(i, (0, y))
        y += i.height
    return out


def extract_images(data, log=lambda t: None):
    """[photo dict] of the PDF's photos: {id, page, name, raw, w, h, box}
    with box = (left, top, right, bottom) as fractions of the page, top
    down. Strips of one photo are joined; slivers, tiny images and
    repeated logos are left out."""
    from PIL import Image
    import image_catalog as ic
    reader = _reader(data)
    found, counts = [], {}
    for pno, page in enumerate(reader.pages[:MAX_PAGES], start=1):
        try:
            images = list(page.images)
        except Exception:
            continue
        where = placements(reader, page)
        pw = float(page.mediabox.width) or 612.0
        ph = float(page.mediabox.height) or 792.0
        items = []
        for img in images:
            try:
                raw = img.data
                im = Image.open(io.BytesIO(raw))
                im.load()
            except Exception:
                continue
            key = img.name.rsplit(".", 1)[0].lstrip("/")
            box = where.get(key)
            items.append({"im": im, "raw": raw, "box": box})
        # Join strips: same left and right edges, touching top to bottom.
        items.sort(key=lambda x: (-(x["box"][3]) if x["box"] else 0))
        joined = []
        for it in items:
            prev = joined[-1] if joined else None
            if (prev and it["box"] and prev["box"] and abs(it["box"][0] - prev["box"][0]) < 2.5
                    and abs(it["box"][2] - prev["box"][2]) < 2.5 and abs(prev["box"][1] - it["box"][3]) < 3):
                prev["parts"].append(it["im"])
                b = prev["box"]
                prev["box"] = (b[0], it["box"][1], b[2], b[3])
                continue
            joined.append(dict(it, parts=[it["im"]]))
        k = 0
        for it in joined:
            im = _stack(it["parts"]) if len(it["parts"]) > 1 else it["im"]
            w, h = im.size
            box = it["box"]
            if min(w, h) < MIN_SIDE or max(w, h) < MIN_LONG or max(w, h) > 6 * min(w, h):
                continue
            if box and (box[2] - box[0] < MIN_PT or box[3] - box[1] < MIN_PT):
                continue
            if len(it["parts"]) > 1 or (im.format or "").upper() not in ("JPEG", "PNG", "WEBP"):
                buf = io.BytesIO()
                im.convert("RGB").save(buf, "JPEG", quality=88)
                raw, ext = buf.getvalue(), "jpg"
            else:
                raw = it["raw"]
                ext = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}[im.format.upper()]
            digest = ic.sha256(raw)
            counts[digest] = counts.get(digest, 0) + 1
            if counts[digest] > 1:
                continue
            k += 1
            frac = None
            if box:
                frac = (round(box[0] / pw, 3), round(1 - box[3] / ph, 3), round(box[2] / pw, 3), round(1 - box[1] / ph, 3))
            found.append({"id": f"P{pno}-{k}", "page": pno, "name": f"page-{pno:03d}-{k}.{ext}", "raw": raw,
                          "w": w, "h": h, "box": frac, "digest": digest})
            if len(found) >= MAX_IMAGES:
                break
        if len(found) >= MAX_IMAGES:
            break
    # An image that appears on 3+ pages is a logo / decoration, not a photo of a place.
    photos = [p for p in found if counts[p["digest"]] < 3]
    log(f"{len(photos)} photo(s) found in the PDF")
    return photos


def thumbnail(raw):
    from PIL import Image
    im = Image.open(io.BytesIO(raw)).convert("RGB")
    im.thumbnail((THUMB, THUMB))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=70)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def where_on_page(box):
    if not box:
        return "position unknown"
    l, t, r, b = box
    mid = (t + b) / 2
    vert = "top" if mid < 0.33 else "middle" if mid < 0.66 else "bottom"
    horiz = "left" if (l + r) / 2 < 0.4 else "right" if (l + r) / 2 > 0.6 else "centre"
    return f"{vert} {horiz}, {round(t * 100)}%-{round(b * 100)}% down the page"


# ---- asking Claude ---------------------------------------------------------------------------------------

def _tool():
    props = {f: {"type": "string"} for f in ["description"] + SCALARS}
    props.update({
        "name": {"type": "string"}, "alt_names": {"type": "array", "items": {"type": "string"}},
        "number": {"type": "string", "description": "The place's number or label in the document, if it has one"},
        "city": {"type": "string"}, "country": {"type": "string"}, "poi_type": {"type": "string"},
        "history": {"type": "string",
                    "description": "Everything else the document says about the place -- history, who built it, "
                                   "events, land, its condition today, visiting notes -- as the document gives it"},
        "coordinates": {"type": "string", "description": "Coordinates exactly as the document gives them"},
        "links": {"type": "array", "items": {"type": "object", "properties": {
            "url": {"type": "string"}, "type": {"type": "string", "enum": ["video", "images", "document", "other"]},
            "title": {"type": "string"}}, "required": ["url"]}},
        "photos": {"type": "array", "items": {"type": "string"},
                   "description": "The ids (e.g. P9-3) of the photos that show this place"},
        "main_pages": {"type": "array", "items": {"type": "integer"},
                       "description": "Page numbers (as printed in the page list) where this place is described"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    })
    return {"name": "report_places", "description": "Report the points of interest described in this part of the PDF.",
            "input_schema": {"type": "object", "properties": {"places": {"type": "array", "items": {
                "type": "object", "properties": props, "required": ["name", "main_pages"]}}}, "required": ["places"]}}


def read_part(client, part_pdf, first, last, file_name, poi_types, meter, photos=(), one_place=None):
    """[place dicts] for one part of the PDF; page numbers in the result are
    the original PDF's. photos: the photo dicts on these pages, shown to
    Claude to assign. one_place: the name of the single POI the whole file
    is about ("One PDF per POI")."""
    scope = (f"The whole document is about ONE point of interest{': ' + one_place if one_place else ''}. Report "
             "exactly one place, gathering everything the document says about it.\n\n") if one_place is not None else (
        "List every point of interest that these pages DESCRIBE (not ones only mentioned in passing).\n\n")
    prompt = (
        f"This is pages {first}-{last} of the PDF \"{file_name}\". It is being used to build a tourism platform's "
        "catalog of Points of Interest (monuments, religious sites, museums, forts, parks, natural sites, markets, "
        "landmarks...).\n\n" + scope +
        "For each place give only what the document states:\n"
        "- name: as the document spells it (without its list number); other spellings or names in alt_names;\n"
        "- number: its number in the document's list, if any;\n"
        "- city or nearest town or village, and country;\n"
        "- poi_type: one of: " + ", ".join(poi_types[:40]) + ", or your own short type;\n"
        "- description: 2-3 sentences in your own words saying what the place is, for an itinerary;\n"
        "- history: everything else the document says about the place (history, people, events, land, its "
        "condition today, visiting notes), in the document's own words as far as possible, keeping every fact, "
        "but without the coordinates and link text;\n"
        "- coordinates exactly as written (e.g. 31°36'22.6\"N 74°19'23.5\"E);\n"
        "- links: every web link given for the place, with its type (video for 'watch a video', images for "
        "photo galleries, document for PDFs and articles, other);\n"
        "- year founded or era, opening days / hours, entry fee, website, phone, address or landmark;\n"
        f"- main_pages: the page numbers ({first}-{last}, counting the first page of this part as {first});\n"
        "- confidence (0-1) that the details are stated clearly.\n"
        "Leave out anything the document doesn't state.")
    content = [{"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                               "data": base64.b64encode(part_pdf).decode("ascii")}}]
    if photos:
        prompt += ("\n\nThe photos in these pages follow, each with its id, page and position. In each place's "
                   "photos, list the ids of the photos that show that place (usually the photo beside or just "
                   "after its entry). Leave out photos that show no listed place.")
    content.append({"type": "text", "text": prompt})
    for ph in photos:
        content.append({"type": "text", "text": f"Photo {ph['id']}: page {ph['page']}, {where_on_page(ph['box'])}"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                    "data": thumbnail(ph["raw"])}})
    content.append({"type": "text", "text": "Call report_places once."})
    resp = agent_runs.create_message(client, model=pa.model(), max_tokens=16000, tools=[_tool()],
                                     tool_choice={"type": "tool", "name": "report_places"},
                                     messages=[{"role": "user", "content": content}])
    meter(getattr(resp, "usage", None))
    places = []
    for b in getattr(resp, "content", []) or []:
        if getattr(b, "type", None) == "tool_use" and getattr(b, "name", None) == "report_places":
            places = (b.input or {}).get("places") or []
    ids = {ph["id"] for ph in photos}
    out = []
    for p in places:
        name = (p.get("name") or "").strip()
        if not name:
            continue
        pages = sorted({int(x) for x in (p.get("main_pages") or []) if str(x).lstrip("-").isdigit()
                        and first <= int(x) <= last})
        out.append(dict(p, name=name[:200], main_pages=pages,
                        photos=[x for x in (p.get("photos") or []) if x in ids]))
    if one_place is not None and len(out) > 1:
        out = [merge_places(out, force=True)[0]]
    return out


def coords_of(place):
    """(lat, lon) from the place's coordinates text, or its latitude /
    longitude fields."""
    from utils import parse_coordinates
    text = (place.get("coordinates") or "").strip()
    if text:
        got = parse_coordinates(text)
        if got:
            return round(got[0], 6), round(got[1], 6)
    lat, _ = pa.validate("lat", place.get("latitude"))
    lon, _ = pa.validate("lon", place.get("longitude"))
    return (lat, lon) if lat is not None and lon is not None else (None, None)


# ---- matching to the catalog -------------------------------------------------------------------------------------

def catalog(db):
    from fuzzy import split_alt_names
    return [{"id": r["poi_id"], "name": r["name"], "alt": split_alt_names(r["alt_names"]),
             "city": (r["city_label"] or r["city_text"] or ""), "row": r}
            for r in db.execute("""SELECT p.*, ci.label AS city_label FROM platform_pois p
                                   LEFT JOIN cities ci ON ci.city_id = p.city_id""")]


def name_contained(a, b):
    """One name is the other plus extra words ("Gurdwara Nanakgarh" /
    "Gurdwara Nanakgarh Patshahi Pehli"): every word of the shorter one,
    at least two, is in the longer one (sounds-alike words count)."""
    from fuzzy import normalize, sound_key
    wa, wb = normalize(a or "").split(), normalize(b or "").split()
    short, long_ = (wa, wb) if len(wa) <= len(wb) else (wb, wa)
    if len(short) < 2 or len(short) == len(long_):
        return False
    keys = {sound_key(w) for w in long_}
    return all(w in long_ or sound_key(w) in keys for w in short)


def match(place, pois):
    """(the platform POI this place is, or None; how). Name -- exact,
    sounds-like or close -- and city must agree; a close name counts when
    the coordinates are within 1 km; coordinates within 150 m and a
    sounds-like name count even if the city is spelled differently."""
    from fuzzy import compare, normalize
    from utils import haversine_km
    best, rank, how_best = None, 0, None
    city = normalize(place.get("city") or "")
    lat, lon = coords_of(place)
    for p in pois:
        how = compare(place["name"], p["name"], p["alt"], ignore=[place.get("city") or "", p["city"]])
        for alt in place.get("alt_names") or []:
            if how != "same":
                how = compare(alt, p["name"], p["alt"]) or how
        km = None
        if lat is not None and p["row"]["latitude"] is not None and p["row"]["longitude"] is not None:
            km = haversine_km(lat, lon, p["row"]["latitude"], p["row"]["longitude"])
        r = {"same": 3, "sounds": 2}.get(how, 0)
        if how == "similar" and km is not None and km <= 1.0:
            r = 2
        if not r and (name_contained(place["name"], p["name"])
                      or any(name_contained(place["name"], a) for a in p["alt"])):
            r, how = 1, "similar"  # needs the same city (below) and is shown for checking
        if not r:
            continue
        pc = normalize(p["city"])
        city_differs = city and pc and city != pc and compare(place.get("city"), p["city"]) not in ("same", "sounds")
        if city_differs and not (km is not None and km <= 0.15):
            continue  # same name, different city: a different place
        if r == 1 and not (city and pc and not city_differs) and not (km is not None and km <= 1.0):
            continue  # a shorter / longer name counts only in the same city
        if km is not None and km > 25:
            continue  # same name, far apart: a different place
        if r > rank:
            best, rank, how_best = p, r, how
    return best, how_best


def merge_places(places, force=False):
    """The same place reported by two parts (or twice) becomes one; its
    history texts are joined without repeating sentences. force: merge
    everything into one place."""
    from fuzzy import compare
    import text_dedup
    out = []

    def same_place(p, q):
        if p.get("city") and q.get("city") and compare(p["city"], q["city"]) not in ("same", "sounds"):
            return False
        if compare(p["name"], q["name"], q.get("alt_names") or []) in ("same", "sounds"):
            return True
        if name_contained(p["name"], q["name"]):
            (la, lo), (lb, lob) = coords_of(p), coords_of(q)
            if la is not None and lb is not None:
                from utils import haversine_km
                return haversine_km(la, lo, lb, lob) <= 0.3
            if p.get("history") and q.get("history"):
                _left, share = text_dedup.wording_check(q["history"], p["history"])
                return share >= 0.5
        return False

    for p in places:
        twin = out[0] if (force and out) else next((q for q in out if same_place(p, q)), None)
        if twin is None:
            out.append(dict(p, photos=list(p.get("photos") or []), links=list(p.get("links") or [])))
            continue
        twin["main_pages"] = sorted(set(twin["main_pages"]) | set(p["main_pages"]))
        twin["photos"] = twin["photos"] + [x for x in p.get("photos") or [] if x not in twin["photos"]]
        have = {(lk.get("url") or "").strip().lower() for lk in twin["links"]}
        twin["links"] += [lk for lk in p.get("links") or [] if (lk.get("url") or "").strip().lower() not in have]
        if p.get("history"):
            extra = text_dedup.new_sentences(twin.get("history") or "", p["history"])
            if extra:
                twin["history"] = ((twin.get("history") or "") + " " + " ".join(extra)).strip()
        for k, v in p.items():
            if k in ("photos", "links", "history", "main_pages"):
                continue
            if v not in (None, "", []) and twin.get(k) in (None, "", []):
                twin[k] = v
    return out


# ---- the run -------------------------------------------------------------------------------------------------------

def start(db, files, params, user_id, agent_key="pdf", modes=MODES):
    """files: [(file name, bytes)]. Store the uploads and start the run.
    Returns run_id. agent_key / modes: the Accommodation and Restaurants PDF
    agents (pdf_catalog_agent.py) start their runs here too."""
    files = [(n, d) for n, d in files if d]
    if not files:
        raise PdfError("Choose a PDF file.")
    if len(files) > MAX_FILES:
        raise PdfError(f"At most {MAX_FILES} PDFs at a time.")
    if sum(len(d) for _n, d in files) > MAX_BYTES:
        raise PdfError(f"Those PDFs add up to more than {MAX_BYTES // (1024 * 1024)} MB. Upload them in smaller groups.")
    checked = []
    for name, data in files:
        try:
            pages = inspect(data)
        except PdfError as e:
            raise PdfError(f"{name}: {e}")
        digest = fingerprint(data)
        seen = db.execute("""SELECT u.file_name, u.created_at, u.run_id FROM platform_agent_uploads u
                             JOIN platform_agent_runs r ON r.run_id = u.run_id
                             WHERE u.file_hash = ? AND r.agent_key = ? ORDER BY u.upload_id DESC LIMIT 1""",
                          (digest, agent_key)).fetchone()
        if seen and not params.get("again"):
            raise PdfError(f"“{name}” was already read on {seen['created_at'][:10]} (run {seen['run_id']}, as "
                           f"“{seen['file_name']}”). Tick “Read it again anyway” to read it once more.")
        checked.append((name, data, pages, digest))
    pa.anthropic_client()
    mode = params.get("mode") if params.get("mode") in modes else "multi"
    params = dict(params, mode=mode)
    total_pages = sum(min(p, MAX_PAGES) for _n, _d, p, _h in checked)
    label = (checked[0][0] if len(checked) == 1 else f"{len(checked)} PDFs") + \
        f" ({total_pages} page{'s' if total_pages != 1 else ''}; {modes[mode].lower()})"
    cur = db.execute("INSERT INTO platform_agent_runs (agent_key, scope_label, params, created_by, items_total) "
                     "VALUES (?, ?, ?, ?, ?)", (agent_key, label, json.dumps(params), user_id, len(checked) + 1))
    run_id = cur.lastrowid
    for name, data, pages, digest in checked:
        db.execute("INSERT INTO platform_agent_uploads (run_id, file_name, mime_type, file_size, page_count, file_data, "
                   "file_hash) VALUES (?, ?, 'application/pdf', ?, ?, ?, ?)", (run_id, name, len(data), pages, data, digest))
    db.commit()
    return run_id


def uploads(db, run_id):
    return db.execute("SELECT * FROM platform_agent_uploads WHERE run_id = ? ORDER BY upload_id", (run_id,)).fetchall()


def upload(db, run_id, upload_id=None):
    if upload_id:
        return db.execute("SELECT * FROM platform_agent_uploads WHERE run_id = ? AND upload_id = ?",
                          (run_id, upload_id)).fetchone()
    return db.execute("SELECT * FROM platform_agent_uploads WHERE run_id = ? ORDER BY upload_id LIMIT 1",
                      (run_id,)).fetchone()


def source_ref(run_id, pages, upload_id=None):
    """The 'source' of a proposal: the uploaded PDF at the place's first page."""
    first = pages[0] if pages else 1
    return f"/platform/agents/run/{run_id}/pdf" + (f"?u={upload_id}" if upload_id else "") + f"#page={first}"


def name_from_file(file_name):
    """'Gurdwara Rori Sahib - 2.pdf' -> 'Gurdwara Rori Sahib'."""
    stem = re.sub(r"\.pdf$", "", file_name, flags=re.I)
    stem = re.sub(r"[_]+", " ", stem)
    stem = re.sub(r"\s*[-(]\s*\d{1,3}\)?\s*$", "", stem)
    return stem.strip()


def run(db, run_id, client, log, meter, cancelled):
    """Called by platform_agents.run for agent 'pdf'. Returns the number of proposals."""
    ups = uploads(db, run_id)
    if not ups:
        raise PdfError("The uploaded PDF is missing.")
    params = json.loads(db.execute("SELECT params FROM platform_agent_runs WHERE run_id = ?", (run_id,)).fetchone()[0] or "{}")
    mode = params.get("mode") or "multi"
    poi_types = [r[0] for r in db.execute(
        "SELECT pt.label FROM poi_types pt JOIN tenants t ON t.tenant_id = pt.tenant_id WHERE t.is_platform = 1 "
        "AND pt.is_active = 1 ORDER BY pt.sort_order, pt.label")]
    import image_catalog as ic
    pois = catalog(db)
    owners = [{"id": p["id"], "name": p["name"], "city": p["city"]} for p in pois]
    places, all_photos = [], {}
    for n, up in enumerate(ups):
        db.execute("UPDATE platform_agent_runs SET items_done = ?, heartbeat_at = datetime('now') WHERE run_id = ?", (n, run_id))
        db.commit()
        if cancelled():
            return 0
        data, file_name = up["file_data"], up["file_name"]
        n_pages = min(up["page_count"] or inspect(data), MAX_PAGES)
        photos = extract_images(data, lambda t: log(f"{file_name}: {t}"))
        for ph in photos:
            ph["upload_id"] = up["upload_id"]
            ph["key"] = f"{up['upload_id']}:{ph['id']}"
            all_photos[ph["key"]] = ph
        per_page = {}
        for ph in photos:
            per_page[ph["page"]] = per_page.get(ph["page"], 0) + 1
        chunks = plan_parts(n_pages, per_page)
        one = None
        file_poi = None
        if mode == "per_poi":
            one = name_from_file(file_name)
            oid, how, note, _rest = ic.match_file(one, owners, "Point of Interest")
            if oid and how == "exact":
                file_poi = next(p for p in pois if p["id"] == oid)
                log(f"{file_name}: file name matches the POI “{file_poi['name']}”")
            elif oid:
                log(f"{file_name}: {note}")
        log(f"{file_name}: {up['page_count']} page(s), {len(photos)} photo(s), read in {len(chunks)} part(s)"
            + (f" (only the first {MAX_PAGES} pages)" if (up["page_count"] or 0) > MAX_PAGES else ""))
        found_here = []
        for first, last in chunks:
            if cancelled():
                return 0
            db.execute("UPDATE platform_agent_runs SET heartbeat_at = datetime('now') WHERE run_id = ?", (run_id,))
            db.commit()
            part_photos = [ph for ph in photos if first <= ph["page"] <= last]
            log(f"   pages {first}-{last}: reading…")
            try:
                found = read_part(client, sub_pdf(data, first, last), first, last, file_name, poi_types, meter,
                                  part_photos, one_place=one)
            except Exception as e:
                fatal = pa.explain_api_error(e)
                if fatal:
                    raise pa.AgentError(fatal) from e
                log(f"   failed: {e}")
                continue
            log(f"   {len(found)} place(s): " + "; ".join(p["name"] for p in found[:12]) + ("…" if len(found) > 12 else ""))
            for p in found:
                p["photos"] = [f"{up['upload_id']}:{x}" for x in p.get("photos") or []]
                p["upload_id"], p["file_name"] = up["upload_id"], file_name
            found_here += found
        if mode == "per_poi" and found_here:
            p = merge_places(found_here, force=True)[0]
            p["photos"] = [ph["key"] for ph in photos]  # every photo in the file is of its place
            if file_poi:
                p["poi_id_hint"] = file_poi["id"]
            found_here = [p]
        places += found_here
    places = merge_places(places)
    total = propose(db, run_id, places, params, client=client, meter=meter, log=log)
    log(f"{total} value(s) proposed for {len(places)} place(s)")
    db.execute("UPDATE platform_agent_runs SET items_done = ?, proposals = ?, heartbeat_at = datetime('now') WHERE run_id = ?",
               (len(ups), total, run_id))
    db.commit()
    if cancelled():
        return total
    stage_images(db, run_id, ups, places, all_photos, log)
    return total


def _existing_text(db, poi):
    """What a known POI already says: description, notes, and text waiting
    in pending proposals."""
    parts = [poi["row"]["description"] or "", poi["row"]["notes"] or ""]
    parts += [r[0] for r in db.execute(
        "SELECT proposed_value FROM platform_agent_proposals WHERE entity = 'platform_pois' AND record_key = ? "
        "AND field IN ('notes', 'description') AND status = 'pending'", (str(poi["id"]),))]
    return "\n".join(x for x in parts if x)


def _credit(p):
    pages = p.get("main_pages") or []
    return f"(From “{p.get('file_name') or 'PDF'}”" + (f", p. {', '.join(map(str, pages[:4]))}" if pages else "") + ")"


def propose(db, run_id, places, params, client=None, meter=lambda u: None, log=lambda t: None):
    """Write the proposals: changes to known POIs, and new POIs."""
    import poi_links
    import text_dedup
    pois = catalog(db)
    by_id = {p["id"]: p for p in pois}
    fields = pa.FIELDS["platform_pois"]
    total, new_n = 0, 0
    # Duplicate text: known POIs' new text is checked against what they have.
    checks = []
    for i, p in enumerate(places):
        known = by_id.get(p.get("poi_id_hint")) if p.get("poi_id_hint") else None
        how = "file name" if known else None
        if known is None:
            known, how = match(p, pois)
        p["_known"], p["_how"] = known, how
        if not known:
            continue
        existing = _existing_text(db, known)
        new_text = " ".join(x for x in [p.get("history") or ""] if x)
        if not existing.strip() and not (known["row"]["description"] or "").strip():
            continue
        if p.get("description") and (known["row"]["description"] or "").strip():
            new_text = (p["description"] + " " + new_text).strip()  # description already set: offer it as notes
        if not new_text:
            continue
        left, share = text_dedup.wording_check(existing, new_text)
        p["_dup_share"] = share
        if not left:
            p["_notes"], p["_dup"] = "", "duplicate"
            continue
        checks.append({"key": str(i), "name": known["name"], "existing": existing, "new": left})
    verdicts = {}
    if checks and client is not None:
        try:
            verdicts = text_dedup.meaning_check(client, pa.model(), checks, meter,
                                                create=lambda **kw: agent_runs.create_message(client, **kw))
        except Exception as e:
            log(f"   duplicate-text check failed ({e}); new text is proposed unchecked")
    dup_n = 0
    for i, p in enumerate(places):
        known = p.pop("_known", None)
        how = p.pop("_how", None)
        conf = p.get("confidence")
        conf = float(conf) if isinstance(conf, (int, float)) else None
        src = source_ref(run_id, p["main_pages"], p.get("upload_id"))
        pages_note = (f"{p.get('file_name') or 'PDF'}, page{'s' if len(p['main_pages']) != 1 else ''} "
                      f"{', '.join(map(str, p['main_pages'][:8]))}") if p["main_pages"] else (p.get("file_name") or None)
        lat, lon = coords_of(p)
        rows = []  # (field, current, value, note)
        if known:
            p["poi_id"] = known["id"]
            key, label, entity = str(known["id"]), known["name"], "platform_pois"
            if how in ("sounds", "similar"):
                label += f"  (matched “{p['name']}” by a similar name — check)"
            for f in SCALARS + (["description"] if not (known["row"]["description"] or "").strip() else []):
                value, err = pa.validate(fields[f][1], p.get(f))
                if err:
                    continue
                cur = known["row"][f]
                if cur not in (None, "") and str(cur).strip().lower() == str(value).strip().lower():
                    continue
                rows.append((f, None if cur in (None, "") else str(cur), value, None))
            if lat is not None and known["row"]["latitude"] is None:
                rows += [("latitude", None, lat, None), ("longitude", None, lon, None)]
            # Notes / History: only what's new.
            if p.get("_dup") == "duplicate":
                dup_n += 1
            else:
                text, note = None, None
                v = verdicts.get(str(i))
                if v is not None:
                    if v["verdict"] == "duplicate" or not v["new_text"]:
                        dup_n += 1
                    else:
                        text = v["new_text"]
                        note = ("Conflicts with what's stored: " + "; ".join(v["conflicts"])) if v["conflicts"] else \
                            "New facts only; the rest is already on this POI."
                        if v.get("unchecked"):
                            note = "Not checked for duplicates (the check didn't answer)."
                elif p.get("history"):
                    existing = _existing_text(db, known)
                    if existing.strip():
                        left, _share = text_dedup.wording_check(existing, p["history"])
                        text = left or None
                        if not left:
                            dup_n += 1
                    else:
                        text = p["history"]
                if text:
                    cur_notes = known["row"]["notes"]
                    rows.append(("notes", f"{len(cur_notes)} characters already" if cur_notes else None,
                                 f"{text}\n{_credit(p)}", note))
            have = {poi_links.norm_url(r["url"]) for r in poi_links.links(db, known["id"])}
        else:
            new_n += 1
            key, entity = f"new:{run_id}:{new_n}", NEW_ENTITY
            city = (p.get("city") or "").strip()
            label = f"{p['name']} (new{', ' + city if city else ''})"
            p["new_key"] = key
            rows.append(("name", None, p["name"], None))
            if city:
                rows.append(("city", None, city + (f", {p['country']}" if p.get("country") else ""), None))
            ptype = (p.get("poi_type") or params.get("poi_type_label") or "").strip()
            if ptype:
                rows.append(("poi_type", None, ptype, None))
            for f in ["description"] + SCALARS:
                value, err = pa.validate(fields[f][1], p.get(f))
                if not err:
                    rows.append((f, None, value, None))
            if lat is not None:
                rows += [("latitude", None, lat, None), ("longitude", None, lon, None)]
            if p.get("history"):
                rows.append(("notes", None, f"{p['history'].strip()}\n{_credit(p)}", None))
            have = set()
        for lk in p.get("links") or []:
            url = poi_links.clean_url(lk.get("url"))
            if not url or poi_links.norm_url(url) in have:
                continue
            have.add(poi_links.norm_url(url))
            typ = lk.get("type") if lk.get("type") in poi_links.LINK_TYPE else poi_links.guess_type(url, lk.get("title") or "")
            rows.append(("link", None, pa.link_value(typ, url, (lk.get("title") or "").strip() or None), None))
        for f, cur, value, note in rows:
            db.execute("""INSERT INTO platform_agent_proposals (run_id, entity, record_key, record_label, field,
                              current_value, proposed_value, confidence, source_url, note)
                          VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                       (run_id, entity, key, label, f, cur, str(value), conf, src,
                        " · ".join(x for x in (note, pages_note) if x) or None))
            total += 1
    if dup_n:
        log(f"   {dup_n} place(s): their text is already on the POI -- nothing proposed for Notes / History")
    db.commit()
    return total


def stage_images(db, run_id, ups, places, all_photos, log):
    """Put the PDF's photos in one Master Image Catalog upload for curation,
    each with the place Claude assigned it to."""
    import image_catalog as ic
    from platform_lookups import platform_tenant_id
    if not all_photos:
        return None
    owner_of, by_page = {}, {}
    for p in places:
        for k in p.get("photos") or []:
            owner_of.setdefault(k, p)
        for pg in p.get("main_pages") or []:
            by_page.setdefault((p.get("upload_id"), pg), []).append(p)
    for key, ph in all_photos.items():  # not assigned: the only place on its page, if there's one
        if key not in owner_of and len(by_page.get((ph["upload_id"], ph["page"]), [])) == 1:
            owner_of[key] = by_page[(ph["upload_id"], ph["page"])][0]
    names = {u["upload_id"]: u["file_name"] for u in ups}
    files, meta = [], {}
    many = len(ups) > 1
    for key, ph in all_photos.items():
        fname = (f"{re.sub(r'[^A-Za-z0-9]+', '-', names[ph['upload_id']])[:40]}-" if many else "") + ph["name"]
        m = {"source_url": source_ref(run_id, [ph["page"]], ph["upload_id"] if many else None),
             "licence": f"From “{names[ph['upload_id']]}”, page {ph['page']}"}
        p = owner_of.get(key)
        if p:
            m["title"] = f"{p['name']} — page {ph['page']}"
            if p.get("poi_id"):
                m["owner_id"] = p["poi_id"]
            elif p.get("new_key"):
                m["new_key"], m["new_name"] = p["new_key"], p["name"]
        files.append((fname, ph["raw"]))
        meta[fname] = m
    pid = platform_tenant_id(db)
    label = names[ups[0]["upload_id"]] if not many else f"{len(ups)} PDFs"
    batch_id = ic.create_batch(db, pid, [], source="AI Agent", contributor=AGENT_NAME, kind="platform_poi",
                               upload_name=f"{label} (photos)")
    ic.add_items(db, pid, batch_id, files, meta=meta)
    for name, m in meta.items():
        if m.get("new_key"):
            db.execute("UPDATE image_import_items SET match_note = ? WHERE batch_id = ? AND file_name = ?",
                       (f"For the new POI “{m['new_name']}”: approve it in the TMS Agents review queue and this photo "
                        f"is assigned to it. [{m['new_key']}]", batch_id, name))
    db.execute("UPDATE platform_agent_runs SET batch_id = ? WHERE run_id = ?", (batch_id, run_id))
    db.commit()
    matched = sum(1 for m in meta.values() if m.get("owner_id") or m.get("new_key"))
    log(f"{len(files)} photo(s) staged for curation ({matched} assigned to a place; the rest: choose the POI while curating)")
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
