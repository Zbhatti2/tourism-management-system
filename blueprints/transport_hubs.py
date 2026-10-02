"""
Module — Transport Hubs (SystemAdmin, Platform Data).

One shared list of airports, railway stations, bus terminals and seaports /
ferry terminals (Group A of the platform master-data plan): no tenant_id,
maintained only by the SystemAdmin, read by every tenant. A hub_types
lookup says which kind each row is, so a new kind (Metro Station, Heliport)
is one new row, not a new table or screen.

Later steps link package route stops (arrival / departure hub) and
transport components (from / to hub) here, and work out "nearest hub" for
a POI or city from the coordinates, which is why latitude/longitude are
real numbers here rather than the free-text map_coordinates the tenant POI
table still uses.

Tables: hub_types, transport_hubs (db.py TRANSPORT_HUBS_DDL / schema.sql
MODULE PD).
"""
import sqlite3

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for

from auth.decorators import login_required, system_admin_required
from db import get_db, log_action
from utils import parse_coordinates

transport_hubs_bp = Blueprint("transport_hubs", __name__)

SCOPES = ["International", "Domestic", "Regional"]
LIST_LIMIT = 500


def _safe_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _hub_types(db, active_only=False):
    sql = "SELECT * FROM hub_types" + (" WHERE is_active = 1" if active_only else "") + " ORDER BY sort_order, label"
    return db.execute(sql).fetchall()


def _get_hub(db, hub_id):
    hub = db.execute(
        """SELECT h.*, ht.label AS type_label, ht.code AS type_code, ht.icon AS type_icon,
                  r.label AS region_label, co.label AS country_label,
                  COALESCE(s.label, h.state_province_text) AS state_label,
                  COALESCE(c.label, h.city_text) AS city_label
           FROM transport_hubs h
           JOIN hub_types ht ON ht.hub_type_id = h.hub_type_id
           LEFT JOIN regions r ON r.region_id = h.region_id
           LEFT JOIN countries co ON co.country_id = h.country_id
           LEFT JOIN states s ON s.state_id = h.state_id
           LEFT JOIN cities c ON c.city_id = h.city_id
           WHERE h.hub_id = ?""",
        (hub_id,),
    ).fetchone()
    if hub is None:
        abort(404)
    return hub


@transport_hubs_bp.route("/")
@system_admin_required
def list_hubs():
    db = get_db()
    q = request.args.get("q", "").strip()
    hub_type_id = request.args.get("hub_type_id", "")
    country_id = request.args.get("country_id", "")
    show = request.args.get("show", "active")

    where, params = [], []
    if q:
        where.append("(h.name LIKE ? OR h.code LIKE ? OR h.icao_code LIKE ? OR COALESCE(c.label, h.city_text) LIKE ? "
                     "OR h.operator LIKE ?)")
        params += [f"%{q}%"] * 5
    if hub_type_id:
        where.append("h.hub_type_id = ?")
        params.append(hub_type_id)
    if country_id:
        where.append("h.country_id = ?")
        params.append(country_id)
    if show == "active":
        where.append("h.is_active = 1")
    elif show == "inactive":
        where.append("h.is_active = 0")

    from_sql = f"""FROM transport_hubs h
            JOIN hub_types ht ON ht.hub_type_id = h.hub_type_id
            LEFT JOIN countries co ON co.country_id = h.country_id
            LEFT JOIN cities c ON c.city_id = h.city_id
            {'WHERE ' + ' AND '.join(where) if where else ''}"""
    total = db.execute(f"SELECT COUNT(*) {from_sql}", params).fetchone()[0]
    # Thousands of airports alone -- show the first LIST_LIMIT and ask for a
    # narrower search rather than render one huge page.
    hubs = db.execute(
        f"""SELECT h.*, ht.label AS type_label, ht.icon AS type_icon, co.label AS country_label,
                   COALESCE(c.label, h.city_text) AS city_label
            {from_sql}
            ORDER BY co.label COLLATE NOCASE, city_label COLLATE NOCASE, ht.sort_order, h.name COLLATE NOCASE
            LIMIT {LIST_LIMIT}""",
        params,
    ).fetchall()
    country_options = db.execute(
        """SELECT DISTINCT co.country_id, co.label FROM transport_hubs h
           JOIN countries co ON co.country_id = h.country_id ORDER BY co.label COLLATE NOCASE"""
    ).fetchall()
    type_counts = {r["hub_type_id"]: r["n"] for r in db.execute(
        "SELECT hub_type_id, COUNT(*) n FROM transport_hubs WHERE is_active = 1 GROUP BY hub_type_id")}
    return render_template(
        "transport_hubs/list.html", hubs=hubs, total=total, limit=LIST_LIMIT, hub_types=_hub_types(db),
        type_counts=type_counts,
        country_options=country_options, q=q, hub_type_id=hub_type_id, country_id=country_id, show=show,
    )


@transport_hubs_bp.route("/<int:hub_id>")
@system_admin_required
def view_hub(hub_id):
    db = get_db()
    return render_template("transport_hubs/view.html", hub=_get_hub(db, hub_id))


def _form_values(db, form):
    """(values, errors) from the submitted form."""
    errors = []
    hub_type_id = _safe_int(form.get("hub_type_id"))
    hub_type = db.execute("SELECT * FROM hub_types WHERE hub_type_id = ?", (hub_type_id,)).fetchone()
    if hub_type is None:
        errors.append("Type is required.")
    name = form.get("name", "").strip()
    if not name:
        errors.append("Name is required.")

    code = form.get("code", "").strip().upper() or None
    icao = form.get("icao_code", "").strip().upper() or None
    if hub_type is not None and hub_type["code"] == "AIRPORT":
        if code and not (len(code) == 3 and code.isalnum()):
            errors.append("An airport's IATA code is 3 letters (e.g. LHE).")
        if icao and not (len(icao) == 4 and icao.isalnum()):
            errors.append("An ICAO code is 4 letters (e.g. OPLA).")
    else:
        icao = None  # ICAO codes apply to airports only; the field is hidden for other types

    lat = lon = None
    coords_raw = form.get("coordinates", "").strip()
    if coords_raw:
        parsed = parse_coordinates(coords_raw)
        if parsed is None:
            errors.append("Coordinates weren't recognised. Paste them as decimal degrees (31.5193, 74.4093) "
                          "or degrees/minutes/seconds.")
        else:
            lat, lon = round(parsed[0], 6), round(parsed[1], 6)

    scope = form.get("scope", "").strip() or None
    if scope not in SCOPES:
        scope = None

    values = {
        "hub_type_id": hub_type_id,
        "name": name,
        "code": code,
        "icao_code": icao,
        "region_id": _safe_int(form.get("region_id")),
        "country_id": _safe_int(form.get("country_id")),
        "state_id": _safe_int(form.get("state_id")),
        "state_province_text": form.get("state_province_text", "").strip() or None,
        "city_id": _safe_int(form.get("city_id")),
        "city_text": form.get("city_text", "").strip() or None,
        "latitude": lat,
        "longitude": lon,
        "operator": form.get("operator", "").strip() or None,
        "scope": scope,
        "address": form.get("address", "").strip() or None,
        "phone": form.get("phone", "").strip() or None,
        "website": form.get("website", "").strip() or None,
        "notes": form.get("notes", "").strip() or None,
        "is_major": 1 if form.get("is_major") else 0,
        "is_active": 1 if form.get("is_active") else 0,
    }
    if values["state_id"]:
        values["state_province_text"] = None
    if values["city_id"]:
        values["city_text"] = None
    return values, errors


def _render_form(db, hub, form=None):
    return render_template("transport_hubs/form.html", hub=hub, form=form, hub_types=_hub_types(db, True),
                           scopes=SCOPES)


@transport_hubs_bp.route("/new", methods=["GET", "POST"])
@system_admin_required
def new_hub():
    db = get_db()
    if request.method == "POST":
        values, errors = _form_values(db, request.form)
        if errors:
            for e in errors:
                flash(e, "error")
            return _render_form(db, None, request.form)
        try:
            cur = db.execute(
                f"INSERT INTO transport_hubs ({', '.join(values)}) VALUES ({', '.join('?' * len(values))})",
                tuple(values.values()),
            )
            db.commit()
        except sqlite3.IntegrityError:
            flash(f"Another hub of this type already has the code {values['code']}.", "error")
            return _render_form(db, None, request.form)
        log_action("Create", "transport_hubs", cur.lastrowid, f"Added transport hub '{values['name']}'")
        flash(f"'{values['name']}' added.", "success")
        return redirect(url_for("transport_hubs.view_hub", hub_id=cur.lastrowid))
    preset = {"hub_type_id": request.args.get("hub_type_id", ""), "is_major": "1", "is_active": "1"}
    return _render_form(db, None, preset)


@transport_hubs_bp.route("/<int:hub_id>/edit", methods=["GET", "POST"])
@system_admin_required
def edit_hub(hub_id):
    db = get_db()
    hub = _get_hub(db, hub_id)
    if request.method == "POST":
        values, errors = _form_values(db, request.form)
        if errors:
            for e in errors:
                flash(e, "error")
            return _render_form(db, hub, request.form)
        try:
            db.execute(
                f"UPDATE transport_hubs SET {', '.join(f'{c} = ?' for c in values)}, updated_at = datetime('now') "
                f"WHERE hub_id = ?",
                tuple(values.values()) + (hub_id,),
            )
            db.commit()
        except sqlite3.IntegrityError:
            flash(f"Another hub of this type already has the code {values['code']}.", "error")
            return _render_form(db, hub, request.form)
        log_action("Update", "transport_hubs", hub_id, f"Updated transport hub '{values['name']}'")
        flash(f"'{values['name']}' saved.", "success")
        return redirect(url_for("transport_hubs.view_hub", hub_id=hub_id))
    return _render_form(db, hub)


@transport_hubs_bp.route("/<int:hub_id>/delete", methods=["POST"])
@system_admin_required
def delete_hub(hub_id):
    db = get_db()
    hub = _get_hub(db, hub_id)
    db.execute("DELETE FROM transport_hubs WHERE hub_id = ?", (hub_id,))
    db.commit()
    log_action("Delete", "transport_hubs", hub_id, f"Deleted transport hub '{hub['name']}'")
    flash(f"'{hub['name']}' deleted.", "success")
    return redirect(url_for("transport_hubs.list_hubs"))


# ---- Hub types ---------------------------------------------------------------

@transport_hubs_bp.route("/types", methods=["GET", "POST"])
@system_admin_required
def hub_types():
    """POST: add a hub type (the form is on Platform Lookups' Transport Hubs
    tab). GET: sends you to that tab."""
    db = get_db()
    if request.method == "POST":
        label = request.form.get("label", "").strip()
        icon = request.form.get("icon", "").strip() or None
        if not label:
            flash("Label is required.", "error")
        else:
            from seed_data import _slug
            code = _slug(label)
            try:
                db.execute(
                    "INSERT INTO hub_types (code, label, icon, sort_order) "
                    "VALUES (?, ?, ?, (SELECT COALESCE(MAX(sort_order), -1) + 1 FROM hub_types))",
                    (code, label, icon),
                )
                db.commit()
                log_action("Create", "hub_types", None, f"Added hub type '{label}'")
                flash(f"'{label}' added.", "success")
            except sqlite3.IntegrityError:
                flash(f"A hub type with code {code} already exists.", "error")
        return redirect(url_for("platform_lookups.index", table="hub_types"))
    # The list itself lives on Platform Lookups, under its "Transport Hubs" tab.
    return redirect(url_for("platform_lookups.index", table="hub_types"))


def hub_type_rows(db):
    """Hub types with how many hubs use each -- for the Platform Lookups tab."""
    return db.execute(
        """SELECT ht.*, (SELECT COUNT(*) FROM transport_hubs h WHERE h.hub_type_id = ht.hub_type_id) AS hub_count
           FROM hub_types ht ORDER BY ht.sort_order, ht.label"""
    ).fetchall()


@transport_hubs_bp.route("/types/<int:hub_type_id>/edit", methods=["GET", "POST"])
@system_admin_required
def edit_hub_type(hub_type_id):
    db = get_db()
    row = db.execute("SELECT * FROM hub_types WHERE hub_type_id = ?", (hub_type_id,)).fetchone()
    if row is None:
        abort(404)
    if request.method == "POST":
        label = request.form.get("label", "").strip()
        if not label:
            flash("Label is required.", "error")
            return render_template("transport_hubs/type_form.html", row=row)
        db.execute(
            "UPDATE hub_types SET label = ?, icon = ?, sort_order = ?, is_active = ? WHERE hub_type_id = ?",
            (label, request.form.get("icon", "").strip() or None, _safe_int(request.form.get("sort_order"), 0),
             1 if request.form.get("is_active") else 0, hub_type_id),
        )
        db.commit()
        log_action("Update", "hub_types", hub_type_id, f"Updated hub type '{label}'")
        flash(f"'{label}' saved.", "success")
        return redirect(url_for("platform_lookups.index", table="hub_types"))
    return render_template("transport_hubs/type_form.html", row=row)


# ---- Lookup for tenants' pickers (Packages) -----------------------------------

def hub_label(row):
    """'Allama Iqbal International Airport (LHE) — Lahore' style label."""
    label = row["name"]
    if row["code"]:
        label += f" ({row['code']})"
    place = row["city_label"] or row["country_label"]
    if place:
        label += f" — {place}"
    return label


@transport_hubs_bp.route("/lookup")
@login_required
def lookup():
    """JSON search used by the hub picker (static/js/hub_picker.js) on
    tenant screens such as Route Stops and Airline Tickets. Any signed-in
    user may read the shared list; only active hubs are returned.

    ?q=      name, IATA/ICAO/station code, or city (2+ characters)
    ?types=  comma-separated hub type codes, e.g. AIRPORT
    ?country_id= hubs in this country are listed first"""
    db = get_db()
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify([])
    types = [t for t in request.args.get("types", "").upper().split(",") if t]
    country_id = _safe_int(request.args.get("country_id"), 0)
    where = ["h.is_active = 1",
             "(h.name LIKE ? OR h.code = ? OR h.icao_code = ? OR COALESCE(c.label, h.city_text) LIKE ?)"]
    params = [f"%{q}%", q.upper(), q.upper(), f"{q}%"]
    if types:
        where.append(f"ht.code IN ({', '.join('?' * len(types))})")
        params += types
    rows = db.execute(
        f"""SELECT h.hub_id, h.name, h.code, ht.code AS type_code, ht.label AS type_label, ht.icon,
                   COALESCE(c.label, h.city_text) AS city_label, co.label AS country_label
            FROM transport_hubs h
            JOIN hub_types ht ON ht.hub_type_id = h.hub_type_id
            LEFT JOIN cities c ON c.city_id = h.city_id
            LEFT JOIN countries co ON co.country_id = h.country_id
            WHERE {' AND '.join(where)}
            ORDER BY (h.code = ?) DESC, (h.country_id = ?) DESC, h.is_major DESC, h.name COLLATE NOCASE
            LIMIT 25""",
        params + [q.upper(), country_id],
    ).fetchall()
    return jsonify([
        {"id": r["hub_id"], "label": hub_label(r), "type": r["type_label"], "icon": r["icon"],
         "country": r["country_label"]}
        for r in rows
    ])


def hub_labels(db, hub_ids):
    """{hub_id: label} for a handful of hub ids -- to pre-fill pickers."""
    ids = [i for i in set(hub_ids) if i]
    if not ids:
        return {}
    rows = db.execute(
        f"""SELECT h.hub_id, h.name, h.code, COALESCE(c.label, h.city_text) AS city_label, co.label AS country_label
            FROM transport_hubs h LEFT JOIN cities c ON c.city_id = h.city_id
            LEFT JOIN countries co ON co.country_id = h.country_id
            WHERE h.hub_id IN ({', '.join('?' * len(ids))})""",
        ids,
    ).fetchall()
    return {r["hub_id"]: hub_label(r) for r in rows}

