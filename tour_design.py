"""
Tour Design -> Package (Zeb, Oct 2026): "Tour Planning is where all the
Tour Ideation and early decisions are done. Several people may be working
independently on 'Tour Design', each proposing their design. The 'Tour
Package' module will only import one or more appropriate selections from
the list of Tour Designs. So the Tour Design may require a separate set of
tables that shadow the 'Package'."

A Tour Design is a Tour Planner plan (tour_plans + tour_plan_stages; its
working copy, which keeps changing while people work on it). When every
built stage is approved, a designer marks it **Ready for package**: the
design is frozen into tour_design_versions -- the shadow of a Package (the
brief and every stage's result as approved, the open flags with their
answers). Package Management offers only Ready designs, and "Add from Tour
Design" builds one Package per chosen design from its frozen version:

    packages              <- name, days, nights, guests, currency, summary, notes
    package_route_stops   <- the checkpoints (city, nights), in order
    package_days          <- the day-by-day itinerary (title, guest text, visits, meals, team notes)
    package_day_pois      <- each day's visits matched to the tenant's Points of Interest
    package_components    <- the chosen hotel at each checkpoint (rooms x nights, price, supplier)

The design keeps going after that (a new version can be marked ready and
added as another Package); a Package records the version it came from
(packages.tour_design_version_id).
"""
import json

import tour_planner as tp

DDL = """
CREATE TABLE IF NOT EXISTS tour_design_versions (
    version_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    plan_id         INTEGER NOT NULL REFERENCES tour_plans(plan_id),
    version_no      INTEGER NOT NULL,
    name            TEXT NOT NULL,
    snapshot        TEXT NOT NULL,          -- JSON: {brief, stages: {key: result}, open_flags: [...]}
    days            INTEGER,
    nights          INTEGER,
    guests          INTEGER,
    open_flags      INTEGER NOT NULL DEFAULT 0,
    note            TEXT,                   -- the designer's note when marking it ready
    marked_by       INTEGER REFERENCES users(user_id),
    marked_at       TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (plan_id, version_no)
);
CREATE INDEX IF NOT EXISTS idx_tour_design_versions_tenant ON tour_design_versions(tenant_id);
"""


class DesignError(Exception):
    pass


def missing_for_ready(smap):
    """Labels of the built stages not yet approved."""
    return [s["label"] for k, s in smap.items() if s["built"] and not s["approved_at"]]


def current_version(db, plan):
    vid = plan["ready_version_id"] if "ready_version_id" in plan.keys() else None
    if not vid:
        return None
    return db.execute("SELECT * FROM tour_design_versions WHERE version_id = ?", (vid,)).fetchone()


def _snapshot(smap, brief):
    keys = [k for k in tp.STAGE_KEYS if smap[k]["built"]]
    report = tp.flag_report(smap, brief, keys)
    return {"brief": brief, "stages": {k: smap[k]["data"] for k in keys},
            "open_flags": [{"stage": sec["label"], "flag": it["flag"], "status": it["status"], "note": it["note"]}
                           for sec in report for it in sec["items"]]}


def changed_since(db, plan, smap, brief):
    """True when the working design differs from its Ready version."""
    v = current_version(db, plan)
    if v is None:
        return False
    snap = json.loads(v["snapshot"])
    now = _snapshot(smap, brief)
    return snap["stages"] != now["stages"] or snap["brief"].get("tour_days") != brief.get("tour_days")


def mark_ready(db, tenant_id, plan_id, user_id, note=None):
    plan = db.execute("SELECT * FROM tour_plans WHERE plan_id = ? AND tenant_id = ?", (plan_id, tenant_id)).fetchone()
    if plan is None:
        raise DesignError("Tour Design not found.")
    smap = tp.stages(db, plan_id)
    missing = missing_for_ready(smap)
    if missing:
        raise DesignError("Approve every stage first: " + ", ".join(missing) + ".")
    brief = tp.brief_of(plan)
    snap = _snapshot(smap, brief)
    days = len((snap["stages"].get("days") or {}).get("days") or []) or brief.get("tour_days")
    nights = sum(int(cp.get("nights") or 0) for cp in (snap["stages"].get("checkpoints") or {}).get("checkpoints") or [])
    n = db.execute("SELECT COALESCE(MAX(version_no), 0) + 1 FROM tour_design_versions WHERE plan_id = ?", (plan_id,)).fetchone()[0]
    cur = db.execute("""INSERT INTO tour_design_versions (tenant_id, plan_id, version_no, name, snapshot, days, nights,
                            guests, open_flags, note, marked_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                     (tenant_id, plan_id, n, plan["name"], json.dumps(snap, ensure_ascii=False), days, nights,
                      tp.group_figures(brief)["guests"], len(snap["open_flags"]), (note or "").strip() or None, user_id))
    db.execute("UPDATE tour_plans SET ready_version_id = ?, status = 'done', updated_at = datetime('now') WHERE plan_id = ?",
               (cur.lastrowid, plan_id))
    db.commit()
    return n


def withdraw(db, tenant_id, plan_id):
    db.execute("UPDATE tour_plans SET ready_version_id = NULL, status = 'draft', updated_at = datetime('now') "
               "WHERE plan_id = ? AND tenant_id = ?", (plan_id, tenant_id))
    db.commit()


def ready_designs(db, tenant_id):
    """Designs offered to Package Management: each plan's Ready version."""
    return db.execute(
        """SELECT v.*, p.plan_id, u.username AS marked_by_name,
                  (SELECT COUNT(*) FROM packages k WHERE k.tour_design_version_id = v.version_id
                     AND k.status != 'archived') AS packages_made
           FROM tour_plans p JOIN tour_design_versions v ON v.version_id = p.ready_version_id
           LEFT JOIN users u ON u.user_id = v.marked_by
           WHERE p.tenant_id = ? AND p.status != 'archived' ORDER BY v.marked_at DESC""", (tenant_id,)).fetchall()


# ---- building a Package ---------------------------------------------------------------------------------------------

def _unique_code(db, tenant_id, base):
    code, k = base, 1
    while db.execute("SELECT 1 FROM packages WHERE tenant_id = ? AND package_code = ?", (tenant_id, code)).fetchone():
        k += 1
        code = f"{base}-{k}"
    return code


def _geo(db, city_id):
    if not city_id:
        return {}
    r = db.execute("""SELECT ci.city_id, s.state_id, co.country_id, co.region_id FROM cities ci
                      JOIN states s ON s.state_id = ci.state_id JOIN countries co ON co.country_id = s.country_id
                      WHERE ci.city_id = ?""", (city_id,)).fetchone()
    return dict(r) if r else {}


def _city_id(db, name):
    from fuzzy import compare
    if not name:
        return None
    for r in db.execute("SELECT city_id, label, alt_names FROM cities WHERE is_active = 1"):
        if compare(name, r["label"]) == "same":
            return r["city_id"]
    return None


def _match_poi(pois, name):
    from fuzzy import compare
    for p in pois:
        if compare(name, p["name"]) in ("same", "sounds"):
            return p["poi_id"]
    return None


def _stop_for(stops, place):
    """The route stop for an overnight city name."""
    from fuzzy import compare
    for s in stops:
        if place and compare(place, s["city"]) in ("same", "sounds"):
            return s
    return None


def _day_text(d):
    parts = [(d.get("description") or "").strip()]
    if d.get("visits"):
        parts.append("Visits: " + ", ".join(d["visits"]))
    meals = [f"{m.capitalize()}: {d[m]}" for m in ("breakfast", "lunch", "dinner") if d.get(m)]
    if meals:
        parts.append(" · ".join(meals))
    if d.get("schedule"):
        parts.append("\n".join(f"{x.get('time') or ''} {x.get('item') or ''}".strip() for x in d["schedule"]))
    if d.get("notes"):
        parts.append("Team notes: " + d["notes"].strip())
    return "\n\n".join(p for p in parts if p)


def create_package(db, tenant_id, version_id, user_id=None):
    """A draft Package from a Ready Tour Design version. Returns package_id."""
    v = db.execute("SELECT * FROM tour_design_versions WHERE version_id = ? AND tenant_id = ?",
                   (version_id, tenant_id)).fetchone()
    if v is None:
        raise DesignError("That Tour Design version wasn't found.")
    snap = json.loads(v["snapshot"])
    brief, st = snap["brief"], snap["stages"]
    fig = tp.group_figures(brief)
    days = (st.get("days") or {}).get("days") or []
    cps = (st.get("checkpoints") or {}).get("checkpoints") or []
    lodging = st.get("lodging") or {}
    route = st.get("route") or {}
    summary = (st.get("days") or {}).get("summary") or route.get("summary") or ""
    notes = [f"From Tour Design “{v['name']}”, version {v['version_no']}, marked ready {v['marked_at'][:10]}."]
    if v["note"]:
        notes.append(v["note"])
    if snap.get("open_flags"):
        notes.append("Open items from the design:\n" + "\n".join(
            f"- [{f['stage']}] {f['flag']}" + (f" -> {f['note']}" if f.get("note") else "") for f in snap["open_flags"]))
    code = _unique_code(db, tenant_id, f"TD-{v['plan_id']:04d}-V{v['version_no']}")
    group = db.execute("SELECT package_group_id FROM tour_plans WHERE plan_id = ? AND tenant_id = ?",
                       (v["plan_id"], tenant_id)).fetchone()
    cur = db.execute("""INSERT INTO packages (tenant_id, package_code, package_name, package_type, duration_days,
                            duration_nights, min_pax, max_pax, status, description, base_currency, notes,
                            tour_design_version_id, package_group_id)
                        VALUES (?, ?, ?, 'fixed_departure', ?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?)""",
                     (tenant_id, code, v["name"], len(days) or brief.get("tour_days"), v["nights"], fig["guests"] or None,
                      fig["guests"] or None, summary or None, (brief.get("currency") or "USD")[:3],
                      "\n\n".join(notes), version_id, group[0] if group else None))
    pkg = cur.lastrowid
    # Route stops: the checkpoints, in order.
    stops = []
    for i, cp in enumerate(cps, 1):
        cid = cp.get("city_id") or _city_id(db, cp.get("city"))
        geo = _geo(db, cid)
        r = db.execute("""INSERT INTO package_route_stops (tenant_id, package_id, sequence_number, region_id, country_id,
                              state_id, city_id, city_text, nights, is_checkpoint, border_crossing_notes)
                          VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
                       (tenant_id, pkg, i, geo.get("region_id"), geo.get("country_id"), geo.get("state_id"),
                        geo.get("city_id"), None if geo.get("city_id") else cp.get("city"), int(cp.get("nights") or 0),
                        (cp.get("why") or None)))
        stops.append({"stop_id": r.lastrowid, "city": cp.get("city") or ""})
    # Days.
    pois = [dict(r) for r in db.execute("SELECT poi_id, name FROM points_of_interest WHERE tenant_id = ? AND is_deleted = 0",
                                        (tenant_id,))]
    day_ids, last_stop = {}, None
    for d in sorted(days, key=lambda x: int(x.get("day") or 0)):
        n = int(d.get("day") or 0)
        if n <= 0 or n in day_ids:
            continue
        stop = _stop_for(stops, d.get("overnight")) or last_stop
        last_stop = stop or last_stop
        r = db.execute("INSERT INTO package_days (tenant_id, package_id, route_stop_id, day_number, title, description) "
                       "VALUES (?, ?, ?, ?, ?, ?)", (tenant_id, pkg, stop["stop_id"] if stop else None, n,
                                                    (d.get("title") or f"Day {n}")[:200], _day_text(d) or None))
        day_ids[n] = (r.lastrowid, stop)
        seq = 0
        for name in d.get("visits") or []:
            pid = _match_poi(pois, name)
            if pid and not db.execute("SELECT 1 FROM package_day_pois WHERE day_id = ? AND poi_id = ?",
                                      (r.lastrowid, pid)).fetchone():
                seq += 1
                db.execute("INSERT INTO package_day_pois (tenant_id, day_id, poi_id, sequence_number) VALUES (?, ?, ?, ?)",
                           (tenant_id, r.lastrowid, pid, seq))
    # Hotels: the chosen hotel at each checkpoint, on the first day there, repeated for the checkpoint's nights.
    budget = tp.lodging_budget(brief, lodging)
    by_city = {}
    for cp in lodging.get("checkpoints") or []:
        h = next((x for x in cp.get("hotels") or [] if x.get("recommended")), None)
        if h:
            by_city[cp.get("city")] = h
    for row in budget["rows"]:
        h = by_city.get(row["city"]) or {}
        stop = _stop_for(stops, row["city"])
        first_day = next((did for n, (did, s) in sorted(day_ids.items()) if stop and s and s["stop_id"] == stop["stop_id"]), None)
        supplier = h.get("supplier_id")
        if supplier and not db.execute("SELECT 1 FROM suppliers WHERE supplier_id = ? AND tenant_id = ?",
                                       (supplier, tenant_id)).fetchone():
            supplier = None
        for kind, rooms, price in (("double", row["doubles"], row["double_usd"]), ("single", row["singles"], row["single_usd"])):
            if not rooms:
                continue
            db.execute("""INSERT INTO package_components (tenant_id, package_id, route_stop_id, day_id, is_accommodation,
                              repeat_for_checkpoint, component_type, supplier_id, description, quantity, unit, unit_cost,
                              currency, notes, sequence_number)
                          VALUES (?, ?, ?, ?, 1, ?, 'custom', ?, ?, ?, 'per room/night', ?, 'USD', ?, ?)""",
                       (tenant_id, pkg, stop["stop_id"] if stop else None, first_day, 1 if first_day else 0, supplier,
                        f"{row['hotel']} — {kind} rooms", rooms * row["nights"], price,
                        f"{rooms} {kind} room{'s' if rooms != 1 else ''} × {row['nights']} night{'s' if row['nights'] != 1 else ''}"
                        + (f"; price: {row['basis']}" if row.get("basis") else ""), 1 if kind == "double" else 2))
    db.execute("UPDATE tour_plans SET package_id = ?, updated_at = datetime('now') WHERE plan_id = ?", (pkg, v["plan_id"]))
    db.commit()
    return pkg


def migrate(db, column_exists):
    db.executescript(DDL)
    if not column_exists(db, "tour_plans", "ready_version_id"):
        db.execute("ALTER TABLE tour_plans ADD COLUMN ready_version_id INTEGER REFERENCES tour_design_versions(version_id)")
    if not column_exists(db, "packages", "tour_design_version_id"):
        db.execute("ALTER TABLE packages ADD COLUMN tour_design_version_id INTEGER REFERENCES tour_design_versions(version_id)")
    db.commit()

