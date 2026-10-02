"""
Module — Platform Catalogs (SystemAdmin): Points of Interest, Accommodation,
Restaurants, Embassies & Consulates.

One set of screens serves all four, driven by platform_catalog.CATALOGS:
list (search + country/city/type filters), view, add/edit, delete, plus
Find duplicates, Merge (side by side, choosing the value per field) and the
merge log with Undo. Transport Hubs keep their own list/edit screens
(blueprints/transport_hubs.py) but use the duplicate and merge screens here
(entity key 'hubs').
"""
import json

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from auth.decorators import system_admin_required
from db import get_db, log_action
from fuzzy import split_alt_names
from platform_catalog import CATALOGS, MERGEABLE, all_fields
from platform_merge import REASONS, find_duplicates, merge, merge_fields, undo_merge
from utils import parse_coordinates

platform_catalog_bp = Blueprint("platform_catalog", __name__)
LIST_LIMIT = 500


def _spec(entity, mergeable=False):
    spec = (MERGEABLE if mergeable else CATALOGS).get(entity)
    if spec is None:
        abort(404)
    return spec


def _poi_types(db):
    return db.execute(
        """SELECT pt.poi_type_id AS id, pt.label FROM poi_types pt JOIN tenants t ON t.tenant_id = pt.tenant_id
           WHERE t.is_platform = 1 AND pt.is_system = 1 AND pt.is_active = 1 ORDER BY pt.label COLLATE NOCASE"""
    ).fetchall()


def _countries(db):
    return db.execute("SELECT country_id AS id, label FROM countries ORDER BY label COLLATE NOCASE").fetchall()


def _select_sql(spec):
    t = spec["table"]
    extra = ""
    joins = ""
    if spec.get("type_field") == "poi_type_id":
        extra += ", pt.label AS poi_type_label"
        joins += " LEFT JOIN poi_types pt ON pt.poi_type_id = r.poi_type_id"
    if any(f[0] == "represented_country_id" for f in spec["fields"]):
        extra += ", rc.label AS represented_label"
        joins += " LEFT JOIN countries rc ON rc.country_id = r.represented_country_id"
    return (f"""SELECT r.*, co.label AS country_label, rg.label AS region_label,
                       COALESCE(s.label, r.state_province_text) AS state_label,
                       COALESCE(c.label, r.city_text) AS city_label{extra}
                FROM {t} r
                LEFT JOIN countries co ON co.country_id = r.country_id
                LEFT JOIN regions rg ON rg.region_id = r.region_id
                LEFT JOIN states s ON s.state_id = r.state_id
                LEFT JOIN cities c ON c.city_id = r.city_id{joins}""")


def _get(db, spec, rid):
    row = db.execute(_select_sql(spec) + f" WHERE r.{spec['pk']} = ?", (rid,)).fetchone()
    if row is None:
        abort(404)
    return row


# ---- list / view ---------------------------------------------------------------

@platform_catalog_bp.route("/<entity>/")
@system_admin_required
def list_records(entity):
    spec = _spec(entity)
    db = get_db()
    q = request.args.get("q", "").strip()
    country_id = request.args.get("country_id", "")
    city = request.args.get("city", "").strip()
    type_value = request.args.get("type", "")
    show = request.args.get("show", "active")
    where, params = [], []
    if q:
        where.append("(r.name LIKE ? OR r.alt_names LIKE ? OR r.notes LIKE ?)")
        params += [f"%{q}%"] * 3
    if country_id:
        where.append("r.country_id = ?")
        params.append(country_id)
    if city:
        where.append("COALESCE(c.label, r.city_text) = ?")
        params.append(city)
    if type_value and spec.get("type_field"):
        where.append(f"r.{spec['type_field']} = ?")
        params.append(type_value)
    if show in ("active", "inactive"):
        where.append("r.is_active = ?")
        params.append(1 if show == "active" else 0)
    where_sql = " WHERE " + " AND ".join(where) if where else ""
    base = _select_sql(spec)
    total = db.execute(f"SELECT COUNT(*) FROM ({base}{where_sql})", params).fetchone()[0]
    rows = db.execute(f"{base}{where_sql} ORDER BY country_label COLLATE NOCASE, city_label COLLATE NOCASE, "
                      f"r.name COLLATE NOCASE LIMIT {LIST_LIMIT}", params).fetchall()
    t = spec["table"]
    countries = db.execute(f"""SELECT DISTINCT co.country_id AS id, co.label FROM {t} r
                               JOIN countries co ON co.country_id = r.country_id ORDER BY co.label""").fetchall()
    cities = db.execute(f"""SELECT DISTINCT COALESCE(c.label, r.city_text) AS label FROM {t} r
                            LEFT JOIN cities c ON c.city_id = r.city_id
                            WHERE COALESCE(c.label, r.city_text) IS NOT NULL
                            {'AND r.country_id = ?' if country_id else ''} ORDER BY 1""",
                         (country_id,) if country_id else ()).fetchall()
    types = []
    if spec.get("type_field") == "poi_type_id":
        types = [(str(r["id"]), r["label"]) for r in _poi_types(db)]
    elif spec.get("type_field"):
        opts = next(f[3]["options"] for f in spec["fields"] if f[0] == spec["type_field"])
        types = [(o, o) for o in opts]
    return render_template("platform_catalog/list.html", entity=entity, spec=spec, rows=rows, total=total,
                           limit=LIST_LIMIT, q=q, country_id=country_id, city=city, type_value=type_value,
                           show=show, countries=countries, cities=cities, types=types)


@platform_catalog_bp.route("/<entity>/<int:rid>")
@system_admin_required
def view_record(entity, rid):
    spec = _spec(entity)
    db = get_db()
    return render_template("platform_catalog/view.html", entity=entity, spec=spec, row=_get(db, spec, rid),
                           fields=all_fields(spec), alt_names=split_alt_names(_get(db, spec, rid)["alt_names"]))


# ---- add / edit -------------------------------------------------------------------

def _form_values(db, spec, form):
    errors, values = [], {}
    for col, label, kind, opts in all_fields(spec):
        raw = (form.get(col) or "").strip()
        if kind in ("latitude", "longitude") or col in ("latitude", "longitude"):
            continue
        if kind == "yn":
            values[col] = {"Y": 1, "N": 0}.get(raw)
        elif kind in ("int", "poi_type", "country"):
            try:
                values[col] = int(raw) if raw else None
            except ValueError:
                errors.append(f"{label} must be a whole number.")
        elif kind in ("real", "money"):
            try:
                values[col] = float(raw.replace(",", "")) if raw else None
            except ValueError:
                errors.append(f"{label} must be a number.")
        else:
            values[col] = raw or None
        if opts.get("required") and values.get(col) in (None, ""):
            errors.append(f"{label} is required.")
        if kind == "select" and values.get(col) and values[col] not in opts["options"]:
            errors.append(f"{label}: choose one of the listed values.")
        if values.get(col) is not None and kind in ("int", "real", "money"):
            if "min" in opts and values[col] < opts["min"] or "max" in opts and values[col] > opts["max"]:
                errors.append(f"{label} must be between {opts.get('min')} and {opts.get('max')}.")
    coords = (form.get("coordinates") or "").strip()
    if coords:
        parsed = parse_coordinates(coords)
        if parsed is None:
            errors.append("Coordinates weren't recognised. Paste decimal degrees (31.5580, 74.3507) or degrees/minutes/seconds.")
        else:
            values["latitude"], values["longitude"] = round(parsed[0], 6), round(parsed[1], 6)
    else:
        values["latitude"] = values["longitude"] = None
    for col in ("region_id", "country_id", "state_id", "city_id"):
        try:
            values[col] = int(form.get(col)) if form.get(col) else None
        except ValueError:
            values[col] = None
    values["state_province_text"] = None if values["state_id"] else ((form.get("state_province_text") or "").strip() or None)
    values["city_text"] = None if values["city_id"] else ((form.get("city_text") or "").strip() or None)
    values["is_active"] = 1 if form.get("is_active") else 0
    return values, errors


def _render_form(db, entity, spec, row, form=None):
    return render_template("platform_catalog/form.html", entity=entity, spec=spec, row=row, form=form,
                           fields=all_fields(spec), poi_types=_poi_types(db), countries=_countries(db))


@platform_catalog_bp.route("/<entity>/new", methods=["GET", "POST"])
@system_admin_required
def new_record(entity):
    spec = _spec(entity)
    db = get_db()
    if request.method == "POST":
        values, errors = _form_values(db, spec, request.form)
        if errors:
            for e in errors:
                flash(e, "error")
            return _render_form(db, entity, spec, None, request.form)
        cur = db.execute(f"INSERT INTO {spec['table']} ({', '.join(values)}) VALUES ({', '.join('?' * len(values))})",
                         tuple(values.values()))
        db.commit()
        log_action("Create", spec["table"], cur.lastrowid, f"Added {spec['singular']} '{values['name']}' (platform catalog)")
        flash(f"'{values['name']}' added.", "success")
        return redirect(url_for("platform_catalog.view_record", entity=entity, rid=cur.lastrowid))
    return _render_form(db, entity, spec, None, {"is_active": "1"})


@platform_catalog_bp.route("/<entity>/<int:rid>/edit", methods=["GET", "POST"])
@system_admin_required
def edit_record(entity, rid):
    spec = _spec(entity)
    db = get_db()
    row = _get(db, spec, rid)
    if request.method == "POST":
        values, errors = _form_values(db, spec, request.form)
        if errors:
            for e in errors:
                flash(e, "error")
            return _render_form(db, entity, spec, row, request.form)
        db.execute(f"UPDATE {spec['table']} SET {', '.join(f'{c} = ?' for c in values)}, updated_at = datetime('now') "
                   f"WHERE {spec['pk']} = ?", tuple(values.values()) + (rid,))
        db.commit()
        log_action("Update", spec["table"], rid, f"Updated {spec['singular']} '{values['name']}' (platform catalog)")
        flash(f"'{values['name']}' saved.", "success")
        return redirect(url_for("platform_catalog.view_record", entity=entity, rid=rid))
    return _render_form(db, entity, spec, row)


@platform_catalog_bp.route("/<entity>/<int:rid>/delete", methods=["POST"])
@system_admin_required
def delete_record(entity, rid):
    spec = _spec(entity)
    db = get_db()
    row = _get(db, spec, rid)
    db.execute(f"DELETE FROM {spec['table']} WHERE {spec['pk']} = ?", (rid,))
    db.commit()
    log_action("Delete", spec["table"], rid, f"Deleted {spec['singular']} '{row['name']}' (platform catalog)")
    flash(f"'{row['name']}' deleted.", "success")
    return redirect(url_for("platform_catalog.list_records", entity=entity))


# ---- duplicates / merge ------------------------------------------------------------

def _view_url(entity, rid):
    if entity == "hubs":
        return url_for("transport_hubs.view_hub", hub_id=rid)
    return url_for("platform_catalog.view_record", entity=entity, rid=rid)


def _list_url(entity):
    return url_for("transport_hubs.list_hubs") if entity == "hubs" else url_for("platform_catalog.list_records", entity=entity)


@platform_catalog_bp.route("/<entity>/duplicates")
@system_admin_required
def duplicates(entity):
    spec = _spec(entity, mergeable=True)
    db = get_db()
    pairs = find_duplicates(db, entity)
    return render_template("platform_catalog/duplicates.html", entity=entity, spec=spec, pairs=pairs,
                           reasons=REASONS, view_url=_view_url, list_url=_list_url(entity))


def _merge_row(db, entity, spec, rid):
    if entity == "hubs":
        row = db.execute(
            """SELECT h.*, ht.label AS hub_type_label, co.label AS country_label,
                      COALESCE(c.label, h.city_text) AS city_label, COALESCE(s.label, h.state_province_text) AS state_label
               FROM transport_hubs h JOIN hub_types ht ON ht.hub_type_id = h.hub_type_id
               LEFT JOIN countries co ON co.country_id = h.country_id LEFT JOIN cities c ON c.city_id = h.city_id
               LEFT JOIN states s ON s.state_id = h.state_id WHERE h.hub_id = ?""", (rid,)).fetchone()
        if row is None:
            abort(404)
        return row
    return _get(db, spec, rid)


def _display(row, col, kind):
    """Human-readable value of one field, for the merge comparison."""
    lookup = {"poi_type_id": "poi_type_label", "represented_country_id": "represented_label",
              "hub_type_id": "hub_type_label"}
    if col in lookup and lookup[col] in row.keys():
        return row[lookup[col]]
    v = row[col] if col in row.keys() else None
    if kind == "yn":
        return {1: "Yes", 0: "No"}.get(v)
    return v


@platform_catalog_bp.route("/<entity>/merge", methods=["GET", "POST"])
@system_admin_required
def merge_records(entity):
    """Side-by-side merge of ?a= and ?b=. The kept record is chosen with
    ?keep=a|b; for each field the SystemAdmin picks whose value survives."""
    spec = _spec(entity, mergeable=True)
    db = get_db()
    try:
        a_id, b_id = int(request.args.get("a")), int(request.args.get("b"))
    except (TypeError, ValueError):
        abort(400)
    keep = request.values.get("keep", "a")
    keep_id, remove_id = (a_id, b_id) if keep == "a" else (b_id, a_id)
    kept, removed = _merge_row(db, entity, spec, keep_id), _merge_row(db, entity, spec, remove_id)
    kinds = {f[0]: (f[1], f[2]) for f in all_fields(spec)}
    cols = [c for c in merge_fields(spec) if c in kinds]
    rows = []
    for col in cols:
        label, kind = kinds[col]
        kv, rv = _display(kept, col, kind), _display(removed, col, kind)
        rows.append({"col": col, "label": label, "kept": kv, "removed": rv,
                     "differs": (kv or "") != (rv or ""), "fills": kv in (None, "") and rv not in (None, "")})
    location = {"kept": ", ".join(x for x in (kept["city_label"], kept["state_label"], kept["country_label"]) if x),
                "removed": ", ".join(x for x in (removed["city_label"], removed["state_label"], removed["country_label"]) if x)}
    if request.method == "POST":
        take = [c for c in cols + ["city_id"] if request.form.get(f"take_{c}") == "removed"]
        merge_id = merge(db, entity, keep_id, remove_id, take, user_id=g.user_id)
        log_action("Merge", spec["table"], keep_id,
                   f"Merged {spec['singular']} '{removed['name']}' (#{remove_id}) into '{kept['name']}' (#{keep_id}); merge #{merge_id}")
        flash(f"Merged '{removed['name']}' into '{kept['name']}'. It's kept as an alternate name, and the merge can be undone from the merge log.", "success")
        return redirect(_view_url(entity, keep_id))
    return render_template("platform_catalog/merge.html", entity=entity, spec=spec, kept=kept, removed=removed,
                           rows=rows, location=location, a_id=a_id, b_id=b_id, keep=keep,
                           view_url=_view_url, list_url=_list_url(entity))


@platform_catalog_bp.route("/<entity>/merge-log")
@system_admin_required
def merge_log(entity):
    spec = _spec(entity, mergeable=True)
    db = get_db()
    logs = []
    for r in db.execute("""SELECT m.*, u.username FROM platform_merge_log m LEFT JOIN users u ON u.user_id = m.merged_by
                           WHERE m.entity = ? ORDER BY m.merge_id DESC LIMIT 200""", (entity,)).fetchall():
        logs.append(dict(r, kept_name=json.loads(r["kept_before"]).get("name"),
                         removed_name=json.loads(r["removed_row"]).get("name"),
                         links=len(json.loads(r["repointed"] or "[]"))))
    return render_template("platform_catalog/merge_log.html", entity=entity, spec=spec, logs=logs,
                           view_url=_view_url, list_url=_list_url(entity))


@platform_catalog_bp.route("/<entity>/merge-log/<int:merge_id>/undo", methods=["POST"])
@system_admin_required
def undo(entity, merge_id):
    spec = _spec(entity, mergeable=True)
    db = get_db()
    try:
        undo_merge(db, merge_id, user_id=g.user_id)
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("platform_catalog.merge_log", entity=entity))
    log_action("Unmerge", spec["table"], merge_id, f"Undid merge #{merge_id}")
    flash("Merge undone: both records and their links are back as they were.", "success")
    return redirect(url_for("platform_catalog.merge_log", entity=entity))
