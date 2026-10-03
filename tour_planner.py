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
BUILT_PHASES = {1}

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
    "vehicle_type": "Minibus", "vehicle_seats": 14, "drivers_included": True, "currency": "USD",
    "interests": "", "notes": "",
}
RETURN_MODES = {"overland": "Overland, the same way back", "fly": "Fly back from the destination",
                "one_way": "One way (tour ends at the destination)"}
GUIDE_SEATING = {"one_per_minibus": "One guide in each minibus", "suv": "Guides in a separate SUV",
                 "none": "No guides"}


def brief_of(plan):
    b = dict(DEFAULT_BRIEF)
    try:
        b.update(json.loads(plan["brief"] or "{}"))
    except ValueError:
        pass
    return b


def group_figures(brief):
    """Guests, rooms and vehicles worked out from the brief."""
    parties = brief.get("parties") or []
    guests = sum(int(p.get("guests") or 0) for p in parties)
    doubles = sum(int(p.get("doubles") or 0) for p in parties)
    singles = sum(int(p.get("singles") or 0) for p in parties)
    seats = max(int(brief.get("vehicle_seats") or 14), 2)
    guides = int(brief.get("guides") or 0)
    if brief.get("guide_seating") == "one_per_minibus" and guides:
        per_bus = seats - 1
        buses = max(math.ceil(guests / per_bus), guides) if guests else 0
    else:
        per_bus = seats
        buses = math.ceil(guests / seats) if guests else 0
    split = []
    if buses:
        base, extra = divmod(guests, buses)
        split = [base + (1 if i < extra else 0) for i in range(buses)]
    suv = 1 if brief.get("guide_seating") == "suv" and guides else 0
    drivers = buses + suv
    return {"guests": guests, "doubles": doubles, "singles": singles, "guest_rooms": doubles + singles,
            "guide_rooms": guides, "driver_rooms": drivers if brief.get("drivers_included") else 0,
            "vehicles": buses, "per_vehicle": split, "suv": suv, "drivers": drivers}


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
    rows = db.execute("""SELECT ci.city_id, ci.label, ci.alt_names, ci.latitude, ci.longitude, s.label AS state, co.label AS country
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
             "lat": r["latitude"], "lon": r["longitude"]} for _, r in hits[:3]]})
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
                                  pt.label AS type_label FROM platform_pois p LEFT JOIN poi_types pt ON pt.poi_type_id = p.poi_type_id
                           WHERE p.is_active = 1"""):
        if r["name"].lower() in seen:
            continue
        ll = (r["latitude"], r["longitude"]) if r["latitude"] is not None and r["longitude"] is not None else None
        city, km = near(ll, r["city_id"])
        if city is None:
            continue
        out.append({"source": f"platform POI #{r['poi_id']}", "name": r["name"], "type": r["type_label"], "near": city,
                    "km_from_city": km, "about": (r["description"] or r["significance"] or "")[:200]})
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
        out.append({"city_id": cid, "city": c["label"],
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


TOOLS = {
    "find_cities": (tool_find_cities, "Look up cities in TMS by name (returns ids, province, country, coordinates).",
                    {"names": {"type": "array", "items": {"type": "string"}}}),
    "get_distances": (tool_get_distances, "Road distances and drive times TMS already holds between any of these cities.",
                      {"city_ids": {"type": "array", "items": {"type": "integer"}}}),
    "find_pois": (tool_find_pois, "Points of interest in or near these cities (tenant's own and the platform catalog).",
                  {"city_ids": {"type": "array", "items": {"type": "integer"}}, "radius_km": {"type": "number"}}),
    "lodging_at": (tool_lodging, "Hotels TMS knows in these cities (tenant suppliers, platform catalog, 4-star count).",
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
    for _ in range(max_turns):
        resp = client.messages.create(model=model(), max_tokens=8000, tools=tools, messages=messages)
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
        if report is not None:
            return report
        stop = getattr(resp, "stop_reason", None)
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
                  "vehicle_type": {"type": "string"}, "vehicle_seats": {"type": "integer"},
                  "drivers_included": {"type": "boolean"}, "interests": {"type": "string"}, "notes": {"type": "string"},
                  "parties": {"type": "array", "items": {"type": "object", "properties": {
                      "label": {"type": "string"}, "origin_city": {"type": "string"}, "origin_country": {"type": "string"},
                      "guests": {"type": "integer"}, "doubles": {"type": "integer"}, "singles": {"type": "integer"}},
                      "required": ["guests"]}},
                  "questions": {"type": "array", "items": {"type": "string"},
                                "description": "What the request leaves unclear and the planner must ask"}},
                  "required": ["parties", "questions"]}}


def parse_request(client, text, meter):
    prompt = ("A tour operator typed this request for a group tour. Read it into a Tour Brief. Couples share a double "
              "room; individuals who need their own room get a single. Put only what the request states; leave other "
              "fields out. List as questions anything a planner would need to ask (missing length, return, dates...).\n\n"
              f"Request:\n{text}\n\nCall report_brief.")
    resp = client.messages.create(model=model(), max_tokens=3000, tools=[BRIEF_TOOL],
                                  tool_choice={"type": "tool", "name": "report_brief"},
                                  messages=[{"role": "user", "content": prompt}])
    meter(getattr(resp, "usage", None))
    for b in getattr(resp, "content", []) or []:
        if getattr(b, "type", None) == "tool_use" and b.name == "report_brief":
            return b.input or {}
    raise PlannerError("The request couldn't be read into a brief.")


def merge_brief(brief, found):
    """The parsed request on top of the current brief (non-empty values only)."""
    b = dict(brief)
    for k, v in found.items():
        if k == "questions" or v in (None, "", []):
            continue
        if k in DEFAULT_BRIEF:
            b[k] = v
    return b


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
    b = {k: v for k, v in brief.items() if k not in ("parties",) and v not in (None, "", [])}
    b["group"] = fig
    b["parties"] = brief.get("parties")
    return json.dumps(b, ensure_ascii=False, indent=1)


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
                           "dining_note": {"type": "string"}, "airport": {"type": "string"}},
                           "required": ["city", "nights"]}},
                       "days": {"type": "array", "items": {"type": "object", "properties": {
                           "day": {"type": "integer"}, "date": {"type": "string"}, "from": {"type": "string"},
                           "to": {"type": "string"}, "km": {"type": "number"}, "drive_hours": {"type": "number"},
                           "visits": {"type": "array", "items": {"type": "string"}}, "overnight": {"type": "string"},
                           "note": {"type": "string"}}, "required": ["day", "overnight"]}},
                       "fits": {"type": "boolean", "description": "Whether the plan fits the brief's tour length"},
                       "flags": {"type": "array", "items": {"type": "string"}}},
                       "required": ["checkpoints", "days", "fits", "flags"]}}


def run_checkpoints(db, tenant_id, client, brief, route, instructions, prior, meter, log):
    prompt = (
        "You are planning a group tour. The route is approved. Now choose the CHECKPOINTS -- cities where the group "
        "stays overnight, which need reasonable accommodation and dining for the group -- and lay out the day-by-day "
        "driving budget.\n\n"
        f"Tour Brief:\n{_brief_text(brief)}\n\nApproved route:\n{json.dumps(route, ensure_ascii=False)[:14000]}\n\n"
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
    return agent_loop(db, tenant_id, client, prompt, ["find_cities", "get_distances", "lodging_at", "find_hubs"],
                      CHECKPOINT_TOOL, meter, log, web_searches=3)


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
            found = parse_request(client, plan["request_text"] or r["instructions"] or "", meter)
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
        else:
            raise PlannerError("That stage isn't built yet.")
        db.execute("UPDATE tour_plan_runs SET status = 'done', items_done = 1, finished_at = datetime('now'), result = COALESCE(result, ?) "
                   "WHERE run_id = ? AND status = 'running'", (json.dumps(result, ensure_ascii=False), run_id))
        db.commit()
    except Exception as e:
        import image_collector
        msg = image_collector.explain_api_error(e) or str(e) or traceback.format_exc()[-800:]
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
