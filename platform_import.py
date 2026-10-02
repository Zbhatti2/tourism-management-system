"""
Platform Data Import -- entity-based Excel/CSV import into the shared
platform tables (the SystemAdmin's "Data Import" wizard,
blueprints/platform_import.py).

One template per entity (ENTITIES below): Cities, City Distances, Transport
Hubs, Points of Interest, Accommodation, Restaurants, Embassies &
Consulates. The same column definitions generate the downloadable blank
templates (template_workbook()) and read uploaded files, so the two can't
drift apart.

Flow: parse_file() -> evaluate() labels every row New / Update / Possible
duplicate / Problem and stores it in platform_import_rows (nothing touches
the catalogs yet) -> the SystemAdmin reviews, decides each possible
duplicate (merge / keep as new / skip) -> apply() writes.

Rules
-----
* "N/V", "N/A", "verify", "-" ... are blank. Nothing is guessed.
* An update only fills EMPTY fields; existing values are never overwritten.
* Notes are appended under a dated heading, never replaced.
* Notes-only mode reads Name + City + Notes and ignores every other column;
  it never creates records -- an unmatched row is a Problem.
* Duplicate checks run against the database and against earlier rows in
  the same file: same name once tidied, sounds-alike, very similar, or the
  same type within 150 m (fuzzy.py / platform_merge.py).
"""
import csv
import io
import json
import re
from datetime import date, datetime

from fuzzy import compare, normalize, split_alt_names
from geo_resolve import Resolver
from platform_catalog import AMENITIES, MERGEABLE, MISSION_TYPES, NEW_POI_TYPES, PROPERTY_TYPES
from platform_merge import REASONS, candidates

NO_PROVINCE = "(Province not set)"
BLANK = {"", "n/v", "nv", "n/a", "na", "-", "—", "–", "verify", "not verified", "unknown", "none", "null"}

# ---- column definitions ------------------------------------------------------
# (template header, required, help text, target column or a special key)

GEO_COLS = [("Country", True, "Country name as in TMS, e.g. Pakistan, China", "@country"),
            ("Province / State", False, "Province or state, e.g. Punjab, Gilgit-Baltistan, Xinjiang", "@state"),
            ("City", True, "City or town. Must be in TMS Geography, or loaded first with the Cities template", "@city")]
COORD_COLS = [("Latitude", False, "Decimal degrees, e.g. 31.5580 (south is negative). Blank if not verified.", "latitude"),
              ("Longitude", False, "Decimal degrees, e.g. 74.3507 (west is negative)", "longitude")]
PHONE = ("Phone", False, "As dialled internationally, e.g. +92 42 111 505 505", "phone")
EMAIL = ("Email", False, "", "email")
WEB = ("Website", False, "Full address starting https://", "website")
TAIL = [("Notes", False, "Anything the template has no column for. Added to existing notes, never replacing them.", "notes"),
        ("Source", False, "Where the information came from (document, website, person)", "source"),
        ("Checked On", False, "Date the information was checked, YYYY-MM-DD", "checked_on")]

ENTITIES = {
    "cities": {
        "label": "Cities", "order": 1, "name_col": "City",
        "purpose": "Adds cities, towns and localities to TMS Geography (shared by every tenant) with coordinates and "
                   "time zone. Load this first: every other template refers to cities by name.",
        "match": "Country + Province/State + City (other spellings included). An existing city only has its empty "
                 "coordinates, time zone and altitude filled.",
        "cols": [("Country", True, "Country name as in TMS, e.g. Pakistan, China", "@country"),
                 ("Province / State", True, "Province or state. A missing one is created under the country when "
                                            "'Add missing provinces and cities' is ticked in the wizard.", "@state"),
                 ("City", True, "City, town or locality name", "@cityname")] + COORD_COLS +
                [("Time Zone", False, "IANA name, e.g. Asia/Karachi, Asia/Urumqi", "timezone"),
                 ("Altitude (m)", False, "Metres above sea level (whole number)", "altitude_m")] + TAIL,
        "lists": {},
    },
    "distances": {
        "label": "City Distances", "order": 2, "name_col": None,
        "purpose": "Road distance and driving time between two cities (Geography & Distances). One row covers both "
                   "directions.",
        "match": "The pair of cities, in either order. An existing pair only has its empty fields filled.",
        "cols": [("From City", True, "City name in TMS", "@city_a"),
                 ("From Province / State", False, "Helps when two cities share a name", "@state_a"),
                 ("From Country", False, "", "@country_a"),
                 ("To City", True, "City name in TMS", "@city_b"),
                 ("To Province / State", False, "", "@state_b"),
                 ("To Country", False, "", "@country_b"),
                 ("Road km", False, "Road distance in km. For a range like 80-90 use the middle and put the range in Notes.", "road_km"),
                 ("Drive Time — From (hrs)", False, "Typical driving time in hours, e.g. 2 or 2.5", "@drive_from"),
                 ("Drive Time — To (hrs)", False, "Upper end of a range, e.g. 2.5 for \"2-2.5 hr\"", "@drive_to"),
                 ("Route / Road", False, "Road or route name, e.g. M-2, KKH, N-5", "route_name"),
                 ("Rail Link", False, "Yes, No, or blank if not known", "rail_available")] + TAIL,
        "lists": {"Rail Link": ["Yes", "No"]},
    },
    "hubs": {
        "label": "Transport Hubs", "order": 3, "name_col": "Hub Name",
        "purpose": "Airports, railway stations, bus terminals and seaports for the shared Transport Hubs list.",
        "match": "Hub Type + Code when there is one, otherwise the name in the same city (other spellings included). "
                 "An existing hub only has its empty fields filled.",
        "cols": [("Hub Type", True, "One of the Hub Types in Platform Lookups", "@hub_type"),
                 ("Hub Name", True, "Official name, e.g. Lahore Junction", "name"),
                 ("Code", False, "IATA code for airports (3 letters); station code if one exists", "code"),
                 ("ICAO Code", False, "Airports only (4 letters)", "icao_code"),
                 ("Country", True, "", "@country"),
                 ("Province / State", False, "", "@state"),
                 ("City / Locality", True, "City or locality the hub serves", "@city")] + COORD_COLS +
                [("Address / Landmark", False, "Street or nearest landmark", "address"),
                 ("Operator", False, "e.g. Pakistan Railways, Daewoo Express", "operator"),
                 ("Scope", False, "International, Domestic or Regional", "scope"),
                 ("Major", False, "Y for a major hub", "is_major"), PHONE, WEB] + TAIL,
        "lists": {"Hub Type": "@hub_types", "Scope": ["International", "Domestic", "Regional"], "Major": ["Y", "N"]},
    },
    "pois": {
        "label": "Points of Interest", "order": 4, "name_col": "POI Name",
        "purpose": "Places worth visiting for the platform Points of Interest catalog, which tenants adopt into their "
                   "own lists.",
        "match": "POI Name + City (other spellings included), or the same type within 150 m. An existing POI only has "
                 "its empty fields filled.",
        "cols": [("POI Name", True, "", "name"),
                 ("POI Type", True, "Pick from the list (POI Types in Platform Lookups)", "@poi_type"),
                 ("Significance / Category", False, "Free text, e.g. Historic / National Memorial", "significance"),
                 ("Year Founded / Era", False, "e.g. 1970, 15th–18th c.", "year_founded")] + GEO_COLS +
                [("Address / Landmark", False, "", "address")] + COORD_COLS + [PHONE, EMAIL, WEB] +
                [("Description", False, "2–3 sentences for tour planners", "description"),
                 ("Entry Fee", False, "As published, e.g. Free, PKR 500", "entry_fee"),
                 ("Days / Hours Open", False, "e.g. Tue–Sun 09:00–17:00", "opening_hours"),
                 ("Image URL", False, "Link to a photo you may use (e.g. Wikimedia Commons)", "image_url"),
                 ("Video URL", False, "YouTube or other video link", "video_url")] + TAIL,
        "lists": {"POI Type": "@poi_types"},
    },
    "accommodation": {
        "label": "Accommodation", "order": 5, "name_col": "Property Name",
        "purpose": "Hotels, resorts and guest houses for the platform Accommodation catalog. Rates are indicative "
                   "only; each tenant keeps its own contracted rates.",
        "match": "Property Name + City (other spellings included), or the same type within 150 m. An existing "
                 "property only has its empty fields filled.",
        "cols": [("Property Name", True, "", "name"),
                 ("Property Type", True, "Hotel, Resort, Guest House, …", "property_type"),
                 ("Star Rating", False, "5, 4, 3, 2 or 1: official or class rating", "star_rating"),
                 ("Rating (as published)", False, "e.g. 4-star heritage, Boutique / 3-star class", "rating_note")] + GEO_COLS +
                [("Address / Landmark", False, "", "address")] + COORD_COLS + [PHONE, EMAIL, WEB] +
                [("No. of Rooms", False, "Whole number", "rooms"),
                 ("Currency", False, "PKR, USD, CNY, …", "currency"),
                 ("Avg Rate Single / Queen", False, "Number only, per night", "rate_single"),
                 ("Avg Rate Double / King", False, "Number only, per night", "rate_double"),
                 ("Rate Year", False, "Year the rates apply to", "rate_year")] +
                [(label, False, "Y or N", col) for col, label in AMENITIES] + TAIL,
        "lists": dict({"Property Type": PROPERTY_TYPES, "Star Rating": [5, 4, 3, 2, 1],
                       "Currency": ["PKR", "USD", "CNY", "EUR", "GBP", "AED"]},
                      **{label: ["Y", "N"] for _c, label in AMENITIES}),
    },
    "restaurants": {
        "label": "Restaurants", "order": 6, "name_col": "Restaurant Name",
        "purpose": "Restaurants and eateries for the platform Restaurants catalog.",
        "match": "Restaurant Name + City (other spellings included), or within 150 m. An existing restaurant only "
                 "has its empty fields filled.",
        "cols": [("Restaurant Name", True, "", "name"),
                 ("Rating (out of 5)", False, "Traveller score, e.g. 4.5", "rating"),
                 ("Class", False, "When there is no score, e.g. Upscale, Premium casual, Hotel dining", "class")] + GEO_COLS +
                [("Address / Landmark", False, "", "address")] + COORD_COLS + [PHONE, EMAIL, WEB] +
                [("Cuisine / Specialty", False, "Comma-separated, e.g. Pakistani, BBQ, Seafood", "cuisine"),
                 ("Currency", False, "", "currency"),
                 ("Price per Person — From", False, "Number only", "price_from"),
                 ("Price per Person — To", False, "Number only", "price_to"),
                 ("Group Suitable", False, "Y if it can seat a tour group", "group_suitable")] + TAIL,
        "lists": {"Currency": ["PKR", "USD", "CNY", "EUR", "GBP", "AED"], "Group Suitable": ["Y", "N"]},
    },
    "embassies": {
        "label": "Embassies & Consulates", "order": 7, "name_col": "Mission Name",
        "purpose": "Diplomatic and consular missions for the shared Embassies & Consulates list.",
        "match": "Mission Name + Host City (other spellings included), for the same represented country. An existing "
                 "mission only has its empty fields filled.",
        "cols": [("Represented Country", True, "The country the mission represents, e.g. Pakistan", "@represented"),
                 ("Mission Type", True, "Embassy, High Commission, Consulate General, …", "mission_type"),
                 ("Mission Name", True, "e.g. Embassy of Pakistan", "name"),
                 ("Host Country", True, "Country where the mission is", "@country"),
                 ("Host City", True, "City where the mission is", "@city"),
                 ("Address", False, "Visitor address", "address"),
                 ("Nearby Landmark", False, "", "landmark")] + COORD_COLS +
                [("Main Phone", False, "", "phone"), ("Visa / Consular Phone", False, "", "visa_phone"),
                 ("Fax", False, "", "fax"), EMAIL, ("Website", False, "Official page", "website"),
                 ("Other Source", False, "Second source for contact details", "other_source"),
                 ("Notes", False, "Services, appointment rules, jurisdiction, …", "notes"),
                 ("Checked On", False, "YYYY-MM-DD", "checked_on")],
        "lists": {"Mission Type": MISSION_TYPES},
    },
}
ORDERED = sorted(ENTITIES, key=lambda k: ENTITIES[k]["order"])
INT_COLS = {"star_rating", "rooms", "rate_year", "altitude_m"}
REAL_COLS = {"latitude", "longitude", "rating", "rate_single", "rate_double", "price_from", "price_to", "road_km"}
YN_COLS = {"is_major", "group_suitable"} | {c for c, _l in AMENITIES}


# ---- reading a file ------------------------------------------------------------

def _clean(v):
    if v is None:
        return None
    if isinstance(v, (datetime, date)):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, float) and v.is_integer() and abs(v) > 1e3:
        v = int(v)
    s = str(v).strip()
    if s.lower() in BLANK or s.lower().startswith("n/v"):
        return None
    return s


def _header(h):
    return re.sub(r"\s*\*\s*$", "", str(h or "")).strip()


def required_headers(entity, mode="normal"):
    """Columns a file must have. Notes-only files need just the name,
    country, city and Notes columns."""
    spec = ENTITIES[entity]
    if mode == "notes":
        geo = [h for h in ("Host Country", "Host City") if any(c[0] == h for c in spec["cols"])] or \
              ["Country"] + [h for h in ("City / Locality", "City") if any(c[0] == h for c in spec["cols"])][:1]
        return [spec["name_col"]] + geo + ["Notes"]
    return [c[0] for c in spec["cols"] if c[1]]


def parse_file(entity, filename, data, mode="normal"):
    """[(row_num, {template header: cleaned value}), ...] from an .xlsx or .csv.
    Raises ValueError with a readable message if the columns don't fit."""
    spec = ENTITIES[entity]
    wanted = [c[0] for c in spec["cols"]]
    if filename.lower().endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        sheet = None
        for ws in wb.worksheets:  # the sheet whose first row has the most template headers
            first = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), ())
            score = len({_header(h) for h in first} & set(wanted))
            if score and (sheet is None or score > sheet[1]):
                sheet = (ws, score)
        if sheet is None:
            raise ValueError(f"No sheet in this file has the {spec['label']} template's columns.")
        rows = list(sheet[0].iter_rows(values_only=True))
    elif filename.lower().endswith(".csv"):
        text = data.decode("utf-8-sig", errors="replace")
        rows = list(csv.reader(io.StringIO(text)))
    else:
        raise ValueError("Upload an Excel (.xlsx) or CSV file.")
    if not rows:
        raise ValueError("The file is empty.")
    headers = [_header(h) for h in rows[0]]
    missing = [h for h in required_headers(entity, mode) if h not in headers]
    if missing:
        raise ValueError("These required columns are missing: " + ", ".join(missing) +
                         ". Use the template for this entity.")
    out = []
    for i, r in enumerate(rows[1:], start=2):
        rec = {h: _clean(v) for h, v in zip(headers, r) if h in wanted}
        if any(v is not None for v in rec.values()):
            out.append((i, rec))
    return out


# ---- value parsing ---------------------------------------------------------------

def _num(v, kind=float):
    if v is None:
        return None
    m = re.findall(r"-?\d[\d,]*(?:\.\d+)?", str(v))
    if not m:
        raise ValueError(f"'{v}' is not a number")
    n = float(m[0].replace(",", ""))
    return int(round(n)) if kind is int else n


def _yn(v):
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in ("y", "yes", "1", "true"):
        return 1
    if s in ("n", "no", "0", "false"):
        return 0
    raise ValueError(f"'{v}' should be Y or N")


def _notes_block(notes, source, when=None):
    head = f"— {when or date.today().isoformat()}{', ' + source if source else ''} —"
    return f"{head}\n{notes}"


# ---- evaluating rows ------------------------------------------------------------

class Ctx:
    """Per-run lookups."""
    def __init__(self, db, options):
        self.db = db
        self.options = options
        self.geo = Resolver(db)
        self.poi_types = [dict(r) for r in db.execute(
            """SELECT pt.poi_type_id AS id, pt.label FROM poi_types pt JOIN tenants t ON t.tenant_id = pt.tenant_id
               WHERE t.is_platform = 1 AND pt.is_system = 1 AND pt.is_active = 1""")]
        self.hub_types = [dict(r) for r in db.execute("SELECT hub_type_id AS id, code, label FROM hub_types WHERE is_active = 1")]


def _pick(text, items):
    """Item whose label matches text (same / sounds / similar)."""
    for level in ("same", "sounds", "similar"):
        for it in items:
            if compare(text, it["label"]) == level:
                return it
    return None


def _resolve_place(ctx, country_txt, state_txt, city_txt, notes, allow_create):
    """-> dict(country_id, region_id, state_id, city_id, new_state, new_city) or raises ValueError."""
    country, how = ctx.geo.country(country_txt)
    if country is None or how == "similar":
        raise ValueError(f"Country '{country_txt}' isn't in TMS geography.")
    out = {"country_id": country["country_id"], "region_id": country["region_id"], "state_id": None,
           "city_id": None, "new_state": None, "new_city": None}
    if state_txt:
        state, how = ctx.geo.state(country["country_id"], state_txt)
        if state is not None and how in ("same", "sounds"):
            out["state_id"] = state["state_id"]
            if how != "same":
                notes.append(f"Province '{state_txt}' read as '{state['label']}'.")
        elif allow_create:
            out["new_state"] = state_txt
        else:
            notes.append(f"Province '{state_txt}' isn't in TMS (ignored).")
    if city_txt:
        city, how = ctx.geo.city(country["country_id"], out["state_id"], city_txt)
        if city is not None and how in ("same", "sounds"):
            out["city_id"], out["state_id"] = city["city_id"], city["state_id"]
            out["new_state"] = None
            if how != "same":
                notes.append(f"City '{city_txt}' read as '{city['label']}'.")
        elif allow_create:
            if not (out["state_id"] or out["new_state"]):
                # No province given (common abroad): file it under the country's
                # "(Province not set)" placeholder, which the SystemAdmin can
                # rename or move later in Geography & Distances.
                ph, how = ctx.geo.state(country["country_id"], NO_PROVINCE)
                out["state_id"], out["new_state"] = (ph["state_id"], None) if ph is not None and how == "same" else (None, NO_PROVINCE)
            out["new_city"] = city_txt
            notes.append(f"New city '{city_txt}'" + (f" in new province '{out['new_state']}'" if out["new_state"] else "") + " will be added to TMS geography.")
        else:
            hint = f" Did you mean '{city['label']}'?" if city is not None else ""
            raise ValueError(f"City '{city_txt}' isn't in TMS geography.{hint} Load it with the Cities template "
                             "first, or tick 'Add missing provinces and cities'.")
    return out


def _map_values(entity, rec):
    """Template values -> table columns (plain columns only), with number/YN
    parsing. Raises ValueError listing every bad cell."""
    vals, errs = {}, []
    for header, _req, _help, target in ENTITIES[entity]["cols"]:
        if target.startswith("@") or rec.get(header) is None:
            continue
        v = rec[header]
        try:
            if target in INT_COLS:
                v = _num(v, int)
            elif target in REAL_COLS:
                v = _num(v)
            elif target in YN_COLS:
                v = _yn(v)
            elif target == "rail_available":
                v = {"y": "Yes", "yes": "Yes", "n": "No", "no": "No"}.get(str(v).lower())
            elif target in ("code", "icao_code", "currency"):
                v = str(v).upper()
        except ValueError as e:
            errs.append(f"{header}: {e}")
            continue
        vals[target] = v
    lat, lon = vals.get("latitude"), vals.get("longitude")
    if lat is None and rec.get("Latitude") and "," in str(rec.get("Latitude")):  # "31.55, 74.35" in one cell
        parts = re.findall(r"-?\d+(?:\.\d+)?", str(rec["Latitude"]))
        if len(parts) >= 2:
            lat, lon = float(parts[0]), float(parts[1])
            vals["latitude"], vals["longitude"] = lat, lon
    if (lat is None) != (lon is None):
        errs.append("Latitude and Longitude must both be given (or both left blank).")
    elif lat is not None and not (-90 <= lat <= 90 and -180 <= lon <= 180):
        errs.append("Coordinates are out of range.")
    if vals.get("star_rating") is not None and not 1 <= vals["star_rating"] <= 5:
        errs.append("Star Rating must be 1 to 5.")
    if vals.get("rating") is not None and not 0 <= vals["rating"] <= 5:
        errs.append("Rating must be between 0 and 5.")
    if errs:
        raise ValueError(" ".join(errs))
    return vals


def evaluate(db, entity, mode, rows, options):
    """Label every row. Returns a list of staged dicts:
    {row_num, data, status, match_id, match_row, match_label, message, decision}."""
    ctx = Ctx(db, options)
    if entity == "cities":
        return _eval_cities(ctx, rows)
    if entity == "distances":
        return _eval_distances(ctx, rows)
    return _eval_catalog(ctx, entity, mode, rows)


def _staged(row_num, rec, status, message="", **kw):
    d = {"row_num": row_num, "data": {"raw": rec}, "status": status, "match_id": None, "match_row": None,
         "match_label": None, "message": message, "decision": None}
    d.update(kw)
    return d


def _eval_catalog(ctx, entity, mode, rows):
    spec, espec = MERGEABLE[entity], ENTITIES[entity]
    allow_create = bool(ctx.options.get("create_places")) and mode == "normal"
    out, seen = [], []  # seen: (staged, name, city key, lat, lon, type) for within-file checks
    for row_num, rec in rows:
        notes = []
        name = rec.get(espec["name_col"])
        country_txt = rec.get("Host Country") if entity == "embassies" else rec.get("Country")
        city_txt = rec.get("Host City") or rec.get("City / Locality") or rec.get("City")
        # required fields
        if mode == "notes":
            missing = [h for h in required_headers(entity, "notes") if not rec.get(h)]
        else:
            missing = [h for h, req, _x, _t in espec["cols"] if req and not rec.get(h)]
        if missing:
            out.append(_staged(row_num, rec, "problem", "Missing: " + ", ".join(missing) + "."))
            continue
        try:
            place = _resolve_place(ctx, country_txt, rec.get("Province / State"), city_txt, notes, allow_create)
            vals = {} if mode == "notes" else _map_values(entity, rec)
            if mode == "notes":
                vals["notes"] = rec.get("Notes")
                vals["source"] = rec.get("Source")
            type_value = None
            if mode == "normal":
                if entity == "pois":
                    t = _pick(rec["POI Type"], ctx.poi_types)
                    if t is None:
                        raise ValueError(f"POI Type '{rec['POI Type']}' isn't one of the platform POI Types (Platform Lookups).")
                    vals["poi_type_id"] = type_value = t["id"]
                elif entity == "hubs":
                    t = _pick(rec["Hub Type"], ctx.hub_types)
                    if t is None:
                        raise ValueError(f"Hub Type '{rec['Hub Type']}' isn't one of the Hub Types (Platform Lookups).")
                    vals["hub_type_id"] = type_value = t["id"]
                elif entity == "accommodation":
                    pt = next((p for p in PROPERTY_TYPES if normalize(p) == normalize(rec["Property Type"], False)), None)
                    if pt is None:
                        raise ValueError(f"Property Type '{rec['Property Type']}' should be one of: {', '.join(PROPERTY_TYPES)}.")
                    vals["property_type"] = type_value = pt
                elif entity == "embassies":
                    mt = next((m for m in MISSION_TYPES if normalize(m, False) == normalize(rec["Mission Type"], False)), None)
                    if mt is None:
                        raise ValueError(f"Mission Type '{rec['Mission Type']}' should be one of: {', '.join(MISSION_TYPES)}.")
                    vals["mission_type"] = type_value = mt
                    rc, how = ctx.geo.country(rec["Represented Country"])
                    if rc is None or how == "similar":
                        raise ValueError(f"Represented Country '{rec['Represented Country']}' isn't in TMS geography.")
                    vals["represented_country_id"] = rc["country_id"]
        except ValueError as e:
            out.append(_staged(row_num, rec, "problem", str(e)))
            continue
        vals["name"] = name
        data = {"raw": rec, "vals": vals, "place": place, "notes_msgs": notes}
        st = {"row_num": row_num, "data": data, "status": "new", "match_id": None, "match_row": None,
              "match_label": None, "message": " ".join(notes), "decision": None}

        # 1) code match for hubs (strongest)
        match, how = None, None
        if entity == "hubs" and vals.get("code") and type_value:
            r = ctx.db.execute("SELECT hub_id AS id, name FROM transport_hubs WHERE hub_type_id = ? AND code = ?",
                               (type_value, vals["code"])).fetchone()
            if r:
                match, how = r, "same"
        # 2) name / place candidates in the database
        if match is None and place["city_id"]:
            found = candidates(ctx.db, entity, name, place["city_id"], None, vals.get("latitude"), vals.get("longitude"),
                               type_value)
            if entity == "embassies":
                found = [(r, h) for r, h in found if _same_represented(ctx.db, r["id"], vals.get("represented_country_id"))]
            if found:
                match, how = found[0]
        if match is not None:
            st["match_id"], st["match_label"] = match["id"], match["name"]
            if how == "same":
                st["status"] = "update"
            else:
                st["status"], st["decision"] = "duplicate", _default_decision(how)
                st["message"] = (f"{REASONS[how]} as existing '{match['name']}'. " + st["message"]).strip()
        # 3) earlier rows in this file
        if st["status"] == "new":
            ckey = place["city_id"] or normalize(place["new_city"] or "")
            for prev, pname, pkey, plat, plon, ptype in seen:
                if pkey != ckey or prev["status"] == "problem":
                    continue
                if spec.get("same_type_only") and ptype != type_value:
                    continue
                how = compare(name, pname, ignore=[city_txt])
                if how:
                    st["status"], st["match_row"], st["match_label"] = "duplicate", prev["row_num"], pname
                    st["decision"] = _default_decision(how)
                    st["message"] = (f"{REASONS[how]} as row {prev['row_num']} in this file. " + st["message"]).strip()
                    break
        if mode == "notes" and st["status"] == "new":
            st["status"] = "problem"
            st["message"] = (f"No {spec['singular'].lower()} called '{name}' in {city_txt} — a notes-only import "
                             "never creates records. Check the spelling, or import it in Normal mode.")
        out.append(st)
        seen.append((st, name, place["city_id"] or normalize(place["new_city"] or ""), vals.get("latitude"),
                     vals.get("longitude"), type_value))
    return out


def _default_decision(how):
    """Merge only when the names are the same or sound alike; look-alikes
    (Baltit Fort / Altit Fort) and neighbours default to 'keep as new'."""
    return "merge" if how in ("same", "sounds") else "new"


def _same_represented(db, embassy_id, represented_id):
    r = db.execute("SELECT represented_country_id FROM embassies WHERE embassy_id = ?", (embassy_id,)).fetchone()
    return r is not None and r[0] == represented_id


def _eval_cities(ctx, rows):
    allow_create = bool(ctx.options.get("create_places"))
    out, seen = [], {}
    for row_num, rec in rows:
        missing = [h for h in ("Country", "Province / State", "City") if not rec.get(h)]
        if missing:
            out.append(_staged(row_num, rec, "problem", "Missing: " + ", ".join(missing) + "."))
            continue
        notes = []
        try:
            vals = _map_values("cities", rec)
            country, how = ctx.geo.country(rec["Country"])
            if country is None or how == "similar":
                raise ValueError(f"Country '{rec['Country']}' isn't in TMS geography.")
            state, how = ctx.geo.state(country["country_id"], rec["Province / State"])
            new_state = None
            if state is None or how == "similar":
                if not allow_create:
                    raise ValueError(f"Province '{rec['Province / State']}' isn't in TMS. Tick 'Add missing provinces "
                                     "and cities' to add it.")
                new_state, state = rec["Province / State"], None
                notes.append(f"New province '{new_state}' will be added under {country['label']}.")
            elif how != "same":
                notes.append(f"Province '{rec['Province / State']}' read as '{state['label']}'.")
        except ValueError as e:
            out.append(_staged(row_num, rec, "problem", str(e)))
            continue
        place = {"country_id": country["country_id"], "region_id": country["region_id"],
                 "state_id": state["state_id"] if state else None, "new_state": new_state}
        st = {"row_num": row_num, "data": {"raw": rec, "vals": vals, "place": place, "notes_msgs": notes},
              "status": "new", "match_id": None, "match_row": None, "match_label": None,
              "message": " ".join(notes), "decision": None}
        key = (country["country_id"], normalize(rec["City"]))
        if key in seen:
            st["status"], st["match_row"], st["decision"] = "duplicate", seen[key], "skip"
            st["message"] = f"Same city as row {seen[key]} in this file."
        else:
            city, how = ctx.geo.city(country["country_id"], place["state_id"], rec["City"])
            if city is not None:
                st["match_id"], st["match_label"] = city["city_id"], city["label"]
                if how == "same":
                    st["status"] = "update"
                else:
                    st["status"], st["decision"] = "duplicate", "merge"
                    st["message"] = (f"{REASONS[how]} as existing city '{city['label']}'. " + st["message"]).strip()
            elif not allow_create:
                st["status"] = "problem"
                st["message"] = "New city: tick 'Add missing provinces and cities' to add it."
            seen[key] = row_num
        out.append(st)
    return out


def _eval_distances(ctx, rows):
    out, seen = [], {}
    for row_num, rec in rows:
        if not rec.get("From City") or not rec.get("To City"):
            out.append(_staged(row_num, rec, "problem", "Missing: From City and/or To City."))
            continue
        notes = []
        try:
            vals = _map_values("distances", rec)
            ids = []
            for side, label in (("From", "From City"), ("To", "To City")):
                country_txt = rec.get(f"{side} Country") or "Pakistan"
                country, how = ctx.geo.country(country_txt)
                if country is None or how == "similar":
                    raise ValueError(f"{side} Country '{country_txt}' isn't in TMS geography.")
                state_id = None
                if rec.get(f"{side} Province / State"):
                    s, how = ctx.geo.state(country["country_id"], rec[f"{side} Province / State"])
                    state_id = s["state_id"] if s is not None and how in ("same", "sounds") else None
                city, how = ctx.geo.city(country["country_id"], state_id, rec[label])
                if city is None or how == "similar":
                    hint = f" Did you mean '{city['label']}'?" if city is not None else ""
                    raise ValueError(f"{label} '{rec[label]}' isn't in TMS geography.{hint} Load it with the Cities template first.")
                if how != "same":
                    notes.append(f"'{rec[label]}' read as '{city['label']}'.")
                ids.append(city["city_id"])
            if ids[0] == ids[1]:
                raise ValueError("From and To are the same city.")
            for k, target in (("Drive Time — From (hrs)", "drive_minutes"), ("Drive Time — To (hrs)", "drive_minutes_max")):
                if rec.get(k) is not None:
                    vals[target] = int(round(_num(rec[k]) * 60))
        except ValueError as e:
            out.append(_staged(row_num, rec, "problem", str(e)))
            continue
        a, b = sorted(ids)
        vals.update({"city_a_id": a, "city_b_id": b})
        st = {"row_num": row_num, "data": {"raw": rec, "vals": vals}, "status": "new", "match_id": None,
              "match_row": None, "match_label": f"{rec['From City']} – {rec['To City']}", "message": " ".join(notes),
              "decision": None}
        if (a, b) in seen:
            st["status"], st["match_row"], st["decision"] = "duplicate", seen[(a, b)], "skip"
            st["message"] = f"Same pair of cities as row {seen[(a, b)]} in this file."
        else:
            r = ctx.db.execute("SELECT distance_id FROM city_distances WHERE city_a_id = ? AND city_b_id = ?", (a, b)).fetchone()
            if r:
                st["status"], st["match_id"] = "update", r[0]
            seen[(a, b)] = row_num
        out.append(st)
    return out


# ---- applying -----------------------------------------------------------------------

def _fill_update(db, table, pk, rid, vals, notes, source, name=None):
    """Fill empty columns only; append notes; remember another spelling."""
    row = db.execute(f"SELECT * FROM {table} WHERE {pk} = ?", (rid,)).fetchone()
    if row is None:
        return False
    sets = {}
    for col, v in vals.items():
        if col in ("notes", "name") or v is None or col not in row.keys():
            continue
        if row[col] in (None, ""):
            sets[col] = v
    if notes and "notes" in row.keys():
        if not row["notes"]:
            sets["notes"] = _notes_block(notes, source)
        elif notes.strip() not in row["notes"]:
            sets["notes"] = row["notes"] + "\n\n" + _notes_block(notes, source)
    if name and "alt_names" in row.keys() and normalize(name) != normalize(row["name"] if "name" in row.keys() else row["label"]):
        alts = split_alt_names(row["alt_names"])
        if normalize(name) not in {normalize(a) for a in alts}:
            sets["alt_names"] = "\n".join(alts + [name])
    if not sets:
        return False
    stamp = ", updated_at = datetime('now')" if "updated_at" in row.keys() else ""
    db.execute(f"UPDATE {table} SET {', '.join(f'{c} = ?' for c in sets)}{stamp} WHERE {pk} = ?",
               tuple(sets.values()) + (rid,))
    return True


def _ensure_place(geo, place):
    """Create a missing province / city recorded at evaluation time; returns
    (state_id, city_id)."""
    state_id = place.get("state_id")
    if not state_id and place.get("new_state"):
        s, how = geo.state(place["country_id"], place["new_state"])
        state_id = s["state_id"] if s is not None and how in ("same", "sounds") else geo.add_state(place["country_id"], place["new_state"])["state_id"]
    city_id = place.get("city_id")
    if not city_id and place.get("new_city"):
        c, how = geo.city(place["country_id"], state_id, place["new_city"])
        city_id = c["city_id"] if c is not None and how == "same" else geo.add_city(state_id, place["new_city"])["city_id"]
    return state_id, city_id


def apply(db, entity, mode, staged, file_name):
    """Write the decided rows. Returns {created, updated, merged, skipped, problems, unchanged} and
    sets each staged row's 'result'."""
    counts = dict(created=0, updated=0, merged=0, skipped=0, problems=0, unchanged=0)
    geo = Resolver(db)
    created_by_row = {}
    for st in staged:
        data, status, decision = st["data"], st["status"], st.get("decision")
        if status == "problem":
            st["result"] = "problem"
            counts["problems"] += 1
            continue
        if status == "duplicate" and decision == "skip":
            st["result"] = "skipped"
            counts["skipped"] += 1
            continue
        target_id = st.get("match_id")
        if status == "duplicate" and decision == "merge" and st.get("match_row"):
            target_id = created_by_row.get(st["match_row"])
            if target_id is None:  # the earlier row wasn't imported
                st["result"] = "skipped"
                counts["skipped"] += 1
                continue
        to_existing = status == "update" or (status == "duplicate" and decision == "merge")
        try:
            if entity == "cities":
                rid, changed = _apply_city(db, geo, data, target_id if to_existing else None)
            elif entity == "distances":
                rid, changed = _apply_distance(db, data, target_id if to_existing else None)
            else:
                rid, changed = _apply_catalog(db, geo, entity, mode, data, target_id if to_existing else None, file_name)
        except Exception as e:  # keep going; report the row
            st["result"] = f"failed: {e}"
            counts["problems"] += 1
            continue
        created_by_row[st["row_num"]] = rid
        if not to_existing:
            st["result"] = "created"
            counts["created"] += 1
        elif changed:
            st["result"] = "merged" if status == "duplicate" else "updated"
            counts["merged" if status == "duplicate" else "updated"] += 1
        else:
            st["result"] = "unchanged"
            counts["unchanged"] += 1
    db.commit()
    return counts


def _apply_catalog(db, geo, entity, mode, data, target_id, file_name):
    spec = MERGEABLE[entity]
    vals = dict(data["vals"])
    source = vals.get("source") or file_name
    if target_id:
        notes = vals.pop("notes", None)
        if mode == "notes":
            vals = {}
        place = data["place"]
        loc = {k: place.get(k) for k in ("region_id", "country_id", "state_id", "city_id") if place.get(k)}
        if mode == "normal":
            vals.update(loc)
        changed = _fill_update(db, spec["table"], spec["pk"], target_id, vals, notes, source, name=data["vals"].get("name"))
        return target_id, changed
    state_id, city_id = _ensure_place(geo, data["place"])
    place = data["place"]
    vals.update({"region_id": place.get("region_id"), "country_id": place["country_id"], "state_id": state_id,
                 "city_id": city_id})
    vals.setdefault("source", file_name)
    cur = db.execute(f"INSERT INTO {spec['table']} ({', '.join(vals)}) VALUES ({', '.join('?' * len(vals))})",
                     tuple(vals.values()))
    return cur.lastrowid, True


def _apply_city(db, geo, data, target_id):
    vals = {k: v for k, v in data["vals"].items() if k in ("latitude", "longitude", "timezone", "altitude_m")}
    raw = data["raw"]
    if target_id:
        changed = _fill_update(db, "cities", "city_id", target_id, vals, None, None, name=raw["City"])
        return target_id, changed
    place = data["place"]
    state_id = place.get("state_id")
    if not state_id:
        state_id = geo.add_state(place["country_id"], place["new_state"])["state_id"]
    return geo.add_city(state_id, raw["City"], **vals)["city_id"], True


def _apply_distance(db, data, target_id):
    vals = dict(data["vals"])
    vals["verified_on"] = vals.pop("checked_on", None)
    notes = vals.pop("notes", None)
    if target_id:
        changed = _fill_update(db, "city_distances", "distance_id", target_id, vals, notes, vals.get("source"))
        return target_id, changed
    vals["notes"] = notes
    vals = {k: v for k, v in vals.items() if v is not None}
    cur = db.execute(f"INSERT INTO city_distances ({', '.join(vals)}) VALUES ({', '.join('?' * len(vals))})",
                     tuple(vals.values()))
    return cur.lastrowid, True


# ---- staging --------------------------------------------------------------------------

def save_run(db, entity, mode, file_name, options, staged, user_id):
    cur = db.execute("INSERT INTO platform_import_runs (entity, mode, file_name, options, created_by) VALUES (?, ?, ?, ?, ?)",
                     (entity, mode, file_name, json.dumps(options), user_id))
    run_id = cur.lastrowid
    store_rows(db, run_id, staged)
    return run_id


def store_rows(db, run_id, staged):
    db.execute("DELETE FROM platform_import_rows WHERE run_id = ?", (run_id,))
    db.executemany(
        "INSERT INTO platform_import_rows (run_id, row_num, data, status, match_id, match_row, match_label, message, decision, result) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(run_id, s["row_num"], json.dumps(s["data"]), s["status"], s.get("match_id"), s.get("match_row"),
          s.get("match_label"), s.get("message"), s.get("decision"), s.get("result")) for s in staged])
    db.commit()


def load_rows(db, run_id):
    return [dict(r, data=json.loads(r["data"])) for r in db.execute(
        "SELECT * FROM platform_import_rows WHERE run_id = ? ORDER BY row_num", (run_id,)).fetchall()]


# ---- template workbooks ------------------------------------------------------------------

def template_workbook(db, entity):
    """A blank template (bytes) for one entity: Read Me, data sheet, Lists."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.datavalidation import DataValidation

    spec = ENTITIES[entity]
    F, NAVY = "Arial", "1F3A5F"
    head, req, entry = (PatternFill("solid", fgColor=NAVY), PatternFill("solid", fgColor="8B1E1E"),
                        PatternFill("solid", fgColor="FFFBEA"))
    thin = Side(style="thin", color="D9D9D9")
    box = Border(left=thin, right=thin, top=thin, bottom=thin)
    wb = Workbook()
    rm = wb.active
    rm.title = "Read Me"
    rm["A1"] = f"TMS Import Template — {spec['label']}"
    rm["A1"].font = Font(name=F, bold=True, size=16, color=NAVY)
    rm["A2"] = f"Template {spec['order']} of {len(ENTITIES)} · downloaded {date.today().isoformat()}"
    rm["A2"].font = Font(name=F, italic=True, color="666666")
    lines = [
        ("Purpose", spec["purpose"]),
        ("How to fill it in", f"Enter one record per row on the '{spec['label']}' sheet, starting at row 2. Don't rename, "
                              "move or delete the header row. Columns with a dark red header are required."),
        ("Unknown values", 'Leave the cell blank. "N/V", "N/A" and "verify" are also read as blank, so nothing is ever guessed.'),
        ("Drop-down lists", "Columns with a drop-down only accept values from the Lists sheet."),
        ("Matching on import", spec["match"]),
        ("Duplicates", "The import checks every row against TMS and the rest of the file: same name, names that sound "
                       "alike (Ahmad / Ahmed) and, for places, anything of the same type within 150 m. You decide each "
                       "possible duplicate: merge, keep as new, or skip."),
        ("Notes-only import", "To add notes to records already in TMS, fill just the name, Country, City and Notes "
                              "columns and choose 'Notes only' in the wizard. Other columns are then ignored."),
        ("Legend", "Dark blue header = optional column · dark red header = required column · pale yellow cells = data entry area."),
    ]
    r = 4
    for k, v in lines:
        a = rm.cell(r, 1, k)
        a.font, a.alignment = Font(name=F, bold=True), Alignment(vertical="top")
        b = rm.cell(r, 2, v)
        b.font, b.alignment = Font(name=F), Alignment(wrap_text=True, vertical="top")
        rm.merge_cells(start_row=r, start_column=2, end_row=r, end_column=4)
        rm.row_dimensions[r].height = 15 * (len(v) // 100 + 1) + 3
        r += 1
    r += 1
    rm.cell(r, 1, "Column guide").font = Font(name=F, bold=True, size=12, color=NAVY)
    r += 1
    for j, h in enumerate(["Column", "Required", "What to enter"], 1):
        c = rm.cell(r, j, h)
        c.font, c.fill, c.border = Font(name=F, bold=True, color="FFFFFF"), head, box
    for header, required, help_, _t in spec["cols"]:
        r += 1
        for j, v in enumerate([header, "Yes" if required else "", help_], 1):
            c = rm.cell(r, j, v)
            c.font = Font(name=F, bold=(j == 1))
            c.alignment, c.border = Alignment(wrap_text=True, vertical="top"), box
    for L, w in zip("ABCD", (26, 11, 80, 10)):
        rm.column_dimensions[L].width = w
    rm.sheet_view.showGridLines = False

    ws = wb.create_sheet(spec["label"][:31])
    keys = [c[0] for c in spec["cols"]]
    for j, (header, required, _h, _t) in enumerate(spec["cols"], 1):
        c = ws.cell(1, j, header + (" *" if required else ""))
        c.font, c.fill, c.border = Font(name=F, bold=True, color="FFFFFF"), (req if required else head), box
        c.alignment = Alignment(wrap_text=True, vertical="center", horizontal="center")
        ws.column_dimensions[get_column_letter(j)].width = 40 if header in ("Description", "Notes", "Address", "Address / Landmark") else max(12, min(30, len(header) + 4))
    ws.row_dimensions[1].height = 34
    for i in range(2, 502):
        for j in range(1, len(keys) + 1):
            c = ws.cell(i, j)
            c.fill, c.border, c.font = entry, box, Font(name=F)
    ws.freeze_panes = "B2"

    ls = wb.create_sheet("Lists")
    col = 1
    for header, values in spec["lists"].items():
        if values == "@poi_types":
            values = [r["label"] for r in db.execute(
                """SELECT pt.label FROM poi_types pt JOIN tenants t ON t.tenant_id = pt.tenant_id
                   WHERE t.is_platform = 1 AND pt.is_system = 1 AND pt.is_active = 1 ORDER BY pt.label COLLATE NOCASE""")]
        elif values == "@hub_types":
            values = [r["label"] for r in db.execute("SELECT label FROM hub_types WHERE is_active = 1 ORDER BY sort_order")]
        h = ls.cell(1, col, header)
        h.font, h.fill = Font(name=F, bold=True, color="FFFFFF"), head
        for i, v in enumerate(values, 2):
            ls.cell(i, col, v).font = Font(name=F)
        ls.column_dimensions[get_column_letter(col)].width = 28
        L = get_column_letter(col)
        dv = DataValidation(type="list", formula1=f"=Lists!${L}$2:${L}${len(values) + 1}", allow_blank=True,
                            showErrorMessage=True, errorTitle="Not on the list",
                            error="Pick a value from the drop-down (see the Lists sheet).")
        CL = get_column_letter(keys.index(header) + 1)
        dv.add(f"{CL}2:{CL}501")
        ws.add_data_validation(dv)
        col += 1
    if not spec["lists"]:
        ls.cell(1, 1, "This template has no drop-down lists.").font = Font(name=F, italic=True)
    wb.active = 1
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
