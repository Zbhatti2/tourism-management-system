"""
Module — Suppliers. Vendors a tour operator books through (hotels,
transport, restaurants, ...). Kept fully separate from Organizations at
every level, per the request: its own type/sub-type tables
(supplier_types / supplier_subtypes, not organization_types), its own
addresses (supplier_addresses, not the Module A `addresses` table used by
Contacts), its own contacts (supplier_contacts, not the global `contacts`
table), and its own phone numbers (supplier_contact_phones, not
`contact_phones`).

A supplier can have several address locations (Main Office, Billing, ... —
see `supplier_address_types`); each Supplier Contact optionally links to one of
that same supplier's addresses, and has its own phone numbers, each
tagged with a phone type (Cell / Office / Fax / ... — see `phone_types`,
with "Fax" simply being one more phone_type rather than a separate field).

No archive/history tracking here (unlike Contacts) — Suppliers is
master/reference data, same simple soft-delete as Organizations/Points of
Interest. Contacts and their phones are reached only through their parent
supplier's page ("Select Supplier and then Add Contacts for that
Supplier" — no standalone top-level Supplier Contacts screen), so every
nested route below requires the supplier_id in its URL.

External Resources (suppliers.is_external_resource=1 — a contracted
person/resource rather than a vendor company; see
migrate_add_supplier_external_resource.py) get their OWN dedicated
creation route (new_external_resource, "/resources/new") and their own
form/view templates (suppliers/resource_form.html, resource_view.html),
styled after the original Human Resources "External Resource" screens
rather than the generic Supplier form — see
migrate_add_supplier_resource_contact_info.py for why: reusing the generic
form made it easy to forget to check the "External Resource" box, and
carried fields that don't fit a contractor's own record (multiple typed
addresses, a separate "contact person" sub-record). A flagged supplier
gets a Company field (company_name — for billing through a company),
exactly ONE billing address (still stored in supplier_addresses, just
UI-restricted to a single row — see _get_resource_address below), and its
own multiple phones/emails/reference links (supplier_phones/
supplier_emails/supplier_reference_links) instead of a supplier_contacts
sub-record. edit_supplier()/view_supplier() branch on is_external_resource
to route between the generic and resource templates; the generic New/Edit
Supplier form no longer offers the External Resource checkbox at all.
"""
import mimetypes
import os
from datetime import date
from io import BytesIO

from flask import Blueprint, abort, flash, g, jsonify, redirect, render_template, request, send_file, session, url_for

from auth.decorators import login_required
import catalog_image_sync
import catalog_sync
import supplier_groups
from db import get_db, log_action
from utils import (UPLOAD_IMAGE_EXTENSIONS, basename, open_local_path, pick_file_dialog, pick_files_dialog,
                   read_uploaded_file)

suppliers_bp = Blueprint("suppliers", __name__)


def _supplier_types(db, group=None, keep_type_id=None):
    """Active Supplier Types with their group; group (a supplier_groups row)
    limits them to that group. keep_type_id keeps a supplier's current Type
    in the list even when it belongs to another group."""
    sql = """SELECT t.*, sg.label AS group_label, sg.code AS group_code, sg.sort_order AS group_sort
             FROM supplier_types t LEFT JOIN supplier_groups sg ON sg.supplier_group_id = t.supplier_group_id
             WHERE t.is_active = 1 AND t.tenant_id = ?"""
    params = [g.tenant_id]
    if group is not None:
        sql += " AND (t.supplier_group_id = ? OR t.supplier_type_id = ?)"
        params += [group["supplier_group_id"], keep_type_id or -1]
    return db.execute(sql + " ORDER BY sg.sort_order, t.label COLLATE NOCASE", params).fetchall()


# ---- Supplier Groups: the list remembers which group (and filters) you're in --------------------

def _current_group(db):
    """The supplier_groups row the user is working in, or None for All
    Suppliers. ?group=<key> on the list sets it; other pages use the one
    remembered in the session."""
    key = request.args.get("group") if request.endpoint == "suppliers.list_suppliers" else None
    if key is None:
        key = session.get("supplier_group", "all")
    return supplier_groups.by_url_key(db, key)


def _list_url():
    """Back to the Suppliers list exactly as the user left it (group,
    search and filters)."""
    args = session.get("supplier_list_args") or {}
    if "group" not in args:
        args = dict(args, group=session.get("supplier_group", "all"))
    return url_for("suppliers.list_suppliers", **args)


@suppliers_bp.context_processor
def _supplier_list_context():
    return {"supplier_list_url": _list_url}


def _supplier_subtypes(db):
    return db.execute(
        "SELECT * FROM supplier_subtypes WHERE is_active = 1 AND tenant_id = ? ORDER BY label COLLATE NOCASE", (g.tenant_id,)
    ).fetchall()


def _address_types(db):
    return db.execute(
        "SELECT * FROM supplier_address_types WHERE is_active = 1 AND tenant_id = ? ORDER BY label COLLATE NOCASE", (g.tenant_id,)
    ).fetchall()


def _phone_types(db):
    return db.execute(
        "SELECT * FROM phone_types WHERE is_active = 1 AND tenant_id = ? ORDER BY label COLLATE NOCASE", (g.tenant_id,)
    ).fetchall()


def _get_resource_address(db, supplier_id):
    """The single billing address for an External Resource — the same
    supplier_addresses table an ordinary supplier's (multiple, typed)
    addresses live in, just always exactly one row here (primary/only),
    picked up regardless of is_primary/address_type_id in case either was
    set some other way."""
    return db.execute(
        """SELECT a.*, COALESCE(c.label, a.city_text) AS city_label,
                  COALESCE(st.label, a.state_province_text) AS state_label,
                  co.label AS country_label
           FROM supplier_addresses a
           LEFT JOIN cities c ON c.city_id = a.city_id
           LEFT JOIN states st ON st.state_id = a.state_id
           LEFT JOIN countries co ON co.country_id = a.country_id
           WHERE a.supplier_id = ? AND a.tenant_id = ?
           ORDER BY a.is_primary DESC, a.supplier_address_id ASC
           LIMIT 1""",
        (supplier_id, g.tenant_id),
    ).fetchone()


def _city_options(db):
    """Cities actually in use on a supplier's (any) address, for the
    Suppliers list's City filter — limited to ones in use, same reasoning
    as Organizations' Country filter (organizations.py's country_options),
    so the dropdown stays short and every option is guaranteed to return
    results. A linked city (cities.label) and a free-text city_text
    fallback are both offered, matching how the list's own City column is
    built (COALESCE(city label, city_text))."""
    return db.execute(
        """SELECT DISTINCT COALESCE(c.label, a.city_text) AS city_label
           FROM supplier_addresses a
           LEFT JOIN cities c ON c.city_id = a.city_id
           WHERE a.tenant_id = ? AND COALESCE(c.label, a.city_text) IS NOT NULL
                 AND COALESCE(c.label, a.city_text) != ''
           ORDER BY city_label COLLATE NOCASE""",
        (g.tenant_id,),
    ).fetchall()


def _get_supplier(db, supplier_id):
    supplier = db.execute(
        """SELECT s.*, t.label AS type_label, t.template_key AS template_key, t.code AS type_code,
                  st.label AS subtype_label
           FROM suppliers s
           LEFT JOIN supplier_types t ON t.supplier_type_id = s.supplier_type_id
           LEFT JOIN supplier_subtypes st ON st.supplier_subtype_id = s.supplier_subtype_id
           WHERE s.supplier_id = ? AND s.is_deleted = 0 AND s.tenant_id = ?""",
        (supplier_id, g.tenant_id),
    ).fetchone()
    if supplier is None:
        abort(404)
    return supplier


def _hotel_amenity_options(db, supplier_type_id):
    """The Hotel Template's master Amenities & Facilities list, scoped to
    one Supplier Type (nested lookup — see hotel_amenity_options / Zeb's
    Amenities_and_Facilities1a.txt request and Table Maintenance's
    "associated with Supplier Type Hotel" follow-up), in sub-section
    (category) order."""
    return db.execute(
        "SELECT * FROM hotel_amenity_options WHERE is_active = 1 AND tenant_id = ? AND supplier_type_id = ? "
        "ORDER BY sort_order, category, label",
        (g.tenant_id, supplier_type_id),
    ).fetchall()


def _hotel_room_types(db, supplier_type_id):
    return db.execute(
        "SELECT * FROM hotel_room_types WHERE is_active = 1 AND tenant_id = ? AND supplier_type_id = ? ORDER BY sort_order, label",
        (g.tenant_id, supplier_type_id),
    ).fetchall()


def _document_types(db):
    """Supplier Document Types (Business License, Permit, Agreement, ... —
    MODULE X, "Documents, Links and Images"). A flat lookup, not nested
    under Supplier Type -- every Supplier can have licenses/permits/
    agreements/images regardless of type."""
    return db.execute(
        "SELECT * FROM supplier_document_types WHERE is_active = 1 AND tenant_id = ? ORDER BY sort_order, label COLLATE NOCASE",
        (g.tenant_id,),
    ).fetchall()


def _split_terms(raw: str):
    """Comma-separated Keywords/Hashtags field -> a sorted, de-duplicated
    set of terms, same convention as documents.py's Module D helper."""
    return sorted({t.strip() for t in raw.split(",") if t.strip()})


def _save_document_locations_from_form(db, form, document_id):
    """Reads the optional "Cloud Link" / "Local Drive Path" fields right on
    the Add/Edit Document form (Zeb, Sept 2026: "I don't see a Cloud Link
    (http) entry place in the Supplier Documents") and adds a location row
    for each one that's filled in -- on top of (not instead of) the
    dedicated Locations card on the document's own page, which still
    handles reviewing/removing what's on file and adding further copies."""
    cloud_link = form.get("cloud_link", "").strip()
    if cloud_link:
        db.execute(
            "INSERT INTO supplier_document_locations (tenant_id, supplier_document_id, location_type, path_or_url) VALUES (?, ?, 'Cloud Link', ?)",
            (g.tenant_id, document_id, cloud_link),
        )
    local_path = form.get("local_drive_path", "").strip()
    if local_path:
        db.execute(
            "INSERT INTO supplier_document_locations (tenant_id, supplier_document_id, location_type, path_or_url) VALUES (?, ?, 'Local Drive Path', ?)",
            (g.tenant_id, document_id, local_path),
        )
    # A file chosen in the browser ("Upload a file") is stored in the
    # database, so it works on the hosted app. Returns an error message for
    # a rejected upload (too large / empty) so the caller can flash it.
    data, file_name, mime_type, error = read_uploaded_file(request.files.get("upload_file"))
    if data:
        db.execute(
            """INSERT INTO supplier_document_locations
               (tenant_id, supplier_document_id, location_type, path_or_url, file_data, file_name, mime_type, file_size)
               VALUES (?, ?, 'Stored in Database', ?, ?, ?, ?, ?)""",
            (g.tenant_id, document_id, file_name, data, file_name, mime_type, len(data)),
        )
    if cloud_link or local_path or data:
        db.commit()
    return error


def _get_supplier_document(db, supplier_id, document_id):
    doc = db.execute(
        """SELECT d.*, dt.label AS document_type_label FROM supplier_documents d
           LEFT JOIN supplier_document_types dt ON dt.document_type_id = d.document_type_id
           WHERE d.supplier_document_id = ? AND d.supplier_id = ? AND d.is_deleted = 0 AND d.tenant_id = ?""",
        (document_id, supplier_id, g.tenant_id),
    ).fetchone()
    if doc is None:
        abort(404)
    return doc


# ---------------------------------------------------------------- list/view

@suppliers_bp.route("/")
@login_required
def list_suppliers():
    db = get_db()
    if request.args.get("is_external_resource") and "group" not in request.args:
        # Older links ("External Resources only") open the External Resources group.
        return redirect(url_for("suppliers.list_suppliers", group="external"))
    if not request.args and session.get("supplier_list_args"):
        return redirect(_list_url())
    group = _current_group(db)
    session["supplier_group"] = supplier_groups.URL_KEYS[group["code"]] if group else "all"
    session["supplier_list_args"] = {k: v for k, v in request.args.items() if v and k != "hl"}
    q = request.args.get("q", "").strip()
    is_external_resource = "1" if group and group["code"] == "EXTERNAL_RESOURCES" else ""
    type_id = request.args.get("type_id", "").strip()
    subtype_id = request.args.get("subtype_id", "").strip()
    city = request.args.get("city", "").strip()
    preference = request.args.get("preference", "").strip()
    sql = """
        SELECT s.*, t.label AS type_label, st.label AS subtype_label,
               COALESCE(pc.label, pa.city_text) AS city_label,
               COALESCE(ps.label, pa.state_province_text) AS state_label,
               pco.label AS country_label,
               (SELECT COUNT(*) FROM supplier_documents sd WHERE sd.supplier_id = s.supplier_id AND sd.is_deleted = 0) AS document_count
        FROM suppliers s
        LEFT JOIN supplier_types t ON t.supplier_type_id = s.supplier_type_id
        LEFT JOIN supplier_subtypes st ON st.supplier_subtype_id = s.supplier_subtype_id
        LEFT JOIN supplier_addresses pa ON pa.supplier_address_id = (
            SELECT a.supplier_address_id FROM supplier_addresses a
            WHERE a.supplier_id = s.supplier_id AND a.tenant_id = s.tenant_id
            ORDER BY a.is_primary DESC, a.supplier_address_id ASC
            LIMIT 1
        )
        LEFT JOIN cities pc ON pc.city_id = pa.city_id
        LEFT JOIN states ps ON ps.state_id = pa.state_id
        LEFT JOIN countries pco ON pco.country_id = pa.country_id
        WHERE s.is_deleted = 0 AND s.tenant_id = ?
    """
    params = [g.tenant_id]
    if group:
        sql += " AND s.supplier_group_id = ?"
        params.append(group["supplier_group_id"])
    if q:
        sql += " AND s.supplier_name LIKE ?"
        params.append(f"%{q}%")
    if type_id:
        sql += " AND s.supplier_type_id = ?"
        params.append(type_id)
    if subtype_id:
        sql += " AND s.supplier_subtype_id = ?"
        params.append(subtype_id)
    if city:
        # Matches the same City column the list itself displays (a linked
        # city, or the free-text fallback) — see _city_options above.
        sql += " AND COALESCE(pc.label, pa.city_text) = ?"
        params.append(city)
    if preference in ("Primary", "Secondary"):
        # Per Zeb's request: with a City and a Type both picked, this is
        # exactly "show me the Top / Secondary choice among the 100+
        # Hotels in Lahore" — the filter combination this field exists for.
        sql += " AND s.preference = ?"
        params.append(preference)
    sql += " ORDER BY s.supplier_name"
    rows = db.execute(sql, params).fetchall()
    return render_template(
        "suppliers/list.html", suppliers=rows, q=q, is_external_resource=is_external_resource,
        catalog_ids=catalog_sync.linked_local_ids(db, g.tenant_id, "suppliers"),
        catalog_pending=catalog_sync.pending_count(db, g.tenant_id),
        image_updates_pending=catalog_image_sync.pending_count(db, g.tenant_id),
        type_id=type_id, subtype_id=subtype_id, city=city, preference=preference,
        supplier_types=_supplier_types(db, group), subtypes_json=_subtypes_for_type_json(db),
        city_options=_city_options(db), group=group, groups=supplier_groups.all_groups(db),
        group_counts=supplier_groups.counts(db, g.tenant_id), url_keys=supplier_groups.URL_KEYS,
        group_key=session["supplier_group"], highlight=request.args.get("hl", type=int),
    )


@suppliers_bp.route("/<int:supplier_id>")
@login_required
def view_supplier(supplier_id):
    db = get_db()
    supplier = _get_supplier(db, supplier_id)

    if supplier["is_external_resource"]:
        address = _get_resource_address(db, supplier_id)
        emails = db.execute(
            "SELECT * FROM supplier_emails WHERE supplier_id = ? AND tenant_id = ? ORDER BY is_primary DESC, email_address",
            (supplier_id, g.tenant_id),
        ).fetchall()
        phones = db.execute(
            """SELECT p.*, pt.label AS phone_type_label
               FROM supplier_phones p
               LEFT JOIN phone_types pt ON pt.phone_type_id = p.phone_type_id
               WHERE p.supplier_id = ? AND p.tenant_id = ?
               ORDER BY p.is_primary DESC, pt.sort_order""",
            (supplier_id, g.tenant_id),
        ).fetchall()
        links = db.execute(
            "SELECT * FROM supplier_reference_links WHERE supplier_id = ? AND tenant_id = ? ORDER BY link_id",
            (supplier_id, g.tenant_id),
        ).fetchall()
        return render_template(
            "suppliers/resource_view.html", supplier=supplier, address=address,
            emails=emails, phones=phones, links=links,
        )

    addresses = db.execute(
        """SELECT a.*, at.label AS address_type_label,
                  COALESCE(c.label, a.city_text) AS city_label,
                  COALESCE(st.label, a.state_province_text) AS state_label,
                  co.label AS country_label
           FROM supplier_addresses a
           LEFT JOIN supplier_address_types at ON at.address_type_id = a.address_type_id
           LEFT JOIN cities c ON c.city_id = a.city_id
           LEFT JOIN states st ON st.state_id = a.state_id
           LEFT JOIN countries co ON co.country_id = a.country_id
           WHERE a.supplier_id = ? AND a.tenant_id = ?
           ORDER BY a.is_primary DESC, at.sort_order""",
        (supplier_id, g.tenant_id),
    ).fetchall()
    # Supplier-level Emails/Phones (supplier_emails/supplier_phones) --
    # these tables have existed all along and already have full CRUD (see
    # new_supplier_email/new_supplier_phone below), but were only ever
    # surfaced on the resource_view.html branch above (is_external_resource
    # suppliers). An ordinary supplier had no UI path to a company-level
    # email/phone that isn't tied to a named Contact (Zeb, Sept 2026:
    # "There is no place to add Suppliers Phones, and emails") even though
    # the data model already supported it -- this wires the same existing
    # queries/routes/cards into the ordinary view too. Named
    # supplier_emails/supplier_phones here (not emails/phones) to avoid
    # colliding with the per-contact `phones` local the loop below reuses.
    supplier_emails = db.execute(
        "SELECT * FROM supplier_emails WHERE supplier_id = ? AND tenant_id = ? ORDER BY is_primary DESC, email_address",
        (supplier_id, g.tenant_id),
    ).fetchall()
    supplier_phones = db.execute(
        """SELECT p.*, pt.label AS phone_type_label
           FROM supplier_phones p
           LEFT JOIN phone_types pt ON pt.phone_type_id = p.phone_type_id
           WHERE p.supplier_id = ? AND p.tenant_id = ?
           ORDER BY p.is_primary DESC, pt.sort_order""",
        (supplier_id, g.tenant_id),
    ).fetchall()
    contacts = []
    for c in db.execute(
        """SELECT sc.*, sa.street AS address_street, sat.label AS address_type_label
           FROM supplier_contacts sc
           LEFT JOIN supplier_addresses sa ON sa.supplier_address_id = sc.supplier_address_id
           LEFT JOIN supplier_address_types sat ON sat.address_type_id = sa.address_type_id
           WHERE sc.supplier_id = ? AND sc.is_deleted = 0 AND sc.tenant_id = ?
           ORDER BY sc.name""",
        (supplier_id, g.tenant_id),
    ).fetchall():
        phones = db.execute(
            """SELECT p.*, pt.label AS type_label FROM supplier_contact_phones p
               LEFT JOIN phone_types pt ON pt.phone_type_id = p.phone_type_id
               WHERE p.supplier_contact_id = ? AND p.tenant_id = ?
               ORDER BY p.is_primary DESC, pt.sort_order""",
            (c["supplier_contact_id"], g.tenant_id),
        ).fetchall()
        contacts.append({"row": c, "phones": phones})
    from knowledge_graph import get_edges_for
    kg_edges = get_edges_for(db, g.tenant_id, "Supplier", supplier_id)

    hotel_amenities_by_category = None
    hotel_rooms = None
    hotel_room_total = 0
    if supplier["template_key"] == "hotel":
        checked = db.execute(
            """SELECT o.category, o.label, o.icon
               FROM supplier_amenities sa
               JOIN hotel_amenity_options o ON o.amenity_option_id = sa.amenity_option_id
               WHERE sa.supplier_id = ? AND sa.tenant_id = ?
               ORDER BY o.sort_order, o.category, o.label""",
            (supplier_id, g.tenant_id),
        ).fetchall()
        hotel_amenities_by_category = {}
        for row in checked:
            hotel_amenities_by_category.setdefault(row["category"], []).append(row)
        hotel_rooms = db.execute(
            """SELECT r.*, rt.label AS room_type_label, rt.description AS default_description
               FROM supplier_rooms r
               JOIN hotel_room_types rt ON rt.room_type_id = r.room_type_id
               WHERE r.supplier_id = ? AND r.tenant_id = ?
               ORDER BY rt.sort_order, rt.label""",
            (supplier_id, g.tenant_id),
        ).fetchall()
        hotel_room_total = sum(r["number_of_rooms"] for r in hotel_rooms)

    documents_and_images = db.execute(
        """SELECT d.*, dt.label AS document_type_label,
                  (SELECT COUNT(*) FROM supplier_document_locations l WHERE l.supplier_document_id = d.supplier_document_id) AS location_count
           FROM supplier_documents d
           LEFT JOIN supplier_document_types dt ON dt.document_type_id = d.document_type_id
           WHERE d.supplier_id = ? AND d.is_deleted = 0 AND d.tenant_id = ?
           ORDER BY d.document_name COLLATE NOCASE""",
        (supplier_id, g.tenant_id),
    ).fetchall()
    # "Documents and Links" and "Images / Photographs" are two separate
    # cards on this page (Zeb, Sept 2026: "Separate 'Documents and Links'
    # AND 'Images/Photographs' into Separate Sections") even though both
    # still live in the one supplier_documents table underneath -- an
    # Image/Photograph is just a document row whose Document Type is
    # 'Image / Photograph' (same convention bulk_import_images() already
    # used to default that field). No new column/table needed to split
    # them; this is just partitioning the one query result in Python.
    documents = [d for d in documents_and_images if d["document_type_label"] != IMAGE_DOCUMENT_TYPE_LABEL]
    # Images card shows only the most recent few (Zeb, Sept 2026: "display
    # only the first three most recent images, like the POI's Form" -- the
    # rest are one click away via "Catalog"), so re-sort by created_at
    # DESC here rather than the document_name order documents_and_images
    # was fetched in above (that alphabetical order is still right for the
    # Documents and Links card, which isn't capped).
    # Images Catalog (Oct 2026): the card previews the album in its curated
    # order -- Sort order first, then newest -- the same order the album opens in.
    images = sorted(
        (d for d in documents_and_images if d["document_type_label"] == IMAGE_DOCUMENT_TYPE_LABEL),
        key=lambda d: (d["sort_order"] if d["sort_order"] is not None else 999999, -d["supplier_document_id"]),
    )

    return render_template(
        "suppliers/view.html", supplier=supplier, addresses=addresses, contacts=contacts, kg_edges=kg_edges,
        emails=supplier_emails, phones=supplier_phones,
        hotel_amenities_by_category=hotel_amenities_by_category, hotel_rooms=hotel_rooms, hotel_room_total=hotel_room_total,
        documents=documents, images=images,
        catalog=catalog_sync.view_context(db, g.tenant_id, "suppliers", supplier_id),
    )


# ------------------------------------------------------------------- form

def _subtypes_for_type_json(db):
    """All active sub-types (every type), for the client-side Type -> Sub-Type
    cascade on the supplier form — small enough per tenant to ship whole
    rather than round-tripping per selection."""
    rows = db.execute(
        "SELECT supplier_subtype_id, supplier_type_id, label FROM supplier_subtypes WHERE is_active = 1 AND tenant_id = ? ORDER BY label COLLATE NOCASE",
        (g.tenant_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _form_fields(form):
    preference = form.get("preference") or None
    return {
        "supplier_name": form.get("supplier_name", "").strip(),
        "supplier_type_id": form.get("supplier_type_id") or None,
        "supplier_subtype_id": form.get("supplier_subtype_id") or None,
        "web_page": form.get("web_page", "").strip() or None,
        "notes": form.get("notes", "").strip() or None,
        "knowledge_graph_data": form.get("knowledge_graph_data", "").strip() or None,
        # 'Primary' / 'Secondary' / None -- see schema.sql's CHECK constraint.
        # A form value outside those two is treated the same as blank
        # (None) rather than trusted straight into the query, since it's a
        # plain <select> value an unmodified client can only ever send as
        # one of the three.
        "preference": preference if preference in ("Primary", "Secondary") else None,
    }


def _resource_form_fields(form):
    return {
        "supplier_name": form.get("supplier_name", "").strip(),
        "supplier_type_id": form.get("supplier_type_id") or None,
        "supplier_subtype_id": form.get("supplier_subtype_id") or None,
        "company_name": form.get("company_name", "").strip() or None,
        "tax_id": form.get("tax_id", "").strip() or None,
        "national_id_number": form.get("national_id_number", "").strip() or None,
        "hourly_rate": form.get("hourly_rate") or None,
        "notes": form.get("notes", "").strip() or None,
    }


@suppliers_bp.route("/new", methods=["GET", "POST"])
@login_required
def new_supplier():
    db = get_db()
    if request.method == "POST":
        f = _form_fields(request.form)
        if not f["supplier_name"]:
            flash("Supplier name is required.", "error")
            return render_template(
                "suppliers/form.html", supplier=None, supplier_types=_supplier_types(db, _current_group(db)),
                subtypes_json=_subtypes_for_type_json(db), group=_current_group(db),
            )
        db.execute(
            """INSERT INTO suppliers
               (tenant_id, supplier_name, supplier_type_id, supplier_subtype_id,
                web_page, notes, knowledge_graph_data, preference)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                g.tenant_id, f["supplier_name"], f["supplier_type_id"], f["supplier_subtype_id"],
                f["web_page"], f["notes"], f["knowledge_graph_data"], f["preference"],
            ),
        )
        db.commit()
        supplier_id = db.execute("SELECT last_insert_rowid() id").fetchone()["id"]
        log_action("Create", "supplier", supplier_id, f"Created supplier {f['supplier_name']}")
        flash("Supplier created. Add its addresses, contacts and images here, then use Back to return to the list.",
              "success")
        return redirect(url_for("suppliers.view_supplier", supplier_id=supplier_id))
    group = _current_group(db)
    return render_template(
        "suppliers/form.html", supplier=None, supplier_types=_supplier_types(db, group),
        subtypes_json=_subtypes_for_type_json(db), group=group,
    )


@suppliers_bp.route("/resources/new", methods=["GET", "POST"])
@login_required
def new_external_resource():
    """The dedicated "New External Resource" form — reachable from the
    Human Resources hub and the Suppliers list, kept separate from
    new_supplier() above so an External Resource can never be created
    without the flag being set (see this module's docstring)."""
    db = get_db()
    if request.method == "POST":
        f = _resource_form_fields(request.form)
        if not f["supplier_name"]:
            flash("Name is required.", "error")
            return render_template(
                "suppliers/resource_form.html", supplier=None, supplier_types=_supplier_types(db, _external_group(db)),
                subtypes_json=_subtypes_for_type_json(db),
            )
        db.execute(
            """INSERT INTO suppliers
               (tenant_id, supplier_name, supplier_type_id, supplier_subtype_id, is_external_resource,
                company_name, tax_id, national_id_number, hourly_rate, notes)
               VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?)""",
            (
                g.tenant_id, f["supplier_name"], f["supplier_type_id"], f["supplier_subtype_id"],
                f["company_name"], f["tax_id"], f["national_id_number"], f["hourly_rate"], f["notes"],
            ),
        )
        db.commit()
        supplier_id = db.execute("SELECT last_insert_rowid() id").fetchone()["id"]
        log_action("Create", "supplier", supplier_id, f"Created external resource {f['supplier_name']}")
        flash("External Resource created.", "success")
        return redirect(url_for("suppliers.view_supplier", supplier_id=supplier_id))
    return render_template(
        "suppliers/resource_form.html", supplier=None, supplier_types=_supplier_types(db, _external_group(db)),
        subtypes_json=_subtypes_for_type_json(db),
    )


@suppliers_bp.route("/<int:supplier_id>/edit", methods=["GET", "POST"])
@login_required
def edit_supplier(supplier_id):
    db = get_db()
    supplier = _get_supplier(db, supplier_id)

    if supplier["is_external_resource"]:
        if request.method == "POST":
            f = _resource_form_fields(request.form)
            if not f["supplier_name"]:
                flash("Name is required.", "error")
                return render_template(
                    "suppliers/resource_form.html", supplier=supplier,
                    supplier_types=_supplier_types(db, _external_group(db), supplier["supplier_type_id"]),
                    subtypes_json=_subtypes_for_type_json(db),
                )
            db.execute(
                """UPDATE suppliers SET supplier_name=?, supplier_type_id=?, supplier_subtype_id=?,
                   company_name=?, tax_id=?, national_id_number=?, hourly_rate=?, notes=?,
                   updated_at=datetime('now') WHERE supplier_id=? AND tenant_id=?""",
                (
                    f["supplier_name"], f["supplier_type_id"], f["supplier_subtype_id"],
                    f["company_name"], f["tax_id"], f["national_id_number"], f["hourly_rate"], f["notes"],
                    supplier_id, g.tenant_id,
                ),
            )
            db.commit()
            log_action("Update", "supplier", supplier_id, f"Updated external resource {f['supplier_name']}")
            flash(f"External Resource '{f['supplier_name']}' updated.", "success")
            return _back_to_list(supplier_id)
        return render_template(
            "suppliers/resource_form.html", supplier=supplier,
            supplier_types=_supplier_types(db, _external_group(db), supplier["supplier_type_id"]),
            subtypes_json=_subtypes_for_type_json(db),
        )

    if request.method == "POST":
        f = _form_fields(request.form)
        if not f["supplier_name"]:
            flash("Supplier name is required.", "error")
            return render_template(
                "suppliers/form.html", supplier=supplier,
                supplier_types=_supplier_types(db, _current_group(db), supplier["supplier_type_id"]),
                subtypes_json=_subtypes_for_type_json(db), group=_current_group(db),
            )
        db.execute(
            """UPDATE suppliers SET supplier_name=?, supplier_type_id=?, supplier_subtype_id=?,
               web_page=?, notes=?, knowledge_graph_data=?, preference=?, updated_at=datetime('now')
               WHERE supplier_id=? AND tenant_id=?""",
            (
                f["supplier_name"], f["supplier_type_id"], f["supplier_subtype_id"],
                f["web_page"], f["notes"], f["knowledge_graph_data"], f["preference"], supplier_id, g.tenant_id,
            ),
        )
        db.commit()
        log_action("Update", "supplier", supplier_id, f"Updated supplier {f['supplier_name']}")
        flash(f"'{f['supplier_name']}' updated.", "success")
        return _back_to_list(supplier_id)
    group = _current_group(db)
    return render_template(
        "suppliers/form.html", supplier=supplier,
        supplier_types=_supplier_types(db, group, supplier["supplier_type_id"]),
        subtypes_json=_subtypes_for_type_json(db), group=group,
    )


def _external_group(db):
    return db.execute("SELECT * FROM supplier_groups WHERE code = 'EXTERNAL_RESOURCES'").fetchone()


def _back_to_list(supplier_id=None):
    """After a save or delete: back to the Suppliers list the user came
    from, with the supplier just changed highlighted."""
    url = _list_url()
    if supplier_id:
        url += ("&" if "?" in url else "?") + f"hl={supplier_id}#s{supplier_id}"
    return redirect(url)


@suppliers_bp.route("/<int:supplier_id>/delete", methods=["POST"])
@login_required
def delete_supplier(supplier_id):
    db = get_db()
    supplier = _get_supplier(db, supplier_id)
    db.execute(
        "UPDATE suppliers SET is_deleted = 1, updated_at = datetime('now') WHERE supplier_id = ? AND tenant_id = ?",
        (supplier_id, g.tenant_id),
    )
    db.commit()
    log_action("Delete", "supplier", supplier_id, f"Deleted supplier {supplier['supplier_name']}")
    flash(f"'{supplier['supplier_name']}' deleted.", "success")
    return _back_to_list()


# --------------------------------------------------------------- addresses

@suppliers_bp.route("/<int:supplier_id>/addresses/new", methods=["GET", "POST"])
@login_required
def new_supplier_address(supplier_id):
    db = get_db()
    supplier = _get_supplier(db, supplier_id)
    if supplier["is_external_resource"]:
        # External Resources get exactly ONE (billing) address — if one
        # already exists, "+" on the view page routes here anyway (it's a
        # single shared "Billing Address" affordance), so send it to Edit
        # instead of letting a second row be created.
        existing = _get_resource_address(db, supplier_id)
        if existing:
            return redirect(url_for(
                "suppliers.edit_supplier_address", supplier_id=supplier_id,
                address_id=existing["supplier_address_id"],
            ))
    if request.method == "POST":
        form = request.form
        if form.get("is_primary"):
            db.execute(
                "UPDATE supplier_addresses SET is_primary = 0 WHERE supplier_id = ? AND tenant_id = ?",
                (supplier_id, g.tenant_id),
            )
        db.execute(
            """INSERT INTO supplier_addresses
               (tenant_id, supplier_id, address_type_id, street, unit, region_id, country_id,
                state_id, state_province_text, city_id, city_text, postal_code, is_primary)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                g.tenant_id, supplier_id, form.get("address_type_id") or None,
                form.get("street", "").strip() or None, form.get("unit", "").strip() or None,
                form.get("region_id") or None, form.get("country_id") or None,
                form.get("state_id") or None, form.get("state_province_text", "").strip() or None,
                form.get("city_id") or None, form.get("city_text", "").strip() or None,
                form.get("postal_code", "").strip() or None, 1 if form.get("is_primary") else 0,
            ),
        )
        db.commit()
        log_action("Create", "supplier_address", supplier_id, "Added supplier address")
        flash("Address added.", "success")
        return redirect(url_for("suppliers.view_supplier", supplier_id=supplier_id))
    return render_template(
        "suppliers/address_form.html", supplier=supplier, address=None, address_types=_address_types(db)
    )


@suppliers_bp.route("/<int:supplier_id>/addresses/<int:address_id>/edit", methods=["GET", "POST"])
@login_required
def edit_supplier_address(supplier_id, address_id):
    db = get_db()
    supplier = _get_supplier(db, supplier_id)
    address = db.execute(
        "SELECT * FROM supplier_addresses WHERE supplier_address_id = ? AND supplier_id = ? AND tenant_id = ?",
        (address_id, supplier_id, g.tenant_id),
    ).fetchone()
    if address is None:
        abort(404)
    if request.method == "POST":
        form = request.form
        if form.get("is_primary"):
            db.execute(
                "UPDATE supplier_addresses SET is_primary = 0 WHERE supplier_id = ? AND tenant_id = ?",
                (supplier_id, g.tenant_id),
            )
        db.execute(
            """UPDATE supplier_addresses SET address_type_id=?, street=?, unit=?, region_id=?, country_id=?,
               state_id=?, state_province_text=?, city_id=?, city_text=?, postal_code=?, is_primary=?,
               updated_at=datetime('now') WHERE supplier_address_id=? AND tenant_id=?""",
            (
                form.get("address_type_id") or None, form.get("street", "").strip() or None,
                form.get("unit", "").strip() or None,
                form.get("region_id") or None, form.get("country_id") or None,
                form.get("state_id") or None, form.get("state_province_text", "").strip() or None,
                form.get("city_id") or None, form.get("city_text", "").strip() or None,
                form.get("postal_code", "").strip() or None, 1 if form.get("is_primary") else 0,
                address_id, g.tenant_id,
            ),
        )
        db.commit()
        log_action("Update", "supplier_address", supplier_id, f"Updated supplier address #{address_id}")
        flash("Address updated.", "success")
        return redirect(url_for("suppliers.view_supplier", supplier_id=supplier_id))
    return render_template(
        "suppliers/address_form.html", supplier=supplier, address=address, address_types=_address_types(db)
    )


@suppliers_bp.route("/<int:supplier_id>/addresses/<int:address_id>/delete", methods=["POST"])
@login_required
def delete_supplier_address(supplier_id, address_id):
    db = get_db()
    _get_supplier(db, supplier_id)
    address = db.execute(
        "SELECT * FROM supplier_addresses WHERE supplier_address_id = ? AND supplier_id = ? AND tenant_id = ?",
        (address_id, supplier_id, g.tenant_id),
    ).fetchone()
    if address is None:
        abort(404)
    # Any contact currently linked to this address just loses that link
    # (set NULL) rather than blocking the delete — the contact record
    # itself, and its phones, are unaffected.
    db.execute(
        "UPDATE supplier_contacts SET supplier_address_id = NULL WHERE supplier_address_id = ? AND tenant_id = ?",
        (address_id, g.tenant_id),
    )
    db.execute("DELETE FROM supplier_addresses WHERE supplier_address_id = ? AND tenant_id = ?", (address_id, g.tenant_id))
    db.commit()
    log_action("Delete", "supplier_address", supplier_id, f"Deleted supplier address #{address_id}")
    flash("Address deleted.", "success")
    return redirect(url_for("suppliers.view_supplier", supplier_id=supplier_id))


# ------------------------------------------------ Hotel Template: amenities

@suppliers_bp.route("/<int:supplier_id>/amenities/edit", methods=["GET", "POST"])
@login_required
def edit_supplier_amenities(supplier_id):
    db = get_db()
    supplier = _get_supplier(db, supplier_id)
    if supplier["template_key"] != "hotel":
        abort(404)
    if request.method == "POST":
        checked_ids = {int(v) for v in request.form.getlist("amenity_option_id") if v.strip()}
        db.execute("DELETE FROM supplier_amenities WHERE supplier_id = ? AND tenant_id = ?", (supplier_id, g.tenant_id))
        for option_id in checked_ids:
            db.execute(
                "INSERT INTO supplier_amenities (tenant_id, supplier_id, amenity_option_id) VALUES (?, ?, ?)",
                (g.tenant_id, supplier_id, option_id),
            )
        db.commit()
        log_action("Update", "supplier_amenities", supplier_id, "Updated Amenities & Facilities")
        flash("Amenities & Facilities updated.", "success")
        return redirect(url_for("suppliers.view_supplier", supplier_id=supplier_id))

    options = _hotel_amenity_options(db, supplier["supplier_type_id"])
    checked = {
        r["amenity_option_id"] for r in db.execute(
            "SELECT amenity_option_id FROM supplier_amenities WHERE supplier_id = ? AND tenant_id = ?",
            (supplier_id, g.tenant_id),
        ).fetchall()
    }
    grouped_options = {}
    for opt in options:
        grouped_options.setdefault(opt["category"], []).append(opt)
    return render_template(
        "suppliers/amenities_form.html", supplier=supplier, grouped_options=grouped_options, checked=checked
    )


# ---------------------------------------------------- Hotel Template: rooms

def _parse_price(raw):
    """Form price field -> float, or None if blank/unparseable. Never
    raises -- an un-parseable price is treated the same as a blank one
    rather than 500ing the whole save."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _save_room_price(db, supplier_room_id, price, as_of):
    """Updates a Room Type's current price cache (supplier_rooms.
    price_per_night/price_as_of) and appends a supplier_room_price_history
    row -- but only when there's actually a new price to record, and only
    when it's genuinely different from what's already on file (comparing
    against the row's OWN current price/as_of, not the form's old values,
    so this stays correct however it's called). A blank price clears the
    cache without logging anything (there's no price to log); an
    unchanged price+date pair is a silent no-op, so re-saving the same
    Room Type without touching pricing doesn't spam the history log with
    identical entries. Zeb, Sept 2026: "Pricing will change over time so
    I need to track history prices in a price history log as well."
    """
    if price is None:
        db.execute(
            "UPDATE supplier_rooms SET price_per_night = NULL, price_as_of = NULL, updated_at = datetime('now') "
            "WHERE supplier_room_id = ? AND tenant_id = ?",
            (supplier_room_id, g.tenant_id),
        )
        db.commit()
        return
    as_of = as_of or date.today().isoformat()
    current = db.execute(
        "SELECT price_per_night, price_as_of FROM supplier_rooms WHERE supplier_room_id = ? AND tenant_id = ?",
        (supplier_room_id, g.tenant_id),
    ).fetchone()
    if current and current["price_per_night"] == price and current["price_as_of"] == as_of:
        return  # nothing actually changed -- don't log a duplicate entry
    db.execute(
        "UPDATE supplier_rooms SET price_per_night = ?, price_as_of = ?, updated_at = datetime('now') "
        "WHERE supplier_room_id = ? AND tenant_id = ?",
        (price, as_of, supplier_room_id, g.tenant_id),
    )
    db.execute(
        "INSERT INTO supplier_room_price_history (tenant_id, supplier_room_id, price_per_night, price_as_of) "
        "VALUES (?, ?, ?, ?)",
        (g.tenant_id, supplier_room_id, price, as_of),
    )
    db.commit()


@suppliers_bp.route("/<int:supplier_id>/rooms/new", methods=["GET", "POST"])
@login_required
def new_supplier_room(supplier_id):
    db = get_db()
    supplier = _get_supplier(db, supplier_id)
    if supplier["template_key"] != "hotel":
        abort(404)
    # One row per Room Type -- Types already on file for this Supplier are
    # excluded from the picker; edit that existing row to change its count
    # instead of adding a duplicate.
    used_type_ids = {
        r["room_type_id"] for r in db.execute(
            "SELECT room_type_id FROM supplier_rooms WHERE supplier_id = ? AND tenant_id = ?",
            (supplier_id, g.tenant_id),
        ).fetchall()
    }
    available_types = [t for t in _hotel_room_types(db, supplier["supplier_type_id"]) if t["room_type_id"] not in used_type_ids]
    if request.method == "POST":
        form = request.form
        room_type_id = form.get("room_type_id") or None
        if not room_type_id:
            flash("Choose a Room Type.", "danger")
            return render_template("suppliers/room_form.html", supplier=supplier, room=None, room_types=available_types)
        db.execute(
            "INSERT INTO supplier_rooms (tenant_id, supplier_id, room_type_id, description, number_of_rooms) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                g.tenant_id, supplier_id, room_type_id,
                form.get("description", "").strip() or None,
                int(form.get("number_of_rooms") or 0),
            ),
        )
        db.commit()
        new_room_id = db.execute("SELECT last_insert_rowid() id").fetchone()["id"]
        _save_room_price(db, new_room_id, _parse_price(form.get("price_per_night")), form.get("price_as_of", "").strip() or None)
        log_action("Create", "supplier_room", supplier_id, "Added a Room Type")
        flash("Room Type added.", "success")
        return redirect(url_for("suppliers.view_supplier", supplier_id=supplier_id))
    return render_template("suppliers/room_form.html", supplier=supplier, room=None, room_types=available_types)


@suppliers_bp.route("/<int:supplier_id>/rooms/<int:room_id>/edit", methods=["GET", "POST"])
@login_required
def edit_supplier_room(supplier_id, room_id):
    db = get_db()
    supplier = _get_supplier(db, supplier_id)
    if supplier["template_key"] != "hotel":
        abort(404)
    room = db.execute(
        """SELECT r.*, rt.label AS room_type_label, rt.description AS default_description
           FROM supplier_rooms r JOIN hotel_room_types rt ON rt.room_type_id = r.room_type_id
           WHERE r.supplier_room_id = ? AND r.supplier_id = ? AND r.tenant_id = ?""",
        (room_id, supplier_id, g.tenant_id),
    ).fetchone()
    if room is None:
        abort(404)
    if request.method == "POST":
        form = request.form
        db.execute(
            "UPDATE supplier_rooms SET description = ?, number_of_rooms = ?, updated_at = datetime('now') "
            "WHERE supplier_room_id = ? AND tenant_id = ?",
            (form.get("description", "").strip() or None, int(form.get("number_of_rooms") or 0), room_id, g.tenant_id),
        )
        db.commit()
        _save_room_price(db, room_id, _parse_price(form.get("price_per_night")), form.get("price_as_of", "").strip() or None)
        log_action("Update", "supplier_room", supplier_id, f"Updated Room Type #{room_id}")
        flash("Room Type updated.", "success")
        return redirect(url_for("suppliers.view_supplier", supplier_id=supplier_id))
    # Editing an existing row keeps its Room Type fixed (shown read-only) -- only description/count/price change.
    return render_template("suppliers/room_form.html", supplier=supplier, room=room, room_types=None)


@suppliers_bp.route("/<int:supplier_id>/rooms/<int:room_id>/delete", methods=["POST"])
@login_required
def delete_supplier_room(supplier_id, room_id):
    db = get_db()
    _get_supplier(db, supplier_id)
    room = db.execute(
        "SELECT * FROM supplier_rooms WHERE supplier_room_id = ? AND supplier_id = ? AND tenant_id = ?",
        (room_id, supplier_id, g.tenant_id),
    ).fetchone()
    if room is None:
        abort(404)
    db.execute("DELETE FROM supplier_rooms WHERE supplier_room_id = ? AND tenant_id = ?", (room_id, g.tenant_id))
    db.commit()
    log_action("Delete", "supplier_room", supplier_id, f"Deleted Room Type #{room_id}")
    flash("Room Type removed.", "success")
    return redirect(url_for("suppliers.view_supplier", supplier_id=supplier_id))


@suppliers_bp.route("/<int:supplier_id>/rooms/<int:room_id>/price-history")
@login_required
def room_price_history(supplier_id, room_id):
    """The "History" link on the Edit Room Type form -- every price this
    Room Type has ever had on file for this Hotel, newest first."""
    db = get_db()
    supplier = _get_supplier(db, supplier_id)
    room = db.execute(
        """SELECT r.*, rt.label AS room_type_label
           FROM supplier_rooms r JOIN hotel_room_types rt ON rt.room_type_id = r.room_type_id
           WHERE r.supplier_room_id = ? AND r.supplier_id = ? AND r.tenant_id = ?""",
        (room_id, supplier_id, g.tenant_id),
    ).fetchone()
    if room is None:
        abort(404)
    history = db.execute(
        "SELECT * FROM supplier_room_price_history WHERE supplier_room_id = ? AND tenant_id = ? "
        "ORDER BY price_as_of DESC, price_history_id DESC",
        (room_id, g.tenant_id),
    ).fetchall()
    return render_template("suppliers/room_price_history.html", supplier=supplier, room=room, history=history)


# --------------------------------------------------- documents, links and images
#
# "Documents, Links and Images" sub-module (MODULE X, Sept 2026) -- per
# Zeb's request, copies of a Supplier's business licenses, permits, rules &
# regulations, agreements and images/photographs. Mirrors the Module D
# Documents & Knowledge Base pattern (documents.py's content/
# content_locations) but scoped to one Supplier, without the Knowledge
# Domains link ("not required for Suppliers") or the Contacts Link feature
# (out of scope for what was asked; Suppliers already has its own Contacts
# sub-module above). Applies to every Supplier Type -- not gated behind
# template_key like Amenities & Facilities/Rooms are.

DOCUMENT_DESCRIPTION_MAX_LENGTH = 200

# The Document Type label that marks a supplier_documents row as belonging
# to the "Images / Photographs" card rather than "Documents and Links" --
# see view_supplier()'s split above and _image_document_type_id() below.
IMAGE_DOCUMENT_TYPE_LABEL = "Image / Photograph"

# Extensions the thumbnail route (image_thumbnail() below) will actually
# serve from a Local Drive Path -- anything else 404s rather than being
# streamed back as if it were an image.
IMAGE_FILE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".heic"}


def _image_document_type_id(db):
    row = db.execute(
        "SELECT document_type_id FROM supplier_document_types WHERE tenant_id = ? AND label = ?",
        (g.tenant_id, IMAGE_DOCUMENT_TYPE_LABEL),
    ).fetchone()
    return row["document_type_id"] if row else None


@suppliers_bp.route("/<int:supplier_id>/documents/new", methods=["GET", "POST"])
@login_required
def new_supplier_document(supplier_id):
    db = get_db()
    supplier = _get_supplier(db, supplier_id)
    if request.method == "POST":
        form = request.form
        document_name = form.get("document_name", "").strip()
        description = form.get("description", "").strip()
        if not document_name:
            flash("Document name is required.", "error")
            return render_template(
                "suppliers/document_form.html", supplier=supplier, item=form, document_id=None,
                keywords=form.get("keywords", ""), hashtags=form.get("hashtags", ""), document_types=_document_types(db),
            )
        if len(description) > DOCUMENT_DESCRIPTION_MAX_LENGTH:
            flash(f"Description must be {DOCUMENT_DESCRIPTION_MAX_LENGTH} characters or fewer (currently {len(description)}).", "error")
            return render_template(
                "suppliers/document_form.html", supplier=supplier, item=form, document_id=None,
                keywords=form.get("keywords", ""), hashtags=form.get("hashtags", ""), document_types=_document_types(db),
            )
        db.execute(
            "INSERT INTO supplier_documents (tenant_id, supplier_id, document_name, document_type_id, authors, description, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                g.tenant_id, supplier_id, document_name, form.get("document_type_id") or None,
                form.get("authors", "").strip() or None, description or None,
                form.get("notes", "").strip() or None,
            ),
        )
        db.commit()
        document_id = db.execute("SELECT last_insert_rowid() id").fetchone()["id"]

        for term in _split_terms(form.get("keywords", "")):
            db.execute(
                "INSERT OR IGNORE INTO supplier_document_keywords (tenant_id, supplier_document_id, term) VALUES (?, ?, ?)",
                (g.tenant_id, document_id, term),
            )
        for term in _split_terms(form.get("hashtags", "")):
            db.execute(
                "INSERT OR IGNORE INTO supplier_document_hashtags (tenant_id, supplier_document_id, term) VALUES (?, ?, ?)",
                (g.tenant_id, document_id, term),
            )
        db.commit()
        upload_error = _save_document_locations_from_form(db, form, document_id)
        if upload_error:
            flash(upload_error, "error")

        log_action("Create", "supplier_document", document_id, f"Added document '{document_name}' to {supplier['supplier_name']}")
        flash("Document added.", "success")
        return redirect(url_for("suppliers.view_supplier_document", supplier_id=supplier_id, document_id=document_id))
    return render_template(
        "suppliers/document_form.html", supplier=supplier, item=None, document_id=None,
        keywords="", hashtags="", document_types=_document_types(db),
    )


@suppliers_bp.route("/<int:supplier_id>/documents/<int:document_id>")
@login_required
def view_supplier_document(supplier_id, document_id):
    db = get_db()
    supplier = _get_supplier(db, supplier_id)
    item = _get_supplier_document(db, supplier_id, document_id)
    locations = db.execute(
        "SELECT * FROM supplier_document_locations WHERE supplier_document_id = ? ORDER BY location_id", (document_id,)
    ).fetchall()
    keywords = db.execute(
        "SELECT term FROM supplier_document_keywords WHERE supplier_document_id = ? ORDER BY term", (document_id,)
    ).fetchall()
    hashtags = db.execute(
        "SELECT term FROM supplier_document_hashtags WHERE supplier_document_id = ? ORDER BY term", (document_id,)
    ).fetchall()
    return render_template(
        "suppliers/document_view.html", supplier=supplier, item=item, locations=locations,
        keywords=keywords, hashtags=hashtags,
    )


@suppliers_bp.route("/<int:supplier_id>/documents/<int:document_id>/edit", methods=["GET", "POST"])
@login_required
def edit_supplier_document(supplier_id, document_id):
    db = get_db()
    supplier = _get_supplier(db, supplier_id)
    item = _get_supplier_document(db, supplier_id, document_id)
    if request.method == "POST":
        form = request.form
        document_name = form.get("document_name", "").strip()
        description = form.get("description", "").strip()
        if not document_name:
            flash("Document name is required.", "error")
            return render_template(
                "suppliers/document_form.html", supplier=supplier, item=form, document_id=document_id,
                keywords=form.get("keywords", ""), hashtags=form.get("hashtags", ""), document_types=_document_types(db),
            )
        if len(description) > DOCUMENT_DESCRIPTION_MAX_LENGTH:
            flash(f"Description must be {DOCUMENT_DESCRIPTION_MAX_LENGTH} characters or fewer (currently {len(description)}).", "error")
            return render_template(
                "suppliers/document_form.html", supplier=supplier, item=form, document_id=document_id,
                keywords=form.get("keywords", ""), hashtags=form.get("hashtags", ""), document_types=_document_types(db),
            )
        db.execute(
            "UPDATE supplier_documents SET document_name=?, document_type_id=?, authors=?, description=?, notes=?, updated_at=datetime('now') "
            "WHERE supplier_document_id=? AND supplier_id=? AND tenant_id=?",
            (
                document_name, form.get("document_type_id") or None, form.get("authors", "").strip() or None,
                description or None, form.get("notes", "").strip() or None, document_id, supplier_id, g.tenant_id,
            ),
        )
        db.execute("DELETE FROM supplier_document_keywords WHERE supplier_document_id = ? AND tenant_id = ?", (document_id, g.tenant_id))
        for term in _split_terms(form.get("keywords", "")):
            db.execute(
                "INSERT OR IGNORE INTO supplier_document_keywords (tenant_id, supplier_document_id, term) VALUES (?, ?, ?)",
                (g.tenant_id, document_id, term),
            )
        db.execute("DELETE FROM supplier_document_hashtags WHERE supplier_document_id = ? AND tenant_id = ?", (document_id, g.tenant_id))
        for term in _split_terms(form.get("hashtags", "")):
            db.execute(
                "INSERT OR IGNORE INTO supplier_document_hashtags (tenant_id, supplier_document_id, term) VALUES (?, ?, ?)",
                (g.tenant_id, document_id, term),
            )
        db.commit()
        upload_error = _save_document_locations_from_form(db, form, document_id)
        if upload_error:
            flash(upload_error, "error")

        log_action("Update", "supplier_document", document_id, f"Updated document '{document_name}'")
        flash("Document updated.", "success")
        return redirect(url_for("suppliers.view_supplier_document", supplier_id=supplier_id, document_id=document_id))

    keywords = ", ".join(r["term"] for r in db.execute("SELECT term FROM supplier_document_keywords WHERE supplier_document_id = ? ORDER BY term", (document_id,)).fetchall())
    hashtags = ", ".join(r["term"] for r in db.execute("SELECT term FROM supplier_document_hashtags WHERE supplier_document_id = ? ORDER BY term", (document_id,)).fetchall())
    return render_template(
        "suppliers/document_form.html", supplier=supplier, item=item, document_id=document_id,
        keywords=keywords, hashtags=hashtags, document_types=_document_types(db),
    )


@suppliers_bp.route("/<int:supplier_id>/documents/<int:document_id>/delete", methods=["POST"])
@login_required
def delete_supplier_document(supplier_id, document_id):
    db = get_db()
    _get_supplier(db, supplier_id)
    item = _get_supplier_document(db, supplier_id, document_id)
    db.execute(
        "UPDATE supplier_documents SET is_deleted = 1, updated_at = datetime('now') WHERE supplier_document_id = ? AND tenant_id = ?",
        (document_id, g.tenant_id),
    )
    db.commit()
    log_action("Delete", "supplier_document", document_id, f"Deleted document '{item['document_name']}'")
    flash("Document deleted.", "success")
    return redirect(url_for("suppliers.view_supplier", supplier_id=supplier_id))


@suppliers_bp.route("/<int:supplier_id>/documents/browse-file")
@login_required
def browse_document_file(supplier_id):
    path, error = pick_file_dialog()
    return jsonify(path=path, error=error)


# Extensions offered by the bulk-import file picker's "Image files" filter.
# "All files" is offered alongside it in the dialog for anything scanned/
# exported under an unusual extension.
IMAGE_FILE_TYPES = [
    ("Image files", "*.jpg *.jpeg *.png *.gif *.bmp *.webp *.tif *.tiff *.heic"),
    ("All files", "*.*"),
]


@suppliers_bp.route("/<int:supplier_id>/documents/bulk-import-images", methods=["GET", "POST"])
@login_required
def bulk_import_images(supplier_id):
    """Bulk-import several Images/Photographs at once (Zeb, Sept 2026: "add
    a bulk import feature for importing images/photographs"; later revised
    to "create an Interim Step before commit table" requiring Image Name,
    Author, Description, Notes and Keywords for EACH image, entered by
    clicking the image and filling in a form/window) -- one native
    multi-select file dialog (see pick_bulk_import_files below), then one
    details pass per picked file (templates/suppliers/bulk_import_images.html's
    modal) before the final Import commits anything. Each row becomes its
    own supplier_documents row (Document Type defaulted to 'Image /
    Photograph', overridable for the whole batch) plus its own Keywords --
    the exact same fields/tables the one-at-a-time Add Document flow above
    writes to, just filled in for several files in one pass instead of one
    page each.

    Zeb, Sept 2026, after Bulk-Importing 47 real images: "convert to the
    model where images are stored in the database" -- a path/URL reference
    is fragile (breaks if the source file moves, is renamed, or is
    deleted; a db backup alone doesn't back up the images), so each picked
    file's bytes are read right here, at import time, and stored directly
    in supplier_document_locations (location_type='Stored in Database') --
    not merely a path pointing back at wherever it happened to be on disk.
    A file that can't be read any more by the time Import is clicked (moved/
    deleted since it was picked, or now permission-denied) is skipped
    rather than aborting the whole batch, and reported back by name."""
    if request.method == "GET":
        # Superseded by Add images -> curation (blueprints/images.py, Oct 2026).
        return redirect(url_for("images.add_images", kind="supplier", owner_id=supplier_id))
    db = get_db()
    supplier = _get_supplier(db, supplier_id)
    if request.method == "POST":
        form = request.form
        paths = form.getlist("paths")
        names = form.getlist("names")
        authors = form.getlist("authors")
        descriptions = form.getlist("descriptions")
        notes_list = form.getlist("notes")
        keywords_list = form.getlist("keywords")
        document_type_id = form.get("document_type_id") or None
        imported = 0
        skipped = []
        uploads = {k: f for k, f in zip(form.getlist("upload_keys"), request.files.getlist("uploads"))}
        for i, path in enumerate(paths):
            path = path.strip()
            name = (names[i] if i < len(names) else "").strip()
            if not path or not name:
                continue
            if path.startswith("upload:"):
                # Chosen in the browser (templates/suppliers/bulk_import_images.html)
                file_bytes, file_name, mime_type, upload_error = read_uploaded_file(
                    uploads.get(path[len("upload:"):]), UPLOAD_IMAGE_EXTENSIONS
                )
                if upload_error or not file_bytes:
                    skipped.append(name)
                    continue
                path = file_name
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
            author = (authors[i] if i < len(authors) else "").strip() or None
            description = (descriptions[i] if i < len(descriptions) else "").strip()[:DOCUMENT_DESCRIPTION_MAX_LENGTH] or None
            row_notes = (notes_list[i] if i < len(notes_list) else "").strip() or None
            db.execute(
                """INSERT INTO supplier_documents
                   (tenant_id, supplier_id, document_name, document_type_id, authors, description, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (g.tenant_id, supplier_id, name, document_type_id, author, description, row_notes),
            )
            new_document_id = db.execute("SELECT last_insert_rowid() id").fetchone()["id"]
            db.execute(
                """INSERT INTO supplier_document_locations
                   (tenant_id, supplier_document_id, location_type, path_or_url, file_data, file_name, mime_type, file_size)
                   VALUES (?, ?, 'Stored in Database', ?, ?, ?, ?, ?)""",
                (g.tenant_id, new_document_id, path, file_bytes, file_name, mime_type, len(file_bytes)),
            )
            row_keywords = keywords_list[i] if i < len(keywords_list) else ""
            for term in _split_terms(row_keywords):
                db.execute(
                    "INSERT OR IGNORE INTO supplier_document_keywords (tenant_id, supplier_document_id, term) VALUES (?, ?, ?)",
                    (g.tenant_id, new_document_id, term),
                )
            imported += 1
        db.commit()
        if imported:
            log_action("Create", "supplier_document", supplier_id, f"Bulk-imported {imported} image(s)/photograph(s) for {supplier['supplier_name']}")
            flash(f"Imported {imported} image{'s' if imported != 1 else ''} — stored in the database.", "success")
        else:
            flash("Nothing was imported — select at least one file first.", "error")
        if skipped:
            flash(
                f"Skipped {len(skipped)} file{'s' if len(skipped) != 1 else ''} that could no longer be read "
                f"(moved, deleted, or not an image file): {', '.join(skipped)}",
                "error",
            )
        return redirect(url_for("suppliers.view_supplier", supplier_id=supplier_id))

    return render_template(
        "suppliers/bulk_import_images.html", supplier=supplier, document_types=_document_types(db),
        default_document_type_id=_image_document_type_id(db),
        description_max_length=DOCUMENT_DESCRIPTION_MAX_LENGTH,
    )


@suppliers_bp.route("/<int:supplier_id>/documents/bulk-import-images/pick")
@login_required
def pick_bulk_import_files(supplier_id):
    paths, error = pick_files_dialog(title="Select Images / Photographs to import", filetypes=IMAGE_FILE_TYPES)
    files = [{"path": p, "name": os.path.splitext(basename(p))[0]} for p in paths]
    return jsonify(files=files, error=error)


# ------------------------------------------------------------- image catalog



@suppliers_bp.route("/<int:supplier_id>/documents/catalog")
@login_required
def image_catalog(supplier_id):
    """The old "Images / Photographs" Catalog list, superseded by the Images
    Catalog album (blueprints/images.py, Oct 2026) -- kept as a redirect so
    old links and bookmarks still land in the right place."""
    _get_supplier(get_db(), supplier_id)
    return redirect(url_for("images.album", kind="supplier", owner_id=supplier_id, view="list", q=request.args.get("q") or None))


@suppliers_bp.route("/<int:supplier_id>/documents/catalog/delete", methods=["POST"])
@login_required
def delete_catalog_images(supplier_id):
    """Select + Delete (single or bulk) from the Image Catalog list -- soft-
    deletes every checked row the same way delete_supplier_document() does
    one at a time, just for however many were checked at once."""
    db = get_db()
    supplier = _get_supplier(db, supplier_id)
    document_ids = [d for d in request.form.getlist("document_ids") if d.strip()]
    deleted = 0
    for document_id in document_ids:
        row = db.execute(
            "SELECT supplier_document_id FROM supplier_documents WHERE supplier_document_id = ? AND supplier_id = ? AND tenant_id = ? AND is_deleted = 0",
            (document_id, supplier_id, g.tenant_id),
        ).fetchone()
        if row is None:
            continue
        db.execute(
            "UPDATE supplier_documents SET is_deleted = 1, updated_at = datetime('now') WHERE supplier_document_id = ? AND tenant_id = ?",
            (document_id, g.tenant_id),
        )
        deleted += 1
    db.commit()
    if deleted:
        log_action("Delete", "supplier_document", supplier_id, f"Deleted {deleted} image(s) from catalog for {supplier['supplier_name']}")
        flash(f"Deleted {deleted} image{'s' if deleted != 1 else ''}.", "success")
    else:
        flash("Nothing was deleted — select at least one image first.", "error")
    return redirect(url_for("suppliers.image_catalog", supplier_id=supplier_id))


@suppliers_bp.route("/<int:supplier_id>/documents/<int:document_id>/thumbnail")
@login_required
def image_thumbnail(supplier_id, document_id):
    """Serves (or redirects to) an image document's first Location, for the
    Catalog/card Thumbnail column.

    A 'Stored in Database' location (every image Bulk-Imported since the
    Sept 2026 "store images in the database" change) is served straight
    from its file_data BLOB -- no disk access, no dependence on the
    original file still existing where it was picked. The two older
    reference-only kinds are still honored for anything imported before
    that change: a Local Drive Path is read straight off this machine's
    disk (TMS runs locally, right where those files live) and streamed
    back only if it looks like an image file; a Cloud Link is just
    redirected to and left to the browser. No location, an inaccessible/
    non-image local file, or a document that isn't this tenant's/
    supplier's 404s -- the <img> tag's onerror swaps in a generic
    placeholder icon rather than showing a broken image."""
    db = get_db()
    doc = db.execute(
        "SELECT supplier_document_id FROM supplier_documents WHERE supplier_document_id = ? AND supplier_id = ? AND tenant_id = ? AND is_deleted = 0",
        (document_id, supplier_id, g.tenant_id),
    ).fetchone()
    if doc is None:
        abort(404)
    location = db.execute(
        "SELECT * FROM supplier_document_locations WHERE supplier_document_id = ? AND tenant_id = ? ORDER BY location_id LIMIT 1",
        (document_id, g.tenant_id),
    ).fetchone()
    if location is None:
        abort(404)
    if location["location_type"] == "Stored in Database":
        if not location["file_data"]:
            abort(404)
        return send_file(
            BytesIO(location["file_data"]),
            mimetype=location["mime_type"] or "application/octet-stream",
            download_name=location["file_name"] or "image",
        )
    if location["location_type"] == "Cloud Link":
        return redirect(location["path_or_url"])
    path = location["path_or_url"]
    if not path or os.path.splitext(path)[1].lower() not in IMAGE_FILE_EXTENSIONS or not os.path.isfile(path):
        abort(404)
    return send_file(path)


@suppliers_bp.route("/<int:supplier_id>/documents/bulk-import-images/preview")
@login_required
def preview_bulk_import_file(supplier_id):
    """Live thumbnail preview for the Bulk Import "Image Details" modal --
    Zeb, Sept 2026: "to be able to add a image name/description and
    keyword I need to see a decent sized thumbnail on the Image Details
    Form." At the point that modal is open, the picked file isn't a
    supplier_documents row yet (Import hasn't been clicked), so there's
    nothing in the database yet to serve a thumbnail from -- this instead
    reads the file directly off disk, straight from the Local Drive Path
    the native file-picker dialog itself returned a moment earlier (see
    pick_bulk_import_files below), the same access pick_files_dialog and
    image_thumbnail already rely on for a locally-run app. Only serves a
    path that looks like one of the offered image extensions and actually
    exists as a file -- anything else 404s, same guard as image_thumbnail
    uses for a Local Drive Path."""
    path = request.args.get("path", "")
    if not path or os.path.splitext(path)[1].lower() not in IMAGE_FILE_EXTENSIONS or not os.path.isfile(path):
        abort(404)
    return send_file(path)


@suppliers_bp.route("/<int:supplier_id>/documents/<int:document_id>/locations/new", methods=["GET", "POST"])
@login_required
def new_document_location(supplier_id, document_id):
    db = get_db()
    supplier = _get_supplier(db, supplier_id)
    item = _get_supplier_document(db, supplier_id, document_id)
    if request.method == "POST":
        form = request.form
        if form.get("location_type") == "Stored in Database":
            data, file_name, mime_type, error = read_uploaded_file(request.files.get("upload_file"))
            if error or not data:
                flash(error or "Choose a file to upload.", "error")
                return render_template("suppliers/document_location_form.html", supplier=supplier, item=item)
            db.execute(
                """INSERT INTO supplier_document_locations
                   (tenant_id, supplier_document_id, location_type, path_or_url, file_data, file_name, mime_type, file_size)
                   VALUES (?, ?, 'Stored in Database', ?, ?, ?, ?, ?)""",
                (g.tenant_id, document_id, file_name, data, file_name, mime_type, len(data)),
            )
        else:
            path_or_url = form.get("path_or_url", "").strip()
            if not path_or_url:
                flash("Enter the link.", "error")
                return render_template("suppliers/document_location_form.html", supplier=supplier, item=item)
            db.execute(
                "INSERT INTO supplier_document_locations (tenant_id, supplier_document_id, location_type, path_or_url) VALUES (?, ?, ?, ?)",
                (g.tenant_id, document_id, form["location_type"], path_or_url),
            )
        db.commit()
        log_action("Create", "supplier_document_location", document_id, "Added location")
        return redirect(url_for("suppliers.view_supplier_document", supplier_id=supplier_id, document_id=document_id))
    return render_template("suppliers/document_location_form.html", supplier=supplier, item=item)


@suppliers_bp.route("/<int:supplier_id>/documents/<int:document_id>/locations/<int:location_id>/delete", methods=["POST"])
@login_required
def delete_document_location(supplier_id, document_id, location_id):
    db = get_db()
    _get_supplier(db, supplier_id)
    _get_supplier_document(db, supplier_id, document_id)
    db.execute(
        "DELETE FROM supplier_document_locations WHERE location_id = ? AND supplier_document_id = ? AND tenant_id = ?",
        (location_id, document_id, g.tenant_id),
    )
    db.commit()
    log_action("Delete", "supplier_document_location", document_id, f"Deleted location #{location_id}")
    return redirect(url_for("suppliers.view_supplier_document", supplier_id=supplier_id, document_id=document_id))


@suppliers_bp.route("/<int:supplier_id>/documents/<int:document_id>/locations/<int:location_id>/file")
@login_required
def document_location_file(supplier_id, document_id, location_id):
    """Opens (in the browser) or downloads a file stored in the database --
    works the same locally and on the hosted app. ?download=1 forces a
    download instead of opening it in a new tab."""
    db = get_db()
    _get_supplier(db, supplier_id)
    _get_supplier_document(db, supplier_id, document_id)
    loc = db.execute(
        "SELECT * FROM supplier_document_locations WHERE location_id = ? AND supplier_document_id = ? AND tenant_id = ?",
        (location_id, document_id, g.tenant_id),
    ).fetchone()
    if loc is None or loc["location_type"] != "Stored in Database" or not loc["file_data"]:
        abort(404)
    return send_file(
        BytesIO(loc["file_data"]),
        mimetype=loc["mime_type"] or "application/octet-stream",
        download_name=loc["file_name"] or "document",
        as_attachment=bool(request.args.get("download")),
    )


@suppliers_bp.route("/<int:supplier_id>/documents/<int:document_id>/locations/<int:location_id>/open")
@login_required
def open_document_location(supplier_id, document_id, location_id):
    db = get_db()
    _get_supplier(db, supplier_id)
    loc = db.execute(
        "SELECT * FROM supplier_document_locations WHERE location_id = ? AND supplier_document_id = ? AND tenant_id = ?",
        (location_id, document_id, g.tenant_id),
    ).fetchone()
    if loc is None:
        return jsonify(ok=False, error="Location not found."), 404
    if loc["location_type"] != "Local Drive Path":
        return jsonify(ok=True)
    path = loc["path_or_url"]
    if not os.path.exists(path):
        return jsonify(ok=False, error=f"File not found on disk: {path}")
    try:
        open_local_path(path)
    except Exception as e:
        return jsonify(ok=False, error=f"Couldn't open the file: {e}")
    log_action("Open", "supplier_document_location", document_id, f"Opened {path}")
    return jsonify(ok=True)


# ---------------------------------------------------------------- contacts

@suppliers_bp.route("/<int:supplier_id>/contacts/new", methods=["GET", "POST"])
@login_required
def new_supplier_contact(supplier_id):
    db = get_db()
    supplier = _get_supplier(db, supplier_id)
    addresses = db.execute(
        "SELECT supplier_address_id, street, unit FROM supplier_addresses WHERE supplier_id = ? AND tenant_id = ? ORDER BY is_primary DESC",
        (supplier_id, g.tenant_id),
    ).fetchall()
    if request.method == "POST":
        form = request.form
        name = form.get("name", "").strip()
        if not name:
            flash("Contact name is required.", "error")
            return render_template("suppliers/contact_form.html", supplier=supplier, contact=None, addresses=addresses)
        db.execute(
            """INSERT INTO supplier_contacts (tenant_id, supplier_id, name, title, supplier_address_id, email, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                g.tenant_id, supplier_id, name, form.get("title", "").strip() or None,
                form.get("supplier_address_id") or None, form.get("email", "").strip() or None,
                form.get("notes", "").strip() or None,
            ),
        )
        db.commit()
        log_action("Create", "supplier_contact", supplier_id, f"Added supplier contact {name}")
        flash("Contact added.", "success")
        return redirect(url_for("suppliers.view_supplier", supplier_id=supplier_id))
    return render_template("suppliers/contact_form.html", supplier=supplier, contact=None, addresses=addresses)


@suppliers_bp.route("/<int:supplier_id>/contacts/<int:contact_id>/edit", methods=["GET", "POST"])
@login_required
def edit_supplier_contact(supplier_id, contact_id):
    db = get_db()
    supplier = _get_supplier(db, supplier_id)
    contact = db.execute(
        "SELECT * FROM supplier_contacts WHERE supplier_contact_id = ? AND supplier_id = ? AND is_deleted = 0 AND tenant_id = ?",
        (contact_id, supplier_id, g.tenant_id),
    ).fetchone()
    if contact is None:
        abort(404)
    addresses = db.execute(
        "SELECT supplier_address_id, street, unit FROM supplier_addresses WHERE supplier_id = ? AND tenant_id = ? ORDER BY is_primary DESC",
        (supplier_id, g.tenant_id),
    ).fetchall()
    if request.method == "POST":
        form = request.form
        name = form.get("name", "").strip()
        if not name:
            flash("Contact name is required.", "error")
            return render_template("suppliers/contact_form.html", supplier=supplier, contact=contact, addresses=addresses)
        db.execute(
            """UPDATE supplier_contacts SET name=?, title=?, supplier_address_id=?, email=?, notes=?,
               updated_at=datetime('now') WHERE supplier_contact_id=? AND tenant_id=?""",
            (
                name, form.get("title", "").strip() or None, form.get("supplier_address_id") or None,
                form.get("email", "").strip() or None, form.get("notes", "").strip() or None,
                contact_id, g.tenant_id,
            ),
        )
        db.commit()
        log_action("Update", "supplier_contact", supplier_id, f"Updated supplier contact {name}")
        flash("Contact updated.", "success")
        return redirect(url_for("suppliers.view_supplier", supplier_id=supplier_id))
    return render_template("suppliers/contact_form.html", supplier=supplier, contact=contact, addresses=addresses)


@suppliers_bp.route("/<int:supplier_id>/contacts/<int:contact_id>/delete", methods=["POST"])
@login_required
def delete_supplier_contact(supplier_id, contact_id):
    db = get_db()
    _get_supplier(db, supplier_id)
    contact = db.execute(
        "SELECT * FROM supplier_contacts WHERE supplier_contact_id = ? AND supplier_id = ? AND is_deleted = 0 AND tenant_id = ?",
        (contact_id, supplier_id, g.tenant_id),
    ).fetchone()
    if contact is None:
        abort(404)
    db.execute(
        "UPDATE supplier_contacts SET is_deleted = 1, updated_at = datetime('now') WHERE supplier_contact_id = ? AND tenant_id = ?",
        (contact_id, g.tenant_id),
    )
    db.commit()
    log_action("Delete", "supplier_contact", supplier_id, f"Deleted supplier contact {contact['name']}")
    flash("Contact deleted.", "success")
    return redirect(url_for("suppliers.view_supplier", supplier_id=supplier_id))


# ------------------------------------------------------------ contact phones

@suppliers_bp.route("/<int:supplier_id>/contacts/<int:contact_id>/phones/new", methods=["GET", "POST"])
@login_required
def new_contact_phone(supplier_id, contact_id):
    db = get_db()
    supplier = _get_supplier(db, supplier_id)
    contact = db.execute(
        "SELECT * FROM supplier_contacts WHERE supplier_contact_id = ? AND supplier_id = ? AND is_deleted = 0 AND tenant_id = ?",
        (contact_id, supplier_id, g.tenant_id),
    ).fetchone()
    if contact is None:
        abort(404)
    if request.method == "POST":
        form = request.form
        if not form.get("number", "").strip():
            flash("Number is required.", "error")
            return render_template(
                "suppliers/phone_form.html", supplier=supplier, contact=contact, phone=None, phone_types=_phone_types(db)
            )
        if form.get("is_primary"):
            db.execute(
                "UPDATE supplier_contact_phones SET is_primary = 0 WHERE supplier_contact_id = ? AND tenant_id = ?",
                (contact_id, g.tenant_id),
            )
        db.execute(
            """INSERT INTO supplier_contact_phones
               (tenant_id, supplier_contact_id, phone_type_id, country_code, area_code, number, extension, is_primary)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                g.tenant_id, contact_id, form.get("phone_type_id") or None,
                form.get("country_code", "").strip() or None, form.get("area_code", "").strip() or None,
                form["number"].strip(), form.get("extension", "").strip() or None,
                1 if form.get("is_primary") else 0,
            ),
        )
        db.commit()
        log_action("Create", "supplier_contact_phone", contact_id, "Added supplier contact phone")
        flash("Phone added.", "success")
        return redirect(url_for("suppliers.view_supplier", supplier_id=supplier_id))
    return render_template(
        "suppliers/phone_form.html", supplier=supplier, contact=contact, phone=None, phone_types=_phone_types(db)
    )


@suppliers_bp.route("/<int:supplier_id>/contacts/<int:contact_id>/phones/<int:phone_id>/edit", methods=["GET", "POST"])
@login_required
def edit_contact_phone(supplier_id, contact_id, phone_id):
    db = get_db()
    supplier = _get_supplier(db, supplier_id)
    contact = db.execute(
        "SELECT * FROM supplier_contacts WHERE supplier_contact_id = ? AND supplier_id = ? AND is_deleted = 0 AND tenant_id = ?",
        (contact_id, supplier_id, g.tenant_id),
    ).fetchone()
    if contact is None:
        abort(404)
    phone = db.execute(
        "SELECT * FROM supplier_contact_phones WHERE phone_id = ? AND supplier_contact_id = ? AND tenant_id = ?",
        (phone_id, contact_id, g.tenant_id),
    ).fetchone()
    if phone is None:
        abort(404)
    if request.method == "POST":
        form = request.form
        if not form.get("number", "").strip():
            flash("Number is required.", "error")
            return render_template(
                "suppliers/phone_form.html", supplier=supplier, contact=contact, phone=phone, phone_types=_phone_types(db)
            )
        if form.get("is_primary"):
            db.execute(
                "UPDATE supplier_contact_phones SET is_primary = 0 WHERE supplier_contact_id = ? AND tenant_id = ?",
                (contact_id, g.tenant_id),
            )
        db.execute(
            """UPDATE supplier_contact_phones SET phone_type_id=?, country_code=?, area_code=?, number=?,
               extension=?, is_primary=?, updated_at=datetime('now') WHERE phone_id=? AND tenant_id=?""",
            (
                form.get("phone_type_id") or None, form.get("country_code", "").strip() or None,
                form.get("area_code", "").strip() or None, form["number"].strip(),
                form.get("extension", "").strip() or None, 1 if form.get("is_primary") else 0,
                phone_id, g.tenant_id,
            ),
        )
        db.commit()
        log_action("Update", "supplier_contact_phone", contact_id, f"Updated supplier contact phone #{phone_id}")
        flash("Phone updated.", "success")
        return redirect(url_for("suppliers.view_supplier", supplier_id=supplier_id))
    return render_template(
        "suppliers/phone_form.html", supplier=supplier, contact=contact, phone=phone, phone_types=_phone_types(db)
    )


@suppliers_bp.route("/<int:supplier_id>/contacts/<int:contact_id>/phones/<int:phone_id>/delete", methods=["POST"])
@login_required
def delete_contact_phone(supplier_id, contact_id, phone_id):
    db = get_db()
    _get_supplier(db, supplier_id)
    phone = db.execute(
        "SELECT * FROM supplier_contact_phones WHERE phone_id = ? AND supplier_contact_id = ? AND tenant_id = ?",
        (phone_id, contact_id, g.tenant_id),
    ).fetchone()
    if phone is None:
        abort(404)
    db.execute("DELETE FROM supplier_contact_phones WHERE phone_id = ? AND tenant_id = ?", (phone_id, g.tenant_id))
    db.commit()
    log_action("Delete", "supplier_contact_phone", contact_id, f"Deleted supplier contact phone #{phone_id}")
    flash("Phone deleted.", "success")
    return redirect(url_for("suppliers.view_supplier", supplier_id=supplier_id))


# ------------------------------------------------------- resource emails
#
# A supplier can have more than one email of its own. Used by the External
# Resource view (mirrors organizations.py's organization_emails CRUD
# exactly, against supplier_emails instead) — not offered on an ordinary
# supplier's page, which keeps its single email on its supplier_contacts
# "contact person" sub-record instead.

@suppliers_bp.route("/<int:supplier_id>/emails/new", methods=["GET", "POST"])
@login_required
def new_supplier_email(supplier_id):
    db = get_db()
    supplier = _get_supplier(db, supplier_id)
    if request.method == "POST":
        form = request.form
        if form.get("is_primary"):
            db.execute(
                "UPDATE supplier_emails SET is_primary = 0 WHERE supplier_id = ? AND tenant_id = ?",
                (supplier_id, g.tenant_id),
            )
        db.execute(
            "INSERT INTO supplier_emails (tenant_id, supplier_id, email_address, is_primary) VALUES (?, ?, ?, ?)",
            (g.tenant_id, supplier_id, form["email_address"].strip(), 1 if form.get("is_primary") else 0),
        )
        db.commit()
        log_action("Create", "supplier_email", supplier_id, f"Added email {form['email_address']}")
        flash("Email added.", "success")
        return redirect(url_for("suppliers.view_supplier", supplier_id=supplier_id))
    return render_template("suppliers/resource_email_form.html", supplier=supplier, email=None)


@suppliers_bp.route("/<int:supplier_id>/emails/<int:email_id>/edit", methods=["GET", "POST"])
@login_required
def edit_supplier_email(supplier_id, email_id):
    db = get_db()
    supplier = _get_supplier(db, supplier_id)
    email = db.execute(
        "SELECT * FROM supplier_emails WHERE supplier_email_id = ? AND supplier_id = ? AND tenant_id = ?",
        (email_id, supplier_id, g.tenant_id),
    ).fetchone()
    if email is None:
        abort(404)
    if request.method == "POST":
        form = request.form
        if form.get("is_primary"):
            db.execute(
                "UPDATE supplier_emails SET is_primary = 0 WHERE supplier_id = ? AND tenant_id = ?",
                (supplier_id, g.tenant_id),
            )
        db.execute(
            "UPDATE supplier_emails SET email_address = ?, is_primary = ?, updated_at = datetime('now') "
            "WHERE supplier_email_id = ? AND tenant_id = ?",
            (form["email_address"].strip(), 1 if form.get("is_primary") else 0, email_id, g.tenant_id),
        )
        db.commit()
        log_action("Update", "supplier_email", supplier_id, f"Updated email #{email_id}")
        flash("Email updated.", "success")
        return redirect(url_for("suppliers.view_supplier", supplier_id=supplier_id))
    return render_template("suppliers/resource_email_form.html", supplier=supplier, email=email)


@suppliers_bp.route("/<int:supplier_id>/emails/<int:email_id>/delete", methods=["POST"])
@login_required
def delete_supplier_email(supplier_id, email_id):
    db = get_db()
    _get_supplier(db, supplier_id)
    email = db.execute(
        "SELECT * FROM supplier_emails WHERE supplier_email_id = ? AND supplier_id = ? AND tenant_id = ?",
        (email_id, supplier_id, g.tenant_id),
    ).fetchone()
    if email is None:
        abort(404)
    db.execute(
        "DELETE FROM supplier_emails WHERE supplier_email_id = ? AND tenant_id = ?", (email_id, g.tenant_id)
    )
    db.commit()
    log_action("Delete", "supplier_email", supplier_id, f"Deleted email #{email_id}")
    flash("Email deleted.", "success")
    return redirect(url_for("suppliers.view_supplier", supplier_id=supplier_id))


# ------------------------------------------------------- resource phones
#
# A supplier can have more than one phone number of its own (Cell, Office,
# Fax, ... — reuses the same phone_types lookup as supplier_contact_phones
# above). Same External-Resource-only usage note as resource emails above.

@suppliers_bp.route("/<int:supplier_id>/phones/new", methods=["GET", "POST"])
@login_required
def new_supplier_phone(supplier_id):
    db = get_db()
    supplier = _get_supplier(db, supplier_id)
    if request.method == "POST":
        form = request.form
        if not form.get("number", "").strip():
            flash("Number is required.", "error")
            return render_template(
                "suppliers/resource_phone_form.html", supplier=supplier, phone=None, phone_types=_phone_types(db)
            )
        if form.get("is_primary"):
            db.execute(
                "UPDATE supplier_phones SET is_primary = 0 WHERE supplier_id = ? AND tenant_id = ?",
                (supplier_id, g.tenant_id),
            )
        db.execute(
            """INSERT INTO supplier_phones
               (tenant_id, supplier_id, phone_type_id, country_code, area_code, number, extension, is_primary)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                g.tenant_id, supplier_id, form.get("phone_type_id") or None,
                form.get("country_code", "").strip() or None, form.get("area_code", "").strip() or None,
                form["number"].strip(), form.get("extension", "").strip() or None,
                1 if form.get("is_primary") else 0,
            ),
        )
        db.commit()
        log_action("Create", "supplier_phone", supplier_id, "Added supplier phone")
        flash("Phone added.", "success")
        return redirect(url_for("suppliers.view_supplier", supplier_id=supplier_id))
    return render_template(
        "suppliers/resource_phone_form.html", supplier=supplier, phone=None, phone_types=_phone_types(db)
    )


@suppliers_bp.route("/<int:supplier_id>/phones/<int:phone_id>/edit", methods=["GET", "POST"])
@login_required
def edit_supplier_phone(supplier_id, phone_id):
    db = get_db()
    supplier = _get_supplier(db, supplier_id)
    phone = db.execute(
        "SELECT * FROM supplier_phones WHERE supplier_phone_id = ? AND supplier_id = ? AND tenant_id = ?",
        (phone_id, supplier_id, g.tenant_id),
    ).fetchone()
    if phone is None:
        abort(404)
    if request.method == "POST":
        form = request.form
        if not form.get("number", "").strip():
            flash("Number is required.", "error")
            return render_template(
                "suppliers/resource_phone_form.html", supplier=supplier, phone=phone, phone_types=_phone_types(db)
            )
        if form.get("is_primary"):
            db.execute(
                "UPDATE supplier_phones SET is_primary = 0 WHERE supplier_id = ? AND tenant_id = ?",
                (supplier_id, g.tenant_id),
            )
        db.execute(
            """UPDATE supplier_phones SET phone_type_id=?, country_code=?, area_code=?, number=?, extension=?,
               is_primary=?, updated_at=datetime('now') WHERE supplier_phone_id=? AND tenant_id=?""",
            (
                form.get("phone_type_id") or None, form.get("country_code", "").strip() or None,
                form.get("area_code", "").strip() or None, form["number"].strip(),
                form.get("extension", "").strip() or None, 1 if form.get("is_primary") else 0,
                phone_id, g.tenant_id,
            ),
        )
        db.commit()
        log_action("Update", "supplier_phone", supplier_id, f"Updated supplier phone #{phone_id}")
        flash("Phone updated.", "success")
        return redirect(url_for("suppliers.view_supplier", supplier_id=supplier_id))
    return render_template(
        "suppliers/resource_phone_form.html", supplier=supplier, phone=phone, phone_types=_phone_types(db)
    )


@suppliers_bp.route("/<int:supplier_id>/phones/<int:phone_id>/delete", methods=["POST"])
@login_required
def delete_supplier_phone(supplier_id, phone_id):
    db = get_db()
    _get_supplier(db, supplier_id)
    phone = db.execute(
        "SELECT * FROM supplier_phones WHERE supplier_phone_id = ? AND supplier_id = ? AND tenant_id = ?",
        (phone_id, supplier_id, g.tenant_id),
    ).fetchone()
    if phone is None:
        abort(404)
    db.execute("DELETE FROM supplier_phones WHERE supplier_phone_id = ? AND tenant_id = ?", (phone_id, g.tenant_id))
    db.commit()
    log_action("Delete", "supplier_phone", supplier_id, f"Deleted supplier phone #{phone_id}")
    flash("Phone deleted.", "success")
    return redirect(url_for("suppliers.view_supplier", supplier_id=supplier_id))


# -------------------------------------------------- resource reference links
#
# "Documents Link" feature on a supplier's own record — a web URL and/or a
# local document path, each with a short description/notes. Mirrors
# organizations.py's organization_reference_links CRUD exactly, against
# supplier_reference_links instead. Same External-Resource-only usage note
# as resource emails above.

@suppliers_bp.route("/<int:supplier_id>/links/new", methods=["GET", "POST"])
@login_required
def new_supplier_link(supplier_id):
    db = get_db()
    supplier = _get_supplier(db, supplier_id)
    if request.method == "POST":
        form = request.form
        db.execute(
            "INSERT INTO supplier_reference_links (tenant_id, supplier_id, url, document_path, description, notes) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                g.tenant_id, supplier_id, form.get("url", "").strip() or None, form.get("document_path", "").strip() or None,
                form.get("description", "").strip() or None, form.get("notes", "").strip() or None,
            ),
        )
        db.commit()
        log_action("Create", "supplier_reference_link", supplier_id, "Added reference link")
        flash("Reference link added.", "success")
        return redirect(url_for("suppliers.view_supplier", supplier_id=supplier_id))
    return render_template("suppliers/resource_link_form.html", supplier=supplier, link=None)


@suppliers_bp.route("/<int:supplier_id>/links/<int:link_id>/edit", methods=["GET", "POST"])
@login_required
def edit_supplier_link(supplier_id, link_id):
    db = get_db()
    supplier = _get_supplier(db, supplier_id)
    link = db.execute(
        "SELECT * FROM supplier_reference_links WHERE link_id = ? AND supplier_id = ? AND tenant_id = ?",
        (link_id, supplier_id, g.tenant_id),
    ).fetchone()
    if link is None:
        abort(404)
    if request.method == "POST":
        form = request.form
        db.execute(
            "UPDATE supplier_reference_links SET url = ?, document_path = ?, description = ?, notes = ? "
            "WHERE link_id = ? AND supplier_id = ? AND tenant_id = ?",
            (
                form.get("url", "").strip() or None, form.get("document_path", "").strip() or None,
                form.get("description", "").strip() or None, form.get("notes", "").strip() or None,
                link_id, supplier_id, g.tenant_id,
            ),
        )
        db.commit()
        log_action("Update", "supplier_reference_link", supplier_id, f"Updated reference link #{link_id}")
        flash("Reference link updated.", "success")
        return redirect(url_for("suppliers.view_supplier", supplier_id=supplier_id))
    return render_template("suppliers/resource_link_form.html", supplier=supplier, link=link)


@suppliers_bp.route("/<int:supplier_id>/links/<int:link_id>/delete", methods=["POST"])
@login_required
def delete_supplier_link(supplier_id, link_id):
    db = get_db()
    _get_supplier(db, supplier_id)
    db.execute(
        "DELETE FROM supplier_reference_links WHERE link_id = ? AND supplier_id = ? AND tenant_id = ?",
        (link_id, supplier_id, g.tenant_id),
    )
    db.commit()
    log_action("Delete", "supplier_reference_link", supplier_id, f"Deleted reference link #{link_id}")
    flash("Reference link deleted.", "success")
    return redirect(url_for("suppliers.view_supplier", supplier_id=supplier_id))


@suppliers_bp.route("/<int:supplier_id>/links/<int:link_id>/open")
@login_required
def open_supplier_link(supplier_id, link_id):
    db = get_db()
    link = db.execute(
        "SELECT * FROM supplier_reference_links WHERE link_id = ? AND supplier_id = ? AND tenant_id = ?",
        (link_id, supplier_id, g.tenant_id),
    ).fetchone()
    if link is None:
        return jsonify(ok=False, error="Reference link not found."), 404
    path = link["document_path"]
    if not path:
        return jsonify(ok=False, error="No document path on this reference link.")
    if not os.path.exists(path):
        return jsonify(ok=False, error=f"File not found on disk: {path}")
    try:
        open_local_path(path)
    except Exception as e:
        return jsonify(ok=False, error=f"Couldn't open the file: {e}")
    log_action("Open", "supplier_reference_link", supplier_id, f"Opened {path}")
    return jsonify(ok=True)
