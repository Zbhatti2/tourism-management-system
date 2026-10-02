"""
Module — Data Import (SystemAdmin): the entity-based import wizard for the
platform tables. Logic lives in platform_import.py; this is the screens.

1. Pick the entity and mode (Normal, or Notes only), download the blank
   template if needed, upload the filled .xlsx / .csv.
2. Preview: every row labelled New / Update / Possible duplicate / Problem
   with the reason. Decide each possible duplicate (merge / keep as new /
   skip). Re-check with "Add missing provinces and cities" on or off.
   Nothing has been saved yet.
3. Import: writes the rows; the run page then shows what happened to each.
"""
import json

from flask import Blueprint, Response, abort, flash, g, redirect, render_template, request, url_for

import catalog_sync
from auth.decorators import system_admin_required
from db import get_db, log_action
import platform_import as pi

platform_import_bp = Blueprint("platform_import", __name__)

MAX_BYTES = 15 * 1024 * 1024
STATUS_LABELS = {"new": "New", "update": "Update", "duplicate": "Possible duplicate", "problem": "Problem"}
TABLES = {"cities": "cities", "distances": "city_distances", "hubs": "transport_hubs", "pois": "platform_pois",
          "accommodation": "platform_accommodation", "restaurants": "platform_restaurants", "embassies": "embassies"}


def _entity(key):
    if key not in pi.ENTITIES:
        abort(404)
    return pi.ENTITIES[key]


@platform_import_bp.route("/")
@system_admin_required
def index():
    db = get_db()
    cards = []
    for key in pi.ORDERED:
        spec = pi.ENTITIES[key]
        count = db.execute(f"SELECT COUNT(*) FROM {TABLES[key]}").fetchone()[0]
        cards.append({"key": key, "label": spec["label"], "order": spec["order"], "count": count,
                      "purpose": spec["purpose"]})
    runs = db.execute("""SELECT r.*, u.username,
                                (SELECT COUNT(*) FROM platform_import_rows x WHERE x.run_id = r.run_id) AS row_count
                         FROM platform_import_runs r LEFT JOIN users u ON u.user_id = r.created_by
                         ORDER BY r.run_id DESC LIMIT 25""").fetchall()
    selected = request.args.get("entity") if request.args.get("entity") in pi.ENTITIES else None
    return render_template("platform_import/index.html", cards=cards, runs=runs, selected=selected,
                           entities=pi.ENTITIES, ordered=pi.ORDERED)


@platform_import_bp.route("/template/<entity>.xlsx")
@system_admin_required
def template(entity):
    spec = _entity(entity)
    data = pi.template_workbook(get_db(), entity)
    name = f"TMS_Import_{spec['order']}_{spec['label'].replace(' & ', '_and_').replace(' ', '_')}.xlsx"
    return Response(data, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@platform_import_bp.route("/upload", methods=["POST"])
@system_admin_required
def upload():
    db = get_db()
    entity = request.form.get("entity")
    _entity(entity)
    mode = "notes" if request.form.get("mode") == "notes" else "normal"
    if mode == "notes" and entity in ("cities", "distances"):
        mode = "normal"
    options = {"create_places": bool(request.form.get("create_places"))}
    f = request.files.get("file")
    if not f or not f.filename:
        flash("Choose a file to upload.", "error")
        return redirect(url_for("platform_import.index", entity=entity))
    data = f.read()
    if len(data) > MAX_BYTES:
        flash("That file is larger than 15 MB. Split it into smaller files.", "error")
        return redirect(url_for("platform_import.index", entity=entity))
    try:
        rows = pi.parse_file(entity, f.filename, data, mode)
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("platform_import.index", entity=entity))
    except Exception:
        flash("That file couldn't be read. Save it as .xlsx or .csv and try again.", "error")
        return redirect(url_for("platform_import.index", entity=entity))
    if not rows:
        flash("No data rows found under the header row.", "error")
        return redirect(url_for("platform_import.index", entity=entity))
    staged = pi.evaluate(db, entity, mode, rows, options)
    run_id = pi.save_run(db, entity, mode, f.filename, options, staged, g.user_id)
    log_action("ImportPreview", TABLES[entity], run_id, f"Previewed {len(rows)} row(s) from '{f.filename}' ({entity}, {mode})")
    return redirect(url_for("platform_import.run", run_id=run_id))


def _get_run(db, run_id):
    run = db.execute("SELECT * FROM platform_import_runs WHERE run_id = ?", (run_id,)).fetchone()
    if run is None:
        abort(404)
    return run


def _record_url(entity, rid):
    if not rid:
        return None
    if entity in ("pois", "accommodation", "restaurants", "embassies"):
        return url_for("platform_catalog.view_record", entity=entity, rid=rid)
    if entity == "hubs":
        return url_for("transport_hubs.view_hub", hub_id=rid)
    if entity == "distances":
        return url_for("geography_admin.edit_distance", distance_id=rid)
    if entity == "cities":
        return url_for("geography_admin.edit_entry", table_key="cities", entry_id=rid)
    return None


@platform_import_bp.route("/run/<int:run_id>")
@system_admin_required
def run(run_id):
    db = get_db()
    run = _get_run(db, run_id)
    spec = pi.ENTITIES[run["entity"]]
    rows = pi.load_rows(db, run_id)
    show = request.args.get("show", "all")
    counts = {k: sum(1 for r in rows if r["status"] == k) for k in STATUS_LABELS}
    result_counts = json.loads(run["summary"]) if run["summary"] else None
    visible = [r for r in rows if show == "all" or r["status"] == show]
    for r in rows:
        raw = r["data"]["raw"]
        if run["entity"] == "distances":
            r["name"] = f"{raw.get('From City') or '?'} – {raw.get('To City') or '?'}"
            r["city"] = ""
        else:
            r["name"] = raw.get(spec["name_col"]) or "—"
            r["city"] = raw.get("Host City") or raw.get("City / Locality") or raw.get("City") or ""
        r["match_url"] = _record_url(run["entity"], r["match_id"])
    return render_template("platform_import/run.html", run=run, spec=spec, rows=visible, all_count=len(rows),
                           counts=counts, show=show, labels=STATUS_LABELS, options=json.loads(run["options"] or "{}"),
                           result_counts=result_counts, record_url=_record_url)


def _save_decisions(db, run_id, rows, form):
    for r in rows:
        if r["status"] == "duplicate":
            d = form.get(f"decision_{r['row_num']}")
            if d in ("merge", "new", "skip"):
                r["decision"] = d
                db.execute("UPDATE platform_import_rows SET decision = ? WHERE run_id = ? AND row_num = ?",
                           (d, run_id, r["row_num"]))
    db.commit()


@platform_import_bp.route("/run/<int:run_id>/recheck", methods=["POST"])
@system_admin_required
def recheck(run_id):
    """Re-evaluate the stored rows, e.g. after ticking 'Add missing provinces
    and cities', or after fixing geography in another tab."""
    db = get_db()
    run = _get_run(db, run_id)
    if run["status"] != "previewed":
        abort(400)
    options = {"create_places": bool(request.form.get("create_places"))}
    old = {r["row_num"]: r for r in pi.load_rows(db, run_id)}
    raw_rows = [(n, r["data"]["raw"]) for n, r in sorted(old.items())]
    staged = pi.evaluate(db, run["entity"], run["mode"], raw_rows, options)
    for s in staged:  # keep decisions already made on rows that are still duplicates of the same thing
        prev = old.get(s["row_num"])
        if prev and prev["status"] == s["status"] == "duplicate" and prev["match_id"] == s["match_id"] and prev["decision"]:
            s["decision"] = prev["decision"]
    pi.store_rows(db, run_id, staged)
    db.execute("UPDATE platform_import_runs SET options = ? WHERE run_id = ?", (json.dumps(options), run_id))
    db.commit()
    flash("Rows checked again.", "success")
    return redirect(url_for("platform_import.run", run_id=run_id))


@platform_import_bp.route("/run/<int:run_id>/import", methods=["POST"])
@system_admin_required
def do_import(run_id):
    db = get_db()
    run = _get_run(db, run_id)
    if run["status"] != "previewed":
        flash("This file has already been imported or discarded.", "error")
        return redirect(url_for("platform_import.run", run_id=run_id))
    rows = pi.load_rows(db, run_id)
    _save_decisions(db, run_id, rows, request.form)
    counts = pi.apply(db, run["entity"], run["mode"], rows, run["file_name"])
    for r in rows:
        db.execute("UPDATE platform_import_rows SET result = ? WHERE run_id = ? AND row_num = ?",
                   (r.get("result"), run_id, r["row_num"]))
    db.execute("UPDATE platform_import_runs SET status = 'imported', imported_at = datetime('now'), summary = ? "
               "WHERE run_id = ?", (json.dumps(counts), run_id))
    db.commit()
    log_action("Import", TABLES[run["entity"]], run_id,
               f"Imported '{run['file_name']}' ({run['entity']}, {run['mode']}): " +
               ", ".join(f"{v} {k}" for k, v in counts.items() if v))
    synced = catalog_sync.push(db, run["entity"]) if run["mode"] == "normal" else {}
    flash("Import finished: " + ", ".join(f"{v} {k}" for k, v in counts.items() if v) + "." +
          (f" Synced to {len(synced)} tenant{'s' if len(synced) != 1 else ''}." if synced else ""), "success")
    return redirect(url_for("platform_import.run", run_id=run_id))


@platform_import_bp.route("/run/<int:run_id>/discard", methods=["POST"])
@system_admin_required
def discard(run_id):
    db = get_db()
    run = _get_run(db, run_id)
    if run["status"] == "previewed":
        db.execute("UPDATE platform_import_runs SET status = 'discarded' WHERE run_id = ?", (run_id,))
        db.commit()
        flash("Preview discarded. Nothing was imported.", "success")
    return redirect(url_for("platform_import.index", entity=run["entity"]))
