"""
Images Catalog screens (logic in image_catalog.py; what an album can belong
to in image_owners.py). <kind> is one of:

* supplier      a tenant's Hotel / Resort / Restaurant
* poi           a tenant's Point of Interest (own images + images inherited
                from the platform's Master Image Catalog)
* platform_poi  a platform POI's Master Image Catalog (SystemAdmin)

Routes:
* /images/import, /images/import/<kind>
                                  A Zip (or image files) for many owners at once,
                                  matched by file name.
* /images/<kind>/<id>/add         Add images from one owner's own page.
* /images/batch/<id>              Curation: owner, Title, Description, Date, Sort
                                  order, keep or not -- then Add to albums.
* /images/<kind>/<id>/album       The owner's Images Catalog: slider and list,
                                  search, sort, edit, reorder, delete.
* /images/collect, /images/<kind>/collect, /images/<kind>/<id>/collect,
  /images/agent/<run>             The AI Image Collector (image_collector.py):
                                  tenant level for hotels, a TMS Agent for
                                  platform POIs.
* /images/platform-updates        Tenant: changes to the POI Master Image
                                  Catalog waiting for review (poi_image_sync.py).
"""
import functools
import json
from datetime import date
from io import BytesIO

from flask import Blueprint, abort, flash, g, jsonify, redirect, render_template, request, send_file, session, url_for

import agent_runs
import ai_usage
import image_catalog as ic
import image_collector as collector
import catalog_image_sync
import poi_image_sync
from auth.decorators import login_required, system_admin_required
from db import get_db, log_action
from image_owners import KINDS
from platform_lookups import platform_tenant_id

images_bp = Blueprint("images", __name__)

STATE_LABELS = {"ready": "Ready", "check": "Check match", "unmatched": "No match", "duplicate": "Duplicate",
                "added": "Added", "rejected": "Left out"}
STATE_BADGES = {"ready": "bg-success-subtle text-success", "check": "bg-warning-subtle text-warning-emphasis",
                "unmatched": "bg-danger-subtle text-danger", "duplicate": "bg-secondary-subtle text-dark",
                "added": "bg-primary-subtle text-primary", "rejected": "bg-light text-muted border"}


def any_user(view):
    """Tenant users (login_required) or the SystemAdmin (system_admin_required);
    each route then checks the kind / batch belongs to them."""
    tenant_view, admin_view = login_required(view), system_admin_required(view)

    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        return (admin_view if session.get("role") == "SystemAdmin" else tenant_view)(*args, **kwargs)
    return wrapped


def _kind(key):
    k = KINDS.get(key)
    if k is None:
        abort(404)
    return k


def _scope(db, kind):
    """The tenant_id images of this kind are kept under for this user."""
    if kind.platform:
        if g.get("role") != "SystemAdmin":
            abort(403)
        pid = platform_tenant_id(db)
        if pid is None:
            abort(404)
        return pid
    if g.get("tenant_id") is None:
        abort(404)
    return g.tenant_id


def _owner(db, kind, scope, owner_id):
    o = kind.owner(db, scope, owner_id)
    if o is None:
        abort(404)
    return o


def _batch(db, batch_id):
    """(batch, kind, scope) -- 404 unless the batch belongs to this user."""
    b = db.execute("SELECT * FROM image_import_batches WHERE batch_id = ?", (batch_id,)).fetchone()
    if b is None:
        abort(404)
    kind = ic.batch_kind(b)
    scope = _scope(db, kind)
    if b["tenant_id"] != scope:
        abort(404)
    return b, kind, scope


def _valid_date(text):
    text = (text or "").strip()
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def _start_batch(db, kind, scope, files, owner_id=None):
    source = request.form.get("source") if request.form.get("source") in ic.SOURCES else "Individual"
    try:
        found = ic.read_upload(files)
    except ValueError as e:
        flash(str(e), "error")
        return None
    names = ", ".join(f.filename for f in files if f and f.filename)
    batch_id = ic.create_batch(db, scope, found, source=source,
                               contributor=(request.form.get("contributor") or "").strip() or None,
                               default_date=_valid_date(request.form.get("default_date")), owner_id=owner_id,
                               user_id=g.user_id, upload_name=names[:200], kind=kind)
    log_action("ImageUpload", "image_import_batches", batch_id,
               f"Uploaded {len(found)} image(s) for curation ({names[:120]})", tenant_id=None if kind.platform else scope)
    return batch_id


def _album_url(kind, owner_id, **kw):
    return url_for("images.album", kind=kind.key, owner_id=owner_id, **kw)


def _import_url(kind):
    return url_for("images.import_images") if kind.key == "supplier" else url_for("images.import_kind", kind=kind.key)


# ---- uploads -------------------------------------------------------------------------------------

@images_bp.route("/import", methods=["GET", "POST"])
@any_user
def import_images():
    return _import_page("supplier")


@images_bp.route("/import/<kind>", methods=["GET", "POST"])
@any_user
def import_kind(kind):
    return _import_page(kind)


def _import_page(kind_key):
    kind = _kind(kind_key)
    db = get_db()
    scope = _scope(db, kind)
    if request.method == "POST":
        batch_id = _start_batch(db, kind, scope, request.files.getlist("files"))
        if batch_id:
            return redirect(url_for("images.curate", batch_id=batch_id))
        return redirect(_import_url(kind))
    owners = kind.owners(db, scope)
    runs, allowance = [], None
    if kind.key == "supplier" or kind.platform:
        collector.mark_stale(db, scope)
        runs = db.execute("SELECT * FROM image_agent_runs WHERE tenant_id = ? AND owner_kind = ? ORDER BY run_id DESC "
                          "LIMIT 10", (scope, kind.key)).fetchall()
        allowance = None if kind.platform else _allowance(db)
    return render_template("images/import.html", kind=kind, batches=ic.open_batches(db, scope, kind), owners=owners,
                           today=date.today().isoformat(), sources=ic.SOURCES, owner=None, runs=runs,
                           cities=sorted({o["city"] for o in owners if o.get("city")}),
                           types=sorted({o["type_label"] for o in owners if o.get("type_label")}),
                           allowance=allowance, import_url=_import_url(kind))


@images_bp.route("/<kind>/<int:owner_id>/add", methods=["GET", "POST"])
@any_user
def add_images(kind, owner_id):
    kind = _kind(kind)
    db = get_db()
    scope = _scope(db, kind)
    owner = _owner(db, kind, scope, owner_id)
    if request.method == "POST":
        batch_id = _start_batch(db, kind, scope, request.files.getlist("files"), owner_id=owner_id)
        if batch_id:
            return redirect(url_for("images.curate", batch_id=batch_id))
        return redirect(url_for("images.add_images", kind=kind.key, owner_id=owner_id))
    return render_template("images/import.html", kind=kind, owner=owner, today=date.today().isoformat(),
                           sources=ic.SOURCES, batches=[], owners=[], page_url=kind.page_url(url_for, owner_id),
                           import_url=None)


@images_bp.route("/names.csv")
@images_bp.route("/<kind>/names.csv")
@any_user
def names_csv(kind="supplier"):
    """The exact names to send to whoever is collecting photos, so their
    file names match."""
    import csv
    import io
    kind = _kind(kind)
    db = get_db()
    scope = _scope(db, kind)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Name (start each file name with this, then a hyphen)", "Type", "City", "Example file name"])
    example = "Lobby" if kind.key == "supplier" else "Main entrance"
    for o in kind.owners(db, scope):
        w.writerow([o["name"], o.get("type_label") or "", o.get("city") or "", f"{o['name']}-{example}.jpg"])
    data = out.getvalue().encode("utf-8-sig")
    fname = "Hotel_and_Restaurant_names.csv" if kind.key == "supplier" else "Point_of_Interest_names.csv"
    return send_file(BytesIO(data), mimetype="text/csv", as_attachment=True, download_name=fname)


# ---- AI Image Collector ---------------------------------------------------------------------------

def _allowance(db):
    limit = ai_usage.monthly_limit(db, g.tenant_id)
    return {"limit": limit, "spent": ai_usage.month_spend(db, g.tenant_id)}


def _start_collector(db, kind, scope, ids, per_owner, label, back):
    try:
        run_id = collector.start_run(db, scope, g.user_id, ids, per_supplier=per_owner, scope_label=label, kind=kind.key)
    except collector.CollectorError as e:
        flash(str(e), "error")
        return redirect(back)
    log_action("ImageCollector", "image_agent_runs", run_id, f"Started the AI Image Collector for {label}",
               tenant_id=None if kind.platform else scope)
    return redirect(url_for("images.agent_run", run_id=run_id))


def _collector_kind(key):
    kind = _kind(key)
    if not (kind.key == "supplier" or kind.platform):
        abort(404)
    return kind


@images_bp.route("/<kind>/<int:owner_id>/collect", methods=["POST"])
@any_user
def collect_for_owner(kind, owner_id):
    kind = _collector_kind(kind)
    db = get_db()
    scope = _scope(db, kind)
    owner = _owner(db, kind, scope, owner_id)
    return _start_collector(db, kind, scope, [owner_id], request.form.get("per_supplier", 8), owner["name"],
                            _album_url(kind, owner_id))


@images_bp.route("/collect", methods=["POST"])
@images_bp.route("/<kind>/collect", methods=["POST"])
@any_user
def collect_many(kind="supplier"):
    """Several owners at once: by type and city, optionally only those with
    no photos yet."""
    kind = _collector_kind(kind)
    db = get_db()
    scope = _scope(db, kind)
    type_label = request.form.get("type") or ""
    city = request.form.get("city") or ""
    only_empty = bool(request.form.get("only_empty"))
    try:
        limit = max(1, min(int(request.form.get("max_suppliers") or 10), 25))
    except ValueError:
        limit = 10
    chosen = []
    for o in kind.owners(db, scope):
        if (type_label and o.get("type_label") != type_label) or (city and o.get("city") != city):
            continue
        if only_empty and kind.has_images(db, scope, o["id"]):
            continue
        chosen.append(o["id"])
    back = _import_url(kind)
    if not chosen:
        flash(f"No {kind.plural} match that choice.", "error")
        return redirect(back)
    if kind.key == "platform_poi":
        label = f"{type_label + ' POIs' if type_label else 'Points of Interest'}{' in ' + city if city else ''}"
    elif kind.platform:
        label = f"{type_label or kind.singular}{' in ' + city if city else ''}"
    else:
        label = f"{type_label + 's' if type_label else 'Hotels, Resorts and Restaurants'}{' in ' + city if city else ''}"
    label += f" ({min(len(chosen), limit)}{' of ' + str(len(chosen)) if len(chosen) > limit else ''})"
    return _start_collector(db, kind, scope, chosen[:limit], request.form.get("per_supplier", 6), label, back)


def _run(db, run_id):
    """(run, kind, scope) -- 404 unless the run belongs to this user."""
    run = db.execute("SELECT * FROM image_agent_runs WHERE run_id = ?", (run_id,)).fetchone()
    if run is None:
        abort(404)
    kind = _kind(run["owner_kind"] or "supplier")
    scope = _scope(db, kind)
    if run["tenant_id"] != scope:
        abort(404)
    return run, kind, scope


@images_bp.route("/agent/<int:run_id>/status.json")
@any_user
def agent_run_status(run_id):
    db = get_db()
    _run_, _kind_, scope = _run(db, run_id)
    collector.mark_stale(db, scope)
    return jsonify(agent_runs.status_json(db, "image_agent_runs", run_id, extra_cols=("images_staged", "batch_id")))


@images_bp.route("/agent/<int:run_id>/stop", methods=["POST"])
@any_user
def agent_run_stop(run_id):
    db = get_db()
    _run_, kind, scope = _run(db, run_id)
    if agent_runs.stop(db, "image_agent_runs", run_id, g.get("display_name") or g.get("username")):
        log_action("ImageCollector", "image_agent_runs", run_id, "Stopped the AI Image Collector",
                   tenant_id=None if kind.platform else scope)
        flash("Run stopped. Photos it already found are kept.", "success")
    return redirect(url_for("images.agent_run", run_id=run_id))


@images_bp.route("/agent/<int:run_id>")
@any_user
def agent_run(run_id):
    db = get_db()
    run, kind, scope = _run(db, run_id)
    collector.mark_stale(db, scope)
    run = db.execute("SELECT * FROM image_agent_runs WHERE run_id = ?", (run_id,)).fetchone()
    cost, calls, searches = collector.run_cost(db, run_id)
    ids = json.loads(run["supplier_ids"])
    owner = kind.owner(db, scope, ids[0]) if len(ids) == 1 else None
    return render_template("images/agent_run.html", run=run, cost=cost, calls=calls, searches=searches or 0,
                           owner=owner, kind=kind, import_url=_import_url(kind), now_utc=_now_utc(db),
                           status_url=url_for("images.agent_run_status", run_id=run_id),
                           stop_url=url_for("images.agent_run_stop", run_id=run_id),
                           unit="POIs" if kind.key == "platform_poi" else (kind.owner_word + "s") if kind.platform else "hotels / restaurants")


def _now_utc(db):
    return db.execute("SELECT datetime('now')").fetchone()[0]


# ---- curation -------------------------------------------------------------------------------------

@images_bp.route("/batch/<int:batch_id>")
@any_user
def curate(batch_id):
    db = get_db()
    batch, kind, scope = _batch(db, batch_id)
    items = [dict(r, state=ic.item_state(r)) for r in ic.batch_items(db, scope, batch_id)]
    counts = {k: sum(1 for i in items if i["state"] == k) for k in STATE_LABELS}
    show = request.args.get("show", "all")
    visible = [i for i in items if show == "all" or i["state"] == show]
    for_owner = _owner(db, kind, scope, batch["owner_id"]) if batch["owner_id"] else None
    owners = [] if for_owner else kind.owners(db, scope)
    return render_template("images/curate.html", batch=batch, items=visible, all_count=len(items), counts=counts,
                           show=show, labels=STATE_LABELS, badges=STATE_BADGES, owners=owners, kind=kind,
                           for_owner=for_owner, max_desc=ic.DESCRIPTION_MAX, import_url=_import_url(kind))


@images_bp.route("/batch/<int:batch_id>/save", methods=["POST"])
@any_user
def save_batch(batch_id):
    db = get_db()
    batch, kind, scope = _batch(db, batch_id)
    action = request.form.get("action", "save")
    back = url_for("images.curate", batch_id=batch_id, show=request.form.get("show") or "all")
    tenant_for_log = None if kind.platform else scope
    if batch["status"] != "open":
        flash("This upload is already finished.", "error")
        return redirect(back)
    if action == "discard":
        ic.discard(db, scope, batch_id)
        log_action("ImageDiscard", "image_import_batches", batch_id, "Discarded image upload", tenant_id=tenant_for_log)
        flash("Upload discarded. Nothing was added to any album.", "success")
        return redirect(url_for("images.add_images", kind=kind.key, owner_id=batch["owner_id"]) if batch["owner_id"]
                        else _import_url(kind))
    allowed = {o["id"] for o in kind.owners(db, scope)}
    if batch["owner_id"]:
        allowed.add(batch["owner_id"])
    ic.save_edits(db, scope, batch_id, request.form, allowed, with_licence=kind.platform)
    if action == "add":
        added, problems = ic.approve(db, scope, batch_id)
        for p in problems[:10]:
            flash(p, "error")
        if added:
            log_action("ImageCurate", "image_import_batches", batch_id, f"Added {added} curated image(s) to albums",
                       tenant_id=tenant_for_log)
            msg = f"Added {added} image{'s' if added != 1 else ''} to {'the album' if batch['owner_id'] else 'the albums'}."
            if kind.platform:
                msg += " Tenants get them according to their POI image update setting."
            flash(msg, "success")
        elif not problems:
            flash("Nothing ticked to add.", "error")
        if batch["owner_id"] and not db.execute("SELECT 1 FROM image_import_items WHERE batch_id = ? AND status = 'pending'",
                                                (batch_id,)).fetchone():
            return redirect(_album_url(kind, batch["owner_id"]))
    elif action == "reject":
        n = ic.reject_unticked(db, scope, batch_id)
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


@images_bp.route("/item/<int:item_id>/<size>")
@any_user
def item_image(item_id, size):
    db = get_db()
    r = db.execute("SELECT batch_id, thumb_data, file_data, mime_type, file_name FROM image_import_items "
                   "WHERE item_id = ?", (item_id,)).fetchone()
    if r is None or size not in ("thumb", "full"):
        abort(404)
    _batch(db, r["batch_id"])
    if size == "thumb" and r["thumb_data"]:
        return _blob(r["thumb_data"], "image/jpeg")
    return _blob(r["file_data"], r["mime_type"], r["file_name"])


# ---- album ------------------------------------------------------------------------------------------

@images_bp.route("/<kind>/<int:owner_id>/album")
@any_user
def album(kind, owner_id):
    kind = _kind(kind)
    db = get_db()
    scope = _scope(db, kind)
    owner = _owner(db, kind, scope, owner_id)
    q = request.args.get("q", "").strip()
    sort = request.args.get("sort", "album")
    sort = sort if sort in ic.SORT_LABELS else "album"
    # mode=view: the read-only album opened from the top of the owner's page --
    # slider and search only, no adding, AI collecting, editing or deleting.
    readonly = request.args.get("mode") == "view"
    view = "slider" if readonly else request.args.get("view", "slider")
    date_from, date_to = _valid_date(request.args.get("from")), _valid_date(request.args.get("to"))
    images = kind.album(db, scope, owner_id, q=q, sort=sort, date_from=date_from, date_to=date_to)
    total = len(kind.album(db, scope, owner_id)) if (q or date_from or date_to) else len(images)
    pending = ic.pending_for_owner(db, scope, kind, owner_id)
    last_run = None
    if owner.get("has_collector"):
        last_run = db.execute("SELECT * FROM image_agent_runs WHERE tenant_id = ? AND owner_kind = ? AND supplier_ids = ? "
                              "ORDER BY run_id DESC LIMIT 1", (scope, kind.key, f"[{owner_id}]")).fetchone()
    updates = poi_image_sync.pending(db, scope, owner_id) if kind.key == "poi" else \
        catalog_image_sync.pending(db, scope, owner_id) if kind.key == "supplier" else []
    linked = None
    if kind.key == "poi":
        linked = db.execute("SELECT catalog_id FROM tenant_catalog_links WHERE tenant_id = ? AND entity = 'pois' "
                            "AND local_id = ? AND how != 'skipped'", (scope, owner_id)).fetchone()
    elif kind.key == "supplier":
        linked = db.execute("SELECT catalog_id FROM tenant_catalog_links WHERE tenant_id = ? AND entity IN "
                            "('accommodation', 'restaurants') AND local_id = ? AND how != 'skipped'",
                            (scope, owner_id)).fetchone()
    return render_template("images/album.html", kind=kind, owner=owner, images=images, total=total, q=q, sort=sort,
                           view=view, date_from=date_from or "", date_to=date_to or "", sorts=ic.SORT_LABELS,
                           pending=pending, max_desc=ic.DESCRIPTION_MAX, last_run=last_run, readonly=readonly,
                           page_url=kind.page_url(url_for, owner_id), updates=updates, linked=linked)


@images_bp.route("/<kind>/<int:owner_id>/album/<int:image_id>/<size>")
@any_user
def album_image(kind, owner_id, image_id, size):
    kind = _kind(kind)
    db = get_db()
    scope = _scope(db, kind)
    if size not in ("thumb", "full"):
        abort(404)
    data, mime = kind.image(db, scope, owner_id, image_id, size)
    if data is None:
        abort(404)
    if data == "redirect":
        endpoint, args = mime
        return redirect(url_for(endpoint, **args))
    return _blob(data, mime)


@images_bp.route("/<kind>/<int:owner_id>/album/save", methods=["POST"])
@any_user
def save_album(kind, owner_id):
    """List view: save Title / Description / Date / Sort order (and, for the
    platform, Licence) of every row, or delete the ticked ones."""
    kind = _kind(kind)
    db = get_db()
    scope = _scope(db, kind)
    owner = _owner(db, kind, scope, owner_id)
    ids = [int(x) for x in request.form.getlist("ids") if x.isdigit()]
    owned = kind.owns(db, scope, owner_id, ids)
    back = _album_url(kind, owner_id, view="slider" if request.form.get("back") == "slider" else "list",
                      q=request.form.get("q") or None, sort=request.form.get("sort") or None)
    tenant_for_log = None if kind.platform else scope
    if request.form.get("action") == "delete":
        chosen = [int(x) for x in request.form.getlist("selected") if x.isdigit() and int(x) in owned]
        for d in chosen:
            kind.delete(db, scope, owner_id, d)
        if chosen:
            kind.after_change(db, [owner_id])
        db.commit()
        if chosen:
            log_action("Delete", "images", owner_id, f"Deleted {len(chosen)} image(s) from {owner['name']}'s album",
                       tenant_id=tenant_for_log)
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
        kind.update(db, scope, owner_id, d, title,
                    (request.form.get(f"description_{d}") or "").strip()[:ic.DESCRIPTION_MAX] or None,
                    _valid_date(request.form.get(f"date_{d}")), sort,
                    licence=(request.form.get(f"licence_{d}") or "").strip()[:200] or None)
    kind.after_change(db, [owner_id])
    db.commit()
    log_action("Update", "images", owner_id, f"Edited the image album of {owner['name']}", tenant_id=tenant_for_log)
    flash("Album saved." + (f" {errors} row(s) without a Title were left unchanged." if errors else ""),
          "success" if not errors else "error")
    return redirect(back)


# ---- tenant: POI Master Image Catalog updates ----------------------------------------------------------

@images_bp.route("/platform-updates", methods=["GET", "POST"])
@login_required
def platform_updates():
    if g.get("tenant_id") is None:
        abort(404)
    db = get_db()
    poi_id = request.values.get("poi", type=int)
    supplier_id = request.values.get("supplier", type=int)
    if request.method == "POST":
        ids = [int(x) for x in request.form.getlist("ids") if x.isdigit()]
        sids = [int(x) for x in request.form.getlist("sids") if x.isdigit()]
        accept = request.form.get("action") == "accept"
        n = poi_image_sync.decide(db, g.tenant_id, ids, accept, g.user_id)
        n += catalog_image_sync.decide(db, g.tenant_id, sids, accept, g.user_id)
        if n:
            log_action("ImageUpdates", "poi_image_updates", None,
                       f"{'Accepted' if accept else 'Skipped'} {n} platform image update(s)")
            flash(f"{'Accepted' if accept else 'Skipped'} {n} update{'s' if n != 1 else ''}.", "success")
        else:
            flash("Tick the updates first.", "error")
        return redirect(request.form.get("next") or url_for("images.platform_updates", poi=poi_id, supplier=supplier_id))
    groups = {}
    if not supplier_id:
        for u in poi_image_sync.pending(db, g.tenant_id, poi_id):
            groups.setdefault(("poi", u["poi_id"], u["poi_name"]), []).append(dict(u, field="ids", kind="poi"))
    if not poi_id:
        for u in catalog_image_sync.pending(db, g.tenant_id, supplier_id):
            groups.setdefault(("supplier", u["supplier_id"], u["owner_name"]), []).append(dict(u, field="sids", kind="supplier"))
    total = sum(len(v) for v in groups.values())
    return render_template("images/platform_updates.html", groups=groups, total=total, poi_id=poi_id,
                           supplier_id=supplier_id, policy=poi_image_sync.policy(db, g.tenant_id),
                           policies=poi_image_sync.POLICIES)


@images_bp.route("/platform-updates/<int:update_id>/<size>")
@login_required
def platform_update_image(update_id, size):
    """Preview of the master image an update is about (also for a 'new' image
    the tenant doesn't have yet)."""
    if g.get("tenant_id") is None or size not in ("thumb", "full"):
        abort(404)
    db = get_db()
    from image_owners import platform_catalog_image, platform_image
    if request.args.get("kind") == "supplier":
        u = db.execute("SELECT platform_image_id FROM supplier_image_updates WHERE update_id = ? AND tenant_id = ?",
                       (update_id, g.tenant_id)).fetchone()
        if u is None:
            abort(404)
        data, mime = platform_catalog_image(db, u[0], size)
        return _blob(data, mime)
    u = db.execute("SELECT platform_image_id FROM poi_image_updates WHERE update_id = ? AND tenant_id = ?",
                   (update_id, g.tenant_id)).fetchone()
    if u is None:
        abort(404)
    data, mime = platform_image(db, u[0], size)
    return _blob(data, mime)
