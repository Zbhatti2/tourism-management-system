"""
Module — Points of Interest. Tenant-scoped attractions (Sikh Gurdwaras,
mosques, museums, and the rest of the POI Type lookup table) that a tour
operator maintains for planning itineraries. Follows the same CRUD +
soft-delete shape as Contacts, and reuses the Region -> Country ->
Province/State -> City cascade (templates/_geography_fields.html +
static/js/geography_picker.js) already built for Contacts/Personal
Accounts addresses — points_of_interest carries the same column names
(region_id, country_id, state_id, state_province_text, city_id, city_text)
so the partial works here unmodified.

Replaces the removed "Platforms & Subscriptions" and "Accounts" modules —
see Points_Of_Interest_Table.docx for the field spec this table follows.
"""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from auth.decorators import login_required
from db import get_db, log_action
from utils import normalize_map_coordinates

poi_bp = Blueprint("poi", __name__)


def _poi_types(db):
    return db.execute(
        "SELECT * FROM poi_types WHERE is_active = 1 AND tenant_id = ? ORDER BY label COLLATE NOCASE", (g.tenant_id,)
    ).fetchall()


def _contacts(db):
    return db.execute(
        "SELECT contact_id, full_name, file_as FROM contacts "
        "WHERE is_deleted = 0 AND tenant_id = ? ORDER BY file_as, full_name",
        (g.tenant_id,),
    ).fetchall()


def _organizations(db):
    return db.execute(
        "SELECT organization_id, organization_name FROM organizations "
        "WHERE tenant_id = ? ORDER BY organization_name",
        (g.tenant_id,),
    ).fetchall()


def _form_fields(form):
    return {
        "name": form.get("name", "").strip(),
        "poi_type_id": form.get("poi_type_id") or None,
        "year_established": form.get("year_established", "").strip() or None,
        "region_id": form.get("region_id") or None,
        "country_id": form.get("country_id") or None,
        "state_id": form.get("state_id") or None,
        "state_province_text": form.get("state_province_text", "").strip() or None,
        "city_id": form.get("city_id") or None,
        "city_text": form.get("city_text", "").strip() or None,
        "local_location": form.get("local_location", "").strip() or None,
        "phone": form.get("phone", "").strip() or None,
        "fax": form.get("fax", "").strip() or None,
        "website": form.get("website", "").strip() or None,
        "historical_significance": form.get("historical_significance", "").strip() or None,
        "local_contact_id": form.get("local_contact_id") or None,
        "governing_authority_id": form.get("governing_authority_id") or None,
        "directions": form.get("directions", "").strip() or None,
        # Whatever recognizable format was typed/pasted (decimal degrees,
        # straight-quote DMS, ...) is normalized to the canonical DMS
        # display format here at save time — see utils.normalize_map_
        # coordinates and templates/poi/view.html's Google Maps link.
        "map_coordinates": normalize_map_coordinates(form.get("map_coordinates", "")) or None,
        "notes": form.get("notes", "").strip() or None,
        "links": form.get("links", "").strip() or None,
        "knowledge_graph_data": form.get("knowledge_graph_data", "").strip() or None,
    }


def _poi_city_options(db):
    # Each dropdown is limited to values actually in use on one of this
    # tenant's (non-deleted) POIs — same "only offer options that will
    # return results" approach used for Organizations' Country filter —
    # rather than the full global geography lookups, which would make for
    # a very long, mostly-empty-result dropdown. Covers both a linked
    # city_id (c.label) and the free-text fallback (p.city_text) for POIs
    # whose city wasn't seeded in the geography lookups.
    return db.execute(
        """SELECT DISTINCT COALESCE(c.label, p.city_text) AS city_label
           FROM points_of_interest p
           LEFT JOIN cities c ON c.city_id = p.city_id
           WHERE p.tenant_id = ? AND p.is_deleted = 0
             AND COALESCE(c.label, p.city_text) IS NOT NULL
           ORDER BY city_label""",
        (g.tenant_id,),
    ).fetchall()


def _poi_state_options(db):
    return db.execute(
        """SELECT DISTINCT COALESCE(s.label, p.state_province_text) AS state_label
           FROM points_of_interest p
           LEFT JOIN states s ON s.state_id = p.state_id
           WHERE p.tenant_id = ? AND p.is_deleted = 0
             AND COALESCE(s.label, p.state_province_text) IS NOT NULL
           ORDER BY state_label""",
        (g.tenant_id,),
    ).fetchall()


def _poi_country_options(db):
    return db.execute(
        """SELECT DISTINCT co.country_id, co.label
           FROM points_of_interest p
           JOIN countries co ON co.country_id = p.country_id
           WHERE p.tenant_id = ? AND p.is_deleted = 0
           ORDER BY co.label""",
        (g.tenant_id,),
    ).fetchall()


@poi_bp.route("/")
@login_required
def list_pois():
    db = get_db()
    q = request.args.get("q", "").strip()
    poi_type_id = request.args.get("poi_type_id", "").strip()
    city = request.args.get("city", "").strip()
    state = request.args.get("state", "").strip()
    country_id = request.args.get("country_id", "").strip()
    sql = """
        SELECT p.*, pt.label AS type_label,
               COALESCE(c.label, p.city_text) AS city_label,
               COALESCE(s.label, p.state_province_text) AS state_label,
               co.label AS country_label
        FROM points_of_interest p
        LEFT JOIN poi_types pt ON pt.poi_type_id = p.poi_type_id
        LEFT JOIN cities c ON c.city_id = p.city_id
        LEFT JOIN states s ON s.state_id = p.state_id
        LEFT JOIN countries co ON co.country_id = p.country_id
        WHERE p.is_deleted = 0 AND p.tenant_id = ?
    """
    params = [g.tenant_id]
    if q:
        sql += " AND p.name LIKE ?"
        params.append(f"%{q}%")
    if poi_type_id:
        sql += " AND p.poi_type_id = ?"
        params.append(poi_type_id)
    if city:
        sql += " AND COALESCE(c.label, p.city_text) = ?"
        params.append(city)
    if state:
        sql += " AND COALESCE(s.label, p.state_province_text) = ?"
        params.append(state)
    if country_id:
        sql += " AND p.country_id = ?"
        params.append(country_id)
    sql += " ORDER BY p.name"
    rows = db.execute(sql, params).fetchall()
    return render_template(
        "poi/list.html", pois=rows, q=q,
        poi_type_id=poi_type_id, city=city, state=state, country_id=country_id,
        poi_types=_poi_types(db), city_options=_poi_city_options(db),
        state_options=_poi_state_options(db), country_options=_poi_country_options(db),
    )


@poi_bp.route("/<int:poi_id>")
@login_required
def view_poi(poi_id):
    db = get_db()
    poi = db.execute(
        """SELECT p.*, pt.label AS type_label,
                  r.label AS region_label, co.label AS country_label,
                  COALESCE(s.label, p.state_province_text) AS state_label,
                  COALESCE(c.label, p.city_text) AS city_label,
                  ct.full_name AS local_contact_name,
                  og.organization_name AS governing_authority_name
           FROM points_of_interest p
           LEFT JOIN poi_types pt ON pt.poi_type_id = p.poi_type_id
           LEFT JOIN regions r ON r.region_id = p.region_id
           LEFT JOIN countries co ON co.country_id = p.country_id
           LEFT JOIN states s ON s.state_id = p.state_id
           LEFT JOIN cities c ON c.city_id = p.city_id
           LEFT JOIN contacts ct ON ct.contact_id = p.local_contact_id
           LEFT JOIN organizations og ON og.organization_id = p.governing_authority_id
           WHERE p.poi_id = ? AND p.is_deleted = 0 AND p.tenant_id = ?""",
        (poi_id, g.tenant_id),
    ).fetchone()
    if poi is None:
        abort(404)
    return render_template("poi/view.html", poi=poi)


@poi_bp.route("/new", methods=["GET", "POST"])
@login_required
def new_poi():
    db = get_db()
    if request.method == "POST":
        f = _form_fields(request.form)
        if not f["name"]:
            flash("Name is required.", "error")
            return render_template(
                "poi/form.html", poi=None, poi_types=_poi_types(db),
                contacts=_contacts(db), organizations=_organizations(db),
            )
        db.execute(
            """INSERT INTO points_of_interest
               (tenant_id, name, poi_type_id, year_established, region_id, country_id, state_id,
                state_province_text, city_id, city_text, local_location, phone, fax, website,
                historical_significance, local_contact_id, governing_authority_id, directions,
                map_coordinates, notes, links, knowledge_graph_data)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                g.tenant_id, f["name"], f["poi_type_id"], f["year_established"], f["region_id"],
                f["country_id"], f["state_id"], f["state_province_text"], f["city_id"], f["city_text"],
                f["local_location"], f["phone"], f["fax"], f["website"], f["historical_significance"],
                f["local_contact_id"], f["governing_authority_id"], f["directions"],
                f["map_coordinates"], f["notes"], f["links"], f["knowledge_graph_data"],
            ),
        )
        db.commit()
        poi_id = db.execute("SELECT last_insert_rowid() id").fetchone()["id"]
        log_action("Create", "point_of_interest", poi_id, f"Created point of interest {f['name']}")
        flash("Point of interest created.", "success")
        return redirect(url_for("poi.view_poi", poi_id=poi_id))
    return render_template(
        "poi/form.html", poi=None, poi_types=_poi_types(db),
        contacts=_contacts(db), organizations=_organizations(db),
    )


@poi_bp.route("/<int:poi_id>/edit", methods=["GET", "POST"])
@login_required
def edit_poi(poi_id):
    db = get_db()
    poi = db.execute(
        "SELECT * FROM points_of_interest WHERE poi_id = ? AND is_deleted = 0 AND tenant_id = ?",
        (poi_id, g.tenant_id),
    ).fetchone()
    if poi is None:
        abort(404)
    if request.method == "POST":
        f = _form_fields(request.form)
        if not f["name"]:
            flash("Name is required.", "error")
            return render_template(
                "poi/form.html", poi=poi, poi_types=_poi_types(db),
                contacts=_contacts(db), organizations=_organizations(db),
            )
        db.execute(
            """UPDATE points_of_interest SET
               name=?, poi_type_id=?, year_established=?, region_id=?, country_id=?, state_id=?,
               state_province_text=?, city_id=?, city_text=?, local_location=?, phone=?, fax=?,
               website=?, historical_significance=?, local_contact_id=?, governing_authority_id=?,
               directions=?, map_coordinates=?, notes=?, links=?, knowledge_graph_data=?,
               updated_at=datetime('now')
               WHERE poi_id=? AND tenant_id=?""",
            (
                f["name"], f["poi_type_id"], f["year_established"], f["region_id"], f["country_id"],
                f["state_id"], f["state_province_text"], f["city_id"], f["city_text"],
                f["local_location"], f["phone"], f["fax"], f["website"], f["historical_significance"],
                f["local_contact_id"], f["governing_authority_id"], f["directions"],
                f["map_coordinates"], f["notes"], f["links"], f["knowledge_graph_data"], poi_id, g.tenant_id,
            ),
        )
        db.commit()
        log_action("Update", "point_of_interest", poi_id, f"Updated point of interest {f['name']}")
        flash("Point of interest updated.", "success")
        return redirect(url_for("poi.view_poi", poi_id=poi_id))
    return render_template(
        "poi/form.html", poi=poi, poi_types=_poi_types(db),
        contacts=_contacts(db), organizations=_organizations(db),
    )


@poi_bp.route("/<int:poi_id>/delete", methods=["POST"])
@login_required
def delete_poi(poi_id):
    db = get_db()
    poi = db.execute(
        "SELECT * FROM points_of_interest WHERE poi_id = ? AND is_deleted = 0 AND tenant_id = ?",
        (poi_id, g.tenant_id),
    ).fetchone()
    if poi is None:
        abort(404)
    db.execute(
        "UPDATE points_of_interest SET is_deleted = 1, updated_at = datetime('now') WHERE poi_id = ? AND tenant_id = ?",
        (poi_id, g.tenant_id),
    )
    db.commit()
    log_action("Delete", "point_of_interest", poi_id, f"Deleted point of interest {poi['name']}")
    flash(f"'{poi['name']}' deleted.", "success")
    return redirect(url_for("poi.list_pois"))
