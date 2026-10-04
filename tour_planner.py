"""
Tour Planner -- a tenant-level AI agent that turns a Tour Brief into a draft
Package (design: the "TMS Tour Planner Agent -- Design Plan" doc, 3 Oct 2026).

A plan goes through stages; each stage's result is reviewed (and can be
edited or re-run with instructions) and approved before the next one runs:

    brief        Tour Brief (form; can be filled from a free-text request)
    arrivals     flights and layovers per party            (phase 3)
    route        main route city by city, POIs, detours    (phase 1)
    checkpoints  overnight cities, nights, day budget      (phase 1)
    grid         journey-time grid, cities and checkpoints (phase 1, computed)
    lodging      hotels and restaurants per checkpoint      (phase 2)
    transport    vehicles, providers, border change         (phase 3)
    days         day-by-day itinerary                       (phase 2)
    pretour      visas, regulatory, kits, costs             (phase 4)
    package      write the draft Package                    (phase 5)

Each AI stage runs in the background (tour_plan_runs; progress, timer and Stop
like the other agents, agent_runs.py). The agent looks in TMS first -- the
tenant's own Suppliers and POIs, the platform's cities, distances, POIs,
hotels and transport hubs -- through tools it calls, and searches the web
only for gaps, citing pages. The tenant pays (ai_usage.py, feature
"Tour Planner"). Distances it finds that TMS lacks are proposed to the
platform's TMS Agents review queue, never written straight in.
"""
import json
import math
import re
import threading
import traceback

import agent_runs
import ai_usage

FEATURE = "Tour Planner"

STAGES = [
    # key, label, short description, phase, kind ('form' | 'ai' | 'computed')
    ("brief", "Tour Brief", "who, when, where, standard", 1, "form"),
    ("arrivals", "Arrivals", "flights, layovers", 3, "ai"),
    ("route", "Route and POIs", "main route, attractions, detours", 1, "ai"),
    ("checkpoints", "Checkpoints", "overnight cities, day budget", 1, "ai"),
    ("grid", "Journey grid", "km and drive times", 1, "computed"),
    ("lodging", "Hotels and dining", "Suppliers first", 2, "ai"),
    ("transport", "Transport", "vehicles, border at Sost", 3, "ai"),
    ("days", "Day by day", "visits, meals", 2, "ai"),
    ("pretour", "Pre-Tour and costs", "visas, kits, costing", 4, "ai"),
    ("package", "Draft Package", "write it into Packages", 5, "computed"),
]
STAGE_KEYS = [s[0] for s in STAGES]
STAGE = {s[0]: {"key": s[0], "label": s[1], "blurb": s[2], "phase": s[3], "kind": s[4]} for s in STAGES}
BUILT_PHASES = {1, 2}

DDL = """
CREATE TABLE IF NOT EXISTS tour_plans (
    plan_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    name            TEXT NOT NULL,
    request_text    TEXT,                   -- the free-text request, as typed
    brief           TEXT,                   -- JSON: the Tour Brief
    status          TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','done','archived')),
    package_id      INTEGER REFERENCES packages(package_id),
    created_by      INTEGER REFERENCES users(user_id),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_tour_plans_tenant ON tour_plans(tenant_id);
CREATE TABLE IF NOT EXISTS tour_plan_stages (
    stage_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    plan_id         INTEGER NOT NULL REFERENCES tour_plans(plan_id),
    stage_key       TEXT NOT NULL,
    result          TEXT,                   -- JSON: the stage's current result (as edited)
    run_id          INTEGER,                -- the run that produced it (tour_plan_runs)
    approved_at     TEXT,
    approved_by     INTEGER REFERENCES users(user_id),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (plan_id, stage_key)
);
CREATE TABLE IF NOT EXISTS tour_plan_runs (
    run_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    plan_id         INTEGER NOT NULL REFERENCES tour_plans(plan_id),
    stage_key       TEXT NOT NULL,
    instructions    TEXT,                   -- "ask for changes" text for a re-run
    status          TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','done','failed')),
    progress        TEXT,
    error           TEXT,
    result          TEXT,                   -- JSON as the agent returned it
    created_by      INTEGER REFERENCES users(user_id),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    started_at      TEXT,
    finished_at     TEXT,
    heartbeat_at    TEXT,
    items_total     INTEGER,
    items_done      INTEGER NOT NULL DEFAULT 0,
    cancel_requested INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tour_plan_runs_plan ON tour_plan_runs(plan_id);
"""


class PlannerError(Exception):
    pass


# ---- the Tour Brief -------------------------------------------------------------------------------

DEFAULT_BRIEF = {
    "name": "", "standard": "4-Star", "start_city": "Islamabad", "start_date": "", "destination": "",
    "route_note": "", "return_mode": "overland", "fly_home": True, "tour_days": 10, "weather_days": 0,
    "max_drive_hours": 8, "parties": [], "guides": 2, "guide_seating": "one_per_minibus",
    "vehicle_type": "Minibus", "vehicle_seats": 14, "vehicle_capacity": 15, "max_guests_per_vehicle": None,
    "drivers_included": True, "currency": "USD",
    "interests": "", "notes": "", "checkpoint_criteria": {}, "flag_responses": {},
}

# What makes a city a good overnight checkpoint (set on the Checkpoints stage).
DEFAULT_CRITERIA = {"max_km": None, "min_km": None, "hotel": "", "food": "", "pois": "", "prefer_flagged": True,
                    "other": ""}


def criteria_of(brief):
    c = dict(DEFAULT_CRITERIA)
    c.update(brief.get("checkpoint_criteria") or {})
    return c
RETURN_MODES = {"overland": "Overland, the same way back", "fly": "Fly back from the destination",
                "one_way": "One way (tour ends at the destination)"}
GUIDE_SEATING = {"one_per_minibus": "One guide in each vehicle", "suv": "Guides in a separate SUV",
                 "none": "No guides"}


TRANSPORT_TYPES = ["Car (sedan)", "SUV / Jeep", "Hiace van", "Minibus", "Coaster", "Coach / Bus"]


def brief_of(plan):
    b = dict(DEFAULT_BRIEF)
    try:
        b.update(json.loads(plan["brief"] or "{}"))
    except ValueError:
        pass
    return normalise_transport(b)


def normalise_transport(b):
    """Transport (Zeb, Oct 2026): Transport Type, Max Guests per Vehicle,
    Guides and the Total Vehicle Capacity including the driver and guides.
    Older briefs only had 'seats per vehicle' (guests and guide, without the
    driver): their capacity is that plus the driver."""
    if not b.get("vehicle_capacity"):
        b["vehicle_capacity"] = int(b.get("vehicle_seats") or 14) + 1
    b["vehicle_capacity"] = max(int(b["vehicle_capacity"]), 3)
    b["vehicle_seats"] = b["vehicle_capacity"] - 1
    return b


def transport_figures(brief):
    """(seats left for guests in each vehicle, guide riding in each, the
    planner's max guests per vehicle or None)."""
    capacity = int(brief.get("vehicle_capacity") or 15)
    guide_in = 1 if brief.get("guide_seating") == "one_per_minibus" and int(brief.get("guides") or 0) else 0
    room = max(capacity - 1 - guide_in, 1)  # one seat is the driver's
    wanted = brief.get("max_guests_per_vehicle")
    wanted = int(wanted) if wanted not in (None, "", 0, "0") else None
    return room, guide_in, wanted


def group_figures(brief):
    """Guests, rooms and vehicles worked out from the brief."""
    parties = brief.get("parties") or []
    guests = sum(int(p.get("guests") or 0) for p in parties)
    doubles = sum(int(p.get("doubles") or 0) for p in parties)
    singles = sum(int(p.get("singles") or 0) for p in parties)
    guides = int(brief.get("guides") or 0)
    room, guide_in, wanted = transport_figures(brief)
    per_bus = min(wanted, room) if wanted else room
    if guide_in:
        buses = max(math.ceil(guests / per_bus), guides) if guests else 0
    else:
        buses = math.ceil(guests / per_bus) if guests else 0
    split = []
    if buses:
        base, extra = divmod(guests, buses)
        split = [base + (1 if i < extra else 0) for i in range(buses)]
    suv = 1 if brief.get("guide_seating") == "suv" and guides else 0
    drivers = buses + suv
    capacity = int(brief.get("vehicle_capacity") or 15)
    riding = guests + buses + (min(guides, buses) if guide_in else 0)  # guests, drivers, guides in the vehicles
    return {"guests": guests, "doubles": doubles, "singles": singles, "guest_rooms": doubles + singles,
            "guide_rooms": guides, "driver_rooms": drivers if brief.get("drivers_included") else 0,
            "vehicles": buses, "per_vehicle": split, "suv": suv, "drivers": drivers,
            "max_guests_per_vehicle": per_bus, "vehicle_capacity": capacity, "total_capacity": buses * capacity,
            "riding": riding, "guides_in_vehicles": min(guides, buses) if guide_in else 0}


def brief_problems(brief):
    out = []
    if not brief.get("name"):
        out.append("Give the tour a name.")
    if not brief.get("start_city"):
        out.append("Choose the assembly city.")
    if not brief.get("destination"):
        out.append("Choose the destination.")
    if not brief.get("start_date"):
        out.append("Set the assembly date.")
    fig = group_figures(brief)
    if not fig["guests"]:
        out.append("Add the travelling parties (at least one guest).")
    for p in brief.get("parties") or []:
        g, d, s = int(p.get("guests") or 0), int(p.get("doubles") or 0), int(p.get("singles") or 0)
        if g and 2 * d + s < g:
            out.append(f"{p.get('label') or 'A party'}: {g} guests but rooms for only {2 * d + s}.")
    room, guide_in, wanted = transport_figures(brief)
    if wanted and wanted > room:
        out.append(f"Max guests per vehicle is {wanted}, but a vehicle of {brief.get('vehicle_capacity')} seats has room "
                   f"for only {room} guests after the driver{' and guide' if guide_in else ''}.")
    return out


# ---- plans and stages ----------------------------------------------------------------------------

def create_plan(db, tenant_id, user_id, name, request_text=None, brief=None):
    b = dict(DEFAULT_BRIEF, **(brief or {}))
    b["name"] = name or b.get("name") or "New tour"
    cur = db.execute("INSERT INTO tour_plans (tenant_id, name, request_text, brief, created_by) VALUES (?, ?, ?, ?, ?)",
                     (tenant_id, b["name"], request_text, json.dumps(b), user_id))
    plan_id = cur.lastrowid
    for key in STAGE_KEYS:
        db.execute("INSERT INTO tour_plan_stages (tenant_id, plan_id, stage_key) VALUES (?, ?, ?)", (tenant_id, plan_id, key))
    db.commit()
    return plan_id


def get_plan(db, tenant_id, plan_id):
    return db.execute("SELECT * FROM tour_plans WHERE plan_id = ? AND tenant_id = ?", (plan_id, tenant_id)).fetchone()


def stages(db, plan_id):
    """{key: dict(stage row, run=latest run or None, result=parsed)} in pipeline order."""
    rows = {r["stage_key"]: dict(r) for r in db.execute("SELECT * FROM tour_plan_stages WHERE plan_id = ?", (plan_id,))}
    out = {}
    for key in STAGE_KEYS:
        st = rows.get(key) or {"stage_key": key, "result": None, "run_id": None, "approved_at": None}
        run = db.execute("SELECT * FROM tour_plan_runs WHERE plan_id = ? AND stage_key = ? ORDER BY run_id DESC LIMIT 1",
                         (plan_id, key)).fetchone()
        st["run"] = dict(run) if run else None
        try:
            st["data"] = json.loads(st["result"]) if st.get("result") else None
        except ValueError:
            st["data"] = None
        st["broken"] = False
        need = REPORT_KEYS.get(key)
        if need and st["data"] is not None and (not isinstance(st["data"], dict) or not all(k in st["data"] for k in need)):
            st["data"], st["broken"] = None, True  # an empty or cut-off report: run again
        st.update(STAGE[key])
        st["built"] = STAGE[key]["phase"] in BUILT_PHASES
        out[key] = st
    # Status for display.
    blocked = False
    for key in STAGE_KEYS:
        st = out[key]
        working = st["run"] and st["run"]["status"] in ("queued", "running")
        if not st["built"]:
            st["state"] = "later"
            continue
        if st["approved_at"]:
            st["state"] = "approved"
        elif working:
            st["state"] = "working"
        elif blocked:
            st["state"] = "waiting"
        elif st["data"] is not None:
            st["state"] = "review"
        elif st["run"] and st["run"]["status"] == "failed":
            st["state"] = "failed"
        else:
            st["state"] = "ready"
        if not st["approved_at"]:
            blocked = True
    return out


# The parts a stage's saved report must have (an earlier run could save a
# cut-off, empty one).
REPORT_KEYS = {"route": ["outbound"], "checkpoints": ["checkpoints", "days"], "lodging": ["checkpoints"],
               "days": ["days"]}


def previous_approved(stage_map, key):
    """True when every built stage before `key` is approved."""
    for k in STAGE_KEYS[:STAGE_KEYS.index(key)]:
        st = stage_map[k]
        if st["built"] and not st["approved_at"]:
            return False
    return True


def set_result(db, plan_id, key, data, run_id=None):
    db.execute("UPDATE tour_plan_stages SET result = ?, run_id = COALESCE(?, run_id), approved_at = NULL, "
               "approved_by = NULL, updated_at = datetime('now') WHERE plan_id = ? AND stage_key = ?",
               (json.dumps(data, ensure_ascii=False) if data is not None else None, run_id, plan_id, key))
    reset_after(db, plan_id, key)


def reset_after(db, plan_id, key):
    """A stage changed: everything after it has to be redone."""
    later = STAGE_KEYS[STAGE_KEYS.index(key) + 1:]
    if later:
        db.execute(f"UPDATE tour_plan_stages SET result = NULL, approved_at = NULL, approved_by = NULL, "
                   f"updated_at = datetime('now') WHERE plan_id = ? AND stage_key IN ({','.join('?' * len(later))})",
                   [plan_id] + later)


def approve(db, plan_id, key, user_id):
    db.execute("UPDATE tour_plan_stages SET approved_at = datetime('now'), approved_by = ? WHERE plan_id = ? AND stage_key = ?",
               (user_id, plan_id, key))
    db.execute("UPDATE tour_plans SET updated_at = datetime('now') WHERE plan_id = ?", (plan_id,))
    db.commit()


def save_brief(db, plan_id, brief):
    db.execute("UPDATE tour_plans SET brief = ?, name = ?, updated_at = datetime('now') WHERE plan_id = ?",
               (json.dumps(brief, ensure_ascii=False), brief.get("name") or "Tour", plan_id))
    db.execute("UPDATE tour_plan_stages SET result = ?, approved_at = NULL, approved_by = NULL, updated_at = datetime('now') "
               "WHERE plan_id = ? AND stage_key = 'brief'", (json.dumps(brief, ensure_ascii=False), plan_id))
    reset_after(db, plan_id, "brief")
    db.commit()


# ---- seams (replaced in tests) -----------------------------------------------------------------------

def anthropic_client():
    import image_collector
    return image_collector.anthropic_client()


def model():
    from config import Config
    return Config.ANTHROPIC_MODEL


# ---- TMS data the agent can look up (its tools) ----------------------------------------------------------

def _haversine(a, b):
    (lat1, lon1), (lat2, lon2) = a, b
    p = math.pi / 180
    h = (math.sin((lat2 - lat1) * p / 2) ** 2 +
         math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lon2 - lon1) * p / 2) ** 2)
    return 12742 * math.asin(math.sqrt(h))


def _city_row(db, city_id):
    return db.execute("""SELECT ci.city_id, ci.label, ci.latitude, ci.longitude, ci.alt_names, s.label AS state,
                                co.label AS country, co.country_id
                         FROM cities ci JOIN states s ON s.state_id = ci.state_id JOIN countries co ON co.country_id = s.country_id
                         WHERE ci.city_id = ?""", (city_id,)).fetchone()


def tool_find_cities(db, tenant_id, args):
    from fuzzy import compare, split_alt_names
    out = []
    rows = db.execute("""SELECT ci.city_id, ci.label, ci.alt_names, ci.latitude, ci.longitude, ci.is_checkpoint,
                                s.label AS state, co.label AS country
                         FROM cities ci JOIN states s ON s.state_id = ci.state_id JOIN countries co ON co.country_id = s.country_id
                         WHERE ci.is_active = 1""").fetchall()
    for name in (args.get("names") or [])[:30]:
        hits = []
        for r in rows:
            how = compare(name, r["label"], split_alt_names(r["alt_names"]))
            if how in ("same", "sounds"):
                hits.append((0 if how == "same" else 1, r))
        hits.sort(key=lambda x: x[0])
        out.append({"name": name, "matches": [
            {"city_id": r["city_id"], "city": r["label"], "province": r["state"], "country": r["country"],
             "lat": r["latitude"], "lon": r["longitude"], "tms_overnight_checkpoint": bool(r["is_checkpoint"])}
            for _, r in hits[:3]]})
    return out


def tool_get_distances(db, tenant_id, args):
    ids = [int(x) for x in (args.get("city_ids") or []) if str(x).isdigit()][:40]
    if len(ids) < 2:
        return []
    q = ",".join("?" * len(ids))
    out = []
    for r in db.execute(f"""SELECT d.*, a.label AS a_label, b.label AS b_label FROM city_distances d
                            JOIN cities a ON a.city_id = d.city_a_id JOIN cities b ON b.city_id = d.city_b_id
                            WHERE d.city_a_id IN ({q}) AND d.city_b_id IN ({q})""", ids + ids):
        out.append({"from_id": r["city_a_id"], "from": r["a_label"], "to_id": r["city_b_id"], "to": r["b_label"],
                    "road_km": r["road_km"], "drive_minutes": r["drive_minutes"], "drive_minutes_max": r["drive_minutes_max"],
                    "route": r["route_name"]})
    return out


def _coords(text):
    m = re.findall(r"-?\d+(?:\.\d+)?", text or "")
    if len(m) >= 2:
        try:
            lat, lon = float(m[0]), float(m[1])
            if -90 <= lat <= 90 and -180 <= lon <= 180:
                return lat, lon
        except ValueError:
            pass
    return None


def tool_find_pois(db, tenant_id, args):
    """POIs in or near the given cities: the tenant's own first, then the
    platform catalog's (those the tenant hasn't got)."""
    ids = [int(x) for x in (args.get("city_ids") or []) if str(x).isdigit()][:30]
    radius = min(float(args.get("radius_km") or 25), 80)
    cities = [c for c in (_city_row(db, i) for i in ids) if c]
    centres = [(c["city_id"], c["label"], (c["latitude"], c["longitude"])) for c in cities if c["latitude"] is not None]
    out, seen = [], set()

    def near(lat_lon, city_id):
        if city_id in ids:
            return next((c[1] for c in centres if c[0] == city_id), None) or "listed city", 0
        if not lat_lon:
            return None, None
        best = None
        for cid, label, ll in centres:
            d = _haversine(lat_lon, ll)
            if d <= radius and (best is None or d < best[1]):
                best = (label, round(d, 1))
        return best if best else (None, None)

    for r in db.execute("""SELECT p.poi_id, p.name, p.city_id, p.map_coordinates, p.historical_significance, pt.label AS type_label
                           FROM points_of_interest p LEFT JOIN poi_types pt ON pt.poi_type_id = p.poi_type_id
                           WHERE p.tenant_id = ? AND p.is_deleted = 0""", (tenant_id,)):
        city, km = near(_coords(r["map_coordinates"]), r["city_id"])
        if city is None:
            continue
        seen.add(r["name"].lower())
        out.append({"source": f"tenant POI #{r['poi_id']}", "name": r["name"], "type": r["type_label"], "near": city,
                    "km_from_city": km, "about": (r["historical_significance"] or "")[:200]})
    for r in db.execute("""SELECT p.poi_id, p.name, p.city_id, p.latitude, p.longitude, p.description, p.significance,
                                  p.opening_hours, p.entry_fee, pt.label AS type_label FROM platform_pois p LEFT JOIN poi_types pt ON pt.poi_type_id = p.poi_type_id
                           WHERE p.is_active = 1"""):
        if r["name"].lower() in seen:
            continue
        ll = (r["latitude"], r["longitude"]) if r["latitude"] is not None and r["longitude"] is not None else None
        city, km = near(ll, r["city_id"])
        if city is None:
            continue
        out.append({"source": f"platform POI #{r['poi_id']}", "name": r["name"], "type": r["type_label"], "near": city,
                    "km_from_city": km, "about": (r["description"] or r["significance"] or "")[:200],
                    "opening_hours": r["opening_hours"], "entry_fee": r["entry_fee"]})
    return out[:150]


def tool_lodging(db, tenant_id, args):
    """Hotels on record per city: the tenant's Accommodation suppliers and
    the platform catalog."""
    ids = [int(x) for x in (args.get("city_ids") or []) if str(x).isdigit()][:30]
    out = []
    for cid in ids:
        c = _city_row(db, cid)
        if not c:
            continue
        mine = db.execute("""SELECT s.supplier_name, st.label AS subtype FROM suppliers s
                             JOIN supplier_groups g ON g.supplier_group_id = s.supplier_group_id AND g.code = 'ACCOMMODATION'
                             LEFT JOIN supplier_subtypes st ON st.supplier_subtype_id = s.supplier_subtype_id
                             JOIN supplier_addresses a ON a.supplier_id = s.supplier_id
                             WHERE s.tenant_id = ? AND s.is_deleted = 0 AND a.city_id = ? GROUP BY s.supplier_id""",
                          (tenant_id, cid)).fetchall()
        plat = db.execute("SELECT name, star_rating, property_type FROM platform_accommodation WHERE city_id = ? AND is_active = 1",
                          (cid,)).fetchall()
        flagged = db.execute("SELECT is_checkpoint FROM cities WHERE city_id = ?", (cid,)).fetchone()
        out.append({"city_id": cid, "city": c["label"], "tms_overnight_checkpoint": bool(flagged and flagged[0]),
                    "tenant_hotels": [f"{r['supplier_name']}{' (' + r['subtype'] + ')' if r['subtype'] else ''}" for r in mine][:15],
                    "catalog_hotels": [f"{r['name']}{' ' + str(r['star_rating']) + '*' if r['star_rating'] else ''}" for r in plat][:15],
                    "four_star_or_better": sum(1 for r in plat if (r["star_rating"] or 0) >= 4)})
    return out


def tool_find_hubs(db, tenant_id, args):
    ids = [int(x) for x in (args.get("city_ids") or []) if str(x).isdigit()][:30]
    if not ids:
        return []
    q = ",".join("?" * len(ids))
    return [{"hub_id": r["hub_id"], "name": r["name"], "code": r["code"], "city": r["city"], "type": r["type_label"]}
            for r in db.execute(f"""SELECT h.hub_id, h.name, h.code, ci.label AS city, ht.label AS type_label
                                    FROM transport_hubs h LEFT JOIN cities ci ON ci.city_id = h.city_id
                                    LEFT JOIN hub_types ht ON ht.hub_type_id = h.hub_type_id
                                    WHERE h.is_active = 1 AND h.city_id IN ({q})""", ids)]


def _reference_rate(rate, as_of):
    """The Reference Room Rate (USD per double room per night) -- the
    tenant's own figure on its Supplier (which overrides the catalog's), or
    the platform catalog's. A guide for planning, not a contracted price."""
    if rate is None:
        return {}
    return {"reference_rate_usd": rate, "reference_rate_as_of": as_of}


def tool_hotels(db, tenant_id, args):
    """Hotels per city with what TMS knows about them: the tenant's own
    Accommodation suppliers with their room types and latest prices, then
    the platform catalog's hotels (the tenant may not have them yet)."""
    ids = [int(x) for x in (args.get("city_ids") or []) if str(x).isdigit()][:20]
    out = []
    for cid in ids:
        c = _city_row(db, cid)
        if not c:
            continue
        mine = []
        for s_ in db.execute("""SELECT s.supplier_id, s.supplier_name, s.preference, t.label AS type_label, st.label AS subtype,
                                       s.ref_room_rate, s.ref_rate_as_of
                                FROM suppliers s JOIN supplier_groups g ON g.supplier_group_id = s.supplier_group_id AND g.code = 'ACCOMMODATION'
                                LEFT JOIN supplier_types t ON t.supplier_type_id = s.supplier_type_id
                                LEFT JOIN supplier_subtypes st ON st.supplier_subtype_id = s.supplier_subtype_id
                                WHERE s.tenant_id = ? AND s.is_deleted = 0 AND EXISTS (
                                    SELECT 1 FROM supplier_addresses a WHERE a.supplier_id = s.supplier_id AND a.city_id = ?)""",
                             (tenant_id, cid)).fetchall():
            rooms = [{"room_type": r["label"], "price_per_night_usd": r["price_per_night"], "price_as_of": r["price_as_of"],
                      "rooms_in_hotel": r["number_of_rooms"]}
                     for r in db.execute("""SELECT rt.label, sr.price_per_night, sr.price_as_of, sr.number_of_rooms
                                            FROM supplier_rooms sr JOIN hotel_room_types rt ON rt.room_type_id = sr.room_type_id
                                            WHERE sr.supplier_id = ? AND sr.tenant_id = ?""", (s_["supplier_id"], tenant_id))]
            mine.append({"supplier_id": s_["supplier_id"], "name": s_["supplier_name"], "type": s_["type_label"],
                         "grade": s_["subtype"], "preference": s_["preference"], "rooms": rooms,
                         **_reference_rate(s_["ref_room_rate"], s_["ref_rate_as_of"])})
        names = {m["name"].lower() for m in mine}
        plat = [{"accommodation_id": r["accommodation_id"], "name": r["name"], "stars": r["star_rating"],
                 "type": r["property_type"], "rooms": r["rooms"], "dining": bool(r["amen_dining"]),
                 **_reference_rate(r["ref_room_rate"] if "ref_room_rate" in r.keys() else None,
                                   r["ref_rate_as_of"] if "ref_rate_as_of" in r.keys() else None)}
                for r in db.execute("SELECT * FROM platform_accommodation WHERE city_id = ? AND is_active = 1", (cid,))
                if r["name"].lower() not in names]
        out.append({"city_id": cid, "city": c["label"], "tenant_suppliers": mine[:12], "platform_catalog": plat[:12]})
    return out


def tool_restaurants(db, tenant_id, args):
    ids = [int(x) for x in (args.get("city_ids") or []) if str(x).isdigit()][:20]
    out = []
    for cid in ids:
        c = _city_row(db, cid)
        if not c:
            continue
        mine = [{"supplier_id": r["supplier_id"], "name": r["supplier_name"], "type": r["type_label"]}
                for r in db.execute("""SELECT s.supplier_id, s.supplier_name, t.label AS type_label FROM suppliers s
                                       JOIN supplier_groups g ON g.supplier_group_id = s.supplier_group_id AND g.code = 'FOOD_BEVERAGE'
                                       LEFT JOIN supplier_types t ON t.supplier_type_id = s.supplier_type_id
                                       WHERE s.tenant_id = ? AND s.is_deleted = 0 AND EXISTS (
                                           SELECT 1 FROM supplier_addresses a WHERE a.supplier_id = s.supplier_id AND a.city_id = ?)""",
                                    (tenant_id, cid))]
        names = {m["name"].lower() for m in mine}
        plat = [{"restaurant_id": r["restaurant_id"], "name": r["name"], "cuisine": r["cuisine"], "class": r["class"],
                 "price_from": r["price_from"], "price_to": r["price_to"], "currency": r["currency"],
                 "group_suitable": r["group_suitable"]}
                for r in db.execute("SELECT * FROM platform_restaurants WHERE city_id = ? AND is_active = 1", (cid,))
                if r["name"].lower() not in names]
        out.append({"city_id": cid, "city": c["label"], "tenant_suppliers": mine[:12], "platform_catalog": plat[:12]})
    return out


TOOLS = {
    "find_cities": (tool_find_cities, "Look up cities in TMS by name (returns ids, province, country, coordinates).",
                    {"names": {"type": "array", "items": {"type": "string"}}}),
    "get_distances": (tool_get_distances, "Road distances and drive times TMS already holds between any of these cities.",
                      {"city_ids": {"type": "array", "items": {"type": "integer"}}}),
    "find_pois": (tool_find_pois, "Points of interest in or near these cities (tenant's own and the platform catalog).",
                  {"city_ids": {"type": "array", "items": {"type": "integer"}}, "radius_km": {"type": "number"}}),
    "lodging_at": (tool_lodging, "Hotels TMS knows in these cities (tenant suppliers, platform catalog, 4-star count).",
                   {"city_ids": {"type": "array", "items": {"type": "integer"}}}),
    "hotels_at": (tool_hotels, "Hotels in these cities: the tenant's own suppliers with room types and latest prices (USD), "
                  "then the platform catalog's hotels with star ratings. Either may carry a reference_rate_usd: "
                  "a typical double room per night, for planning.",
                  {"city_ids": {"type": "array", "items": {"type": "integer"}}}),
    "restaurants_at": (tool_restaurants, "Restaurants in these cities: the tenant's own F&B suppliers, then the platform catalog "
                       "(cuisine, price range, group suitability).",
                       {"city_ids": {"type": "array", "items": {"type": "integer"}}}),
    "find_hubs": (tool_find_hubs, "Airports, railway stations and border posts in these cities.",
                  {"city_ids": {"type": "array", "items": {"type": "integer"}}}),
}


def tool_specs(names):
    return [{"name": n, "description": TOOLS[n][1],
             "input_schema": {"type": "object", "properties": TOOLS[n][2]}} for n in names]


# ---- the agent loop -----------------------------------------------------------------------------------------

def agent_loop(db, tenant_id, client, prompt, tool_names, report_tool, meter, log, web_searches=4, max_turns=14):
    """Claude with TMS tools (+ web search) until it calls the report tool.
    Returns the report tool's input."""
    tools = tool_specs(tool_names) + [report_tool]
    if web_searches:
        tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": web_searches}] + tools
    messages = [{"role": "user", "content": prompt}]
    required = report_tool["input_schema"].get("required") or []
    for _ in range(max_turns):
        resp = agent_runs.create_message(client, model=model(), max_tokens=16000, tools=tools, messages=messages)
        meter(getattr(resp, "usage", None))
        content = getattr(resp, "content", []) or []
        results, report = [], None
        for b in content:
            if getattr(b, "type", None) != "tool_use":
                continue
            if b.name == report_tool["name"]:
                report = b.input or {}
                continue
            fn = TOOLS.get(b.name)
            try:
                data = fn[0](db, tenant_id, b.input or {}) if fn else {"error": "unknown tool"}
            except Exception as e:  # a bad lookup shouldn't end the run
                data = {"error": str(e)[:200]}
            log(f"   looked up {b.name.replace('_', ' ')}: {_brief_args(b.input)}")
            results.append({"type": "tool_result", "tool_use_id": b.id, "content": json.dumps(data, ensure_ascii=False)[:30000]})
        stop = getattr(resp, "stop_reason", None)
        if report is not None:
            missing = [k for k in required if k not in report]
            if not missing and stop != "max_tokens":
                return report
            # Cut off (too long) or incomplete: ask again, shorter.
            log(f"   the report came back incomplete ({', '.join(missing) or 'cut off'}); asking again")
            messages = messages + [
                {"role": "assistant", "content": [b for b in content if getattr(b, "type", None) != "tool_use"]
                 or [{"type": "text", "text": "(report cut off)"}]},
                {"role": "user", "content": f"Your {report_tool['name']} call was cut off or incomplete"
                 + (f" (missing: {', '.join(missing)})" if missing else "") + ". Call it again with every required "
                 "field, keeping each note to one short sentence."}]
            continue
        if not results and stop != "pause_turn":
            # Answered in prose without reporting: ask once more for the report.
            messages = messages + [{"role": "assistant", "content": content},
                                   {"role": "user", "content": f"Please call {report_tool['name']} now with your results."}]
            continue
        messages = messages + [{"role": "assistant", "content": content}]
        if results:
            messages.append({"role": "user", "content": results})
    raise PlannerError("The agent didn't finish within its step limit. Try again, or narrow the request.")


def _brief_args(a):
    a = a or {}
    for k in ("names", "city_ids"):
        if a.get(k):
            return ", ".join(str(x) for x in a[k][:8]) + ("…" if len(a[k]) > 8 else "")
    return ""


# ---- stage: brief from a free-text request --------------------------------------------------------------------

BRIEF_TOOL = {"name": "report_brief", "description": "The Tour Brief read from the request, and questions about gaps.",
              "input_schema": {"type": "object", "properties": {
                  "name": {"type": "string"}, "standard": {"type": "string"}, "start_city": {"type": "string"},
                  "start_date": {"type": "string", "description": "YYYY-MM-DD"}, "destination": {"type": "string"},
                  "route_note": {"type": "string"}, "return_mode": {"type": "string", "enum": list(RETURN_MODES)},
                  "fly_home": {"type": "boolean"}, "tour_days": {"type": "integer"}, "guides": {"type": "integer"},
                  "guide_seating": {"type": "string", "enum": list(GUIDE_SEATING)},
                  "vehicle_type": {"type": "string"},
                  "vehicle_capacity": {"type": "integer", "description": "Seats in each vehicle, counting the driver and guide"},
                  "max_guests_per_vehicle": {"type": "integer"},
                  "drivers_included": {"type": "boolean"}, "interests": {"type": "string"}, "notes": {"type": "string"},
                  "parties": {"type": "array", "items": {"type": "object", "properties": {
                      "label": {"type": "string"}, "origin_city": {"type": "string"}, "origin_country": {"type": "string"},
                      "guests": {"type": "integer"}, "doubles": {"type": "integer"}, "singles": {"type": "integer"}},
                      "required": ["guests"]}},
                  "questions": {"type": "array", "items": {"type": "string"},
                                "description": "What the request leaves unclear and the planner must ask"}},
                  "required": ["parties", "questions"]}}


def parse_request(client, text, meter, brief=None):
    current = {k: v for k, v in (brief or {}).items() if v not in (None, "", [])}
    prompt = (
        "A tour operator typed this request for a group tour (it may end with their answers to earlier questions). "
        "Read it into a Tour Brief. Couples share a double room; individuals who need their own room get a single. "
        "Put only what the request states; leave other fields out. Treat anything the planner added under 'Anything "
        "else the agent should know and consider' as instructions for the whole plan, and keep it in notes.\n\n"
        "The brief already holds these values (form defaults or the planner's own entries); they stand unless the "
        f"request says otherwise:\n{json.dumps(current, ensure_ascii=False)}\n\n"
        "Then list QUESTIONS, but only what blocks planning the route, overnight stops and days, and only when neither "
        "the request nor the brief above answers it: at most 5, the most important first, each one short and specific. "
        "Do NOT ask about things later planning stages work out or the brief form already covers -- hotels, budgets, "
        "currency, vehicle counts and seating, drivers, visas, permits, medical or dietary needs, flights, weather "
        "contingency -- and never ask again about something answered at the end of the request. No questions is a "
        "good answer.\n\n"
        f"Request:\n{text}\n\nCall report_brief.")
    resp = client.messages.create(model=model(), max_tokens=3000, tools=[BRIEF_TOOL],
                                  tool_choice={"type": "tool", "name": "report_brief"},
                                  messages=[{"role": "user", "content": prompt}])
    meter(getattr(resp, "usage", None))
    for b in getattr(resp, "content", []) or []:
        if getattr(b, "type", None) == "tool_use" and b.name == "report_brief":
            return b.input or {}
    raise PlannerError("The request couldn't be read into a brief.")


def add_answers(db, plan_id, pairs):
    """Append the planner's answers to the agent's questions to the request,
    so the next reading of it takes them in."""
    plan = db.execute("SELECT request_text FROM tour_plans WHERE plan_id = ?", (plan_id,)).fetchone()
    lines = "\n".join(f"Q: {q}\nA: {a}" for q, a in pairs)
    text = ((plan["request_text"] or "").rstrip() + "\n\nAnswers to the planner's questions:\n" + lines).strip()
    db.execute("UPDATE tour_plans SET request_text = ?, updated_at = datetime('now') WHERE plan_id = ?", (text, plan_id))
    db.commit()


EXTRA_QUESTION = "Anything else the agent should know and consider"


def add_note(db, plan_id, text):
    """Keep the planner's extra guidance in the brief's notes too, so every
    later stage (route, hotels, days...) takes it into account."""
    plan = db.execute("SELECT brief FROM tour_plans WHERE plan_id = ?", (plan_id,)).fetchone()
    b = dict(DEFAULT_BRIEF, **json.loads(plan["brief"] or "{}"))
    notes = (b.get("notes") or "").strip()
    if text.strip() and text.strip() not in notes:
        b["notes"] = (notes + ("\n" if notes else "") + text.strip()).strip()
        db.execute("UPDATE tour_plans SET brief = ? WHERE plan_id = ?", (json.dumps(b, ensure_ascii=False), plan_id))
        db.commit()


def dismiss_questions(db, plan_id):
    run = db.execute("SELECT run_id, result FROM tour_plan_runs WHERE plan_id = ? AND stage_key = 'brief' "
                     "ORDER BY run_id DESC LIMIT 1", (plan_id,)).fetchone()
    if run and run["result"]:
        data = json.loads(run["result"])
        data["questions"] = []
        db.execute("UPDATE tour_plan_runs SET result = ? WHERE run_id = ?", (json.dumps(data, ensure_ascii=False), run["run_id"]))
        db.commit()


def merge_brief(brief, found):
    """The parsed request on top of the current brief (non-empty values only)."""
    found = dict(found)
    if found.get("vehicle_seats") and not found.get("vehicle_capacity"):  # an older-style answer
        found["vehicle_capacity"] = int(found["vehicle_seats"]) + 1
    b = dict(brief)
    for k, v in found.items():
        if k == "questions" or v in (None, "", []):
            continue
        if k == "notes":  # add to the planner's notes, never replace them
            old = (b.get("notes") or "").strip()
            if v.strip() and v.strip() not in old:
                b["notes"] = (old + ("\n" if old else "") + v.strip()).strip()
            continue
        if k in DEFAULT_BRIEF:
            b[k] = v
    return normalise_transport(b)


# ---- stage: route -------------------------------------------------------------------------------------------------

ROUTE_TOOL = {"name": "report_route", "description": "The tour's route, its points of interest and detours.",
              "input_schema": {"type": "object", "properties": {
                  "summary": {"type": "string"},
                  "outbound": {"type": "array", "description": "Cities in order from the assembly city to the destination",
                               "items": {"type": "object", "properties": {
                                   "city": {"type": "string"}, "city_id": {"type": "integer"}, "country": {"type": "string"},
                                   "km_from_previous": {"type": "number"}, "minutes_from_previous": {"type": "integer"},
                                   "minutes_max_from_previous": {"type": "integer"}, "road": {"type": "string"},
                                   "distance_source": {"type": "string", "description": "'TMS', a URL, or 'estimate'"},
                                   "note": {"type": "string"}}, "required": ["city"]}},
                  "return_differs": {"type": "boolean"},
                  "return_route": {"type": "array", "items": {"type": "object", "properties": {
                      "city": {"type": "string"}, "city_id": {"type": "integer"}, "km_from_previous": {"type": "number"},
                      "minutes_from_previous": {"type": "integer"}}, "required": ["city"]}},
                  "pois": {"type": "array", "items": {"type": "object", "properties": {
                      "name": {"type": "string"}, "near_city": {"type": "string"}, "type": {"type": "string"},
                      "on_route": {"type": "boolean"}, "detour_km_round_trip": {"type": "number"},
                      "detour_minutes_round_trip": {"type": "integer"}, "visit_minutes": {"type": "integer"},
                      "why": {"type": "string"}, "source": {"type": "string"}, "major": {"type": "boolean"}},
                      "required": ["name", "near_city"]}},
                  "flags": {"type": "array", "items": {"type": "string"}}},
                  "required": ["outbound", "pois", "flags"]}}


def _brief_text(brief):
    fig = group_figures(brief)
    b = {k: v for k, v in brief.items() if k not in ("parties", "checkpoint_criteria", "flag_responses", "flags_settled")
         and v not in (None, "", [])}
    b["group"] = fig
    b["parties"] = brief.get("parties")
    answers = flag_answers(brief)
    if answers:
        b["planner_answers_to_earlier_flags"] = answers
    return json.dumps(b, ensure_ascii=False, indent=1)


# The planner's answers to the agent's flags (Zeb, Oct 2026: "No place to
# respond to flags"). Kept in the brief by stage and flag text, so they
# survive a re-run and every later stage's agent reads them.
FLAG_STATUSES = [("open", "Open"), ("noted", "Noted, I'll handle it"), ("resolved", "Checked / resolved"),
                 ("dismissed", "Not an issue")]
FLAG_STATUS = dict(FLAG_STATUSES)


def flag_answers(brief):
    out = []
    for key, answers in (brief.get("flag_responses") or {}).items():
        for flag, a in (answers or {}).items():
            if a.get("status", "open") == "open" and not a.get("note"):
                continue
            out.append(f"[{STAGE.get(key, {}).get('label', key)}] Flag: {flag[:300]} -> Planner: "
                       f"{FLAG_STATUS.get(a.get('status'), 'Open')}" + (f". {a['note']}" if a.get("note") else ""))
    return out


def flag_report(smap, brief, keys, show_all=False):
    """[{key, label, approved, items: [{n, flag, status, note}]}] for the
    stages that have flags; without show_all only those still needing
    attention (open, or noted but not resolved)."""
    out = []
    for k in keys:
        data = smap[k]["data"]
        flags = (data or {}).get("flags") or [] if isinstance(data, dict) else []
        answers = (brief.get("flag_responses") or {}).get(k) or {}
        items = []
        for n, f in enumerate(flags, 1):
            a = answers.get(f) or {}
            status = a.get("status") or "open"
            if not show_all and status in ("resolved", "dismissed"):
                continue
            items.append({"n": n, "flag": f, "status": status, "note": a.get("note") or ""})
        if items:
            out.append({"key": k, "label": STAGE[k]["label"], "approved": smap[k]["approved_at"], "items": items,
                        "total": len(flags)})
    return out


def open_flag_count(smap, brief):
    return sum(len(sec["items"]) for sec in flag_report(smap, brief, [k for k in STAGE_KEYS if smap[k]["built"]]))


def save_flag_responses(db, plan_id, key, responses):
    """responses: {flag text: {status, note}} for one stage. Changes no
    stage result, so nothing has to be redone."""
    plan = db.execute("SELECT brief FROM tour_plans WHERE plan_id = ?", (plan_id,)).fetchone()
    b = dict(DEFAULT_BRIEF, **json.loads(plan["brief"] or "{}"))
    fr = dict(b.get("flag_responses") or {})
    fr[key] = {f: a for f, a in responses.items() if a.get("note") or a.get("status", "open") != "open"}
    b["flag_responses"] = fr
    db.execute("UPDATE tour_plans SET brief = ?, updated_at = datetime('now') WHERE plan_id = ?",
               (json.dumps(b, ensure_ascii=False), plan_id))
    db.commit()


def open_flags(smap, brief, key):
    """The flags of one stage nobody has answered yet (status Open)."""
    data = smap[key]["data"]
    flags = ((data or {}).get("flags") or []) if isinstance(data, dict) else []
    answers = (brief.get("flag_responses") or {}).get(key) or {}
    return [f for f in flags if ((answers.get(f) or {}).get("status") or "open") == "open"]


def resolve_open_flags(db, plan_id, key, smap, approved_at=None):
    """Approving a stage accepts it as it stands (Zeb, Oct 2026: "Checkpoint
    Status says Approved. But ... still say 'Open'"): every flag still Open
    becomes Checked / resolved, marked as accepted with the approval. Flags
    the planner answered keep their answer ('Noted, I'll handle it' stays on
    the list of things to do).

    Done once per approval (brief.flags_settled remembers it), so a flag the
    planner sets back to Open afterwards stays Open. Returns how many were
    resolved."""
    plan = db.execute("SELECT brief FROM tour_plans WHERE plan_id = ?", (plan_id,)).fetchone()
    b = dict(DEFAULT_BRIEF, **json.loads(plan["brief"] or "{}"))
    stamp = approved_at or db.execute("SELECT approved_at FROM tour_plan_stages WHERE plan_id = ? AND stage_key = ?",
                                      (plan_id, key)).fetchone()[0]
    settled = dict(b.get("flags_settled") or {})
    if not stamp or settled.get(key) == stamp:
        return 0
    todo = open_flags(smap, b, key)
    fr = dict(b.get("flag_responses") or {})
    answers = dict(fr.get(key) or {})
    for f in todo:
        a = dict(answers.get(f) or {})
        a.update(status="resolved", accepted_on=stamp[:10])
        answers[f] = a
    fr[key] = answers
    b["flag_responses"] = fr
    settled[key] = stamp
    b["flags_settled"] = settled
    db.execute("UPDATE tour_plans SET brief = ? WHERE plan_id = ?", (json.dumps(b, ensure_ascii=False), plan_id))
    db.commit()
    return len(todo)


def revise_instructions(key, responses):
    """'Ask the agent to revise' text from the answers to a stage's flags."""
    lines = [f"- {f[:300]}\n  Planner: {FLAG_STATUS.get(a.get('status'), 'Open')}" + (f". {a['note']}" if a.get("note") else "")
             for f, a in responses.items() if a.get("note") or a.get("status", "open") != "open"]
    if not lines:
        return ""
    return ("Revise your answer using the planner's responses to your flags. Drop flags marked resolved or not an "
            "issue; act on the notes:\n" + "\n".join(lines))


def run_route(db, tenant_id, client, brief, instructions, prior, meter, log):
    prompt = (
        "You are planning a group tour for a tour operator. Work out the ROUTE only (no hotels or days yet).\n\n"
        f"Tour Brief:\n{_brief_text(brief)}\n\n"
        "1. List the cities in order along the main road route from the assembly city to the destination "
        "(every town a traveller passes that matters for stops, fuel, meals or overnights). Use find_cities to get each "
        "city's TMS id, and get_distances for the legs; use TMS figures where they exist (distance_source 'TMS'). For a "
        "missing leg, give your best figure from web search (distance_source = the page URL) or a careful estimate "
        "(distance_source 'estimate'). Drive times are for normal weather and traffic, for a minibus.\n"
        "2. Say whether the return differs from the outbound route; if it does, list it.\n"
        "3. Points of interest: use find_pois for the route cities. List the major attractions on the route and close "
        "to it. For one off the main road (for example Taxila near Islamabad), give the extra round-trip distance and "
        "time from the route, and a typical visit length. Mark major ones.\n"
        "4. Flags: anything that affects the route -- border crossings, permits, seasonal closures or weather on the "
        "brief's dates, security escorts, roads where vehicles must change -- and anything in the brief you don't "
        "understand. Mark facts you could not confirm as 'verify'.\n"
        + (f"\nThe planner reviewed your previous answer and asks for these changes:\n{instructions}\n"
           f"Previous answer:\n{json.dumps(prior, ensure_ascii=False)[:12000]}\n" if instructions and prior else "")
        + "\nWhen done, call report_route.")
    log("Working out the route from TMS data…")
    report = agent_loop(db, tenant_id, client, prompt, ["find_cities", "get_distances", "find_pois", "find_hubs"],
                        ROUTE_TOOL, meter, log, web_searches=5)
    return tidy_route(db, report)


def tidy_route(db, r):
    """Fill TMS ids/figures the agent left out; numbers as numbers."""
    from fuzzy import compare
    cities = db.execute("SELECT city_id, label FROM cities").fetchall()

    def cid_for(name, given):
        if given and _city_row(db, given):
            return given
        for c in cities:
            if compare(name or "", c["label"]) == "same":
                return c["city_id"]
        return None
    for leg_list in ("outbound", "return_route"):
        prev = None
        for leg in r.get(leg_list) or []:
            leg["city_id"] = cid_for(leg.get("city"), leg.get("city_id"))
            if leg["city_id"]:
                f = db.execute("SELECT is_checkpoint FROM cities WHERE city_id = ?", (leg["city_id"],)).fetchone()
                leg["tms_checkpoint"] = bool(f and f[0])
            if prev and leg["city_id"] and prev.get("city_id"):
                a, b = sorted((prev["city_id"], leg["city_id"]))
                d = db.execute("SELECT * FROM city_distances WHERE city_a_id = ? AND city_b_id = ?", (a, b)).fetchone()
                # TMS figures win: they're the ones the platform maintains.
                if d and d["road_km"]:
                    leg["km_from_previous"], leg["distance_source"] = d["road_km"], "TMS"
                if d and d["drive_minutes"]:
                    leg["minutes_from_previous"] = d["drive_minutes"]
                    leg["minutes_max_from_previous"] = d["drive_minutes_max"]
                if d and d["route_name"] and not leg.get("road"):
                    leg["road"] = d["route_name"]
            prev = leg
    for p in r.get("pois") or []:
        p.setdefault("include", bool(p.get("major", True)))
    return r


def propose_distances(db, tenant_id, plan_name, route):
    """Legs the agent found on the web (between two TMS cities that TMS has
    no distance for) go to the platform's TMS Agents review queue."""
    rows = []
    prev = None
    for leg in route.get("outbound") or []:
        if prev and leg.get("city_id") and prev.get("city_id") and str(leg.get("distance_source", "")).startswith("http"):
            a, b = sorted((prev["city_id"], leg["city_id"]))
            if not db.execute("SELECT 1 FROM city_distances WHERE city_a_id = ? AND city_b_id = ? AND road_km IS NOT NULL",
                              (a, b)).fetchone():
                rows.append((a, b, f"{prev['city']} ↔ {leg['city']}", leg))
        prev = leg
    if not rows:
        return 0
    cur = db.execute("INSERT INTO platform_agent_runs (agent_key, scope_label, params, status, progress, started_at, finished_at, "
                     "items_total, items_done) VALUES ('distances', ?, '{}', 'done', ?, datetime('now'), datetime('now'), 1, 1)",
                     (f"From a tenant's Tour Planner: {plan_name}", "Distances found by the Tour Planner, for review."))
    run_id = cur.lastrowid
    n = 0
    for a, b, label, leg in rows:
        for field, value in (("road_km", leg.get("km_from_previous")), ("drive_minutes", leg.get("minutes_from_previous")),
                             ("drive_minutes_max", leg.get("minutes_max_from_previous")), ("route_name", leg.get("road"))):
            if value in (None, ""):
                continue
            db.execute("""INSERT INTO platform_agent_proposals (run_id, entity, record_key, record_label, field, proposed_value,
                              confidence, source_url, note) VALUES (?, 'city_distances', ?, ?, ?, ?, 0.6, ?, 'From the Tour Planner')""",
                       (run_id, f"{a}:{b}", label, field, str(value), leg["distance_source"][:500]))
            n += 1
    db.execute("UPDATE platform_agent_runs SET proposals = ? WHERE run_id = ?", (n, run_id))
    return n


# ---- stage: checkpoints and the day budget --------------------------------------------------------------------------

CHECKPOINT_TOOL = {"name": "report_checkpoints", "description": "Overnight checkpoints and the day-by-day driving budget.",
                   "input_schema": {"type": "object", "properties": {
                       "summary": {"type": "string"},
                       "checkpoints": {"type": "array", "items": {"type": "object", "properties": {
                           "city": {"type": "string"}, "city_id": {"type": "integer"}, "nights": {"type": "integer"},
                           "why": {"type": "string"}, "lodging_note": {"type": "string"},
                           "dining_note": {"type": "string"}, "airport": {"type": "string"},
                           "km_from_previous_checkpoint": {"type": "number"},
                           "criteria_check": {"type": "string",
                                              "description": "How this stop meets each checkpoint criterion (or which it misses)"},
                           "suggest_flag": {"type": "boolean",
                                            "description": "Not a TMS checkpoint yet, but should be flagged as one"}},
                           "required": ["city", "nights"]}},
                       "days": {"type": "array", "items": {"type": "object", "properties": {
                           "day": {"type": "integer"}, "date": {"type": "string"}, "from": {"type": "string"},
                           "to": {"type": "string"}, "km": {"type": "number"}, "drive_hours": {"type": "number"},
                           "visits": {"type": "array", "items": {"type": "string"}}, "overnight": {"type": "string"},
                           "note": {"type": "string"}}, "required": ["day", "overnight"]}},
                       "fits": {"type": "boolean", "description": "Whether the plan fits the brief's tour length"},
                       "considered": {"type": "array", "description": "Other candidate overnight cities and why not",
                                      "items": {"type": "object", "properties": {
                                          "city": {"type": "string"}, "why_not": {"type": "string"}}, "required": ["city"]}},
                       "flags": {"type": "array", "items": {"type": "string"}}},
                       "required": ["checkpoints", "days", "fits", "flags"]}}


def criteria_text(brief):
    c = criteria_of(brief)
    fig = group_figures(brief)
    lines = [f"- Driving between checkpoints: at most {brief.get('max_drive_hours') or 8} hours a day"
             + (f" and at most {c['max_km']} km" if c.get("max_km") else "")
             + (f"; at least {c['min_km']} km between checkpoints" if c.get("min_km") else "") + ".",
             f"- Accommodation: one hotel that takes the whole group ({fig['guest_rooms']} guest rooms: {fig['doubles']} doubles, "
             f"{fig['singles']} singles; plus {fig['guide_rooms']} guide and {fig['driver_rooms']} driver rooms), standard "
             f"{brief.get('standard') or 'any'}." + (f" Also: {c['hotel']}" if c.get("hotel") else ""),
             "- Food and meals: " + (c["food"] or "a restaurant or hotel dining room that can serve the group dinner and breakfast."),
             "- Points of interest: " + (c["pois"] or "prefer stops near the major attractions on the route, so visits fit the days."),
             "- Prefer cities TMS already flags as overnight checkpoints." if c.get("prefer_flagged", True) else
             "- TMS checkpoint flags don't matter; choose on the criteria alone."]
    if c.get("other"):
        lines.append(f"- Also: {c['other']}")
    return "\n".join(lines)


def save_criteria(db, plan_id, criteria):
    """Store the checkpoint criteria; the checkpoints and everything after
    them need redoing."""
    plan = db.execute("SELECT brief FROM tour_plans WHERE plan_id = ?", (plan_id,)).fetchone()
    b = dict(DEFAULT_BRIEF, **json.loads(plan["brief"] or "{}"))
    b["checkpoint_criteria"] = criteria
    db.execute("UPDATE tour_plans SET brief = ?, updated_at = datetime('now') WHERE plan_id = ?",
               (json.dumps(b, ensure_ascii=False), plan_id))
    reset_after(db, plan_id, "route")
    db.commit()


def mark_checkpoints(db, result):
    """Which chosen checkpoints TMS already flags as overnight stops."""
    from fuzzy import compare
    cities = db.execute("SELECT city_id, label, is_checkpoint FROM cities").fetchall()
    for cp in result.get("checkpoints") or []:
        c = None
        if cp.get("city_id"):
            c = next((x for x in cities if x["city_id"] == cp["city_id"]), None)
        if c is None:
            c = next((x for x in cities if compare(cp.get("city") or "", x["label"]) == "same"), None)
        cp["city_id"] = c["city_id"] if c else None
        cp["tms_checkpoint"] = bool(c and c["is_checkpoint"])
        if cp["tms_checkpoint"]:
            cp["suggest_flag"] = False
    return result


def propose_checkpoint_flags(db, plan_name, result):
    """Checkpoints the planner approved that TMS doesn't flag yet: proposed to
    the platform (TMS Agents review queue) as overnight checkpoints."""
    todo = [cp for cp in result.get("checkpoints") or []
            if cp.get("city_id") and not cp.get("tms_checkpoint") and int(cp.get("nights") or 0) > 0
            and not cp.get("flag_proposed")]
    todo = [cp for cp in todo if not db.execute(
        "SELECT 1 FROM platform_agent_proposals WHERE entity = 'city_flags' AND record_key = ? AND status = 'pending'",
        (str(cp["city_id"]),)).fetchone()]
    if not todo:
        return 0
    cur = db.execute("INSERT INTO platform_agent_runs (agent_key, scope_label, params, status, progress, started_at, finished_at, "
                     "items_total, items_done) VALUES ('geography', ?, '{}', 'done', ?, datetime('now'), datetime('now'), 1, 1)",
                     (f"Checkpoints from a tenant's Tour Planner: {plan_name}",
                      "Overnight checkpoints a tenant's planner approved, for review."))
    run_id = cur.lastrowid
    for cp in todo:
        db.execute("""INSERT INTO platform_agent_proposals (run_id, entity, record_key, record_label, field, current_value,
                          proposed_value, confidence, source_url, note)
                      VALUES (?, 'city_flags', ?, ?, 'is_checkpoint', 'No', 'Yes', 0.7, 'Tour Planner', ?)""",
                   (run_id, str(cp["city_id"]), cp["city"],
                    (cp.get("criteria_check") or cp.get("why") or "Chosen as an overnight stop")[:500]))
        cp["flag_proposed"] = True
    db.execute("UPDATE platform_agent_runs SET proposals = ? WHERE run_id = ?", (len(todo), run_id))
    return len(todo)


def run_checkpoints(db, tenant_id, client, brief, route, instructions, prior, meter, log):
    prompt = (
        "You are planning a group tour. The route is approved. Now choose the CHECKPOINTS -- cities where the group "
        "stays overnight, which need reasonable accommodation and dining for the group -- and lay out the day-by-day "
        "driving budget.\n\n"
        f"Tour Brief:\n{_brief_text(brief)}\n\nApproved route:\n{json.dumps(route, ensure_ascii=False)[:14000]}\n\n"
        f"CHECKPOINT CRITERIA set by the planner (apply every one; say in criteria_check how each stop meets them):\n"
        f"{criteria_text(brief)}\n\n"
        "Cities marked tms_overnight_checkpoint are known good overnight stops; prefer them when the criteria allow. Any "
        "city on the route may be a checkpoint, though: when one that is not flagged in TMS suits the criteria better, "
        "recommend it and set suggest_flag = true so TMS can flag it. List the other candidate cities you weighed and "
        "why not (considered).\n\n"
        "Rules: day 1 is the assembly date in the assembly city's morning (travel to the assembly city is not part of "
        "the tour length); the tour length is the brief's tour_days; keep each day's driving under the brief's "
        "max_drive_hours, and add the time of the visits on that day; include the major POIs where they fit; plan the "
        "return as the brief says (overland, flown, or one way) and end in the assembly city if overland so parties "
        "can fly home. Use lodging_at to check hotels TMS knows at each candidate city and prefer cities with hotels "
        "of the brief's standard; name the nearest airport for checkpoints where a party could fly. If the plan cannot "
        "fit the tour length, say so (fits = false) and say in flags what you'd cut or how many days it needs. "
        "Account for weather days if the brief asks for them, and flag seasonal risks on the dates.\n"
        + (f"\nThe planner reviewed your previous answer and asks for these changes:\n{instructions}\n"
           f"Previous answer:\n{json.dumps(prior, ensure_ascii=False)[:12000]}\n" if instructions and prior else "")
        + "\nCall report_checkpoints when done.")
    log("Choosing checkpoints and the day budget…")
    report = agent_loop(db, tenant_id, client, prompt, ["find_cities", "get_distances", "lodging_at", "find_hubs", "find_pois"],
                        CHECKPOINT_TOOL, meter, log, web_searches=3)
    return mark_checkpoints(db, report)


# ---- stage: hotels and dining -------------------------------------------------------------------------------------

LODGING_TOOL = {"name": "report_lodging", "description": "Hotel options and restaurants for each checkpoint.",
                "input_schema": {"type": "object", "properties": {
                    "summary": {"type": "string"},
                    "checkpoints": {"type": "array", "items": {"type": "object", "properties": {
                        "city": {"type": "string"}, "nights": {"type": "integer"},
                        "hotels": {"type": "array", "items": {"type": "object", "properties": {
                            "name": {"type": "string"}, "supplier_id": {"type": "integer"},
                            "accommodation_id": {"type": "integer"}, "stars": {"type": "number"},
                            "double_usd": {"type": "number", "description": "per room per night"},
                            "single_usd": {"type": "number"},
                            "price_basis": {"type": "string", "description": "'TMS price as of <date>', a URL, or 'estimate'"},
                            "source": {"type": "string", "description": "'tenant supplier', 'platform catalog' or a URL"},
                            "meets_standard": {"type": "boolean"}, "why": {"type": "string"},
                            "recommended": {"type": "boolean"}}, "required": ["name"]}},
                        "restaurants": {"type": "array", "items": {"type": "object", "properties": {
                            "name": {"type": "string"}, "supplier_id": {"type": "integer"},
                            "restaurant_id": {"type": "integer"}, "cuisine": {"type": "string"},
                            "lunch_usd_pp": {"type": "number"}, "dinner_usd_pp": {"type": "number"},
                            "group_ok": {"type": "boolean"}, "source": {"type": "string"}, "note": {"type": "string"}},
                            "required": ["name"]}},
                        "meals_note": {"type": "string", "description": "e.g. breakfast and dinner at the hotel"}},
                        "required": ["city", "hotels"]}},
                    "flags": {"type": "array", "items": {"type": "string"}}},
                    "required": ["checkpoints", "flags"]}}


def run_lodging(db, tenant_id, client, brief, route, checkpoints, instructions, prior, meter, log):
    city_ids = {}
    for leg in (route.get("outbound") or []) + (route.get("return_route") or []):
        if leg.get("city_id"):
            city_ids[leg["city"]] = leg["city_id"]
    cps = [{"city": cp["city"], "city_id": city_ids.get(cp["city"]), "nights": cp.get("nights")}
           for cp in checkpoints.get("checkpoints") or []]
    prompt = (
        "You are planning a group tour. The route and the overnight checkpoints are approved. Now find the HOTELS and "
        "RESTAURANTS at each checkpoint.\n\n"
        f"Tour Brief:\n{_brief_text(brief)}\n\nCheckpoints (with TMS city ids):\n{json.dumps(cps, ensure_ascii=False)}\n\n"
        "For each checkpoint, use hotels_at and restaurants_at. Offer 2-3 hotel options, best first, and mark ONE as "
        "recommended. Prefer, in this order: the tenant's own suppliers (they have contracts and prices; a 'Primary' "
        "preference is their top choice), then hotels in the platform catalog, then hotels you find on the web. The "
        "hotel must take the whole group in one place (the brief's guest rooms plus guide and driver rooms) and meet "
        "the brief's standard; where nothing of that standard exists, say so (meets_standard = false) and offer the best "
        "available. Give per-night prices in USD for a double room and a single room: the tenant's TMS price where it "
        "has one in its room types (price_basis 'TMS price as of <date>'), otherwise the hotel's TMS Reference Room Rate "
        "(reference_rate_usd -- a double room; price_basis 'TMS reference rate as of <date>'; for a single room use "
        "about 80% of it unless you know better), otherwise a web rate (price_basis = URL) or a careful estimate "
        "(price_basis 'estimate'). For restaurants, give 1-3 places that can seat the group, with a typical lunch and "
        "dinner cost per person in USD; say where meals are better taken at the hotel. Flag anything uncertain.\n"
        + (f"\nThe planner reviewed your previous answer and asks for these changes:\n{instructions}\n"
           f"Previous answer:\n{json.dumps(prior, ensure_ascii=False)[:12000]}\n" if instructions and prior else "")
        + "\nCall report_lodging when done.")
    log("Looking for hotels and restaurants at each checkpoint…")
    report = agent_loop(db, tenant_id, client, prompt, ["hotels_at", "restaurants_at", "find_cities"], LODGING_TOOL,
                        meter, log, web_searches=6, max_turns=18)
    nights = {cp["city"]: cp.get("nights") for cp in checkpoints.get("checkpoints") or []}
    for cp in report.get("checkpoints") or []:
        cp["nights"] = nights.get(cp.get("city"), cp.get("nights"))
        hotels = cp.get("hotels") or []
        picked = [h for h in hotels if h.get("recommended")]
        for i, h in enumerate(hotels):
            h["recommended"] = bool(picked and h is picked[0]) or (not picked and i == 0)
    return report


def lodging_budget(brief, lodging):
    """Rooms x nights x price at each checkpoint's chosen hotel. Guide and
    driver rooms are priced as singles."""
    fig = group_figures(brief)
    rows, total, unknown = [], 0.0, False
    for cp in (lodging or {}).get("checkpoints") or []:
        h = next((x for x in cp.get("hotels") or [] if x.get("recommended")), None)
        n = int(cp.get("nights") or 0)
        if not h or not n:
            continue
        dbl, sgl = h.get("double_usd"), h.get("single_usd") or h.get("double_usd")
        singles = fig["singles"] + fig["guide_rooms"] + fig["driver_rooms"]
        if dbl is None or sgl is None:
            unknown = True
            cost = None
        else:
            cost = n * (fig["doubles"] * dbl + singles * sgl)
            total += cost
        rows.append({"city": cp["city"], "hotel": h["name"], "nights": n, "doubles": fig["doubles"], "singles": singles,
                     "double_usd": dbl, "single_usd": sgl, "cost": cost, "basis": h.get("price_basis")})
    per_guest = round(total / fig["guests"], 2) if fig["guests"] else None
    return {"rows": rows, "total": round(total, 2), "per_guest": per_guest, "partial": unknown}


# ---- stage: day by day ---------------------------------------------------------------------------------------------

DAYS_TOOL = {"name": "report_days", "description": "The day-by-day itinerary.",
             "input_schema": {"type": "object", "properties": {
                 "summary": {"type": "string"},
                 "days": {"type": "array", "items": {"type": "object", "properties": {
                     "day": {"type": "integer"}, "date": {"type": "string"}, "title": {"type": "string"},
                     "from": {"type": "string"}, "to": {"type": "string"}, "km": {"type": "number"},
                     "drive_hours": {"type": "number"},
                     "schedule": {"type": "array", "items": {"type": "object", "properties": {
                         "time": {"type": "string"}, "item": {"type": "string"}}, "required": ["item"]}},
                     "visits": {"type": "array", "items": {"type": "string"}},
                     "breakfast": {"type": "string"}, "lunch": {"type": "string"}, "dinner": {"type": "string"},
                     "overnight": {"type": "string"}, "hotel": {"type": "string"},
                     "description": {"type": "string", "description": "2-4 sentences for the guest-facing itinerary"},
                     "notes": {"type": "string", "description": "operational notes for the tour team"}},
                     "required": ["day", "title", "overnight"]}},
                 "flags": {"type": "array", "items": {"type": "string"}}},
                 "required": ["days", "flags"]}}


def run_days(db, tenant_id, client, brief, route, checkpoints, lodging, instructions, prior, meter, log):
    pois = [p for p in route.get("pois") or [] if p.get("include")]
    picks = [{"city": cp["city"], "nights": cp.get("nights"),
              "hotel": next((h["name"] for h in cp.get("hotels") or [] if h.get("recommended")), None),
              "restaurants": [r["name"] for r in cp.get("restaurants") or []], "meals_note": cp.get("meals_note")}
             for cp in (lodging or {}).get("checkpoints") or []]
    prompt = (
        "You are writing the DAY-BY-DAY itinerary of a group tour. The route, checkpoints, day budget and hotels are "
        "approved; follow them (same days, same overnights, same hotels).\n\n"
        f"Tour Brief:\n{_brief_text(brief)}\n\nDay budget (approved):\n"
        f"{json.dumps(checkpoints.get('days'), ensure_ascii=False)[:8000]}\n\nPoints of interest to include:\n"
        f"{json.dumps(pois, ensure_ascii=False)[:6000]}\n\nHotels and restaurants (approved):\n"
        f"{json.dumps(picks, ensure_ascii=False)[:6000]}\n\n"
        "For each day give: a short title; a timed schedule (departure, drives with comfort and fuel stops, visits with "
        "their length, meals, arrival); the visits; where breakfast, lunch and dinner are taken; the overnight city and "
        "hotel; a 2-4 sentence description written for the guests; and operational notes for the tour team (permits "
        "to show, border formalities, escorts, early starts). Use find_pois for opening hours and entry fees where it "
        "helps; respect opening days. Keep driving within the brief's max_drive_hours. Flag anything that doesn't fit.\n"
        + (f"\nThe planner reviewed your previous answer and asks for these changes:\n{instructions}\n"
           f"Previous answer:\n{json.dumps(prior, ensure_ascii=False)[:12000]}\n" if instructions and prior else "")
        + "\nCall report_days when done.")
    log("Writing the day-by-day itinerary…")
    report = agent_loop(db, tenant_id, client, prompt, ["find_pois", "find_cities"], DAYS_TOOL, meter, log,
                        web_searches=3, max_turns=12)
    return fill_dates(brief, report)


def fill_dates(brief, report):
    from datetime import date, timedelta
    try:
        start = date.fromisoformat(brief.get("start_date") or "")
    except ValueError:
        return report
    for d in report.get("days") or []:
        try:
            d["date"] = (start + timedelta(days=int(d.get("day") or 1) - 1)).isoformat()
        except (TypeError, ValueError):
            pass
    return report


# ---- stage: journey grid (computed) ------------------------------------------------------------------------------------

def journey_grid(route, checkpoints):
    """Cumulative distance along the outbound route, the leg table, and a
    checkpoint x checkpoint grid of km and drive time."""
    legs, cum = [], []
    km = mins = mins_max = 0.0
    unknown = False
    for i, c in enumerate(route.get("outbound") or []):
        if i:
            k = c.get("km_from_previous")
            m = c.get("minutes_from_previous")
            mx = c.get("minutes_max_from_previous") or m
            if k is None or m is None:
                unknown = True
            km += k or 0
            mins += m or 0
            mins_max += mx or 0
            legs.append({"from": route["outbound"][i - 1]["city"], "to": c["city"], "km": k, "minutes": m,
                         "minutes_max": c.get("minutes_max_from_previous"), "road": c.get("road"),
                         "source": c.get("distance_source")})
        cum.append({"city": c["city"], "km": round(km, 1), "minutes": mins, "minutes_max": mins_max, "partial": unknown})
    names = [c["city"] for c in cum]
    cps = [cp["city"] for cp in (checkpoints or {}).get("checkpoints") or [] if cp.get("city") in names]
    cps = list(dict.fromkeys(cps))
    grid = []
    for a in cps:
        row = []
        ia = names.index(a)
        for b in cps:
            ib = names.index(b)
            lo, hi = sorted((ia, ib))
            row.append({"km": round(cum[hi]["km"] - cum[lo]["km"], 1), "minutes": cum[hi]["minutes"] - cum[lo]["minutes"],
                        "minutes_max": cum[hi]["minutes_max"] - cum[lo]["minutes_max"],
                        "partial": cum[hi]["partial"]})
        grid.append({"from": a, "cells": row})
    return {"legs": legs, "cumulative": cum, "checkpoints": cps, "grid": grid,
            "total_km": round(km, 1), "total_minutes": mins, "total_minutes_max": mins_max, "partial": unknown}


def hours(m):
    if not isinstance(m, (int, float, str)) or m == "":
        return "—"
    try:
        m = int(float(m))
    except ValueError:
        return "—"
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}m"


# ---- runs ----------------------------------------------------------------------------------------------------------------

def start_run(db, tenant_id, user_id, plan_id, key, instructions=None, background=True):
    if STAGE[key]["kind"] != "ai" and key != "brief":
        raise PlannerError("That stage isn't run by the agent.")
    over = ai_usage.check_limit(db, tenant_id)
    if over:
        raise PlannerError(over)
    anthropic_client()
    if db.execute("SELECT 1 FROM tour_plan_runs WHERE plan_id = ? AND status IN ('queued','running')", (plan_id,)).fetchone():
        raise PlannerError("The agent is already working on this plan; wait for it to finish (or Stop it).")
    cur = db.execute("INSERT INTO tour_plan_runs (tenant_id, plan_id, stage_key, instructions, created_by, items_total) "
                     "VALUES (?, ?, ?, ?, ?, 1)", (tenant_id, plan_id, key, (instructions or "").strip() or None, user_id))
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
        run(db, run_id)
    finally:
        db.close()


def run(db, run_id):
    r = db.execute("SELECT * FROM tour_plan_runs WHERE run_id = ?", (run_id,)).fetchone()
    tenant_id, plan_id, key = r["tenant_id"], r["plan_id"], r["stage_key"]
    lines = []

    def log(text):
        lines.append(text)
        db.execute("UPDATE tour_plan_runs SET progress = ?, heartbeat_at = datetime('now') WHERE run_id = ?",
                   ("\n".join(lines)[-6000:], run_id))
        db.commit()

    def meter(usage):
        ai_usage.record(db, tenant_id, FEATURE, model(), usage, user_id=r["created_by"], ref_type="tour_plan_runs",
                        ref_id=run_id, note=STAGE[key]["label"])
        db.execute("UPDATE tour_plan_runs SET heartbeat_at = datetime('now') WHERE run_id = ?", (run_id,))
        db.commit()

    db.execute("UPDATE tour_plan_runs SET status = 'running', started_at = datetime('now'), heartbeat_at = datetime('now') "
               "WHERE run_id = ? AND status = 'queued'", (run_id,))
    db.commit()
    try:
        plan = db.execute("SELECT * FROM tour_plans WHERE plan_id = ?", (plan_id,)).fetchone()
        brief = brief_of(plan)
        smap = stages(db, plan_id)
        prior = smap[key]["data"]
        client = anthropic_client()
        if key == "brief":
            log("Reading the request…")
            found = parse_request(client, plan["request_text"] or r["instructions"] or "", meter, brief)
            result = {"brief": merge_brief(brief, found), "questions": found.get("questions") or []}
            if agent_runs.cancelled(db, "tour_plan_runs", run_id):
                return
            db.execute("UPDATE tour_plans SET brief = ?, name = COALESCE(NULLIF(?, ''), name) WHERE plan_id = ?",
                       (json.dumps(result["brief"], ensure_ascii=False), result["brief"].get("name") or "", plan_id))
            set_result(db, plan_id, "brief", result["brief"], run_id)
            db.execute("UPDATE tour_plan_runs SET result = ? WHERE run_id = ?", (json.dumps(result, ensure_ascii=False), run_id))
            log(f"Brief filled in; {len(result['questions'])} question(s) to answer.")
        elif key == "route":
            result = run_route(db, tenant_id, client, brief, r["instructions"], prior, meter, log)
            if agent_runs.cancelled(db, "tour_plan_runs", run_id):
                return
            set_result(db, plan_id, key, result, run_id)
            n = propose_distances(db, tenant_id, plan["name"], result)
            log(f"Route: {len(result.get('outbound') or [])} cities, {len(result.get('pois') or [])} points of interest"
                + (f"; {n} new distance value(s) sent to the platform for review" if n else ""))
        elif key == "checkpoints":
            route = smap["route"]["data"]
            if not route:
                raise PlannerError("Approve the route first.")
            result = run_checkpoints(db, tenant_id, client, brief, route, r["instructions"], prior, meter, log)
            if agent_runs.cancelled(db, "tour_plan_runs", run_id):
                return
            set_result(db, plan_id, key, result, run_id)
            log(f"{len(result.get('checkpoints') or [])} checkpoints over {len(result.get('days') or [])} days"
                + ("" if result.get("fits", True) else " -- does NOT fit the tour length; see flags"))
        elif key == "lodging":
            if not (smap["route"]["data"] and smap["checkpoints"]["data"]):
                raise PlannerError("Approve the route and checkpoints first.")
            result = run_lodging(db, tenant_id, client, brief, smap["route"]["data"], smap["checkpoints"]["data"],
                                 r["instructions"], prior, meter, log)
            if agent_runs.cancelled(db, "tour_plan_runs", run_id):
                return
            set_result(db, plan_id, key, result, run_id)
            b = lodging_budget(brief, result)
            log(f"Hotels for {len(result.get('checkpoints') or [])} checkpoints; accommodation about USD {b['total']:,.0f}"
                + (" (some prices missing)" if b["partial"] else ""))
        elif key == "days":
            if not smap["lodging"]["data"]:
                raise PlannerError("Approve the hotels first.")
            result = run_days(db, tenant_id, client, brief, smap["route"]["data"], smap["checkpoints"]["data"],
                              smap["lodging"]["data"], r["instructions"], prior, meter, log)
            if agent_runs.cancelled(db, "tour_plan_runs", run_id):
                return
            set_result(db, plan_id, key, result, run_id)
            log(f"{len(result.get('days') or [])} days written")
        else:
            raise PlannerError("That stage isn't built yet.")
        db.execute("UPDATE tour_plan_runs SET status = 'done', items_done = 1, finished_at = datetime('now'), result = COALESCE(result, ?) "
                   "WHERE run_id = ? AND status = 'running'", (json.dumps(result, ensure_ascii=False), run_id))
        db.commit()
    except Exception as e:
        import image_collector
        msg = image_collector.friendly_api_error(e) or str(e) or traceback.format_exc()[-800:]
        try:
            log(f"Stopped: {msg}")
        except Exception:
            pass
        db.execute("UPDATE tour_plan_runs SET status = 'failed', error = ?, finished_at = datetime('now') "
                   "WHERE run_id = ? AND status = 'running'", (msg[:1000], run_id))
        db.commit()


def compute_grid(db, plan_id):
    smap = stages(db, plan_id)
    data = journey_grid(smap["route"]["data"] or {}, smap["checkpoints"]["data"] or {})
    set_result(db, plan_id, "grid", data)
    db.commit()
    return data


def mark_stale(db, tenant_id):
    agent_runs.mark_stale(db, "tour_plan_runs", " AND tenant_id = ?", (tenant_id,))


def run_cost(db, plan_id):
    return db.execute("""SELECT COALESCE(SUM(u.cost_usd), 0), COUNT(*) FROM ai_usage_log u
                         JOIN tour_plan_runs r ON r.run_id = u.ref_id AND u.ref_type = 'tour_plan_runs'
                         WHERE r.plan_id = ?""", (plan_id,)).fetchone()
