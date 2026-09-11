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
import os

from flask import Blueprint, abort, flash, g, jsonify, redirect, render_template, request, url_for

from auth.decorators import login_required
from db import get_db, log_action
from utils import open_local_path

suppliers_bp = Blueprint("suppliers", __name__)


def _supplier_types(db):
    return db.execute(
        "SELECT * FROM supplier_types WHERE is_active = 1 AND tenant_id = ? ORDER BY label COLLATE NOCASE", (g.tenant_id,)
    ).fetchall()


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
        """SELECT s.*, t.label AS type_label, t.template_key AS template_key, st.label AS subtype_label
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


# ---------------------------------------------------------------- list/view

@suppliers_bp.route("/")
@login_required
def list_suppliers():
    db = get_db()
    q = request.args.get("q", "").strip()
    is_external_resource = request.args.get("is_external_resource", "").strip()
    type_id = request.args.get("type_id", "").strip()
    subtype_id = request.args.get("subtype_id", "").strip()
    city = request.args.get("city", "").strip()
    preference = request.args.get("preference", "").strip()
    sql = """
        SELECT s.*, t.label AS type_label, st.label AS subtype_label,
               COALESCE(pc.label, pa.city_text) AS city_label,
               COALESCE(ps.label, pa.state_province_text) AS state_label,
               pco.label AS country_label
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
    if q:
        sql += " AND s.supplier_name LIKE ?"
        params.append(f"%{q}%")
    if is_external_resource:
        sql += " AND s.is_external_resource = 1"
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
        type_id=type_id, subtype_id=subtype_id, city=city, preference=preference,
        supplier_types=_supplier_types(db), subtypes_json=_subtypes_for_type_json(db),
        city_options=_city_options(db),
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

    return render_template(
        "suppliers/view.html", supplier=supplier, addresses=addresses, contacts=contacts, kg_edges=kg_edges,
        hotel_amenities_by_category=hotel_amenities_by_category, hotel_rooms=hotel_rooms, hotel_room_total=hotel_room_total,
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
                "suppliers/form.html", supplier=None, supplier_types=_supplier_types(db),
                subtypes_json=_subtypes_for_type_json(db),
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
        flash("Supplier created.", "success")
        return redirect(url_for("suppliers.view_supplier", supplier_id=supplier_id))
    return render_template(
        "suppliers/form.html", supplier=None, supplier_types=_supplier_types(db),
        subtypes_json=_subtypes_for_type_json(db),
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
                "suppliers/resource_form.html", supplier=None, supplier_types=_supplier_types(db),
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
        "suppliers/resource_form.html", supplier=None, supplier_types=_supplier_types(db),
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
                    "suppliers/resource_form.html", supplier=supplier, supplier_types=_supplier_types(db),
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
            flash("External Resource updated.", "success")
            return redirect(url_for("suppliers.view_supplier", supplier_id=supplier_id))
        return render_template(
            "suppliers/resource_form.html", supplier=supplier, supplier_types=_supplier_types(db),
            subtypes_json=_subtypes_for_type_json(db),
        )

    if request.method == "POST":
        f = _form_fields(request.form)
        if not f["supplier_name"]:
            flash("Supplier name is required.", "error")
            return render_template(
                "suppliers/form.html", supplier=supplier, supplier_types=_supplier_types(db),
                subtypes_json=_subtypes_for_type_json(db),
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
        flash("Supplier updated.", "success")
        return redirect(url_for("suppliers.view_supplier", supplier_id=supplier_id))
    return render_template(
        "suppliers/form.html", supplier=supplier, supplier_types=_supplier_types(db),
        subtypes_json=_subtypes_for_type_json(db),
    )


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
    return redirect(url_for("suppliers.list_suppliers"))


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
        log_action("Update", "supplier_room", supplier_id, f"Updated Room Type #{room_id}")
        flash("Room Type updated.", "success")
        return redirect(url_for("suppliers.view_supplier", supplier_id=supplier_id))
    # Editing an existing row keeps its Room Type fixed (shown read-only) -- only description/count change.
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
