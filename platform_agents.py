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

Every run is narrow by design (a chosen country / city / list, capped in
size), works in batches (one Claude call with web search per batch), and each
proposed value carries a confidence and the page it came from. Approved POI
changes reach tenants through Catalog Sync.
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
ENTITY_LABELS = {"cities": "City", "city_distances": "Distance", "platform_pois": "Point of Interest"}

AGENTS = {
    "geography": {"label": "Geography", "icon": "globe-americas", "entity": "cities", "batch": 10, "max": 40,
                  "blurb": "Finds coordinates, time zone and altitude for cities that are missing them."},
    "distances": {"label": "Distances & Drive Times", "icon": "signpost-split", "entity": "city_distances", "batch": 8,
                  "max": 30, "blurb": "Finds road distance, typical drive time, main route and rail for city pairs."},
    "pois": {"label": "POI Enrichment", "icon": "geo-alt", "entity": "platform_pois", "batch": 4, "max": 24,
             "blurb": "Fills in descriptions, opening hours, entry fees, year founded, website, phone and coordinates."},
    "pdf": {"label": "POIs from a PDF", "icon": "file-earmark-pdf", "entity": "platform_pois", "batch": 1, "max": 1,
            "blurb": "Reads a PDF (brochure, guidebook, report): proposes new Points of Interest and updates to known "
                     "ones, and stages its photos for the Master Image Catalog."},
}

# New Points of Interest proposed by the PDF agent (pdf_poi_agent.py): created when approved.
FIELDS["platform_pois_new"] = {"name": ("Name", "text"), "city": ("City", "text"), "poi_type": ("POI Type", "text"),
                               **FIELDS["platform_pois"]}
ENTITY_LABELS["platform_pois_new"] = "New Point of Interest"


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
        if kind == "tz":
            from zoneinfo import available_timezones
            return (v, None) if v in available_timezones() else (None, "not an IANA time zone")
        if kind == "yesno":
            s = str(v).strip().lower()
            return ("Yes", None) if s in ("yes", "y", "true") else ("No", None) if s in ("no", "n", "false") else (None, "Yes or No")
        if kind == "url":
            return (v, None) if str(v).startswith(("http://", "https://")) else (None, "not a web address")
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
        f"Allowed fields: {', '.join(fields)}.\n\nItems:\n{json.dumps(items, ensure_ascii=False, indent=1)}\n\n"
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
        resp = client.messages.create(model=model(), max_tokens=4000, tools=tools, messages=messages)
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
        if agent_key == "pdf":
            import pdf_poi_agent
            total = pdf_poi_agent.run(db, run_id, client, log, meter,
                                      lambda: agent_runs.cancelled(db, "platform_agent_runs", run_id))
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
        msg = explain_api_error(e) or str(e) or traceback.format_exc()[-800:]
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
    errors, touched_pois = [], set()
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
        kind = FIELDS[p["entity"]][p["field"]][1]
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
                cur = db.execute(f"UPDATE platform_pois SET {p['field']} = ?, updated_at = datetime('now') WHERE poi_id = ?",
                                 (value, int(p["record_key"])))
                if not cur.rowcount:
                    raise AgentError("the POI no longer exists")
            elif p["entity"] == "cities":
                db.execute(f"UPDATE cities SET {p['field']} = ? WHERE city_id = ?", (value, int(p["record_key"])))
            elif p["entity"] == "platform_pois":
                cur = db.execute(f"UPDATE platform_pois SET {p['field']} = ?, checked_on = ?, updated_at = datetime('now'), "
                                 "source = COALESCE(NULLIF(source, ''), ?) WHERE poi_id = ?",
                                 (value, today, f"TMS Agent: {p['source_url']}", int(p["record_key"])))
                if not cur.rowcount:
                    raise AgentError("the POI no longer exists")
                touched_pois.add(int(p["record_key"]))
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
            errors.append(f"{p['record_label']} — {FIELDS[p['entity']][p['field']][0]}: {e}")
    db.commit()
    if touched_pois:
        import catalog_sync
        catalog_sync.push(db, "pois", sorted(touched_pois))
        db.commit()
    return applied, rejected, errors


def pending_count(db):
    return db.execute("SELECT COUNT(*) FROM platform_agent_proposals WHERE status = 'pending'").fetchone()[0]
