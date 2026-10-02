"""
Images Catalog screens (logic in image_catalog.py) -- tenant level.

* /images/import                  Data Exchange: a Zip (or image files) for many
                                  Hotels / Resorts / Restaurants at once, matched
                                  by file name.
* /images/supplier/<id>/add       Add images from one supplier's own page.
* /images/batch/<id>              Curation: supplier, Title, Description, Date,
                                  Sort order, keep or not -- then Add to albums.
* /images/supplier/<id>/album     The supplier's Images Catalog: slider and list,
                                  search, sort, edit, reorder, delete.
* /images/collect, /images/supplier/<id>/collect, /images/agent/<run>
                                  The AI Image Collector (image_collector.py):
                                  start a run, watch it, then curate what it found.
"""
import json
from datetime import date
from io import BytesIO

from flask import Blueprint, abort, flash, g, redirect, render_template, request, send_file, url_for

import ai_usage
import image_catalog as ic
import image_collector as collector
from auth.decorators import login_required
from db import get_db, log_action

images_bp = Blueprint("images", __name__)

STATE_LABELS = {"ready": "Ready", "check": "Check match", "unmatched": "No match", "duplicate": "Duplicate",
                "added": "Added", "rejected": "Left out"}
STATE_BADGES = {"ready": "bg-success-subtle text-success", "check": "bg-warning-subtle text-warning-emphasis",
                "unmatched": "bg-danger-subtle text-danger", "duplicate": "bg-secondary-subtle text-dark",
                "added": "bg-primary-subtle text-primary", "rejected": "bg-light text-muted border"}


def _tenant():
    if g.get("tenant_id") is None:
        abort(404)
    return g.tenant_id


def _supplier(db, supplier_id):
    s = db.execute("""SELECT s.*, t.label AS type_label, t.code AS type_code FROM suppliers s
                      LEFT JOIN supplier_types t ON t.supplier_type_id = s.supplier_type_id
                      WHERE s.supplier_id = ? AND s.tenant_id = ? AND s.is_deleted = 0""",
                   (supplier_id, g.tenant_id)).fetchone()
    if s is None:
        abort(404)
    return s


def _batch(db, batch_id):
    b = db.execute("SELECT * FROM image_import_batches WHERE batch_id = ? AND tenant_id = ?",
                   (batch_id, g.tenant_id)).fetchone()
    if b is None:
        abort(404)
    return b


def _valid_date(text):
    text = (text or "").strip()
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def _start_batch(db, files, supplier_id=None):
    source = request.form.get("source") if request.form.get("source") in ic.SOURCES else "Individual"
    try:
        found = ic.read_upload(files)
    except ValueError as e:
        flash(str(e), "error")
        return None
    names = ", ".join(f.filename for f in files if f and f.filename)
    batch_id = ic.create_batch(db, g.tenant_id, found, source=source,
                               contributor=(request.form.get("contributor") or "").strip() or None,
                               default_date=_valid_date(request.form.get("default_date")), supplier_id=supplier_id,
                               user_id=g.user_id, upload_name=names[:200])
    log_action("ImageUpload", "image_import_batches", batch_id, f"Uploaded {len(found)} image(s) for curation ({names[:120]})")
    return batch_id


# ---- uploads -------------------------------------------------------------------------------------

@images_bp.route("/import", methods=["GET", "POST"])
@login_required
def import_images():
    _tenant()
    db = get_db()
    if request.method == "POST":
        batch_id = _start_batch(db, request.files.getlist("files"))
        if batch_id:
            return redirect(url_for("images.curate", batch_id=batch_id))
        return redirect(url_for("images.import_images"))
    suppliers = ic.album_suppliers(db, g.tenant_id)
    collector.mark_stale(db, g.tenant_id)
    runs = db.execute("SELECT * FROM image_agent_runs WHERE tenant_id = ? ORDER BY run_id DESC LIMIT 10",
                      (g.tenant_id,)).fetchall()
    return render_template("images/import.html", batches=ic.open_batches(db, g.tenant_id), suppliers=suppliers,
                           today=date.today().isoformat(), sources=ic.SOURCES, supplier=None, runs=runs,
                           cities=sorted({s["city"] for s in suppliers if s["city"]}),
                           types=sorted({s["type_label"] for s in suppliers}),
                           allowance=_allowance(db))


@images_bp.route("/supplier/<int:supplier_id>/add", methods=["GET", "POST"])
@login_required
def add_to_supplier(supplier_id):
    _tenant()
    db = get_db()
    supplier = _supplier(db, supplier_id)
    if request.method == "POST":
        batch_id = _start_batch(db, request.files.getlist("files"), supplier_id=supplier_id)
        if batch_id:
            return redirect(url_for("images.curate", batch_id=batch_id))
        return redirect(url_for("images.add_to_supplier", supplier_id=supplier_id))
    return render_template("images/import.html", supplier=supplier, today=date.today().isoformat(),
                           sources=ic.SOURCES, batches=[], suppliers=[])


@images_bp.route("/names.csv")
@login_required
def names_csv():
    """The exact Hotel / Resort / Restaurant names to send to whoever is
    collecting photos, so their file names match."""
    import csv
    import io
    _tenant()
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Name (start each file name with this, then a hyphen)", "Type", "City", "Example file name"])
    for s in ic.album_suppliers(get_db(), g.tenant_id):
        w.writerow([s["supplier_name"], s["type_label"], s["city"] or "", f"{s['supplier_name']}-Lobby.jpg"])
    data = out.getvalue().encode("utf-8-sig")
    return send_file(BytesIO(data), mimetype="text/csv", as_attachment=True, download_name="Hotel_and_Restaurant_names.csv")


# ---- AI Image Collector ---------------------------------------------------------------------------

def _allowance(db):
    limit = ai_usage.monthly_limit(db, g.tenant_id)
    return {"limit": limit, "spent": ai_usage.month_spend(db, g.tenant_id)}


def _start_collector(db, supplier_ids, per_supplier, label, back):
    try:
        run_id = collector.start_run(db, g.tenant_id, g.user_id, supplier_ids, per_supplier=per_supplier, scope_label=label)
    except collector.CollectorError as e:
        flash(str(e), "error")
        return redirect(back)
    log_action("ImageCollector", "image_agent_runs", run_id, f"Started the AI Image Collector for {label}")
    return redirect(url_for("images.agent_run", run_id=run_id))


@images_bp.route("/supplier/<int:supplier_id>/collect", methods=["POST"])
@login_required
def collect_for_supplier(supplier_id):
    _tenant()
    db = get_db()
    supplier = _supplier(db, supplier_id)
    return _start_collector(db, [supplier_id], request.form.get("per_supplier", 8), supplier["supplier_name"],
                            url_for("images.album", supplier_id=supplier_id))


@images_bp.route("/collect", methods=["POST"])
@login_required
def collect_many():
    """Several suppliers at once: by type and city, optionally only those
    with no photos yet."""
    _tenant()
    db = get_db()
    type_label = request.form.get("type") or ""
    city = request.form.get("city") or ""
    only_empty = bool(request.form.get("only_empty"))
    try:
        limit = max(1, min(int(request.form.get("max_suppliers") or 10), 25))
    except ValueError:
        limit = 10
    type_id = ic.image_type_id(db, g.tenant_id)
    chosen = []
    for s in ic.album_suppliers(db, g.tenant_id):
        if (type_label and s["type_label"] != type_label) or (city and s["city"] != city):
            continue
        if only_empty and db.execute("SELECT 1 FROM supplier_documents WHERE supplier_id = ? AND tenant_id = ? "
                                     "AND is_deleted = 0 AND document_type_id = ? LIMIT 1",
                                     (s["supplier_id"], g.tenant_id, type_id)).fetchone():
            continue
        chosen.append(s["supplier_id"])
    if not chosen:
        flash("No Hotels, Resorts or Restaurants match that choice.", "error")
        return redirect(url_for("images.import_images"))
    label = f"{type_label + 's' if type_label else 'Hotels, Resorts and Restaurants'}{' in ' + city if city else ''}"
    label += f" ({min(len(chosen), limit)}{' of ' + str(len(chosen)) if len(chosen) > limit else ''})"
    return _start_collector(db, chosen[:limit], request.form.get("per_supplier", 6), label, url_for("images.import_images"))


@images_bp.route("/agent/<int:run_id>")
@login_required
def agent_run(run_id):
    _tenant()
    db = get_db()
    collector.mark_stale(db, g.tenant_id)
    run = db.execute("SELECT * FROM image_agent_runs WHERE run_id = ? AND tenant_id = ?", (run_id, g.tenant_id)).fetchone()
    if run is None:
        abort(404)
    cost, calls, searches = collector.run_cost(db, run_id)
    ids = json.loads(run["supplier_ids"])
    supplier = _supplier(db, ids[0]) if len(ids) == 1 else None
    return render_template("images/agent_run.html", run=run, cost=cost, calls=calls, searches=searches or 0,
                           supplier=supplier)


# ---- curation -------------------------------------------------------------------------------------

@images_bp.route("/batch/<int:batch_id>")
@login_required
def curate(batch_id):
    _tenant()
    db = get_db()
    batch = _batch(db, batch_id)
    items = [dict(r, state=ic.item_state(r)) for r in ic.batch_items(db, g.tenant_id, batch_id)]
    counts = {k: sum(1 for i in items if i["state"] == k) for k in STATE_LABELS}
    show = request.args.get("show", "all")
    visible = [i for i in items if show == "all" or i["state"] == show]
    suppliers = ic.album_suppliers(db, g.tenant_id)
    if batch["supplier_id"] and not any(s["supplier_id"] == batch["supplier_id"] for s in suppliers):
        s = _supplier(db, batch["supplier_id"])
        suppliers.insert(0, {"supplier_id": s["supplier_id"], "supplier_name": s["supplier_name"],
                             "type_label": s["type_label"], "city": None})
    for_supplier = _supplier(db, batch["supplier_id"]) if batch["supplier_id"] else None
    return render_template("images/curate.html", batch=batch, items=visible, all_count=len(items), counts=counts,
                           show=show, labels=STATE_LABELS, badges=STATE_BADGES, suppliers=suppliers,
                           for_supplier=for_supplier, max_desc=ic.DESCRIPTION_MAX)


@images_bp.route("/batch/<int:batch_id>/save", methods=["POST"])
@login_required
def save_batch(batch_id):
    _tenant()
    db = get_db()
    batch = _batch(db, batch_id)
    action = request.form.get("action", "save")
    back = url_for("images.curate", batch_id=batch_id, show=request.form.get("show") or "all")
    if batch["status"] != "open":
        flash("This upload is already finished.", "error")
        return redirect(back)
    if action == "discard":
        ic.discard(db, g.tenant_id, batch_id)
        log_action("ImageDiscard", "image_import_batches", batch_id, "Discarded image upload")
        flash("Upload discarded. Nothing was added to any album.", "success")
        return redirect(url_for("images.add_to_supplier", supplier_id=batch["supplier_id"]) if batch["supplier_id"]
                        else url_for("images.import_images"))
    allowed = {s["supplier_id"] for s in ic.album_suppliers(db, g.tenant_id)}
    if batch["supplier_id"]:
        allowed.add(batch["supplier_id"])
    ic.save_edits(db, g.tenant_id, batch_id, request.form, allowed)
    if action == "add":
        added, problems = ic.approve(db, g.tenant_id, batch_id)
        for p in problems[:10]:
            flash(p, "error")
        if added:
            log_action("ImageCurate", "supplier_documents", batch_id, f"Added {added} curated image(s) to albums")
            flash(f"Added {added} image{'s' if added != 1 else ''} to {'the album' if batch['supplier_id'] else 'the albums'}.", "success")
        elif not problems:
            flash("Nothing ticked to add.", "error")
        if batch["supplier_id"] and not db.execute("SELECT 1 FROM image_import_items WHERE batch_id = ? AND status = 'pending'",
                                                   (batch_id,)).fetchone():
            return redirect(url_for("images.album", supplier_id=batch["supplier_id"]))
    elif action == "reject":
        n = ic.reject_unticked(db, g.tenant_id, batch_id)
        flash(f"Left out {n} unticked image{'s' if n != 1 else ''}.", "success")
    else:
        flash("Saved. Nothing has been added to an album yet.", "success")
    return redirect(back)


def _blob(data, mime, name="image"):
    if not data:
        abort(404)
    resp = send_file(BytesIO(data), mimetype=mime or "application/octet-stream", download_name=name)
    resp.headers["Cache-Control"] = "private, max-age=86400"
    return resp


@images_bp.route("/item/<int:item_id>/<kind>")
@login_required
def item_image(item_id, kind):
    _tenant()
    db = get_db()
    r = db.execute("SELECT thumb_data, file_data, mime_type, file_name FROM image_import_items "
                   "WHERE item_id = ? AND tenant_id = ?", (item_id, g.tenant_id)).fetchone()
    if r is None or kind not in ("thumb", "full"):
        abort(404)
    if kind == "thumb" and r["thumb_data"]:
        return _blob(r["thumb_data"], "image/jpeg")
    return _blob(r["file_data"], r["mime_type"], r["file_name"])


# ---- album ------------------------------------------------------------------------------------------

@images_bp.route("/supplier/<int:supplier_id>/album")
@login_required
def album(supplier_id):
    _tenant()
    db = get_db()
    supplier = _supplier(db, supplier_id)
    q = request.args.get("q", "").strip()
    sort = request.args.get("sort", "album")
    sort = sort if sort in ic.ALBUM_SORTS else "album"
    view = request.args.get("view", "slider")
    date_from, date_to = _valid_date(request.args.get("from")), _valid_date(request.args.get("to"))
    images = ic.album(db, g.tenant_id, supplier_id, q=q, sort=sort, date_from=date_from, date_to=date_to)
    total = len(ic.album(db, g.tenant_id, supplier_id)) if (q or date_from or date_to) else len(images)
    pending = db.execute("""SELECT b.batch_id, COUNT(*) AS n FROM image_import_items i
                            JOIN image_import_batches b ON b.batch_id = i.batch_id
                            WHERE i.tenant_id = ? AND i.status = 'pending' AND i.supplier_id = ? AND b.status = 'open'
                            GROUP BY b.batch_id ORDER BY b.batch_id DESC""", (g.tenant_id, supplier_id)).fetchall()
    last_run = db.execute("SELECT * FROM image_agent_runs WHERE tenant_id = ? AND supplier_ids = ? "
                          "ORDER BY run_id DESC LIMIT 1", (g.tenant_id, f"[{supplier_id}]")).fetchone()
    return render_template("images/album.html", supplier=supplier, images=images, total=total, q=q, sort=sort,
                           view=view, date_from=date_from or "", date_to=date_to or "", sorts=ic.SORT_LABELS,
                           pending=pending, max_desc=ic.DESCRIPTION_MAX, last_run=last_run,
                           has_album=supplier["type_code"] in ic.ALBUM_TYPE_CODES)


@images_bp.route("/supplier/<int:supplier_id>/album/<int:document_id>/<kind>")
@login_required
def album_image(supplier_id, document_id, kind):
    _tenant()
    db = get_db()
    doc = db.execute("SELECT 1 FROM supplier_documents WHERE supplier_document_id = ? AND supplier_id = ? "
                     "AND tenant_id = ? AND is_deleted = 0", (document_id, supplier_id, g.tenant_id)).fetchone()
    if doc is None or kind not in ("thumb", "full"):
        abort(404)
    if kind == "thumb":
        data, mime = ic.document_thumb(db, g.tenant_id, document_id)
        if data:
            return _blob(data, mime)
    return redirect(url_for("suppliers.image_thumbnail", supplier_id=supplier_id, document_id=document_id))


@images_bp.route("/supplier/<int:supplier_id>/album/save", methods=["POST"])
@login_required
def save_album(supplier_id):
    """List view: save Title / Description / Date / Sort order of every row,
    or delete the ticked ones."""
    _tenant()
    db = get_db()
    supplier = _supplier(db, supplier_id)
    ids = [int(x) for x in request.form.getlist("ids") if x.isdigit()]
    owned = {r[0] for r in db.execute(
        f"SELECT supplier_document_id FROM supplier_documents WHERE supplier_id = ? AND tenant_id = ? AND is_deleted = 0 "
        f"AND supplier_document_id IN ({','.join('?' * len(ids)) or 'NULL'})", [supplier_id, g.tenant_id] + ids)}
    back = url_for("images.album", supplier_id=supplier_id, view="list", q=request.form.get("q") or None,
                   sort=request.form.get("sort") or None)
    if request.form.get("action") == "delete":
        chosen = [int(x) for x in request.form.getlist("selected") if x.isdigit() and int(x) in owned]
        for d in chosen:
            db.execute("UPDATE supplier_documents SET is_deleted = 1, updated_at = datetime('now') "
                       "WHERE supplier_document_id = ? AND tenant_id = ?", (d, g.tenant_id))
        db.commit()
        if chosen:
            log_action("Delete", "supplier_document", supplier_id, f"Deleted {len(chosen)} image(s) from {supplier['supplier_name']}'s album")
            flash(f"Deleted {len(chosen)} image{'s' if len(chosen) != 1 else ''}.", "success")
        else:
            flash("Tick the images to delete first.", "error")
        return redirect(back)
    errors = 0
    for d in owned:
        title = (request.form.get(f"title_{d}") or "").strip()
        if not title:
            errors += 1
            continue
        try:
            sort = int(request.form.get(f"sort_{d}") or 0) or None
        except ValueError:
            sort = None
        db.execute("UPDATE supplier_documents SET document_name = ?, description = ?, image_date = ?, sort_order = ?, "
                   "updated_at = datetime('now') WHERE supplier_document_id = ? AND tenant_id = ?",
                   (title, (request.form.get(f"description_{d}") or "").strip()[:ic.DESCRIPTION_MAX] or None,
                    _valid_date(request.form.get(f"date_{d}")), sort, d, g.tenant_id))
    db.commit()
    log_action("Update", "supplier_document", supplier_id, f"Edited the image album of {supplier['supplier_name']}")
    flash("Album saved." + (f" {errors} row(s) without a Title were left unchanged." if errors else ""),
          "success" if not errors else "error")
    return redirect(back)
