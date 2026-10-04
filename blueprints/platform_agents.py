"""
TMS Agents screens (SystemAdmin) -- logic in platform_agents.py.

* /platform/agents/            Start a run of one of the platform agents
                               (Geography, Distances & Drive Times, POI
                               Enrichment) and see recent runs.
* /platform/agents/run/<id>    A run's progress, cost and the values it proposes.
* /platform/agents/review      Every proposal still waiting, across runs.
* POST .../decide              Approve (write to the catalog) or reject proposals.
"""
from flask import Blueprint, abort, flash, g, jsonify, redirect, render_template, request, url_for

import agent_runs
import platform_agents as pa
from platform_catalog import PROPERTY_TYPES
from auth.decorators import system_admin_required
from db import get_db, log_action

platform_agents_bp = Blueprint("platform_agents", __name__)


def _int(v):
    try:
        return int(v) if v not in (None, "") else None
    except ValueError:
        return None


@platform_agents_bp.route("/")
@system_admin_required
def index():
    db = get_db()
    pa.mark_stale(db)
    runs = db.execute("""SELECT r.*, (SELECT COUNT(*) FROM platform_agent_proposals p WHERE p.run_id = r.run_id
                                      AND p.status = 'pending') AS waiting
                         FROM platform_agent_runs r ORDER BY r.run_id DESC LIMIT 20""").fetchall()
    countries = db.execute("""SELECT DISTINCT co.country_id, co.label FROM countries co JOIN states s ON s.country_id = co.country_id
                              JOIN cities ci ON ci.state_id = s.state_id ORDER BY co.label""").fetchall()
    states = db.execute("""SELECT s.state_id, s.label, co.label AS country FROM states s JOIN countries co ON co.country_id = s.country_id
                           WHERE EXISTS (SELECT 1 FROM cities ci WHERE ci.state_id = s.state_id) ORDER BY co.label, s.label""").fetchall()
    cities = db.execute("""SELECT ci.city_id, ci.label, s.label AS state, co.label AS country FROM cities ci
                           JOIN states s ON s.state_id = ci.state_id JOIN countries co ON co.country_id = s.country_id
                           WHERE ci.is_active = 1 ORDER BY co.label = 'Pakistan' DESC, co.label, ci.label""").fetchall()
    poi_cities = db.execute("""SELECT DISTINCT ci.city_id, ci.label FROM platform_pois p JOIN cities ci ON ci.city_id = p.city_id
                               ORDER BY ci.label""").fetchall()
    poi_types = db.execute("""SELECT DISTINCT pt.poi_type_id, pt.label FROM platform_pois p JOIN poi_types pt ON pt.poi_type_id = p.poi_type_id
                              ORDER BY pt.label""").fetchall()
    gaps = {
        "geography": db.execute("SELECT COUNT(*) FROM cities WHERE is_active = 1 AND (latitude IS NULL OR longitude IS NULL "
                                "OR timezone IS NULL OR altitude_m IS NULL)").fetchone()[0],
        "distances": db.execute("SELECT COUNT(*) FROM city_distances WHERE road_km IS NULL OR drive_minutes IS NULL "
                                "OR route_name IS NULL OR rail_available IS NULL").fetchone()[0],
        "pois": db.execute("SELECT COUNT(*) FROM platform_pois WHERE is_active = 1 AND (" +
                           " OR ".join(f"{f} IS NULL OR {f} = ''" for f in pa.FIELDS["platform_pois"]) + ")").fetchone()[0],
    }
    catalog_cities = {}
    for key in ("accommodation", "restaurants"):
        table = pa.CATALOG_ENTITIES[pa.AGENTS[key]["entity"]][0]
        catalog_cities[key] = db.execute(f"""SELECT ci.city_id, ci.label, COUNT(*) AS n FROM {table} p
                                             JOIN cities ci ON ci.city_id = p.city_id WHERE p.is_active = 1
                                             GROUP BY ci.city_id ORDER BY ci.label""").fetchall()
        gaps[key] = db.execute(f"SELECT COUNT(*) FROM {table} WHERE is_active = 1 AND (" +
                               " OR ".join(f"{c} IS NULL OR {c} = ''" for c in pa.FIELDS[pa.AGENTS[key]["entity"]]) +
                               ")").fetchone()[0]
    all_poi_types = db.execute("""SELECT pt.poi_type_id, pt.label FROM poi_types pt JOIN tenants t ON t.tenant_id = pt.tenant_id
                                  WHERE t.is_platform = 1 AND pt.is_active = 1 ORDER BY pt.label""").fetchall()
    return render_template("platform_agents/index.html", agents=pa.AGENTS, runs=runs, countries=countries, states=states,
                           cities=cities, poi_cities=poi_cities, poi_types=poi_types, gaps=gaps,
                           pending=pa.pending_count(db), all_poi_types=all_poi_types, catalog_cities=catalog_cities,
                           property_types=PROPERTY_TYPES)


@platform_agents_bp.route("/start-pdf", methods=["POST"])
@system_admin_required
def start_pdf():
    """POIs from a PDF (pdf_poi_agent.py): upload, then the run reads it."""
    import pdf_poi_agent
    db = get_db()
    files = [(f.filename.replace("\\", "/").split("/")[-1][:150], f.read())
             for f in request.files.getlist("pdf") if f and f.filename]
    if not files:
        flash("Choose a PDF file.", "error")
        return redirect(url_for("platform_agents.index"))
    params = {"poi_type_id": _int(request.form.get("poi_type_id")),
              "mode": request.form.get("mode") if request.form.get("mode") in pdf_poi_agent.MODES else "multi",
              "again": bool(request.form.get("again"))}
    if params["poi_type_id"]:
        r = db.execute("SELECT label FROM poi_types WHERE poi_type_id = ?", (params["poi_type_id"],)).fetchone()
        params["poi_type_label"] = r[0] if r else None
    name = files[0][0] if len(files) == 1 else f"{len(files)} PDFs"
    try:
        run_id = pdf_poi_agent.start(db, files, params, g.user_id)
    except (pdf_poi_agent.PdfError, pa.AgentError) as e:
        flash(str(e), "error")
        return redirect(url_for("platform_agents.index"))
    pa.launch(run_id)
    log_action("TMSAgent", "platform_agent_runs", run_id, f"Started TMS Agent POIs from a PDF: {name}")
    return redirect(url_for("platform_agents.run_page", run_id=run_id))


@platform_agents_bp.route("/run/<int:run_id>/pdf")
@system_admin_required
def run_pdf(run_id):
    """The PDF a run read -- proposals link to it at their page (#page=N)."""
    import pdf_poi_agent
    from io import BytesIO
    from flask import send_file
    up = pdf_poi_agent.upload(get_db(), run_id, request.args.get("u", type=int))
    if up is None:
        abort(404)
    return send_file(BytesIO(up["file_data"]), mimetype="application/pdf", download_name=up["file_name"])


@platform_agents_bp.route("/start/<agent_key>", methods=["POST"])
@system_admin_required
def start(agent_key):
    if agent_key not in pa.AGENTS:
        abort(404)
    db = get_db()
    f = request.form
    params = {"limit": _int(f.get("limit")) or pa.AGENTS[agent_key]["max"], "only_missing": bool(f.get("only_missing"))}
    parts = []
    if agent_key == "geography":
        params.update(country_id=_int(f.get("country_id")), state_id=_int(f.get("state_id")))
        for key, table, pk in (("state_id", "states", "state_id"), ("country_id", "countries", "country_id")):
            if params[key]:
                r = db.execute(f"SELECT label FROM {table} WHERE {pk} = ?", (params[key],)).fetchone()
                parts.append(r[0] if r else "")
                break
        label = "Cities" + (f" in {parts[0]}" if parts else "")
    elif agent_key == "distances":
        if f.get("mode") == "gaps":
            params.update(from_city_id=None, to_city_ids=[])
            label = "Gaps in existing distances"
        else:
            params.update(from_city_id=_int(f.get("from_city_id")),
                          to_city_ids=[int(x) for x in f.getlist("to_city_ids") if x.isdigit()])
            if not params["from_city_id"] or not params["to_city_ids"]:
                flash("Choose a starting city and at least one destination.", "error")
                return redirect(url_for("platform_agents.index"))
            r = db.execute("SELECT label FROM cities WHERE city_id = ?", (params["from_city_id"],)).fetchone()
            label = f"From {r[0] if r else '?'}"
    elif agent_key in ("accommodation", "restaurants"):
        params.update(country_id=_int(f.get("country_id")), city_id=_int(f.get("city_id")))
        if agent_key == "accommodation":
            ptype = f.get("property_type") or None
            params["property_type"] = ptype if ptype in PROPERTY_TYPES else None
        if params["city_id"]:
            r = db.execute("SELECT label FROM cities WHERE city_id = ?", (params["city_id"],)).fetchone()
            parts.append(f"in {r[0]}" if r else "")
        noun = (params.get("property_type") or "Accommodation") if agent_key == "accommodation" else "Restaurants"
        label = " ".join([noun] + [p for p in parts if p])
    else:
        params.update(country_id=_int(f.get("country_id")), city_id=_int(f.get("city_id")),
                      poi_type_id=_int(f.get("poi_type_id")))
        if params["city_id"]:
            r = db.execute("SELECT label FROM cities WHERE city_id = ?", (params["city_id"],)).fetchone()
            parts.append(f"in {r[0]}" if r else "")
        if params["poi_type_id"]:
            r = db.execute("SELECT label FROM poi_types WHERE poi_type_id = ?", (params["poi_type_id"],)).fetchone()
            parts.insert(0, r[0] if r else "")
        label = " ".join(["POIs"] + [p for p in parts if p])
    try:
        run_id = pa.start_run(db, agent_key, params, g.user_id, label)
    except pa.AgentError as e:
        flash(str(e), "error")
        return redirect(url_for("platform_agents.index"))
    log_action("TMSAgent", "platform_agent_runs", run_id, f"Started TMS Agent {pa.AGENTS[agent_key]['label']}: {label}")
    return redirect(url_for("platform_agents.run_page", run_id=run_id))


def _grouped(rows):
    groups = {}
    for p in rows:
        groups.setdefault((p["entity"], p["record_key"]), {"label": p["record_label"], "entity": p["entity"],
                                                            "items": []})["items"].append(p)
    return list(groups.values())


@platform_agents_bp.route("/run/<int:run_id>/status.json")
@system_admin_required
def run_status(run_id):
    db = get_db()
    pa.mark_stale(db)
    st = agent_runs.status_json(db, "platform_agent_runs", run_id, extra_cols=("proposals",))
    if st is None:
        abort(404)
    return jsonify(st)


@platform_agents_bp.route("/run/<int:run_id>/stop", methods=["POST"])
@system_admin_required
def stop_run(run_id):
    db = get_db()
    if agent_runs.stop(db, "platform_agent_runs", run_id, g.get("display_name") or g.get("username")):
        log_action("PlatformAgent", "platform_agent_runs", run_id, "Stopped a TMS Agent run")
        flash("Run stopped. Values it already found are kept for review.", "success")
    return redirect(url_for("platform_agents.run_page", run_id=run_id))


@platform_agents_bp.route("/run/<int:run_id>")
@system_admin_required
def run_page(run_id):
    db = get_db()
    pa.mark_stale(db)
    run = db.execute("SELECT * FROM platform_agent_runs WHERE run_id = ?", (run_id,)).fetchone()
    if run is None:
        abort(404)
    show = request.args.get("show", "pending")
    sql = "SELECT * FROM platform_agent_proposals WHERE run_id = ?"
    if show in pa.PROPOSAL_STATUSES:
        sql += f" AND status = '{show}'"
    rows = db.execute(sql + " ORDER BY record_label, proposal_id", (run_id,)).fetchall()
    counts = dict(db.execute("SELECT status, COUNT(*) FROM platform_agent_proposals WHERE run_id = ? GROUP BY status",
                             (run_id,)).fetchall())
    cost, calls, searches = pa.run_cost(db, run_id)
    batch = None
    if run["batch_id"]:
        batch = db.execute("""SELECT b.batch_id, b.status,
                                     (SELECT COUNT(*) FROM image_import_items i WHERE i.batch_id = b.batch_id) AS total,
                                     (SELECT COUNT(*) FROM image_import_items i WHERE i.batch_id = b.batch_id
                                      AND i.status = 'pending') AS pending
                              FROM image_import_batches b WHERE b.batch_id = ?""", (run["batch_id"],)).fetchone()
    return render_template("platform_agents/run.html", photo_batch=batch, run=run, agent=pa.AGENTS[run["agent_key"]], groups=_grouped(rows),
                           counts=counts, show=show, cost=cost, calls=calls, searches=searches, fields=pa.PROPOSAL_FIELDS,
                           entity_labels=pa.ENTITY_LABELS, now_utc=db.execute("SELECT datetime('now')").fetchone()[0],
                           status_url=url_for("platform_agents.run_status", run_id=run_id),
                           stop_url=url_for("platform_agents.stop_run", run_id=run_id),
                           unit=RUN_UNITS.get(run["agent_key"], "records"))


RUN_UNITS = {"geography": "cities", "distances": "city pairs", "pois": "POIs", "pdf": "steps"}


@platform_agents_bp.route("/review")
@system_admin_required
def review():
    db = get_db()
    agent_key = request.args.get("agent")
    sql = """SELECT p.*, r.agent_key FROM platform_agent_proposals p JOIN platform_agent_runs r ON r.run_id = p.run_id
             WHERE p.status = 'pending'"""
    args = []
    if agent_key in pa.AGENTS:
        sql += " AND r.agent_key = ?"
        args.append(agent_key)
    rows = db.execute(sql + " ORDER BY p.entity, p.record_label, p.proposal_id LIMIT 600", args).fetchall()
    return render_template("platform_agents/review.html", groups=_grouped(rows), agents=pa.AGENTS, agent_key=agent_key,
                           fields=pa.PROPOSAL_FIELDS, entity_labels=pa.ENTITY_LABELS, total=pa.pending_count(db))


@platform_agents_bp.route("/decide", methods=["POST"])
@system_admin_required
def decide():
    db = get_db()
    action = request.form.get("action")
    back = request.form.get("next") or url_for("platform_agents.review")
    if not back.startswith("/") or back.startswith("//"):
        back = url_for("platform_agents.review")
    if action == "approve_confident":
        ids = [int(x) for x in request.form.getlist("all_ids") if x.isdigit()]
        ids = [r[0] for r in db.execute(
            f"SELECT proposal_id FROM platform_agent_proposals WHERE status = 'pending' AND COALESCE(confidence, 0) >= 0.8 "
            f"AND proposal_id IN ({','.join('?' * len(ids)) or 'NULL'})", ids)]
        approve = True
    else:
        ids = [int(x) for x in request.form.getlist("ids") if x.isdigit()]
        approve = action == "approve"
    if not ids:
        flash("Tick the values to approve or reject first." if action != "approve_confident"
              else "No values here have a confidence of 0.8 or more.", "error")
        return redirect(back)
    applied, rejected, errors = pa.decide(db, ids, approve, g.user_id)
    for e in errors[:8]:
        flash(f"Not applied: {e}", "error")
    if applied:
        log_action("TMSAgentApprove", "platform_agent_proposals", None, f"Approved {applied} TMS Agent value(s)")
        flash(f"Approved and saved {applied} value{'s' if applied != 1 else ''}. Points of Interest changes are synced to tenants.", "success")
    if rejected:
        log_action("TMSAgentReject", "platform_agent_proposals", None, f"Rejected {rejected} TMS Agent value(s)")
        flash(f"Rejected {rejected} value{'s' if rejected != 1 else ''}.", "success")
    return redirect(back)

