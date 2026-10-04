"""
Tour Planner screens (logic in tour_planner.py) -- tenant level.

* /tour-planner/                         plans
* /tour-planner/new                      new plan: name + free-text request
* /tour-planner/<id>?stage=<key>         the plan workspace: stages down the
                                         left, the chosen stage on the right
* /tour-planner/<id>/brief               save / approve the Tour Brief
* /tour-planner/<id>/stage/<key>/run     run (or re-run with changes) an AI stage
* /tour-planner/<id>/stage/<key>/edit    edits to a stage's result
* /tour-planner/<id>/stage/<key>/approve approve a stage
* /tour-planner/<id>/run/<run>/status.json, /stop   live progress, Stop
"""
import json

from flask import Blueprint, abort, flash, g, jsonify, redirect, render_template, request, url_for

import agent_runs
import ai_usage
import tour_planner as tp
from auth.decorators import login_required
from db import get_db, log_action

tour_planner_bp = Blueprint("tour_planner", __name__)


def _tenant():
    if g.get("tenant_id") is None:
        abort(404)
    return g.tenant_id


def _plan(db, plan_id):
    p = tp.get_plan(db, g.tenant_id, plan_id)
    if p is None:
        abort(404)
    return p


def _int(v, default=0):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


@tour_planner_bp.route("/")
@login_required
def index():
    _tenant()
    db = get_db()
    tp.mark_stale(db, g.tenant_id)
    import tour_design
    plans = []
    for p in db.execute("SELECT * FROM tour_plans WHERE tenant_id = ? AND status != 'archived' ORDER BY updated_at DESC",
                        (g.tenant_id,)).fetchall():
        smap = tp.stages(db, p["plan_id"])
        built = [s for s in smap.values() if s["built"]]
        brief = tp.brief_of(p)
        plans.append({"plan": p, "brief": brief, "approved": sum(1 for s in built if s["approved_at"]),
                      "built": len(built), "working": any(s["state"] == "working" for s in built),
                      "ready": tour_design.current_version(db, p),
                      "changed": tour_design.changed_since(db, p, smap, brief)})
    return render_template("tour_planner/index.html", plans=plans)


@tour_planner_bp.route("/new", methods=["GET", "POST"])
@login_required
def new_plan():
    _tenant()
    db = get_db()
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        text = (request.form.get("request_text") or "").strip()
        if not name and not text:
            flash("Give the tour a name, or paste a request.", "error")
            return redirect(url_for("tour_planner.new_plan"))
        plan_id = tp.create_plan(db, g.tenant_id, g.user_id, name or "New tour", request_text=text or None)
        log_action("Create", "tour_plans", plan_id, f"New tour plan {name or '(from a request)'}")
        if text and request.form.get("action") == "fill":
            try:
                tp.start_run(db, g.tenant_id, g.user_id, plan_id, "brief")
            except tp.PlannerError as e:
                flash(f"The brief couldn't be filled in automatically: {e}", "error")
        return redirect(url_for("tour_planner.workspace", plan_id=plan_id, stage="brief"))
    return render_template("tour_planner/new.html", allowance=_allowance(db))


def _allowance(db):
    limit = ai_usage.monthly_limit(db, g.tenant_id)
    return {"limit": limit, "spent": ai_usage.month_spend(db, g.tenant_id)}


@tour_planner_bp.route("/<int:plan_id>")
@login_required
def workspace(plan_id):
    import tour_design
    _tenant()
    db = get_db()
    tp.mark_stale(db, g.tenant_id)
    plan = _plan(db, plan_id)
    smap = tp.stages(db, plan_id)
    key = request.args.get("stage")
    if key not in smap:
        key = next((k for k in tp.STAGE_KEYS if smap[k]["built"] and not smap[k]["approved_at"]), "brief")
    st = smap[key]
    brief = tp.brief_of(plan)
    if key == "grid" and st["data"] is None and tp.previous_approved(smap, "grid"):
        tp.compute_grid(db, plan_id)
        smap = tp.stages(db, plan_id)
        st = smap[key]
    questions = []
    if key == "brief" and st["run"] and st["run"]["result"]:
        try:
            questions = json.loads(st["run"]["result"]).get("questions") or []
        except ValueError:
            questions = []
    cost, calls = tp.run_cost(db, plan_id)
    run = st["run"]
    return render_template(
        "tour_planner/workspace.html", plan=plan, brief=brief, fig=tp.group_figures(brief), stages=smap, key=key, st=st,
        can_run=tp.previous_approved(smap, key), problems=tp.brief_problems(brief), questions=questions,
        return_modes=tp.RETURN_MODES, guide_seating=tp.GUIDE_SEATING, transport_types=tp.TRANSPORT_TYPES, hours=tp.hours, cost=cost, calls=calls,
        run=run, now_utc=db.execute("SELECT datetime('now')").fetchone()[0],
        status_url=url_for("tour_planner.run_status", plan_id=plan_id, run_id=run["run_id"]) if run else None,
        stop_url=url_for("tour_planner.run_stop", plan_id=plan_id, run_id=run["run_id"]) if run else None,
        unit="step", allowance=_allowance(db), criteria=tp.criteria_of(brief), flag_statuses=tp.FLAG_STATUSES, open_flags=tp.open_flag_count(smap, brief),
        ready=tour_design.current_version(db, plan), ready_missing=tour_design.missing_for_ready(smap),
        ready_changed=tour_design.changed_since(db, plan, smap, brief),
        journey=tp.journey_grid(smap["route"]["data"], None) if key in ("route", "checkpoints") and smap["route"]["data"] else None,
        budget=tp.lodging_budget(brief, smap["lodging"]["data"]) if key == "lodging" and smap["lodging"]["data"] else None)


def _brief_from_form(form, brief):
    b = dict(brief)
    for k in ("name", "standard", "start_city", "start_date", "destination", "route_note", "vehicle_type",
              "currency", "interests", "notes"):
        b[k] = (form.get(k) or "").strip()
    b["return_mode"] = form.get("return_mode") if form.get("return_mode") in tp.RETURN_MODES else "overland"
    for k, lo, hi, dflt in (("tour_days", 1, 90, 10), ("weather_days", 0, 10, 0), ("max_drive_hours", 2, 14, 8),
                            ("max_guests_per_vehicle", 1, 60, 12), ("guides_per_vehicle", 0, 5, 1),
                            ("drivers_per_vehicle", 0, 3, 1)):
        b[k] = max(lo, min(hi, _int(form.get(k), dflt)))
    b["fly_home"] = bool(form.get("fly_home"))
    b["drivers_included"] = bool(form.get("drivers_included"))
    parties = []
    labels = form.getlist("p_label")
    for i, label in enumerate(labels):
        row = {"label": label.strip(), "origin_city": (form.getlist("p_city")[i] or "").strip(),
               "origin_country": (form.getlist("p_country")[i] or "").strip(),
               "guests": max(0, _int(form.getlist("p_guests")[i])), "doubles": max(0, _int(form.getlist("p_doubles")[i])),
               "singles": max(0, _int(form.getlist("p_singles")[i]))}
        if row["guests"] or row["label"] or row["origin_city"]:
            parties.append(row)
    b["parties"] = parties
    return tp.normalise_transport(b)


@tour_planner_bp.route("/<int:plan_id>/brief", methods=["POST"])
@login_required
def save_brief(plan_id):
    _tenant()
    db = get_db()
    plan = _plan(db, plan_id)
    brief = _brief_from_form(request.form, tp.brief_of(plan))
    tp.save_brief(db, plan_id, brief)
    if request.form.get("action") == "approve":
        problems = tp.brief_problems(brief)
        if problems:
            for p in problems:
                flash(p, "error")
            return redirect(url_for("tour_planner.workspace", plan_id=plan_id, stage="brief"))
        tp.approve(db, plan_id, "brief", g.user_id)
        log_action("Update", "tour_plans", plan_id, "Approved the Tour Brief")
        flash("Tour Brief approved. Next: the route.", "success")
        return redirect(url_for("tour_planner.workspace", plan_id=plan_id, stage="route"))
    flash("Brief saved.", "success")
    return redirect(url_for("tour_planner.workspace", plan_id=plan_id, stage="brief"))


@tour_planner_bp.route("/<int:plan_id>/brief/answers", methods=["POST"])
@login_required
def answer_questions(plan_id):
    """The planner answers the agent's questions about the request; the
    agent reads the request again with the answers."""
    _tenant()
    db = get_db()
    _plan(db, plan_id)
    if request.form.get("action") == "dismiss":
        tp.dismiss_questions(db, plan_id)
        flash("Questions cleared. Check the brief below and approve it when it's right.", "success")
        return redirect(url_for("tour_planner.workspace", plan_id=plan_id, stage="brief"))
    pairs = []
    for i, q in enumerate(request.form.getlist("q")):
        a = (request.form.get(f"a_{i}") or "").strip()
        if a:
            pairs.append((q, a))
    extra = (request.form.get("extra") or "").strip()
    if extra:
        pairs.append((tp.EXTRA_QUESTION, extra))
    if not pairs:
        flash("Type an answer, or something you want the agent to consider, first.", "error")
        return redirect(url_for("tour_planner.workspace", plan_id=plan_id, stage="brief"))
    tp.add_answers(db, plan_id, pairs)
    if extra:
        tp.add_note(db, plan_id, extra)
    try:
        tp.start_run(db, g.tenant_id, g.user_id, plan_id, "brief")
    except tp.PlannerError as e:
        flash(f"Your answers were saved with the request, but the agent couldn't read them yet: {e}", "error")
    else:
        log_action("TourPlanner", "tour_plans", plan_id, f"Answered {len(pairs)} Tour Planner question(s)")
    return redirect(url_for("tour_planner.workspace", plan_id=plan_id, stage="brief"))


@tour_planner_bp.route("/<int:plan_id>/stage/<key>/run", methods=["POST"])
@login_required
def run_stage(plan_id, key):
    _tenant()
    db = get_db()
    plan = _plan(db, plan_id)
    if key not in tp.STAGE:
        abort(404)
    smap = tp.stages(db, plan_id)
    if key != "brief" and not tp.previous_approved(smap, key):
        flash("Approve the earlier stages first.", "error")
        return redirect(url_for("tour_planner.workspace", plan_id=plan_id, stage=key))
    if key == "brief" and not (plan["request_text"] or request.form.get("instructions")):
        flash("There's no request to read; fill in the brief instead.", "error")
        return redirect(url_for("tour_planner.workspace", plan_id=plan_id, stage=key))
    if key == "brief" and request.form.get("instructions"):
        db.execute("UPDATE tour_plans SET request_text = ? WHERE plan_id = ?", (request.form["instructions"].strip(), plan_id))
        db.commit()
    try:
        tp.start_run(db, g.tenant_id, g.user_id, plan_id, key,
                     instructions=None if key == "brief" else request.form.get("instructions"))
    except tp.PlannerError as e:
        flash(str(e), "error")
    else:
        log_action("TourPlanner", "tour_plans", plan_id, f"Ran the Tour Planner: {tp.STAGE[key]['label']}")
    return redirect(url_for("tour_planner.workspace", plan_id=plan_id, stage=key))


@tour_planner_bp.route("/<int:plan_id>/stage/<key>/edit", methods=["POST"])
@login_required
def edit_stage(plan_id, key):
    """Small edits to a stage's result: which POIs to include (route),
    nights per checkpoint (checkpoints)."""
    _tenant()
    db = get_db()
    _plan(db, plan_id)
    smap = tp.stages(db, plan_id)
    data = smap.get(key, {}).get("data")
    if data is None:
        abort(404)
    if key == "route":
        keep = set(request.form.getlist("poi"))
        before = [bool(p.get("include")) for p in data.get("pois") or []]
        for i, p in enumerate(data.get("pois") or []):
            p["include"] = str(i) in keep
        if before == [p["include"] for p in data.get("pois") or []]:
            flash("No changes to the points of interest.", "info")
            back = request.form.get("back") if request.form.get("back") in tp.STAGE_KEYS else key
            return redirect(url_for("tour_planner.workspace", plan_id=plan_id, stage=back))
    elif key == "checkpoints":
        for i, cp in enumerate(data.get("checkpoints") or []):
            v = request.form.get(f"nights_{i}")
            if v is not None:
                cp["nights"] = max(0, _int(v, cp.get("nights") or 0))
    elif key == "lodging":
        for i, cp in enumerate(data.get("checkpoints") or []):
            pick = request.form.get(f"pick_{i}")
            for j, h in enumerate(cp.get("hotels") or []):
                if pick is not None:
                    h["recommended"] = str(j) == pick
                for f in ("double_usd", "single_usd"):
                    v = (request.form.get(f"{f}_{i}_{j}") or "").replace(",", "").strip()
                    if v == "":
                        continue
                    try:
                        new = round(float(v), 2)
                    except ValueError:
                        continue
                    if new != h.get(f):
                        h[f] = new
                        h["price_basis"] = "entered by the planner"
    elif key == "days":
        for i, d in enumerate(data.get("days") or []):
            for f in ("title", "description", "notes"):
                if f"{f}_{i}" in request.form:
                    d[f] = request.form[f"{f}_{i}"].strip()
    tp.set_result(db, plan_id, key, data)
    db.commit()
    flash("Saved. Stages after this one will be redone.", "success")
    return redirect(url_for("tour_planner.workspace", plan_id=plan_id, stage=key))


@tour_planner_bp.route("/<int:plan_id>/stage/<key>/flags", methods=["POST"])
@login_required
def answer_flags(plan_id, key):
    """The planner's answers to a stage's flags; optionally re-run the
    stage with them."""
    _tenant()
    db = get_db()
    _plan(db, plan_id)
    smap = tp.stages(db, plan_id)
    if key not in smap or not smap[key]["data"]:
        abort(404)
    flags = smap[key]["data"].get("flags") or []
    responses = {}
    for i, f in enumerate(flags):
        status = request.form.get(f"fs_{i}") or "open"
        note = (request.form.get(f"fn_{i}") or "").strip()[:1000]
        responses[str(f)] = {"status": status if status in tp.FLAG_STATUS else "open", "note": note}
    tp.save_flag_responses(db, plan_id, key, responses)
    log_action("Update", "tour_plans", plan_id, f"Answered the Tour Planner flags: {tp.STAGE[key]['label']}")
    if request.form.get("action") == "revise":
        text = tp.revise_instructions(key, responses)
        if not text:
            flash("Answer at least one flag first.", "error")
        elif not tp.previous_approved(smap, key):
            flash("Approve the earlier stages first.", "error")
        else:
            try:
                tp.start_run(db, g.tenant_id, g.user_id, plan_id, key, instructions=text)
            except tp.PlannerError as e:
                flash(str(e), "error")
            else:
                flash("Your answers are saved and the agent is revising this stage with them.", "success")
                return redirect(url_for("tour_planner.workspace", plan_id=plan_id, stage=key))
    else:
        flash("Your answers to the flags are saved. Later stages' agents will read them.", "success")
    return redirect(url_for("tour_planner.workspace", plan_id=plan_id, stage=key))


@tour_planner_bp.route("/<int:plan_id>/ready", methods=["POST"])
@login_required
def mark_ready(plan_id):
    """Freeze the design as a Ready for package version (tour_design.py)."""
    import tour_design
    _tenant()
    db = get_db()
    _plan(db, plan_id)
    if request.form.get("action") == "withdraw":
        tour_design.withdraw(db, g.tenant_id, plan_id)
        log_action("Update", "tour_plans", plan_id, "Withdrew a Tour Design from Ready for package")
        flash("Withdrawn: Package Management no longer offers this design.", "success")
    else:
        try:
            n = tour_design.mark_ready(db, g.tenant_id, plan_id, g.user_id, request.form.get("note"))
        except tour_design.DesignError as e:
            flash(str(e), "error")
        else:
            log_action("Update", "tour_plans", plan_id, f"Marked Tour Design ready for package (version {n})")
            flash(f"Version {n} is Ready for package: Package Management → New Package → Add from Tour Design.", "success")
    return redirect(url_for("tour_planner.workspace", plan_id=plan_id, stage=request.form.get("stage") or None))


@tour_planner_bp.route("/<int:plan_id>/print")
@login_required
def print_flags(plan_id):
    """A printable list of the flags that need attention: one stage
    (?stage=route) or the whole design; ?answered=1 includes the answered
    ones too."""
    _tenant()
    db = get_db()
    plan = _plan(db, plan_id)
    smap = tp.stages(db, plan_id)
    brief = tp.brief_of(plan)
    key = request.args.get("stage")
    keys = [key] if key in smap else [k for k in tp.STAGE_KEYS if smap[k]["built"]]
    show_all = bool(request.args.get("answered"))
    sections = tp.flag_report(smap, brief, keys, show_all)
    return render_template("tour_planner/print.html", plan=plan, brief=brief, fig=tp.group_figures(brief),
                           sections=sections, one_stage=key in smap, show_all=show_all,
                           statuses=tp.FLAG_STATUS, stage_key=key if key in smap else None,
                           printed_at=db.execute("SELECT datetime('now')").fetchone()[0])


@tour_planner_bp.route("/<int:plan_id>/brief/print")
@login_required
def print_brief(plan_id):
    """The Tour Brief on one printable page: the tour, the travelling
    parties with their totals, and transport."""
    _tenant()
    db = get_db()
    plan = _plan(db, plan_id)
    brief = tp.brief_of(plan)
    smap = tp.stages(db, plan_id)
    return render_template("tour_planner/print_brief.html", plan=plan, brief=brief, fig=tp.group_figures(brief),
                           st=smap["brief"], return_modes=tp.RETURN_MODES, guide_seating=tp.GUIDE_SEATING,
                           problems=tp.brief_problems(brief),
                           printed_at=db.execute("SELECT datetime('now')").fetchone()[0])


@tour_planner_bp.route("/<int:plan_id>/criteria", methods=["POST"])
@login_required
def save_criteria(plan_id):
    _tenant()
    db = get_db()
    _plan(db, plan_id)
    f = request.form

    def num(name):
        try:
            v = float(f.get(name) or "")
            return int(v) if v.is_integer() else v
        except ValueError:
            return None
    criteria = {"max_km": num("max_km"), "min_km": num("min_km"),
                "hotel": (f.get("hotel") or "").strip()[:1000], "food": (f.get("food") or "").strip()[:1000],
                "pois": (f.get("pois") or "").strip()[:1000], "other": (f.get("other") or "").strip()[:1000],
                "prefer_flagged": bool(f.get("prefer_flagged"))}
    tp.save_criteria(db, plan_id, criteria)
    log_action("Update", "tour_plans", plan_id, "Set Tour Planner checkpoint criteria")
    flash("Checkpoint criteria saved. Run the agent to choose checkpoints with them.", "success")
    back = f.get("back") if f.get("back") in ("route", "checkpoints") else "checkpoints"
    return redirect(url_for("tour_planner.workspace", plan_id=plan_id, stage=back))


@tour_planner_bp.route("/<int:plan_id>/stage/<key>/approve", methods=["POST"])
@login_required
def approve_stage(plan_id, key):
    _tenant()
    db = get_db()
    _plan(db, plan_id)
    smap = tp.stages(db, plan_id)
    if key not in smap or smap[key]["data"] is None or not tp.previous_approved(smap, key):
        flash("Nothing to approve yet.", "error")
        return redirect(url_for("tour_planner.workspace", plan_id=plan_id, stage=key))
    proposed = 0
    if key == "checkpoints":
        plan = _plan(db, plan_id)
        data = smap[key]["data"]
        proposed = tp.propose_checkpoint_flags(db, plan["name"], data)
        if proposed:  # remember what was sent, without redoing later stages
            db.execute("UPDATE tour_plan_stages SET result = ? WHERE plan_id = ? AND stage_key = 'checkpoints'",
                       (json.dumps(data, ensure_ascii=False), plan_id))
    tp.approve(db, plan_id, key, g.user_id)
    if proposed:
        flash(f"{proposed} new checkpoint cit{'y' if proposed == 1 else 'ies'} suggested to TMS for flagging "
              "as overnight checkpoints.", "info")
    log_action("Update", "tour_plans", plan_id, f"Approved Tour Planner stage: {tp.STAGE[key]['label']}")
    nxt = next((k for k in tp.STAGE_KEYS[tp.STAGE_KEYS.index(key) + 1:] if tp.STAGE[k]["phase"] in tp.BUILT_PHASES), None)
    flash(f"{tp.STAGE[key]['label']} approved." + (f" Next: {tp.STAGE[nxt]['label'].lower()}." if nxt else ""), "success")
    return redirect(url_for("tour_planner.workspace", plan_id=plan_id, stage=nxt or key))


@tour_planner_bp.route("/<int:plan_id>/run/<int:run_id>/status.json")
@login_required
def run_status(plan_id, run_id):
    _tenant()
    db = get_db()
    _plan(db, plan_id)
    tp.mark_stale(db, g.tenant_id)
    st = db.execute("SELECT 1 FROM tour_plan_runs WHERE run_id = ? AND plan_id = ?", (run_id, plan_id)).fetchone()
    if st is None:
        abort(404)
    return jsonify(agent_runs.status_json(db, "tour_plan_runs", run_id))


@tour_planner_bp.route("/<int:plan_id>/run/<int:run_id>/stop", methods=["POST"])
@login_required
def run_stop(plan_id, run_id):
    _tenant()
    db = get_db()
    _plan(db, plan_id)
    r = db.execute("SELECT stage_key FROM tour_plan_runs WHERE run_id = ? AND plan_id = ?", (run_id, plan_id)).fetchone()
    if r is None:
        abort(404)
    if agent_runs.stop(db, "tour_plan_runs", run_id, g.get("display_name") or g.get("username")):
        flash("Stopped.", "success")
    return redirect(url_for("tour_planner.workspace", plan_id=plan_id, stage=r["stage_key"]))


@tour_planner_bp.route("/<int:plan_id>/archive", methods=["POST"])
@login_required
def archive(plan_id):
    _tenant()
    db = get_db()
    plan = _plan(db, plan_id)
    db.execute("UPDATE tour_plans SET status = 'archived', updated_at = datetime('now') WHERE plan_id = ?", (plan_id,))
    db.commit()
    log_action("Delete", "tour_plans", plan_id, f"Archived tour plan {plan['name']}")
    flash(f"'{plan['name']}' archived.", "success")
    return redirect(url_for("tour_planner.index"))
