"""
Module — Platform Lookups (SystemAdmin).

Maintains the platform's locked lookup codes: the Supplier Types, Supplier
Sub-Types and POI Types that platform data (Accommodation, Restaurants,
Points of Interest) will be matched to when a tenant adopts it. The rows
live under the reserved TMS Platform tenant; every add, edit or removal
here is pushed straight out to every tenant by
platform_lookups.sync_platform_lookups(), where those rows show as locked
in Table Maintenance. See platform_lookups.py for the full design note.

Codes are fixed once created -- they are what ties a tenant's copy to the
platform row. Removing an entry from the platform list never deletes
anything in a tenant: their copy is simply unlocked and becomes an ordinary
entry they can edit.
"""
import sqlite3

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from auth.decorators import system_admin_required
from db import get_db, log_action
from platform_lookups import (LOCKABLE_TABLES, platform_rows, platform_tenant_id, sync_platform_lookups,
                              tenant_count_locked)
from seed_data import _slug

platform_lookups_bp = Blueprint("platform_lookups", __name__)

TABLE_ORDER = ["supplier_types", "supplier_subtypes", "poi_types"]
TEMPLATE_KEYS = [("", "None"), ("hotel", "Hotel (Amenities & Facilities, Rooms)")]


def _cfg(key):
    cfg = LOCKABLE_TABLES.get(key)
    if cfg is None:
        abort(404)
    return cfg


def _get_platform_row(db, cfg, entry_id):
    row = db.execute(
        f"SELECT * FROM {cfg['table']} WHERE {cfg['pk']} = ? AND tenant_id = ? AND is_system = 1",
        (entry_id, platform_tenant_id(db)),
    ).fetchone()
    if row is None:
        abort(404)
    return row


def _parent_options(db, cfg):
    if not cfg.get("parent"):
        return None
    return platform_rows(db, cfg["parent"]["key"])


def _safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _sync_and_report(db, what):
    tenants, changed = sync_platform_lookups(db)
    flash(f"{what} Applied to {tenants} tenant{'s' if tenants != 1 else ''}.", "success")


@platform_lookups_bp.route("/")
@system_admin_required
def index():
    db = get_db()
    sections = []
    for key in TABLE_ORDER:
        cfg = LOCKABLE_TABLES[key]
        rows = platform_rows(db, key)
        sections.append({
            "key": key,
            "cfg": cfg,
            "rows": [{"row": r, "tenants": tenant_count_locked(db, key, r["code"])} for r in rows],
        })
    tenant_total = db.execute("SELECT COUNT(*) FROM tenants WHERE is_platform = 0").fetchone()[0]
    # Shared (not copied-per-tenant) option lists, shown as extra tabs.
    hub_types = None
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hub_types'").fetchone():
        from blueprints.transport_hubs import hub_type_rows
        hub_types = hub_type_rows(db)
    valid_tabs = set(LOCKABLE_TABLES) | ({"hub_types"} if hub_types is not None else set())
    active = request.args.get("table") if request.args.get("table") in valid_tabs else TABLE_ORDER[0]
    return render_template("platform_lookups/index.html", sections=sections, tenant_total=tenant_total,
                           active=active, hub_types=hub_types)


def _form_values(form, cfg, creating):
    label = form.get("label", "").strip()
    values = {
        "label": label,
        "description": form.get("description", "").strip() or None,
        "sort_order": _safe_int(form.get("sort_order"), 0),
    }
    if creating:
        values["code"] = (form.get("code", "").strip().upper() or None)
    if cfg.get("parent"):
        values[cfg["parent"]["fk"]] = _safe_int(form.get("parent_id"), None)
    if cfg.get("has_template_key"):
        values["template_key"] = form.get("template_key", "").strip() or None
    return label, values


def _render_form(db, key, cfg, entry, form=None):
    return render_template("platform_lookups/form.html", key=key, cfg=cfg, entry=entry, form=form or {},
                           parent_options=_parent_options(db, cfg), template_keys=TEMPLATE_KEYS)


@platform_lookups_bp.route("/<key>/new", methods=["GET", "POST"])
@system_admin_required
def new_entry(key):
    cfg = _cfg(key)
    db = get_db()
    if request.method == "POST":
        label, values = _form_values(request.form, cfg, creating=True)
        if not label:
            flash("Label is required.", "error")
            return _render_form(db, key, cfg, None, request.form)
        p = cfg.get("parent")
        parent = None
        if p:
            parent = db.execute(
                f"SELECT * FROM {p['table']} WHERE {p['pk']} = ? AND tenant_id = ? AND is_system = 1",
                (values[p["fk"]], platform_tenant_id(db)),
            ).fetchone()
            if parent is None:
                flash(f"{p['nav_label']} is required.", "error")
                return _render_form(db, key, cfg, None, request.form)
        if not values["code"]:
            values["code"] = _slug(f"{parent['code']}_{label}" if parent else label)
        else:
            values["code"] = _slug(values["code"])
        try:
            cols = {"tenant_id": platform_tenant_id(db), **values, "is_active": 1, "is_system": 1}
            db.execute(
                f"INSERT INTO {cfg['table']} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                tuple(cols.values()),
            )
            db.commit()
        except sqlite3.IntegrityError:
            flash(f"Code {values['code']} is already on the platform list.", "error")
            return _render_form(db, key, cfg, None, request.form)
        log_action("Create", cfg["table"], None, f"Platform Lookups: added locked {cfg['label']} entry "
                   f"'{label}' ({values['code']})")
        _sync_and_report(db, f"'{label}' added and locked.")
        return redirect(url_for("platform_lookups.index", table=key))
    return _render_form(db, key, cfg, None)


@platform_lookups_bp.route("/<key>/<int:entry_id>/edit", methods=["GET", "POST"])
@system_admin_required
def edit_entry(key, entry_id):
    cfg = _cfg(key)
    db = get_db()
    entry = _get_platform_row(db, cfg, entry_id)
    if request.method == "POST":
        label, values = _form_values(request.form, cfg, creating=False)
        if not label:
            flash("Label is required.", "error")
            return _render_form(db, key, cfg, entry, request.form)
        p = cfg.get("parent")
        if p and not db.execute(
            f"SELECT 1 FROM {p['table']} WHERE {p['pk']} = ? AND tenant_id = ? AND is_system = 1",
            (values[p["fk"]], platform_tenant_id(db)),
        ).fetchone():
            flash(f"{p['nav_label']} is required.", "error")
            return _render_form(db, key, cfg, entry, request.form)
        db.execute(
            f"UPDATE {cfg['table']} SET {', '.join(f'{c} = ?' for c in values)} WHERE {cfg['pk']} = ?",
            tuple(values.values()) + (entry_id,),
        )
        db.commit()
        log_action("Update", cfg["table"], entry_id, f"Platform Lookups: updated locked {cfg['label']} entry "
                   f"'{label}' ({entry['code']})")
        _sync_and_report(db, f"'{label}' updated.")
        return redirect(url_for("platform_lookups.index", table=key))
    return _render_form(db, key, cfg, entry)


@platform_lookups_bp.route("/<key>/<int:entry_id>/remove", methods=["POST"])
@system_admin_required
def remove_entry(key, entry_id):
    """Takes an entry off the platform list. Tenants keep their copy, now
    unlocked. A Supplier Type that still has platform Sub-Types under it
    must have those removed first."""
    cfg = _cfg(key)
    db = get_db()
    entry = _get_platform_row(db, cfg, entry_id)
    if key == "supplier_types":
        children = db.execute(
            "SELECT COUNT(*) FROM supplier_subtypes WHERE supplier_type_id = ? AND is_system = 1", (entry_id,)
        ).fetchone()[0]
        if children:
            flash(f"'{entry['label']}' still has {children} locked Sub-Type(s) under it. Remove those first.",
                  "error")
            return redirect(url_for("platform_lookups.index", table=key))
    db.execute(f"DELETE FROM {cfg['table']} WHERE {cfg['pk']} = ?", (entry_id,))
    db.commit()
    log_action("Delete", cfg["table"], entry_id, f"Platform Lookups: removed '{entry['label']}' "
               f"({entry['code']}) from the platform list; tenant copies unlocked")
    _sync_and_report(db, f"'{entry['label']}' removed from the platform list. Tenants keep their copy, now unlocked.")
    return redirect(url_for("platform_lookups.index", table=key))


@platform_lookups_bp.route("/sync", methods=["POST"])
@system_admin_required
def sync_now():
    """Re-applies the platform list to every tenant -- normally automatic,
    this is a manual "make sure" button."""
    db = get_db()
    _sync_and_report(db, "All tenants checked against the platform list.")
    return redirect(url_for("platform_lookups.index", table=request.form.get("table") or None))
