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
import mimetypes
import os
from io import BytesIO

from flask import Blueprint, abort, flash, g, jsonify, redirect, render_template, request, send_file, session, url_for

from auth.decorators import login_required
import catalog_sync
import poi_image_sync
from db import get_db, log_action
from utils import (UPLOAD_IMAGE_EXTENSIONS, basename, normalize_map_coordinates, open_local_path, pick_file_dialog,
                   pick_files_dialog, read_uploaded_file)

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

# Extensions the thumbnail/preview routes will actually serve from a Local
# Drive Path -- same list as Suppliers' IMAGE_FILE_EXTENSIONS; anything
# else 404s rather than being streamed back as if it were an image.
IMAGE_FILE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".heic"}

# Zeb, Sept 2026: "Modify the Images Section on Points Of Interest form
# with exactly the same format as Images Section of the Supplier Type
# Hotels Form. Single and Bulk imports with catalog." -- same fields
# (Image Name/Author/Description/Notes/Keywords), same 200-character
# Description cap, same bytes-in-the-database storage and live thumbnail
# preview, same Catalog page (Thumbnail/Name/Author/Description/Date
# Added, Search, Sort, Select+Delete) as Supplier Images
# (blueprints/suppliers.py) -- just without a "Document Type" concept,
# since every poi_images row is already an image and nothing else.
IMAGE_DESCRIPTION_MAX_LENGTH = 200

IMAGE_CATALOG_SORTS = {
    # Applied to the OUTER "SELECT * FROM (...)" wrapper in image_catalog()
    # below -- the same convention (and the same bug avoided) as Suppliers'
    # IMAGE_CATALOG_SORTS: no "i." alias out here, only inside the subquery.
    "recent": "created_at DESC, poi_image_id DESC",
    "name": "image_name COLLATE NOCASE ASC",
    "keywords": "keywords_str COLLATE NOCASE ASC, image_name COLLATE NOCASE ASC",
}


def _split_terms(raw: str):
    """Comma-separated Keywords field -> a sorted, de-duplicated set of
    terms -- same convention as suppliers.py's _split_terms()."""
    return sorted({t.strip() for t in raw.split(",") if t.strip()})


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
    import poi_links
    urls = form.getlist("link_url")
    descriptions = form.getlist("link_description")
    types = form.getlist("link_type") or [""] * len(urls)
    db.execute("DELETE FROM poi_reference_links WHERE poi_id = ? AND tenant_id = ?", (poi_id, g.tenant_id))
    sort_order = 0
    for url, description, link_type in zip(urls, descriptions, types):
        url = url.strip()
        description = description.strip()
        if not url and not description:
            continue
        if link_type not in poi_links.LINK_TYPE:
            link_type = poi_links.guess_type(url, description)
        db.execute(
            "INSERT INTO poi_reference_links (tenant_id, poi_id, url, description, link_type, sort_order) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (g.tenant_id, poi_id, url or None, description or None, link_type, sort_order),
        )
        sort_order += 1
    db.commit()


def _poi_images(db, poi_id):
    return db.execute(
        "SELECT * FROM poi_images WHERE poi_id = ? AND tenant_id = ? AND COALESCE(is_deleted, 0) = 0 "
        "ORDER BY COALESCE(NULLIF(sort_order, 0), 999999), poi_image_id",
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
        "SELECT * FROM poi_images WHERE poi_image_id = ? AND poi_id = ? AND tenant_id = ? AND COALESCE(is_deleted, 0) = 0",
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


def _list_url():
    """Back to the Points of Interest list exactly as the user left it
    (type, city, province, country, search)."""
    return url_for("poi.list_pois", **(session.get("poi_list_args") or {}))


def _back_to_list(poi_id=None):
    """After a save or delete: back to that list, with the POI just changed
    highlighted."""
    url = _list_url()
    if poi_id:
        url += ("&" if "?" in url else "?") + f"hl={poi_id}#p{poi_id}"
    return redirect(url)


@poi_bp.context_processor
def _poi_list_context():
    import poi_links
    return {"poi_list_url": _list_url, "link_types": poi_links.LINK_TYPES}


@poi_bp.route("/")
@login_required
def list_pois():
    db = get_db()
    session["poi_list_args"] = {k: v for k, v in request.args.items() if v and k != "hl"}
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
        catalog_ids=catalog_sync.linked_local_ids(db, g.tenant_id, "points_of_interest"),
        catalog_pending=catalog_sync.pending_count(db, g.tenant_id),
        image_updates_pending=poi_image_sync.pending_count(db, g.tenant_id),
        poi_type_id=poi_type_id, city=city, state=state, country_id=country_id,
        highlight=request.args.get("hl", type=int), poi_types=_poi_types(db), city_options=_poi_city_options(db),
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
    catalog = catalog_sync.view_context(db, g.tenant_id, "points_of_interest", poi_id)
    return render_template("poi/view.html", poi=poi, kg_edges=kg_edges, poi_links=poi_links, poi_images=poi_images,
                           catalog=catalog, poi_image_updates=len(poi_image_sync.pending(db, g.tenant_id, poi_id)))


def _links_from_form(form):
    """Reconstructs the Link + Description rows as submitted, for
    re-rendering the form after a validation error without losing what the
    user had typed (mirrors poi and description round-tripping elsewhere
    on this form)."""
    urls = form.getlist("link_url")
    descriptions = form.getlist("link_description")
    types = form.getlist("link_type") or [""] * len(urls)
    rows = []
    for url, description, link_type in zip(urls, descriptions, types):
        url = url.strip()
        description = description.strip()
        if url or description:
            rows.append({"url": url, "description": description, "link_type": link_type})
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
        return _back_to_list(poi_id)
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
    return _back_to_list()


# ---------------------------------------------------------- images/photographs
# Zeb, Sept 2026: "Add (1) 'Images/Photographs' to 'Point Of Interest'
# form. (2) Bulk import Images." Originally mirrored the Suppliers
# "Documents, Links and Images" sub-module's Cloud Link/Local Drive Path +
# bulk-import pattern (blueprints/suppliers.py), simplified down to just
# images since that's all that was asked for here — each poi_images row
# stands on its own (no separate "document" wrapper record needed).
#
# Sept 2026, later: "Modify the Images Section on Points Of Interest form
# with exactly the same format as Images Section of the Supplier Type
# Hotels Form. Single and Bulk imports with catalog." Brought up to full
# parity with Supplier Images: every image now carries an Image Name (the
# old "caption" column, promoted and required), Author, Description (200
# characters), Notes and Keywords; Bulk Import requires the same interim
# per-image details step (with a live thumbnail preview) before it commits
# anything; a picked Local Drive Path is read and its bytes stored
# straight in the database at save time (Single Add and Bulk Import both);
# and there's a full Catalog page (Thumbnail/Name/Author/Description/Date
# Added, Search, Sort, Select+Delete) reached the same way Supplier
# Images' is. Unlike Suppliers, a POI image never had a separate
# "locations" table — one poi_images row already IS the image and its one
# location — so there's no per-image Locations sub-page here; editing an
# image only ever touches its Name/Author/Description/Notes/Keywords, not
# where its bytes/link came from (delete and re-add to replace the file
# itself, same as a contact/employee profile photo).

@poi_bp.route("/<int:poi_id>/images")
@login_required
def view_poi_images(poi_id):
    """The full Images / Photographs Catalog for one POI — same shape as
    Suppliers' image_catalog() (blueprints/suppliers.py): search (name/
    author/description/keywords), sort (recent/name/keywords), and
    Select+Delete (single or bulk). Kept at this POI's existing "Images/
    Photographs" URL (unchanged since before this redesign) rather than a
    new /catalog path, since this page always was — and now more fully
    is — the one place to see every image on file for a POI."""
    # Superseded by the Images Catalog album (blueprints/images.py, Oct 2026);
    # kept as a redirect so old links and bookmarks still land in the right place.
    _get_poi(get_db(), poi_id)
    return redirect(url_for("images.album", kind="poi", owner_id=poi_id, view="list", q=request.args.get("q") or None))


@poi_bp.route("/<int:poi_id>/images/delete", methods=["POST"])
@login_required
def delete_catalog_poi_images(poi_id):
    """Select + Delete (single or bulk) from the Images Catalog — mirrors
    Suppliers' delete_catalog_images() exactly, just a hard delete (POI
    images never had a soft-delete/undo concept, unlike Suppliers'
    documents) — also removes each deleted image's Keywords rows, since
    there's no ON DELETE CASCADE in SQLite here."""
    db = get_db()
    poi = _get_poi(db, poi_id)
    image_ids = [i for i in request.form.getlist("image_ids") if i.strip()]
    deleted = 0
    for image_id in image_ids:
        row = db.execute(
            "SELECT poi_image_id FROM poi_images WHERE poi_image_id = ? AND poi_id = ? AND tenant_id = ? "
            "AND COALESCE(is_deleted, 0) = 0",
            (image_id, poi_id, g.tenant_id),
        ).fetchone()
        if row is None:
            continue
        # Soft delete: an image inherited from the platform's Master Image
        # Catalog that the tenant removed must not be inherited again.
        db.execute("UPDATE poi_images SET is_deleted = 1 WHERE poi_image_id = ? AND tenant_id = ?", (image_id, g.tenant_id))
        deleted += 1
    db.commit()
    if deleted:
        log_action("Delete", "poi_image", poi_id, f"Deleted {deleted} image(s) from catalog for {poi['name']}")
        flash(f"Deleted {deleted} image{'s' if deleted != 1 else ''}.", "success")
    else:
        flash("Nothing was deleted — select at least one image first.", "error")
    return redirect(url_for("images.album", kind="poi", owner_id=poi_id, view="list"))


def _poi_image_keywords(db, image_id):
    return db.execute(
        "SELECT term FROM poi_image_keywords WHERE poi_image_id = ? ORDER BY term", (image_id,)
    ).fetchall()


@poi_bp.route("/<int:poi_id>/images/<int:image_id>")
@login_required
def view_poi_image(poi_id, image_id):
    db = get_db()
    poi = _get_poi(db, poi_id)
    image = _get_poi_image(db, poi_id, image_id)
    keywords = _poi_image_keywords(db, image_id)
    return render_template("poi/image_view.html", poi=poi, item=image, keywords=keywords)


@poi_bp.route("/<int:poi_id>/images/new", methods=["GET", "POST"])
@login_required
def new_poi_image(poi_id):
    db = get_db()
    poi = _get_poi(db, poi_id)
    if request.method == "POST":
        form = request.form
        image_name = form.get("image_name", "").strip()
        description = form.get("description", "").strip()
        cloud_link = form.get("cloud_link", "").strip()
        local_drive_path = form.get("local_drive_path", "").strip()
        # A file chosen in the browser ("Upload an image") -- works on the
        # hosted app as well as locally. local_drive_path is kept only for
        # backwards compatibility with anything still posting a path.
        upload_bytes, upload_name, upload_mime, upload_error = read_uploaded_file(
            request.files.get("upload_file"), UPLOAD_IMAGE_EXTENSIONS
        )
        error = upload_error
        if error:
            pass
        elif not image_name:
            error = "Image name is required."
        elif len(description) > IMAGE_DESCRIPTION_MAX_LENGTH:
            error = f"Description must be {IMAGE_DESCRIPTION_MAX_LENGTH} characters or fewer (currently {len(description)})."
        elif sum(bool(x) for x in (cloud_link, local_drive_path, upload_bytes)) > 1:
            error = "Upload an image or enter a Cloud Link, not both — one image, one location."
        elif not cloud_link and not local_drive_path and not upload_bytes:
            error = "Upload an image or enter a Cloud Link."
        if error:
            flash(error, "error")
            return render_template("poi/image_form.html", poi=poi, item=form, image_id=None)

        authors = form.get("authors", "").strip() or None
        notes = form.get("notes", "").strip() or None

        if upload_bytes:
            db.execute(
                """INSERT INTO poi_images
                   (tenant_id, poi_id, location_type, path_or_url, image_name, authors, description, notes,
                    file_data, file_name, mime_type, file_size)
                   VALUES (?, ?, 'Stored in Database', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (g.tenant_id, poi_id, upload_name, image_name, authors, description or None, notes,
                 upload_bytes, upload_name, upload_mime, len(upload_bytes)),
            )
        elif local_drive_path:
            if os.path.splitext(local_drive_path)[1].lower() not in IMAGE_FILE_EXTENSIONS or not os.path.isfile(local_drive_path):
                flash(f"Couldn't read that file — check the path and try again: {local_drive_path}", "error")
                return render_template("poi/image_form.html", poi=poi, item=form, image_id=None)
            try:
                with open(local_drive_path, "rb") as f:
                    file_bytes = f.read()
            except OSError as e:
                flash(f"Couldn't read that file — check the path and try again: {e}", "error")
                return render_template("poi/image_form.html", poi=poi, item=form, image_id=None)
            mime_type = mimetypes.guess_type(local_drive_path)[0] or "application/octet-stream"
            db.execute(
                """INSERT INTO poi_images
                   (tenant_id, poi_id, location_type, path_or_url, image_name, authors, description, notes,
                    file_data, file_name, mime_type, file_size)
                   VALUES (?, ?, 'Stored in Database', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (g.tenant_id, poi_id, local_drive_path, image_name, authors, description or None, notes,
                 file_bytes, basename(local_drive_path), mime_type, len(file_bytes)),
            )
        else:
            db.execute(
                """INSERT INTO poi_images
                   (tenant_id, poi_id, location_type, path_or_url, image_name, authors, description, notes)
                   VALUES (?, ?, 'Cloud Link', ?, ?, ?, ?, ?)""",
                (g.tenant_id, poi_id, cloud_link, image_name, authors, description or None, notes),
            )
        db.commit()
        new_image_id = db.execute("SELECT last_insert_rowid() id").fetchone()["id"]
        for term in _split_terms(form.get("keywords", "")):
            db.execute(
                "INSERT OR IGNORE INTO poi_image_keywords (tenant_id, poi_image_id, term) VALUES (?, ?, ?)",
                (g.tenant_id, new_image_id, term),
            )
        db.commit()

        log_action("Create", "poi_image", poi_id, f"Added image '{image_name}' to {poi['name']}")
        flash("Image added — stored in the database.", "success")
        return redirect(url_for("poi.view_poi_image", poi_id=poi_id, image_id=new_image_id))
    return render_template("poi/image_form.html", poi=poi, item=None, image_id=None)


@poi_bp.route("/<int:poi_id>/images/<int:image_id>/edit", methods=["GET", "POST"])
@login_required
def edit_poi_image(poi_id, image_id):
    """Edits an image's Name/Author/Description/Notes/Keywords only — not
    its location. One poi_images row is both the image and its one
    location, so there's nothing to "add another copy" of the way a
    Supplier Document can; replacing the picture itself is delete-and-
    re-add, same as a contact/employee profile photo."""
    db = get_db()
    poi = _get_poi(db, poi_id)
    item = _get_poi_image(db, poi_id, image_id)
    if request.method == "POST":
        form = request.form
        image_name = form.get("image_name", "").strip()
        description = form.get("description", "").strip()
        if not image_name:
            flash("Image name is required.", "error")
            return render_template(
                "poi/image_form.html", poi=poi, item=form, image_id=image_id,
                keywords=form.get("keywords", ""),
            )
        if len(description) > IMAGE_DESCRIPTION_MAX_LENGTH:
            flash(f"Description must be {IMAGE_DESCRIPTION_MAX_LENGTH} characters or fewer (currently {len(description)}).", "error")
            return render_template(
                "poi/image_form.html", poi=poi, item=form, image_id=image_id,
                keywords=form.get("keywords", ""),
            )
        db.execute(
            "UPDATE poi_images SET image_name=?, authors=?, description=?, notes=? WHERE poi_image_id=? AND poi_id=? AND tenant_id=?",
            (
                image_name, form.get("authors", "").strip() or None, description or None,
                form.get("notes", "").strip() or None, image_id, poi_id, g.tenant_id,
            ),
        )
        db.execute("DELETE FROM poi_image_keywords WHERE poi_image_id = ? AND tenant_id = ?", (image_id, g.tenant_id))
        for term in _split_terms(form.get("keywords", "")):
            db.execute(
                "INSERT OR IGNORE INTO poi_image_keywords (tenant_id, poi_image_id, term) VALUES (?, ?, ?)",
                (g.tenant_id, image_id, term),
            )
        db.commit()
        log_action("Update", "poi_image", poi_id, f"Updated image '{image_name}'")
        flash("Image updated.", "success")
        return redirect(url_for("poi.view_poi_image", poi_id=poi_id, image_id=image_id))
    keywords = ", ".join(r["term"] for r in _poi_image_keywords(db, image_id))
    return render_template("poi/image_form.html", poi=poi, item=item, image_id=image_id, keywords=keywords)


@poi_bp.route("/<int:poi_id>/images/<int:image_id>/delete", methods=["POST"])
@login_required
def delete_poi_image(poi_id, image_id):
    db = get_db()
    _get_poi_image(db, poi_id, image_id)
    db.execute(
        "UPDATE poi_images SET is_deleted = 1 WHERE poi_image_id = ? AND poi_id = ? AND tenant_id = ?",
        (image_id, poi_id, g.tenant_id),
    )
    db.commit()
    log_action("Delete", "poi_image", poi_id, f"Deleted image #{image_id}")
    flash("Image deleted.", "success")
    return redirect(url_for("images.album", kind="poi", owner_id=poi_id, view="list"))


@poi_bp.route("/<int:poi_id>/images/<int:image_id>/open")
@login_required
def open_poi_image(poi_id, image_id):
    """Opens a legacy Local Drive Path image with the OS's default app —
    only meaningful for an image imported before the database-storage
    change (a 'Stored in Database' image has no separate file on disk to
    open; its detail page already shows it full-size)."""
    db = get_db()
    image = _get_poi_image(db, poi_id, image_id)
    if image["location_type"] != "Local Drive Path":
        return jsonify(ok=True)
    path = image["path_or_url"]
    if not path or not os.path.exists(path):
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


@poi_bp.route("/<int:poi_id>/images/<int:image_id>/thumbnail")
@login_required
def poi_image_thumbnail(poi_id, image_id):
    """Serves (or redirects to) a POI image — same three-way dispatch as
    Suppliers' image_thumbnail() (blueprints/suppliers.py): 'Stored in
    Database' streams straight from file_data (no disk access at all);
    'Cloud Link' redirects; a legacy 'Local Drive Path' (from before the
    database-storage change) is read straight off this machine's disk and
    streamed back only if it looks like an image file. No image, a
    missing/inaccessible local file, or a POI/image that isn't this
    tenant's 404s — the <img> tag's onerror swaps in a generic placeholder
    rather than showing a broken image."""
    db = get_db()
    image = db.execute(
        "SELECT * FROM poi_images WHERE poi_image_id = ? AND poi_id = ? AND tenant_id = ?",
        (image_id, poi_id, g.tenant_id),
    ).fetchone()
    if image is None:
        abort(404)
    if image["platform_image_id"]:  # inherited: the bytes live in the platform's Master Image Catalog
        return redirect(url_for("images.album_image", kind="poi", owner_id=poi_id, image_id=image_id, size="full"))
    if image["location_type"] == "Stored in Database":
        if not image["file_data"]:
            abort(404)
        return send_file(
            BytesIO(image["file_data"]),
            mimetype=image["mime_type"] or "application/octet-stream",
            download_name=image["file_name"] or "image",
        )
    if image["location_type"] == "Cloud Link":
        return redirect(image["path_or_url"])
    path = image["path_or_url"]
    if not path or os.path.splitext(path)[1].lower() not in IMAGE_FILE_EXTENSIONS or not os.path.isfile(path):
        abort(404)
    return send_file(path)


def _uploads_by_key(req):
    """Files sent by the browser-based bulk import, keyed by the row key the
    page gave each one (hidden 'upload_keys' list, same order as 'uploads')."""
    files = req.files.getlist("uploads")
    keys = req.form.getlist("upload_keys")
    return {k: f for k, f in zip(keys, files)}


@poi_bp.route("/<int:poi_id>/images/bulk-import", methods=["GET", "POST"])
@login_required
def bulk_import_poi_images(poi_id):
    """Bulk-import several Images/Photographs at once — one native multi-
    select file dialog (see pick_bulk_import_poi_images below), then one
    details pass per picked file (templates/poi/bulk_import_images.html's
    modal, with a live thumbnail preview) before the final Import commits
    anything — same interim-details-step contract as Suppliers'
    bulk_import_images(). Each picked file's bytes are read right here, at
    import time, and stored directly in poi_images (location_type=
    'Stored in Database'); a file that can no longer be read by the time
    Import is clicked (moved/deleted since it was picked) is skipped
    rather than aborting the whole batch, and reported back by name."""
    if request.method == "GET":
        # Superseded by Add images -> curation (blueprints/images.py, Oct 2026).
        return redirect(url_for("images.add_images", kind="poi", owner_id=poi_id))
    db = get_db()
    poi = _get_poi(db, poi_id)
    if request.method == "POST":
        form = request.form
        paths = form.getlist("paths")
        names = form.getlist("names")
        authors = form.getlist("authors")
        descriptions = form.getlist("descriptions")
        notes_list = form.getlist("notes")
        keywords_list = form.getlist("keywords")
        imported = 0
        skipped = []
        uploads = _uploads_by_key(request)
        for i, path in enumerate(paths):
            path = path.strip()
            name = (names[i] if i < len(names) else "").strip()
            if not path or not name:
                continue
            if path.startswith("upload:"):
                # Chosen in the browser (templates/poi/bulk_import_images.html)
                file_bytes, file_name, mime_type, upload_error = read_uploaded_file(
                    uploads.get(path[len("upload:"):]), UPLOAD_IMAGE_EXTENSIONS
                )
                if upload_error or not file_bytes:
                    skipped.append(name)
                    continue
                source = file_name
            else:
                if os.path.splitext(path)[1].lower() not in IMAGE_FILE_EXTENSIONS or not os.path.isfile(path):
                    skipped.append(name or basename(path))
                    continue
                try:
                    with open(path, "rb") as f:
                        file_bytes = f.read()
                except OSError:
                    skipped.append(name or basename(path))
                    continue
                file_name = basename(path)
                mime_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
                source = path
            author = (authors[i] if i < len(authors) else "").strip() or None
            description = (descriptions[i] if i < len(descriptions) else "").strip()[:IMAGE_DESCRIPTION_MAX_LENGTH] or None
            row_notes = (notes_list[i] if i < len(notes_list) else "").strip() or None
            db.execute(
                """INSERT INTO poi_images
                   (tenant_id, poi_id, location_type, path_or_url, image_name, authors, description, notes,
                    file_data, file_name, mime_type, file_size)
                   VALUES (?, ?, 'Stored in Database', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (g.tenant_id, poi_id, source, name, author, description, row_notes,
                 file_bytes, file_name, mime_type, len(file_bytes)),
            )
            new_image_id = db.execute("SELECT last_insert_rowid() id").fetchone()["id"]
            row_keywords = keywords_list[i] if i < len(keywords_list) else ""
            for term in _split_terms(row_keywords):
                db.execute(
                    "INSERT OR IGNORE INTO poi_image_keywords (tenant_id, poi_image_id, term) VALUES (?, ?, ?)",
                    (g.tenant_id, new_image_id, term),
                )
            imported += 1
        db.commit()
        if imported:
            log_action("Create", "poi_image", poi_id, f"Bulk-imported {imported} image(s)/photograph(s) for {poi['name']}")
            flash(f"Imported {imported} image{'s' if imported != 1 else ''} — stored in the database.", "success")
        else:
            flash("Nothing was imported — select at least one file first.", "error")
        if skipped:
            flash(
                f"Skipped {len(skipped)} file{'s' if len(skipped) != 1 else ''} that could no longer be read "
                f"(moved, deleted, or not an image file): {', '.join(skipped)}",
                "error",
            )
        return redirect(url_for("images.album", kind="poi", owner_id=poi_id, view="list"))
    return render_template("poi/bulk_import_images.html", poi=poi, description_max_length=IMAGE_DESCRIPTION_MAX_LENGTH)


@poi_bp.route("/<int:poi_id>/images/bulk-import/pick")
@login_required
def pick_bulk_import_poi_images(poi_id):
    paths, error = pick_files_dialog(title="Select Images / Photographs to import", filetypes=IMAGE_FILE_TYPES)
    files = [{"path": p, "name": os.path.splitext(basename(p))[0]} for p in paths]
    return jsonify(files=files, error=error)


@poi_bp.route("/<int:poi_id>/images/bulk-import/preview")
@login_required
def preview_bulk_import_poi_file(poi_id):
    """Live thumbnail preview for the Bulk Import "Image Details" modal —
    same as Suppliers' preview_bulk_import_file(): reads a just-picked,
    not-yet-imported file straight off disk (nothing's in the database yet
    at this point in the flow). Only serves a path that looks like one of
    the offered image extensions and actually exists as a file."""
    path = request.args.get("path", "")
    if not path or os.path.splitext(path)[1].lower() not in IMAGE_FILE_EXTENSIONS or not os.path.isfile(path):
        abort(404)
    return send_file(path)
