"""
TMS Agents -- platform-level AI agents, run by the SystemAdmin, that research
the shared catalogs on the web and PROPOSE changes. Nothing is written until a
proposal is approved on the review screen (blueprints/platform_agents.py).
Their cost is a platform cost: every API call is logged in ai_usage_log with
tenant_id NULL (the "Platform agents" line on AI Usage). They use
PLATFORM_ANTHROPIC_API_KEY when it is set, otherwise ANTHROPIC_API_KEY.

Agents
------
* geography -- cities missing coordinates, time zone or altitude.
* distances -- road distance, drive time (range), route name and rail for city
               pairs: one city to several others, or the gaps in existing rows.
* pois      -- Points of Interest missing a description, opening hours, entry
               fee, year founded, website, phone or coordinates.
* accommodation -- platform Accommodation missing star rating, rooms, contact
               details, coordinates, amenities or the Reference Room Rate.
* restaurants -- platform Restaurants missing rating, class, cuisine, price
               range, group suitability, contact details or coordinates.
* pdf, pdf_accommodation, pdf_restaurants -- read uploaded PDFs for content
               and images (pdf_poi_agent.py, pdf_catalog_agent.py).

SECTIONS groups them for the TMS Agents menu: Content Enrichment, Image
Collectors, PDF Agents, Geography & Distances.

Every run is narrow by design (a chosen country / city / list, capped in
size), works in batches (one Claude call with web search per batch), and each
proposed value carries a confidence and the page it came from. Approved POI,
Accommodation and Restaurant changes reach tenants through Catalog Sync.
"""
import json
import re
import threading
import traceback
from datetime import date

import agent_runs
import ai_usage

PROPOSAL_STATUSES = ("pending", "approved", "rejected")

DDL = """
CREATE TABLE IF NOT EXISTS platform_agent_runs (
    run_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_key       TEXT NOT NULL,          -- geography / distances / pois
    scope_label     TEXT NOT NULL,
    params          TEXT,                   -- JSON: what the run covers
    status          TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','done','failed')),
    progress        TEXT,
    error           TEXT,
    proposals       INTEGER NOT NULL DEFAULT 0,
    created_by      INTEGER REFERENCES users(user_id),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    started_at      TEXT,
    finished_at     TEXT
);
CREATE TABLE IF NOT EXISTS platform_agent_proposals (
    proposal_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER NOT NULL REFERENCES platform_agent_runs(run_id),
    entity          TEXT NOT NULL,          -- cities / city_distances / platform_pois
    record_key      TEXT NOT NULL,          -- city_id, poi_id, or 'a:b' city pair (smaller id first)
    record_label    TEXT,
    field           TEXT NOT NULL,
    current_value   TEXT,
    proposed_value  TEXT NOT NULL,
    confidence      REAL,
    source_url      TEXT,
    note            TEXT,
    status          TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected')),
    decided_by      INTEGER REFERENCES users(user_id),
    decided_at      TEXT,
    error           TEXT                    -- why an approval couldn't be applied
);
CREATE INDEX IF NOT EXISTS idx_platform_agent_proposals_run ON platform_agent_proposals(run_id);
CREATE INDEX IF NOT EXISTS idx_platform_agent_proposals_status ON platform_agent_proposals(status);
"""


class AgentError(Exception):
    pass


# ---- field definitions ---------------------------------------------------------------------------
# entity -> {field: (label, kind)}; kind decides validation when applied.

FIELDS = {
    "cities": {"latitude": ("Latitude", "lat"), "longitude": ("Longitude", "lon"),
               "timezone": ("Time zone", "tz"), "altitude_m": ("Altitude (m)", "int")},
    "city_distances": {"road_km": ("Road distance (km)", "real"), "drive_minutes": ("Drive time — from (min)", "int"),
                       "drive_minutes_max": ("Drive time — to (min)", "int"), "route_name": ("Route / road", "text"),
                       "rail_available": ("Rail available", "yesno")},
    "platform_pois": {"description": ("Description", "text"), "year_founded": ("Year founded / era", "text"),
                      "opening_hours": ("Days / hours open", "text"), "entry_fee": ("Entry fee", "text"),
                      "website": ("Website", "url"), "phone": ("Phone", "text"), "address": ("Address / landmark", "text"),
                      "latitude": ("Latitude", "lat"), "longitude": ("Longitude", "lon")},
}
_CONTACT = {"address": ("Address / landmark", "text"), "phone": ("Phone", "text"), "email": ("Email", "email"),
            "website": ("Website", "url"), "latitude": ("Latitude", "lat"), "longitude": ("Longitude", "lon")}
FIELDS["platform_accommodation"] = {
    "star_rating": ("Star rating (1-5)", "stars"), "rating_note": ("Rating (as published)", "text"),
    "rooms": ("No. of rooms", "int"),
    "ref_room_rate": ("Reference room rate (USD, double / night)", "money"),
    **_CONTACT,
    "amen_dining": ("Dining", "yesno"), "amen_pool": ("Pool", "yesno"), "amen_gym": ("Gym", "yesno"),
    "amen_room_service": ("Room service", "yesno"), "amen_parking": ("Parking", "yesno"),
    "amen_internet": ("Internet / Wi-Fi", "yesno"), "amen_business_center": ("Business center", "yesno"),
    "amen_pets": ("Pets allowed", "yesno")}
FIELDS["platform_restaurants"] = {
    "rating": ("Rating (out of 5)", "rating5"), "class": ("Class", "text"), "cuisine": ("Cuisine / specialty", "text"),
    "currency": ("Currency", "currency"), "price_from": ("Price per person — from", "money"),
    "price_to": ("Price per person — to", "money"), "group_suitable": ("Group suitable", "yesno"), **_CONTACT}
# The platform catalog tables these agents fill: entity -> (table, pk, catalog_sync entity).
CATALOG_ENTITIES = {"platform_accommodation": ("platform_accommodation", "accommodation_id", "accommodation"),
                    "platform_restaurants": ("platform_restaurants", "restaurant_id", "restaurants")}
FIELDS["city_flags"] = {"is_checkpoint": ("Overnight checkpoint", "yesno")}
ENTITY_LABELS = {"city_flags": "City (overnight checkpoint)", "cities": "City", "city_distances": "Distance",
                 "platform_pois": "Point of Interest", "platform_accommodation": "Accommodation",
                 "platform_restaurants": "Restaurant"}

AGENTS = {
    "geography": {"label": "Geography", "icon": "globe-americas", "entity": "cities", "batch": 10, "max": 40,
                  "blurb": "Finds coordinates, time zone and altitude for cities that are missing them."},
    "distances": {"label": "Distances & Drive Times", "icon": "signpost-split", "entity": "city_distances", "batch": 8,
                  "max": 30, "blurb": "Finds road distance, typical drive time, main route and rail for city pairs."},
    "pois": {"label": "POI Enrichment", "icon": "geo-alt", "entity": "platform_pois", "batch": 4, "max": 24,
             "blurb": "Fills in descriptions, opening hours, entry fees, year founded, website, phone and coordinates."},
    "accommodation": {"label": "Accommodation Enrichment", "icon": "building", "entity": "platform_accommodation",
                      "batch": 4, "max": 24,
                      "blurb": "Fills in star rating, rooms, address, phone, email, website, coordinates, amenities and "
                               "a Reference Room Rate for hotels, resorts and guest houses."},
    "restaurants": {"label": "Restaurant Enrichment", "icon": "cup-hot", "entity": "platform_restaurants", "batch": 4,
                    "max": 24,
                    "blurb": "Fills in rating, class, cuisine, price per person, group suitability, address, phone, "
                             "website and coordinates for restaurants."},
    "pdf": {"label": "POIs from a PDF", "icon": "file-earmark-pdf", "entity": "platform_pois", "batch": 1, "max": 1,
            "blurb": "Reads a PDF (brochure, guidebook, report): proposes new Points of Interest and updates to known "
                     "ones, and stages its photos for the Master Image Catalog."},
}

AGENTS["pdf_accommodation"] = {
    "label": "Accommodation from a PDF", "icon": "file-earmark-pdf", "entity": "platform_accommodation", "batch": 1,
    "max": 1, "blurb": "Reads a PDF (a hotel directory, brochure, rate sheet or fact sheet): proposes new properties and "
                       "updates to known ones, and stages its photos for the Master Image Catalog."}
AGENTS["pdf_restaurants"] = {
    "label": "Restaurants from a PDF", "icon": "file-earmark-pdf", "entity": "platform_restaurants", "batch": 1,
    "max": 1, "blurb": "Reads a PDF (a food guide, menu, brochure or listing): proposes new restaurants and updates to "
                       "known ones, and stages its photos for the Master Image Catalog."}
PDF_AGENTS = ("pdf", "pdf_accommodation", "pdf_restaurants")

# The TMS Agents menu (Zeb, Oct 2026): agents grouped by what they do.
SECTIONS = {
    "enrichment": {"label": "Content Enrichment", "icon": "magic", "agents": ("pois", "accommodation", "restaurants"),
                   "blurb": "Research the web to fill the gaps in the Points of Interest, Accommodation and Restaurants "
                            "catalogs."},
    "images": {"label": "Image Collectors", "icon": "images", "agents": (),
               "blurb": "Find photos for the Master Image Catalogs of Points of Interest, Hotels and Restaurants. "
                        "Tenants inherit what you keep."},
    "pdf": {"label": "PDF Agents", "icon": "file-earmark-pdf", "agents": PDF_AGENTS,
            "blurb": "Read PDFs (guides, brochures, directories) for content and images: new records and updates, "
                     "with every value tied to its page."},
    "geo": {"label": "Geography & Distances", "icon": "globe-americas", "agents": ("geography", "distances"),
            "blurb": "Coordinates, time zones and altitude for cities; road distances and drive times between them."},
}
SECTION_OF = {a: k for k, sec in SECTIONS.items() for a in sec["agents"]}

# New Points of Interest proposed by the PDF agent (pdf_poi_agent.py): created when approved.
FIELDS["platform_pois_new"] = {"name": ("Name", "text"), "city": ("City", "text"), "poi_type": ("POI Type", "text"),
                               **FIELDS["platform_pois"]}

# Proposals can also carry these (the PDF importer's): text added to Notes /
# History (never replacing it) and an Additional Link ("type | url | title").
# Kept out of FIELDS so the enrichment agents don't research them.
POI_EXTRA = {"notes": ("Notes / History (added)", "longtext"), "link": ("Additional link", "link")}
# New Accommodation / Restaurants from the PDF agents (pdf_catalog_agent.py), and
# text they add to a record's Notes.
CATALOG_NOTES = {"notes": ("Notes (added)", "longtext")}
FIELDS["platform_accommodation_new"] = {"name": ("Name", "text"), "city": ("City", "text"),
                                        "property_type": ("Property type", "text"), **FIELDS["platform_accommodation"]}
FIELDS["platform_restaurants_new"] = {"name": ("Name", "text"), "city": ("City", "text"), **FIELDS["platform_restaurants"]}
NEW_CATALOG_ENTITIES = {"platform_accommodation_new": "platform_accommodation",
                        "platform_restaurants_new": "platform_restaurants"}
PROPOSAL_FIELDS = {k: dict(v) for k, v in FIELDS.items()}
PROPOSAL_FIELDS["platform_pois"].update(POI_EXTRA)
PROPOSAL_FIELDS["platform_pois_new"].update(POI_EXTRA)
for _e in ("platform_accommodation", "platform_restaurants", "platform_accommodation_new", "platform_restaurants_new"):
    PROPOSAL_FIELDS[_e].update(CATALOG_NOTES)


def link_value(link_type, url, title=None):
    return " | ".join([link_type, url] + ([title] if title else []))


def parse_link_value(value):
    parts = [x.strip() for x in str(value).split(" | ")]
    if len(parts) == 1:
        return None, parts[0], None
    return parts[0], parts[1], (" | ".join(parts[2:]) or None)


def _write_poi_extra(db, poi_id, field, value, source):
    """Notes are appended (unless already there); a link is added unless
    the POI has it."""
    if field == "notes":
        row = db.execute("SELECT notes FROM platform_pois WHERE poi_id = ?", (poi_id,)).fetchone()
        if row is None:
            raise AgentError("the POI no longer exists")
        cur = row[0] or ""
        if value.strip() and value.strip() not in cur:
            db.execute("UPDATE platform_pois SET notes = ?, updated_at = datetime('now') WHERE poi_id = ?",
                       ((cur.rstrip() + "\n\n" if cur.strip() else "") + value.strip(), poi_id))
    elif field == "link":
        import poi_links
        typ, url, title = parse_link_value(value)
        poi_links.add(db, poi_id, url, typ, title, source)
ENTITY_LABELS["platform_pois_new"] = "New Point of Interest"
ENTITY_LABELS["platform_accommodation_new"] = "New Accommodation"
ENTITY_LABELS["platform_restaurants_new"] = "New Restaurant"


def _number(v):
    """The first number in a value like '217 m' or '1,234 km'."""
    m = re.search(r"-?\d[\d,]*(?:\.\d+)?", str(v))
    if not m:
        raise ValueError("no number")
    return float(m.group(0).replace(",", ""))


def validate(kind, raw):
    """(clean value, None) or (None, error)."""
    v = (raw or "").strip() if isinstance(raw, str) else raw
    if v in (None, ""):
        return None, "empty value"
    try:
        if kind in ("lat", "lon"):
            f = _number(v)
            if (kind == "lat" and not -90 <= f <= 90) or (kind == "lon" and not -180 <= f <= 180):
                return None, "out of range"
            return round(f, 6), None
        if kind == "real":
            f = _number(v)
            return (round(f, 1), None) if f > 0 else (None, "must be positive")
        if kind == "int":
            return int(round(_number(v))), None
        if kind == "stars":
            n = int(round(_number(v)))
            return (n, None) if 1 <= n <= 5 else (None, "stars must be 1 to 5")
        if kind == "rating5":
            f = _number(v)
            return (round(f, 1), None) if 0 <= f <= 5 else (None, "rating must be 0 to 5")
        if kind == "money":
            f = _number(v)
            return (round(f, 2), None) if f > 0 else (None, "must be positive")
        if kind == "currency":
            c = str(v).strip().upper()
            return (c, None) if re.fullmatch(r"[A-Z]{3}", c) else (None, "not a 3-letter currency code")
        if kind == "email":
            e = str(v).strip()
            return (e, None) if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", e) else (None, "not an email address")
        if kind == "tz":
            from zoneinfo import available_timezones
            return (v, None) if v in available_timezones() else (None, "not an IANA time zone")
        if kind == "yesno":
            s = str(v).strip().lower()
            return ("Yes", None) if s in ("yes", "y", "true") else ("No", None) if s in ("no", "n", "false") else (None, "Yes or No")
        if kind == "url":
            return (v, None) if str(v).startswith(("http://", "https://")) else (None, "not a web address")
        if kind == "longtext":
            return str(v)[:12000], None
        if kind == "link":
            return (str(v)[:2000], None) if "http" in str(v) else (None, "not a web address")
    except (TypeError, ValueError):
        return None, "not a number"
    return str(v)[:2000], None


# ---- seams (replaced in tests) -----------------------------------------------------------------

def anthropic_client():
    from config import Config
    key = getattr(Config, "PLATFORM_ANTHROPIC_API_KEY", None) or Config.ANTHROPIC_API_KEY
    if not key:
        raise AgentError("No Anthropic API key is configured on the server (PLATFORM_ANTHROPIC_API_KEY or ANTHROPIC_API_KEY).")
    try:
        import anthropic
    except ImportError as e:
        raise AgentError("The 'anthropic' package isn't installed on the server.") from e
    return anthropic.Anthropic(api_key=key, timeout=agent_runs.API_TIMEOUT_SECONDS,
                               max_retries=agent_runs.API_MAX_RETRIES)


def model():
    from config import Config
    return Config.ANTHROPIC_MODEL


def explain_api_error(e):
    from image_collector import explain_api_error as explain
    msg = explain(e)
    return msg.replace("ANTHROPIC_API_KEY", "PLATFORM_ANTHROPIC_API_KEY / ANTHROPIC_API_KEY") if msg else None


# ---- what a run covers ---------------------------------------------------------------------------

def _place(db, city_id):
    r = db.execute("""SELECT ci.label AS city, s.label AS state, co.label AS country FROM cities ci
                      JOIN states s ON s.state_id = ci.state_id JOIN countries co ON co.country_id = s.country_id
                      WHERE ci.city_id = ?""", (city_id,)).fetchone()
    return ", ".join(x for x in (r["city"], r["state"], r["country"]) if x) if r else f"city #{city_id}"


def targets(db, agent_key, params):
    """[{key, label, current: {field: value}}] the run will research."""
    p = params or {}
    limit = min(int(p.get("limit") or AGENTS[agent_key]["max"]), AGENTS[agent_key]["max"])
    out = []
    if agent_key == "geography":
        sql = """SELECT ci.* FROM cities ci JOIN states s ON s.state_id = ci.state_id WHERE ci.is_active = 1"""
        args = []
        if p.get("country_id"):
            sql += " AND s.country_id = ?"
            args.append(p["country_id"])
        if p.get("state_id"):
            sql += " AND ci.state_id = ?"
            args.append(p["state_id"])
        if p.get("only_missing", True):
            sql += " AND (ci.latitude IS NULL OR ci.longitude IS NULL OR ci.timezone IS NULL OR ci.altitude_m IS NULL)"
        for r in db.execute(sql + " ORDER BY ci.label LIMIT ?", args + [limit]):
            out.append({"key": str(r["city_id"]), "label": _place(db, r["city_id"]),
                        "current": {f: r[f] for f in FIELDS["cities"]}})
    elif agent_key == "distances":
        pairs = []
        if p.get("from_city_id") and p.get("to_city_ids"):
            a = int(p["from_city_id"])
            pairs = [tuple(sorted((a, int(b)))) for b in p["to_city_ids"] if int(b) != a]
        else:  # gaps in existing rows
            pairs = [(r[0], r[1]) for r in db.execute(
                "SELECT city_a_id, city_b_id FROM city_distances WHERE road_km IS NULL OR drive_minutes IS NULL "
                "OR route_name IS NULL OR rail_available IS NULL ORDER BY distance_id")]
        for a, b in list(dict.fromkeys(pairs))[:limit]:
            r = db.execute("SELECT * FROM city_distances WHERE city_a_id = ? AND city_b_id = ?", (a, b)).fetchone()
            out.append({"key": f"{a}:{b}", "label": f"{_place(db, a)} ↔ {_place(db, b)}",
                        "current": {f: (r[f] if r else None) for f in FIELDS["city_distances"]}})
    elif agent_key == "pois":
        sql = """SELECT p.*, pt.label AS type_label FROM platform_pois p LEFT JOIN poi_types pt ON pt.poi_type_id = p.poi_type_id
                 WHERE p.is_active = 1"""
        args = []
        if p.get("country_id"):
            sql += " AND p.country_id = ?"
            args.append(p["country_id"])
        if p.get("city_id"):
            sql += " AND p.city_id = ?"
            args.append(p["city_id"])
        if p.get("poi_type_id"):
            sql += " AND p.poi_type_id = ?"
            args.append(p["poi_type_id"])
        if p.get("only_missing", True):
            sql += " AND (" + " OR ".join(f"p.{f} IS NULL OR p.{f} = ''" for f in FIELDS["platform_pois"]) + ")"
        for r in db.execute(sql + " ORDER BY p.name LIMIT ?", args + [limit]):
            where = _place(db, r["city_id"]) if r["city_id"] else (r["city_text"] or "")
            out.append({"key": str(r["poi_id"]), "label": f"{r['name']} ({r['type_label'] or 'POI'}, {where})",
                        "current": {f: r[f] for f in FIELDS["platform_pois"]}})
    elif agent_key in ("accommodation", "restaurants"):
        entity = AGENTS[agent_key]["entity"]
        table, pk, _sync = CATALOG_ENTITIES[entity]
        fields = FIELDS[entity]
        sql = f"SELECT p.* FROM {table} p WHERE p.is_active = 1"
        args = []
        for key in ("country_id", "city_id"):
            if p.get(key):
                sql += f" AND p.{key} = ?"
                args.append(p[key])
        if agent_key == "accommodation" and p.get("property_type"):
            sql += " AND p.property_type = ?"
            args.append(p["property_type"])
        if p.get("only_missing", True):
            sql += " AND (" + " OR ".join(f"p.{f} IS NULL OR p.{f} = ''" for f in fields) + ")"
        for r in db.execute(sql + " ORDER BY p.name LIMIT ?", args + [limit]):
            where = _place(db, r["city_id"]) if r["city_id"] else (r["city_text"] or "")
            what = r["property_type"] if agent_key == "accommodation" else (r["cuisine"] or "Restaurant")
            current = {f: ({1: "Yes", 0: "No"}.get(r[f]) if kind == "yesno" else r[f]) for f, (_l, kind) in fields.items()}
            out.append({"key": str(r[pk]), "label": f"{r['name']} ({what or 'Property'}, {where})", "current": current})
    return out


# ---- one batch ------------------------------------------------------------------------------------

def _instructions(agent_key):
    if agent_key == "geography":
        return ("For each city, find its centre's latitude and longitude (decimal degrees), its IANA time zone "
                "(e.g. Asia/Karachi) and its altitude above sea level in metres.")
    if agent_key == "distances":
        return ("For each pair of cities, find the usual ROAD distance in km by the main road, the typical driving "
                "time in minutes (drive_minutes; if sources give a range, drive_minutes is the lower and "
                "drive_minutes_max the upper end), the main route or road name (e.g. M-2, N-5, KKH), and whether a "
                "passenger RAIL service connects them (rail_available: Yes or No). Do not give straight-line distances.")
    if agent_key == "accommodation":
        return ("Each item is a hotel, resort or guest house. For each, find: its official star rating (star_rating, "
                "1-5; only an official or clearly published class, otherwise leave it out) and the rating as published "
                "(rating_note, e.g. '4-star heritage'), the number of rooms, a typical published rate for a DOUBLE room "
                "per night converted to US dollars (ref_room_rate: a number in USD; say the original price and currency "
                "and the date or season in note), its street address or landmark, main phone, reservations email, "
                "official website, latitude and longitude in decimal degrees, and whether it has each amenity "
                "(amen_dining = restaurant on site, amen_pool, amen_gym, amen_room_service, amen_parking, amen_internet = "
                "Wi-Fi, amen_business_center, amen_pets = pets allowed: Yes or No, only when a source says so). Prefer "
                "the hotel's own site, then tourism boards and major booking sites. Skip anything you cannot find a "
                "source for.")
    if agent_key == "restaurants":
        return ("Each item is a restaurant. For each, find: its traveller rating out of 5 (rating, e.g. 4.4, from a major "
                "review site), its class when there is no rating (class, e.g. Upscale, Casual, Hotel dining), cuisine or "
                "specialty (comma-separated), the typical price per person for a meal (price_from and price_to as "
                "numbers, with the local currency as a 3-letter code in currency), whether it can seat a tour group of "
                "15-30 (group_suitable: Yes or No, only when a source suggests it), its street address or landmark, "
                "phone, email, website (official site or main listing), and latitude and longitude in decimal degrees. "
                "Skip anything you cannot find a source for.")
    return ("For each place, find: a factual description (2-4 sentences: what it is and why it matters to visitors), "
            "the year founded or era, the days and hours it is open, the entry fee (with currency, and foreigner/local "
            "prices if different), its official website, a phone number, a street address or landmark, and its "
            "latitude and longitude in decimal degrees. Prefer official and government / tourism-board sources, "
            "then reputable references. Skip anything you cannot find a source for.")


def research_batch(client, agent_key, batch, meter):
    """[{key, field, value, confidence, source_url, note}] for one batch."""
    fields = FIELDS[AGENTS[agent_key]["entity"]]
    items = []
    for t in batch:
        have = {f: v for f, v in t["current"].items() if v not in (None, "")}
        items.append({"ref": t["key"], "name": t["label"], "already_known": have})
    prompt = (
        f"You are researching reference data for a tourism platform. {_instructions(agent_key)}\n\n"
        "Use web search. Only report a value you found in a source, and give that source page's URL (taken from the "
        "search results, never invented). Give a confidence from 0 to 1 (1 = stated clearly by an official or "
        "authoritative source; 0.5 = found but sources disagree or are informal). Values listed under already_known "
        "are in the database: only report one of those if a source clearly shows it is wrong, and say why in note.\n\n"
        f"Allowed fields: {', '.join(f'{k} ({v[0]})' for k, v in fields.items())}.\n\nItems:\n{json.dumps(items, ensure_ascii=False, indent=1)}\n\n"
        "When done, call report_findings once with everything you found.")
    tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": max(3, 2 * len(batch))},
             {"name": "report_findings", "description": "Report the values found, each with its source.",
              "input_schema": {"type": "object", "properties": {"findings": {"type": "array", "items": {
                  "type": "object", "properties": {
                      "ref": {"type": "string"}, "field": {"type": "string", "enum": list(fields)},
                      "value": {"type": "string"}, "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                      "source_url": {"type": "string"}, "note": {"type": "string"}},
                  "required": ["ref", "field", "value", "source_url"]}}}, "required": ["findings"]}}]
    messages = [{"role": "user", "content": prompt}]
    found = None
    for _ in range(5):
        resp = agent_runs.create_message(client, model=model(), max_tokens=4000, tools=tools, messages=messages)
        meter(getattr(resp, "usage", None))
        for b in getattr(resp, "content", []) or []:
            if getattr(b, "type", None) == "tool_use" and getattr(b, "name", None) == "report_findings":
                found = b.input or {}
        if found is not None or getattr(resp, "stop_reason", None) != "pause_turn":
            break
        messages = messages + [{"role": "assistant", "content": resp.content}]
    refs = {t["key"] for t in batch}
    out = []
    for f in (found or {}).get("findings") or []:
        if f.get("ref") in refs and f.get("field") in fields and str(f.get("source_url", "")).startswith("http"):
            out.append(f)
    return out


# ---- runs ---------------------------------------------------------------------------------------------

def start_run(db, agent_key, params, user_id, label, background=True):
    if agent_key not in AGENTS:
        raise AgentError("Unknown agent.")
    anthropic_client()
    found = targets(db, agent_key, params)
    if not found:
        raise AgentError("Nothing to research for that choice: everything is already filled in, or nothing matches.")
    cur = db.execute("INSERT INTO platform_agent_runs (agent_key, scope_label, params, created_by, items_total) "
                     "VALUES (?, ?, ?, ?, ?)", (agent_key, f"{label} ({len(found)})", json.dumps(params), user_id, len(found)))
    db.commit()
    run_id = cur.lastrowid
    launch(run_id, background)
    return run_id


def launch(run_id, background=True):
    if background:
        threading.Thread(target=_run_in_thread, args=(run_id,), daemon=True).start()
    else:
        _run_in_thread(run_id)


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
        run(db, run_id)
    finally:
        db.close()


def run(db, run_id):
    r = db.execute("SELECT * FROM platform_agent_runs WHERE run_id = ?", (run_id,)).fetchone()
    agent_key = r["agent_key"]
    entity = AGENTS[agent_key]["entity"]
    lines = []

    def log(text):
        lines.append(text)
        db.execute("UPDATE platform_agent_runs SET progress = ?, heartbeat_at = datetime('now') WHERE run_id = ?",
                   ("\n".join(lines)[-8000:], run_id))
        db.commit()

    def meter(usage):
        ai_usage.record(db, None, f"TMS Agent: {AGENTS[agent_key]['label']}", model(), usage,
                        user_id=r["created_by"], ref_type="platform_agent_runs", ref_id=run_id, note=r["scope_label"])

    db.execute("UPDATE platform_agent_runs SET status = 'running', started_at = datetime('now'), heartbeat_at = datetime('now') "
               "WHERE run_id = ? AND status = 'queued'", (run_id,))
    db.commit()
    total = 0
    try:
        client = anthropic_client()
        if agent_key in PDF_AGENTS:
            stop = lambda: agent_runs.cancelled(db, "platform_agent_runs", run_id)  # noqa: E731
            if agent_key == "pdf":
                import pdf_poi_agent
                total = pdf_poi_agent.run(db, run_id, client, log, meter, stop)
            else:
                import pdf_catalog_agent
                total = pdf_catalog_agent.run(db, run_id, agent_key, client, log, meter, stop)
            if agent_runs.cancelled(db, "platform_agent_runs", run_id):
                return
            log(f"Done: {total} value(s) to review." if total else "Done: nothing new was found.")
            db.execute("UPDATE platform_agent_runs SET status = 'done', items_done = items_total, finished_at = datetime('now') "
                       "WHERE run_id = ? AND status = 'running'", (run_id,))
            db.commit()
            return
        found = targets(db, agent_key, json.loads(r["params"] or "{}"))
        by_key = {t["key"]: t for t in found}
        size = AGENTS[agent_key]["batch"]
        db.execute("UPDATE platform_agent_runs SET items_total = ? WHERE run_id = ?", (len(found), run_id))
        for i in range(0, len(found), size):
            db.execute("UPDATE platform_agent_runs SET items_done = ?, heartbeat_at = datetime('now') WHERE run_id = ?",
                       (i, run_id))
            db.commit()
            if agent_runs.cancelled(db, "platform_agent_runs", run_id):
                return
            batch = found[i:i + size]
            log(f"Batch {i // size + 1}: " + "; ".join(t["label"] for t in batch))
            try:
                findings = research_batch(client, agent_key, batch, meter)
            except Exception as e:
                fatal = explain_api_error(e)
                if fatal:
                    raise AgentError(fatal) from e
                log(f"   failed: {e}")
                continue
            n = 0
            for f in findings:
                kind = FIELDS[entity][f["field"]][1]
                value, err = validate(kind, f.get("value"))
                if err:
                    continue
                current = by_key[f["ref"]]["current"].get(f["field"])
                if current not in (None, "") and str(current).strip().lower() == str(value).strip().lower():
                    continue
                db.execute("""INSERT INTO platform_agent_proposals (run_id, entity, record_key, record_label, field,
                                  current_value, proposed_value, confidence, source_url, note)
                              VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                           (run_id, entity, f["ref"], by_key[f["ref"]]["label"], f["field"],
                            None if current in (None, "") else str(current), str(value),
                            f.get("confidence"), f["source_url"][:500], (f.get("note") or "")[:500] or None))
                n += 1
            total += n
            db.execute("UPDATE platform_agent_runs SET proposals = ? WHERE run_id = ?", (total, run_id))
            db.commit()
            log(f"   {n} value(s) proposed")
        if agent_runs.cancelled(db, "platform_agent_runs", run_id):
            return
        log(f"Done: {total} value(s) to review." if total else "Done: nothing new was found.")
        db.execute("UPDATE platform_agent_runs SET status = 'done', items_done = items_total, finished_at = datetime('now') "
                   "WHERE run_id = ? AND status = 'running'", (run_id,))
        db.commit()
    except Exception as e:
        from image_collector import friendly_api_error
        msg = friendly_api_error(e) or str(e) or traceback.format_exc()[-800:]
        log(f"Stopped: {msg}")
        db.execute("UPDATE platform_agent_runs SET status = 'failed', error = ?, finished_at = datetime('now') "
                   "WHERE run_id = ? AND status = 'running'", (msg[:1000], run_id))
        db.commit()


def mark_stale(db):
    """Runs that stopped responding (a hung request, or a server restart)
    are marked failed -- see agent_runs.py."""
    agent_runs.mark_stale(db, "platform_agent_runs")


def run_cost(db, run_id):
    return db.execute("SELECT COALESCE(SUM(cost_usd), 0), COUNT(*), COALESCE(SUM(web_searches), 0) FROM ai_usage_log "
                      "WHERE ref_type = 'platform_agent_runs' AND ref_id = ?", (run_id,)).fetchone()


# ---- approving ------------------------------------------------------------------------------------------

def decide(db, proposal_ids, approve, user_id):
    """Approve (write) or reject proposals. Returns (applied, rejected, errors)."""
    applied = rejected = 0
    errors, touched_pois, touched = [], set(), {}
    today = date.today().isoformat()
    for pid in proposal_ids:
        p = db.execute("SELECT * FROM platform_agent_proposals WHERE proposal_id = ? AND status = 'pending'",
                       (pid,)).fetchone()
        if p is None:
            continue
        if not approve:
            db.execute("UPDATE platform_agent_proposals SET status = 'rejected', decided_by = ?, decided_at = datetime('now') "
                       "WHERE proposal_id = ?", (user_id, pid))
            rejected += 1
            continue
        kind = PROPOSAL_FIELDS[p["entity"]][p["field"]][1]
        value, err = validate(kind, p["proposed_value"])
        try:
            if err:
                raise AgentError(err)
            if p["entity"] == "platform_pois_new":
                import pdf_poi_agent
                if p["record_key"].startswith("new:"):
                    pdf_poi_agent.create_new_poi(db, p["record_key"], user_id)
                    p = db.execute("SELECT * FROM platform_agent_proposals WHERE proposal_id = ?", (pid,)).fetchone()
                touched_pois.add(int(p["record_key"]))
                if p["field"] in pdf_poi_agent.NEW_FIELDS:  # went into the record when it was created
                    if p["status"] == "pending":
                        db.execute("UPDATE platform_agent_proposals SET status = 'approved', decided_by = ?, "
                                   "decided_at = datetime('now'), error = NULL WHERE proposal_id = ?", (user_id, pid))
                    applied += 1
                    continue
                if p["field"] in POI_EXTRA:
                    _write_poi_extra(db, int(p["record_key"]), p["field"], value, p["note"] or p["source_url"])
                else:
                    cur = db.execute(f"UPDATE platform_pois SET {p['field']} = ?, updated_at = datetime('now') WHERE poi_id = ?",
                                     (value, int(p["record_key"])))
                    if not cur.rowcount:
                        raise AgentError("the POI no longer exists")
            elif p["entity"] == "city_flags":
                db.execute("UPDATE cities SET is_checkpoint = ? WHERE city_id = ?",
                           (1 if value == "Yes" else 0, int(p["record_key"])))
            elif p["entity"] == "cities":
                db.execute(f"UPDATE cities SET {p['field']} = ? WHERE city_id = ?", (value, int(p["record_key"])))
            elif p["entity"] == "platform_pois" and p["field"] in POI_EXTRA:
                _write_poi_extra(db, int(p["record_key"]), p["field"], value, p["note"] or p["source_url"])
                touched_pois.add(int(p["record_key"]))
            elif p["entity"] == "platform_pois":
                cur = db.execute(f"UPDATE platform_pois SET {p['field']} = ?, checked_on = ?, updated_at = datetime('now'), "
                                 "source = COALESCE(NULLIF(source, ''), ?) WHERE poi_id = ?",
                                 (value, today, f"TMS Agent: {p['source_url']}", int(p["record_key"])))
                if not cur.rowcount:
                    raise AgentError("the POI no longer exists")
                touched_pois.add(int(p["record_key"]))
            elif p["entity"] in CATALOG_ENTITIES or p["entity"] in NEW_CATALOG_ENTITIES:
                base_entity = NEW_CATALOG_ENTITIES.get(p["entity"], p["entity"])
                table, pk, sync_entity = CATALOG_ENTITIES[base_entity]
                if p["entity"] in NEW_CATALOG_ENTITIES:
                    import pdf_catalog_agent
                    if p["record_key"].startswith("new:"):
                        pdf_catalog_agent.create_new_record(db, p["record_key"], p["entity"], user_id)
                        p = db.execute("SELECT * FROM platform_agent_proposals WHERE proposal_id = ?", (pid,)).fetchone()
                    touched.setdefault(sync_entity, set()).add(int(p["record_key"]))
                    if p["field"] in pdf_catalog_agent.NEW_FIELDS:  # went into the record when it was created
                        if p["status"] == "pending":
                            db.execute("UPDATE platform_agent_proposals SET status = 'approved', decided_by = ?, "
                                       "decided_at = datetime('now'), error = NULL WHERE proposal_id = ?", (user_id, pid))
                        applied += 1
                        continue
                if p["field"] == "notes":
                    _append_notes(db, table, pk, int(p["record_key"]), value)
                    touched.setdefault(sync_entity, set()).add(int(p["record_key"]))
                    db.execute("UPDATE platform_agent_proposals SET status = 'approved', decided_by = ?, "
                               "decided_at = datetime('now'), error = NULL WHERE proposal_id = ?", (user_id, pid))
                    applied += 1
                    continue
                if kind == "yesno":
                    value = 1 if value == "Yes" else 0
                extra = ", ref_rate_as_of = ?" if p["field"] == "ref_room_rate" else ""
                cur = db.execute(f"UPDATE {table} SET {p['field']} = ?{extra}, checked_on = ?, updated_at = datetime('now'), "
                                 f"source = COALESCE(NULLIF(source, ''), ?) WHERE {pk} = ?",
                                 (value,) + ((today,) if extra else ()) +
                                 (today, f"TMS Agent: {p['source_url']}", int(p["record_key"])))
                if not cur.rowcount:
                    raise AgentError("the record no longer exists")
                touched.setdefault(sync_entity, set()).add(int(p["record_key"]))
            elif p["entity"] == "city_distances":
                a, b = (int(x) for x in p["record_key"].split(":"))
                db.execute("INSERT OR IGNORE INTO city_distances (city_a_id, city_b_id) VALUES (?, ?)", (a, b))
                db.execute(f"UPDATE city_distances SET {p['field']} = ?, verified_on = ?, updated_at = datetime('now'), "
                           "source = COALESCE(NULLIF(source, ''), ?) WHERE city_a_id = ? AND city_b_id = ?",
                           (value, today, f"TMS Agent: {p['source_url']}", a, b))
            db.execute("UPDATE platform_agent_proposals SET status = 'approved', decided_by = ?, decided_at = datetime('now'), "
                       "error = NULL WHERE proposal_id = ?", (user_id, pid))
            applied += 1
        except Exception as e:
            db.execute("UPDATE platform_agent_proposals SET error = ? WHERE proposal_id = ?", (str(e)[:300], pid))
            errors.append(f"{p['record_label']} — {PROPOSAL_FIELDS[p['entity']][p['field']][0]}: {e}")
    db.commit()
    if touched_pois:
        touched["pois"] = touched_pois
    if touched:
        import catalog_sync
        for sync_entity, ids in touched.items():
            catalog_sync.push(db, sync_entity, sorted(ids))
        db.commit()
    return applied, rejected, errors


def _append_notes(db, table, pk, rid, value):
    """Text from a PDF is added to the record's Notes (unless already there)."""
    row = db.execute(f"SELECT notes FROM {table} WHERE {pk} = ?", (rid,)).fetchone()
    if row is None:
        raise AgentError("the record no longer exists")
    cur = row[0] or ""
    if value.strip() and value.strip() not in cur:
        db.execute(f"UPDATE {table} SET notes = ?, updated_at = datetime('now') WHERE {pk} = ?",
                   ((cur.rstrip() + "\n\n" if cur.strip() else "") + value.strip(), rid))


def pending_count(db):
    return db.execute("SELECT COUNT(*) FROM platform_agent_proposals WHERE status = 'pending'").fetchone()[0]
