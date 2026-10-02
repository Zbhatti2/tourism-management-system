"""
Catalog Sync screens (logic in catalog_sync.py).

SystemAdmin -- /platform/catalog-sync
  * Tenants list: whether each tenant has had its first sync, how much of
    each catalog it holds, and anything waiting for a decision.
  * Per tenant: a preview of what a sync would do -- Add (no match), Link
    (the tenant already has it under the same name in the same city), Review
    (a possible match: link, add as new or skip) -- and the Sync button.
    After the first sync, platform changes reach the tenant automatically;
    this screen is then only needed for possible matches.

Tenant -- /catalog-updates
  * Every field where the platform now has a different value from one the
    tenant changed: Accept (take the platform's) or Keep mine.
"""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

import catalog_sync as cs
from auth.decorators import login_required, system_admin_required
from db import get_db, log_action
from platform_catalog import CATALOGS

catalog_sync_bp = Blueprint("catalog_sync", __name__)
catalog_updates_bp = Blueprint("catalog_updates", __name__)

ENTITY_LABELS = {"pois": "Points of Interest", "accommodation": "Accommodation", "restaurants": "Restaurants"}
ENTITY_ICONS = {"pois": "geo-alt", "accommodation": "building", "restaurants": "cup-hot"}
HOW_LABELS = {"same": "Same name", "sounds": "Sounds alike", "similar": "Similar name"}


def _tenant(db, tenant_id):
    t = db.execute("SELECT * FROM tenants WHERE tenant_id = ? AND is_platform = 0", (tenant_id,)).fetchone()
    if t is None:
        abort(404)
    return t


def _catalog_count(db, entity):
    return db.execute(f"SELECT COUNT(*) FROM {CATALOGS[entity]['table']} WHERE is_active = 1").fetchone()[0]


def _held(db, tenant_id, entity):
    return db.execute("SELECT COUNT(*) FROM tenant_catalog_links WHERE tenant_id = ? AND entity = ? AND how != 'skipped'",
                      (tenant_id, entity)).fetchone()[0]


@catalog_sync_bp.route("/")
@system_admin_required
def index():
    db = get_db()
    totals = {e: _catalog_count(db, e) for e in cs.SYNC_ENTITIES}
    tenants = []
    for t in db.execute("SELECT * FROM tenants WHERE is_platform = 0 ORDER BY tenant_name COLLATE NOCASE").fetchall():
        row = {"t": t, "held": {}, "waiting": 0, "new": 0}
        for e in cs.SYNC_ENTITIES:
            row["held"][e] = _held(db, t["tenant_id"], e)
            items = cs.plan(db, t["tenant_id"], e)
            row["waiting"] += sum(1 for i in items if i["action"] == "review")
            row["new"] += sum(1 for i in items if i["action"] != "review")
        tenants.append(row)
    return render_template("catalog_sync/index.html", tenants=tenants, totals=totals, labels=ENTITY_LABELS,
                           icons=ENTITY_ICONS)


@catalog_sync_bp.route("/<int:tenant_id>")
@system_admin_required
def preview(tenant_id):
    db = get_db()
    tenant = _tenant(db, tenant_id)
    sections = []
    for e in cs.SYNC_ENTITIES:
        items = cs.plan(db, tenant_id, e)
        updated, pending = cs.preview_updates(db, tenant_id, e)
        skipped = db.execute("SELECT COUNT(*) FROM tenant_catalog_links WHERE tenant_id = ? AND entity = ? AND how = 'skipped'",
                             (tenant_id, e)).fetchone()[0]
        sections.append({
            "entity": e, "label": ENTITY_LABELS[e], "icon": ENTITY_ICONS[e], "catalog": _catalog_count(db, e),
            "held": _held(db, tenant_id, e), "skipped": skipped, "updated": updated, "pending": pending,
            "review": [i for i in items if i["action"] == "review"],
            "link": [i for i in items if i["action"] == "link"],
            "add": [i for i in items if i["action"] == "add"],
        })
    nothing = all(not (s["review"] or s["link"] or s["add"] or s["updated"]) for s in sections)
    return render_template("catalog_sync/preview.html", tenant=tenant, sections=sections, how_labels=HOW_LABELS,
                           nothing=nothing)


@catalog_sync_bp.route("/<int:tenant_id>/sync", methods=["POST"])
@system_admin_required
def run_sync(tenant_id):
    db = get_db()
    tenant = _tenant(db, tenant_id)
    decisions = {}
    for key, value in request.form.items():
        if key.startswith("d_") and value in ("add", "link", "skip"):
            decisions[key[2:]] = value
        elif key.startswith("skip_") and value == "1":
            decisions[key[5:]] = "skip"
    counts = cs.apply(db, tenant_id, decisions=decisions)
    parts = []
    for e, c in counts.items():
        bits = [f"{v} {k}" for k, v in c.items() if v]
        if bits:
            parts.append(f"{ENTITY_LABELS[e]}: " + ", ".join(bits))
    summary = "; ".join(parts) or "nothing to change"
    log_action("CatalogSync", "tenant_catalog_links", tenant_id, f"Catalog sync for {tenant['tenant_name']}: {summary}",
               tenant_id=tenant_id)
    flash(f"Catalog sync for {tenant['tenant_name']} finished — {summary}. Platform changes now reach this tenant automatically.",
          "success")
    return redirect(url_for("catalog_sync.preview", tenant_id=tenant_id))


# ---- tenant side ----------------------------------------------------------------------------

def _local_link(local_table, local_id):
    if local_table == "points_of_interest":
        return url_for("poi.view_poi", poi_id=local_id)
    return url_for("suppliers.view_supplier", supplier_id=local_id)


@catalog_updates_bp.route("/")
@login_required
def updates():
    if g.tenant_id is None:
        abort(404)
    db = get_db()
    groups = []
    for link in cs.pending_links(db, g.tenant_id):
        diffs = cs.differences(db, g.tenant_id, link)
        if not diffs:
            continue
        a = cs.ADAPTERS[link["entity"]](db, g.tenant_id)
        groups.append({"link": link, "name": a.local_name(link["local_id"]), "url": _local_link(link["local_table"], link["local_id"]),
                       "entity_label": ENTITY_LABELS[link["entity"]], "icon": ENTITY_ICONS[link["entity"]], "diffs": diffs})
    groups.sort(key=lambda x: (x["entity_label"], (x["name"] or "").lower()))
    return render_template("catalog_sync/updates.html", groups=groups)


@catalog_updates_bp.route("/<int:link_id>/resolve", methods=["POST"])
@login_required
def resolve(link_id):
    if g.tenant_id is None:
        abort(404)
    db = get_db()
    action = request.form.get("action")
    link = db.execute("SELECT * FROM tenant_catalog_links WHERE link_id = ? AND tenant_id = ?",
                      (link_id, g.tenant_id)).fetchone()
    if link is None or action not in ("accept", "keep", "accept_all", "keep_all"):
        abort(404)
    fields = list(cs._loads(link["pending"]).keys()) if action.endswith("_all") else [request.form.get("field")]
    accept = action.startswith("accept")
    done = sum(1 for f in fields if f and cs.resolve(db, g.tenant_id, link_id, f, accept))
    if done:
        log_action("CatalogUpdate", link["local_table"], link["local_id"],
                   f"{'Accepted' if accept else 'Kept own value over'} platform catalog value for {', '.join(fields)}")
        flash("Platform value accepted." if accept else "Kept your value. You won't be asked about this value again.", "success")
    nxt = request.form.get("next") or ""
    if not nxt.startswith("/") or nxt.startswith("//"):
        nxt = url_for("catalog_updates.updates")
    return redirect(nxt)
