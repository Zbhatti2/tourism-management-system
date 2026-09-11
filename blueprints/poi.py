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
import os

from flask import Blueprint, abort, flash, g, jsonify, redirect, render_template, request, url_for

from auth.decorators import login_required
from db import get_db, log_action
from utils import basename, normalize_map_coordinates, open_local_path, pick_file_dialog, pick_files_dialog

poi_bp = Blueprint("poi", __name__)

# Extensions offered by the "Bulk Import Images" file picker's "Image
# files" filter — same list as Suppliers' IMAGE_FILE_TYPES
# (blueprints/suppliers.py). Duplicated rather than imported so this
# blueprint stays self-contained; "All files" is offered alongside it for
# anything scanned/exported under an unusual extension.
IMAGE_FILE_TYPES = [
    ("Image files", "*.jpg *.jpeg *.png *.gif *.bmp *.webp *.tif *.tiff *.heic"),
    ("All files", "*.*"),
]


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
        # NOTE: the old freeform "links" textarea/column has been replaced
        # on the form by the structured Link + Description row editor (see
        # _save_poi_links_from_form below, writing to poi_reference_links)
        # -- Zeb, Sept 2026: "Fix the Links section for Link and
        # Description". points_of_interest.links itself is deliberately
        # left out of the INSERT/UPDATE column lists below so any legacy
        # text already on file (there was none as of this change) is never
        # touched or overwritten by the app going forward.
        "knowledge_graph_data": form.get("knowledge_graph_data", "").strip() or None,
    }


def _poi_links(db, poi_id):
    return db.execute(
        "SELECT * FROM poi_reference_links WHERE poi_id = ? AND tenant_id = ? ORDER BY sort_order, link_id",
        (poi_id, g.tenant_id),
    ).fetchall()


def _save_poi_links_from_form(db, form, poi_id):
    """Saves the structured Link + Description rows from the dynamic
    editor on the POI form (Zeb, Sept 2026: "Fix the Links section for
    Link and Description as shown in the attached image") -- parallel
    arrays submitted as link_url[]/link_description[], one pair per row
    (see templates/poi/form.html's JS). DELETE-then-reinsert-all, same
    convention already used for Keywords/Hashtags elsewhere in the app;
    blank rows (no URL and no description) are skipped."""
    urls = form.getlist("link_url")
    descriptions = form.getlist("link_description")
    db.execute("DELETE FROM poi_reference_links WHERE poi_id = ? AND tenant_id = ?", (poi_id, g.tenant_id))
    sort_order = 0
    for url, description in zip(urls, descriptions):
        url = url.strip()
        description = description.strip()
        if not url and not description:
            continue
        db.execute(
            "INSERT INTO poi_reference_links (tenant_id, poi_id, url, description, sort_order) VALUES (?, ?, ?, ?, ?)",
            (g.tenant_id, poi_id, url or None, description or None, sort_order),
        )
        sort_order += 1
    db.commit()


def _poi_images(db, poi_id):
    return db.execute(
        "SELECT * FROM poi_images WHERE poi_id = ? AND tenant_id = ? ORDER BY sort_order, poi_image_id",
        (poi_id, g.tenant_id),
    ).fetchall()


def _get_poi(db, poi_id):
    poi = db.execute(
        "SELECT * FROM points_of_interest WHERE poi_id = ? AND is_deleted = 0 AND tenant_id = ?",
        (poi_id, g.tenant_id),
    ).fetchone()
    if poi is None:
        abort(404)
    return poi


def _get_poi_image(db, poi_id, image_id):
    _get_poi(db, poi_id)
    image = db.execute(
        "SELECT * FROM poi_images WHERE poi_image_id = ? AND poi_id = ? AND tenant_id = ?",
        (image_id, poi_id, g.tenant_id),
    ).fetchone()
    if image is None:
        abort(404)
    return image


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
    from knowledge_graph import get_edges_for
    kg_edges = get_edges_for(db, g.tenant_id, "PointOfInterest", poi_id)
    poi_links = _poi_links(db, poi_id)
    poi_images = _poi_images(db, poi_id)
    return render_template("poi/view.html", poi=poi, kg_edges=kg_edges, poi_links=poi_links, poi_images=poi_images)


def _links_from_form(form):
    """Reconstructs the Link + Description rows as submitted, for
    re-rendering the form after a validation error without losing what the
    user had typed (mirrors poi and description round-tripping elsewhere
    on this form)."""
    urls = form.getlist("link_url")
    descriptions = form.getlist("link_description")
    rows = []
    for url, description in zip(urls, descriptions):
        url = url.strip()
        description = description.strip()
        if url or description:
            rows.append({"url": url, "description": description})
    return rows


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
                poi_links=_links_from_form(request.form),
            )
        db.execute(
            """INSERT INTO points_of_interest
               (tenant_id, name, poi_type_id, year_established, region_id, country_id, state_id,
                state_province_text, city_id, city_text, local_location, phone, fax, website,
                historical_significance, local_contact_id, governing_authority_id, directions,
                map_coordinates, notes, knowledge_graph_data)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                g.tenant_id, f["name"], f["poi_type_id"], f["year_established"], f["region_id"],
                f["country_id"], f["state_id"], f["state_province_text"], f["city_id"], f["city_text"],
                f["local_location"], f["phone"], f["fax"], f["website"], f["historical_significance"],
                f["local_contact_id"], f["governing_authority_id"], f["directions"],
                f["map_coordinates"], f["notes"], f["knowledge_graph_data"],
            ),
        )
        db.commit()
        poi_id = db.execute("SELECT last_insert_rowid() id").fetchone()["id"]
        _save_poi_links_from_form(db, request.form, poi_id)
        log_action("Create", "point_of_interest", poi_id, f"Created point of interest {f['name']}")
        flash("Point of interest created.", "success")
        return redirect(url_for("poi.view_poi", poi_id=poi_id))
    return render_template(
        "poi/form.html", poi=None, poi_types=_poi_types(db),
        contacts=_contacts(db), organizations=_organizations(db), poi_links=[],
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
                poi_links=_links_from_form(request.form),
            )
        db.execute(
            """UPDATE points_of_interest SET
               name=?, poi_type_id=?, year_established=?, region_id=?, country_id=?, state_id=?,
               state_province_text=?, city_id=?, city_text=?, local_location=?, phone=?, fax=?,
               website=?, historical_significance=?, local_contact_id=?, governing_authority_id=?,
               directions=?, map_coordinates=?, notes=?, knowledge_graph_data=?,
               updated_at=datetime('now')
               WHERE poi_id=? AND tenant_id=?""",
            (
                f["name"], f["poi_type_id"], f["year_established"], f["region_id"], f["country_id"],
                f["state_id"], f["state_province_text"], f["city_id"], f["city_text"],
                f["local_location"], f["phone"], f["fax"], f["website"], f["historical_significance"],
                f["local_contact_id"], f["governing_authority_id"], f["directions"],
                f["map_coordinates"], f["notes"], f["knowledge_graph_data"], poi_id, g.tenant_id,
            ),
        )
        db.commit()
        _save_poi_links_from_form(db, request.form, poi_id)
        log_action("Update", "point_of_interest", poi_id, f"Updated point of interest {f['name']}")
        flash("Point of interest updated.", "success")
        return redirect(url_for("poi.view_poi", poi_id=poi_id))
    return render_template(
        "poi/form.html", poi=poi, poi_types=_poi_types(db),
        contacts=_contacts(db), organizations=_organizations(db),
        poi_links=_poi_links(db, poi_id),
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


# ---------------------------------------------------------- images/photographs
# Zeb, Sept 2026: "Add (1) 'Images/Photographs' to 'Point Of Interest'
# form. (2) Bulk import Images." Mirrors the Suppliers "Documents, Links
# and Images" sub-module's Cloud Link/Local Drive Path + bulk-import
# pattern (blueprints/suppliers.py), simplified down to just images since
# that's all that was asked for here — each poi_images row stands on its
# own (no separate "document" wrapper record needed).

@poi_bp.route("/<int:poi_id>/images")
@login_required
def view_poi_images(poi_id):
    db = get_db()
    poi = _get_poi(db, poi_id)
    return render_template("poi/images.html", poi=poi, images=_poi_images(db, poi_id))


@poi_bp.route("/<int:poi_id>/images/new", methods=["GET", "POST"])
@login_required
def new_poi_image(poi_id):
    db = get_db()
    poi = _get_poi(db, poi_id)
    if request.method == "POST":
        form = request.form
        caption = form.get("caption", "").strip() or None
        cloud_link = form.get("cloud_link", "").strip()
        local_drive_path = form.get("local_drive_path", "").strip()
        if not cloud_link and not local_drive_path:
            flash("Enter a Cloud Link or a Local Drive Path.", "error")
            return render_template("poi/image_form.html", poi=poi, item=form)
        added = 0
        if cloud_link:
            db.execute(
                "INSERT INTO poi_images (tenant_id, poi_id, location_type, path_or_url, caption) VALUES (?, ?, 'Cloud Link', ?, ?)",
                (g.tenant_id, poi_id, cloud_link, caption),
            )
            added += 1
        if local_drive_path:
            db.execute(
                "INSERT INTO poi_images (tenant_id, poi_id, location_type, path_or_url, caption) VALUES (?, ?, 'Local Drive Path', ?, ?)",
                (g.tenant_id, poi_id, local_drive_path, caption),
            )
            added += 1
        db.commit()
        log_action("Create", "poi_image", poi_id, f"Added {added} image location(s) to {poi['name']}")
        flash("Image added.", "success")
        return redirect(url_for("poi.view_poi_images", poi_id=poi_id))
    return render_template("poi/image_form.html", poi=poi, item=None)


@poi_bp.route("/<int:poi_id>/images/<int:image_id>/delete", methods=["POST"])
@login_required
def delete_poi_image(poi_id, image_id):
    db = get_db()
    image = _get_poi_image(db, poi_id, image_id)
    db.execute(
        "DELETE FROM poi_images WHERE poi_image_id = ? AND poi_id = ? AND tenant_id = ?",
        (image_id, poi_id, g.tenant_id),
    )
    db.commit()
    log_action("Delete", "poi_image", poi_id, f"Deleted image #{image_id}")
    flash("Image deleted.", "success")
    return redirect(url_for("poi.view_poi_images", poi_id=poi_id))


@poi_bp.route("/<int:poi_id>/images/<int:image_id>/open")
@login_required
def open_poi_image(poi_id, image_id):
    db = get_db()
    image = _get_poi_image(db, poi_id, image_id)
    if image["location_type"] != "Local Drive Path":
        return jsonify(ok=True)
    path = image["path_or_url"]
    if not os.path.exists(path):
        return jsonify(ok=False, error=f"File not found on disk: {path}")
    try:
        open_local_path(path)
    except Exception as e:
        return jsonify(ok=False, error=f"Couldn't open the file: {e}")
    log_action("Open", "poi_image", poi_id, f"Opened {path}")
    return jsonify(ok=True)


@poi_bp.route("/<int:poi_id>/images/browse-file")
@login_required
def browse_poi_image_file(poi_id):
    path, error = pick_file_dialog()
    return jsonify(path=path, error=error)


@poi_bp.route("/<int:poi_id>/images/bulk-import", methods=["GET", "POST"])
@login_required
def bulk_import_poi_images(poi_id):
    """Bulk-import several Images/Photographs at once (Zeb, Sept 2026:
    "Bulk import Images") — one native multi-select file dialog (see
    pick_bulk_import_poi_images below) instead of the one-at-a-time Add
    Image flow. Each file picked becomes its own poi_images row with a
    Local Drive Path pointing at that file."""
    db = get_db()
    poi = _get_poi(db, poi_id)
    if request.method == "POST":
        form = request.form
        paths = form.getlist("paths")
        captions = form.getlist("captions")
        imported = 0
        for path, caption in zip(paths, captions):
            path = path.strip()
            caption = caption.strip()
            if not path:
                continue
            db.execute(
                "INSERT INTO poi_images (tenant_id, poi_id, location_type, path_or_url, caption) VALUES (?, ?, 'Local Drive Path', ?, ?)",
                (g.tenant_id, poi_id, path, caption or None),
            )
            imported += 1
        db.commit()
        if imported:
            log_action("Create", "poi_image", poi_id, f"Bulk-imported {imported} image(s)/photograph(s) for {poi['name']}")
            flash(f"Imported {imported} image{'s' if imported != 1 else ''}.", "success")
        else:
            flash("Nothing was imported — select at least one file first.", "error")
        return redirect(url_for("poi.view_poi_images", poi_id=poi_id))
    return render_template("poi/bulk_import_images.html", poi=poi)


@poi_bp.route("/<int:poi_id>/images/bulk-import/pick")
@login_required
def pick_bulk_import_poi_images(poi_id):
    paths, error = pick_files_dialog(title="Select Images / Photographs to import", filetypes=IMAGE_FILE_TYPES)
    files = [{"path": p, "name": os.path.splitext(basename(p))[0]} for p in paths]
    return jsonify(files=files, error=error)
