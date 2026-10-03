"""
The tenant's inventory of AI agents (Zeb, Oct 2026: "All Agents should
show under Agents, even though they can be launched from within other
forms ... it would be an inventory list of the Agents").

AGENTS is the registry the AI Agents page draws its cards from: every
agent a tenant has, where it is started, and how its runs are found.
Some are started from other screens too (the Image Collector from a
hotel's photo album); the page lists them all in one place, with each
agent's runs and this month's cost, and a single list of recent runs
across every agent.

To add an agent: add an entry here, and a branch in recent_runs() that
reads its run table.
"""
from flask import url_for

import ai_usage

AGENTS = [
    {"key": "tour_planner", "label": "Tour Planner", "icon": "map",
     "what": "Describe a tour; the agent plans the route, attractions, overnight stops, hotels and drive times "
             "with you, stage by stage, towards a draft Package.",
     "reads": "TMS data (cities, distances, POIs, your Suppliers) and the web",
     "writes": "A tour plan you review and approve stage by stage",
     "also_from": None, "start": ("tour_planner.index", {}), "start_label": "Open the Tour Planner",
     "features": ["Tour Planner"]},
    {"key": "hotel_intelligence", "label": "Hotel Intelligence", "icon": "building",
     "what": "Reads web pages or images you give it about a hotel (a listing, a booking screenshot) and proposes "
             "updates to the hotel's details: name, address, website, notes.",
     "reads": "Only the links and images you give it",
     "writes": "Proposed changes, field by field, to the Review Queue",
     "also_from": None, "start": ("ai_agents.new_run", {"agent_type": "hotel_intelligence"}),
     "start_label": "New run", "features": ["Agent Run"]},
    {"key": "poi_intelligence", "label": "Points of Interest Intelligence", "icon": "geo-alt",
     "what": "The same for one of your Points of Interest: reads the pages or images you give it and proposes "
             "updates to its details.",
     "reads": "Only the links and images you give it",
     "writes": "Proposed changes, field by field, to the Review Queue",
     "also_from": None, "start": ("ai_agents.new_run", {"agent_type": "poi_intelligence"}),
     "start_label": "New run", "features": ["Agent Run"]},
    {"key": "image_collector", "label": "Image Collector", "icon": "images",
     "what": "Finds photos of your hotels, resorts and restaurants on their own websites and the web, for one "
             "supplier or several at once (by type and city).",
     "reads": "The suppliers' websites and the web",
     "writes": "Photos waiting for you to keep or drop in the image album",
     "also_from": "a supplier's image album (Collect photos)",
     "start": ("images.import_images", {}), "start_label": "Choose suppliers",
     "features": ["Image Collector"]},
]

BY_KEY = {a["key"]: a for a in AGENTS}

# Agents on the drawing board, so a tenant can see what's coming.
PLANNED = [
    {"label": "Travel Advisories", "icon": "exclamation-diamond",
     "what": "Keeps the travel advisories for your destinations up to date."},
    {"label": "Travel Document Requirements", "icon": "passport",
     "what": "Visa and entry requirements by nationality and destination, for your tours."},
]

STATUS = {"completed": "done", "done": "done", "running": "running", "queued": "running", "failed": "failed",
          "pending": "pending"}


def recent_runs(db, tenant_id, limit=30):
    """Runs of every agent, newest first: dicts with agent, scope, status,
    result, created_at, url."""
    from tour_planner import STAGE
    out = []
    for r in db.execute(
            """SELECT r.run_id, r.agent_type, r.scope_label, r.status, r.created_at,
                      COUNT(i.review_id) AS items, SUM(CASE WHEN i.status = 'pending' THEN 1 ELSE 0 END) AS pending
               FROM agent_runs r LEFT JOIN ai_review_items i ON i.run_id = r.run_id
               WHERE r.tenant_id = ? GROUP BY r.run_id ORDER BY r.created_at DESC LIMIT ?""", (tenant_id, limit)):
        result = f"{r['items']} review item{'s' if r['items'] != 1 else ''}"
        out.append({"agent": r["agent_type"], "scope": r["scope_label"], "status": STATUS.get(r["status"], r["status"]),
                    "result": result, "pending": r["pending"] or 0, "created_at": r["created_at"],
                    "url": url_for("ai_agents.view_run", run_id=r["run_id"]),
                    "delete_id": r["run_id"] if not r["items"] and r["status"] != "running" else None})
    for r in db.execute(
            """SELECT run_id, scope_label, status, images_found, images_staged, created_at FROM image_agent_runs
               WHERE tenant_id = ? AND COALESCE(owner_kind, 'supplier') = 'supplier'
               ORDER BY created_at DESC LIMIT ?""", (tenant_id, limit)):
        out.append({"agent": "image_collector", "scope": r["scope_label"], "status": STATUS.get(r["status"], r["status"]),
                    "result": f"{r['images_staged'] or 0} photo{'s' if r['images_staged'] != 1 else ''} collected",
                    "pending": 0, "created_at": r["created_at"], "url": url_for("images.agent_run", run_id=r["run_id"]),
                    "delete_id": None})
    for r in db.execute(
            """SELECT r.run_id, r.plan_id, r.stage_key, r.status, r.created_at, p.name
               FROM tour_plan_runs r JOIN tour_plans p ON p.plan_id = r.plan_id
               WHERE r.tenant_id = ? ORDER BY r.created_at DESC LIMIT ?""", (tenant_id, limit)):
        stage = STAGE.get(r["stage_key"], {}).get("label", r["stage_key"])
        out.append({"agent": "tour_planner", "scope": r["name"], "status": STATUS.get(r["status"], r["status"]),
                    "result": stage, "pending": 0, "created_at": r["created_at"],
                    "url": url_for("tour_planner.workspace", plan_id=r["plan_id"], stage=r["stage_key"]),
                    "delete_id": None})
    out.sort(key=lambda x: x["created_at"] or "", reverse=True)
    return out[:limit]


def agent_stats(db, tenant_id):
    """{agent key: {runs, last, month_cost, waiting}}."""
    stats = {a["key"]: {"runs": 0, "last": None, "month_cost": 0.0, "waiting": 0} for a in AGENTS}

    def put(key, n, last):
        stats[key]["runs"] += n or 0
        if last and (stats[key]["last"] is None or last > stats[key]["last"]):
            stats[key]["last"] = last
    for r in db.execute("SELECT agent_type, COUNT(*), MAX(created_at) FROM agent_runs WHERE tenant_id = ? GROUP BY agent_type",
                        (tenant_id,)):
        if r[0] in stats:
            put(r[0], r[1], r[2])
    r = db.execute("SELECT COUNT(*), MAX(created_at) FROM image_agent_runs WHERE tenant_id = ? "
                   "AND COALESCE(owner_kind, 'supplier') = 'supplier'", (tenant_id,)).fetchone()
    put("image_collector", r[0], r[1])
    r = db.execute("SELECT COUNT(*), MAX(created_at) FROM tour_plan_runs WHERE tenant_id = ?", (tenant_id,)).fetchone()
    put("tour_planner", r[0], r[1])
    # Waiting for a person: review items, photos to keep or drop.
    for r in db.execute("""SELECT r.agent_type, COUNT(*) FROM ai_review_items i JOIN agent_runs r ON r.run_id = i.run_id
                           WHERE i.tenant_id = ? AND i.status = 'pending' GROUP BY r.agent_type""", (tenant_id,)):
        if r[0] in stats:
            stats[r[0]]["waiting"] = r[1]
    try:
        stats["image_collector"]["waiting"] = db.execute(
            """SELECT COUNT(*) FROM image_import_items i JOIN image_import_batches b ON b.batch_id = i.batch_id
               WHERE i.tenant_id = ? AND b.status = 'open' AND b.owner_kind = 'supplier'
               AND b.batch_id IN (SELECT batch_id FROM image_agent_runs WHERE tenant_id = ?)""",
            (tenant_id, tenant_id)).fetchone()[0]
    except Exception:  # older databases without these columns: no count
        pass
    # This month's cost, by the feature name each agent meters under.
    start, end = ai_usage.month_bounds()
    for r in db.execute("""SELECT u.feature, a.agent_type, COALESCE(SUM(u.cost_usd), 0) FROM ai_usage_log u
                           LEFT JOIN agent_runs a ON u.ref_type = 'agent_runs' AND a.run_id = u.ref_id
                           WHERE u.tenant_id = ? AND u.created_at >= ? AND u.created_at < ?
                           GROUP BY u.feature, a.agent_type""", (tenant_id, start, end)):
        if r[0] == "Agent Run" and r[1] in stats:
            stats[r[1]]["month_cost"] += r[2]
        else:
            for a in AGENTS:
                if r[0] in a["features"] and a["features"] != ["Agent Run"]:
                    stats[a["key"]]["month_cost"] += r[2]
    return stats


def start_url(agent):
    endpoint, args = agent["start"]
    return url_for(endpoint, **args)
