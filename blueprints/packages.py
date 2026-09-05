"""
Module — Package Management & Itinerary Builder. Tenant-scoped tour
package templates, each assembled from a Country->City route, a
day-by-day itinerary of Points of Interest, priceable components sourced
from Services/Products/Suppliers, and per-group-size price tiers.

Built from the "Next-Phase Blueprint" roadmap (published as an Artifact
this session) per the request:

    "Please start with Package Management."

This is the load-bearing gap the roadmap identified: Departures, Bookings,
and Invoicing all need a Package to attach to, and nothing else in TTMS
provides that. It absorbs what the source planning docs called "Phase 1:
Product Design" AND "Phase 2: Costing & Pricing" into one module, because
costing here is a property of each package_components row (unit_cost/
unit_price), not a separate stage — Products already carries price/cost.

Unlike Services/Products (blueprints/services.py, blueprints/products.py),
there is no GLOBAL taxonomy layer here — every table is tenant-scoped, and
package_code is a free-text reference the tenant chooses themselves (e.g.
'MC-BOS-PK-14D'), not a generated Category-Group-Subgroup-Sequence code.
What IS shared/coded (Services, Products, Suppliers, Points of Interest,
Geography) gets referenced by FK from package_components and
package_route_stops/package_day_pois, but this module writes no rows to
any of those tables.

Route shape, mirroring the parent/child pattern established in
blueprints/organizations.py and blueprints/suppliers.py (addresses,
emails, phones, links each as their own new/edit/delete triple against
the parent's id): the package master gets the usual
list/view/new/edit/delete(archive)/restore set, and each of its five
child collections (route stops, days, day POIs, components, price tiers)
gets its own new/edit/delete routes, each redirecting back to the
package's view page rather than using modals or client-side reordering —
sequence_number is a plain integer field the person sets directly, same
"no drag-and-drop JS" convention as the rest of this app.

"Delete" on a package archives it (status='archived'), same never-destroy
philosophy as Services/Products — restore_package() reverses it. Child
rows (route stops, days, etc.) are hard-deleted, since they're structural
detail of a package still being drafted, not standalone records anyone
would want to recover independently of the package itself.
"""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from auth.decorators import login_required
from db import get_db, log_action

packages_bp = Blueprint("packages", __name__)


# ---------------------------------------------------------------- helpers

def _get_package(db, package_id):
    package = db.execute(
        "SELECT * FROM packages WHERE package_id = ? AND tenant_id = ?", (package_id, g.tenant_id)
    ).fetchone()
    if package is None:
        abort(404)
    return package


def _route_stops(db, package_id):
    return db.execute(
        """SELECT rs.*, c.label AS country_name, ci.label AS city_label
           FROM package_route_stops rs
           LEFT JOIN countries c ON c.country_id = rs.country_id
           LEFT JOIN cities ci ON ci.city_id = rs.city_id
           WHERE rs.package_id = ? AND rs.tenant_id = ?
           ORDER BY rs.sequence_number""",
        (package_id, g.tenant_id),
    ).fetchall()


def _days(db, package_id):
    return db.execute(
        """SELECT d.*, rs.sequence_number AS stop_sequence, c.label AS country_name, ci.label AS city_label
           FROM package_days d
           LEFT JOIN package_route_stops rs ON rs.stop_id = d.route_stop_id
           LEFT JOIN countries c ON c.country_id = rs.country_id
           LEFT JOIN cities ci ON ci.city_id = rs.city_id
           WHERE d.package_id = ? AND d.tenant_id = ?
           ORDER BY d.day_number""",
        (package_id, g.tenant_id),
    ).fetchall()


def _checkpoints(stops):
    """Returns the subset of route stops flagged is_checkpoint, in route
    order, each carrying a checkpoint_number -- a 1-indexed ordinal among
    ONLY the checkpoint-flagged stops (not the same as the stop's overall
    sequence_number). This isn't stored anywhere; it's computed here
    purely so the Itinerary section can group Days under "Checkpoint 1
    Itinerary", "Checkpoint 2 Itinerary", etc., per Zeb's request that the
    day-by-day plan be organized around Checkpoints rather than shown as
    one flat list (see view_package())."""
    checkpoints = []
    for s in stops:
        if s["is_checkpoint"]:
            checkpoints.append(dict(s, checkpoint_number=len(checkpoints) + 1))
    return checkpoints


def _day_pois(db, day_id):
    return db.execute(
        """SELECT dp.*, poi.name AS poi_name
           FROM package_day_pois dp JOIN points_of_interest poi ON poi.poi_id = dp.poi_id
           WHERE dp.day_id = ? AND dp.tenant_id = ?
           ORDER BY dp.sequence_number""",
        (day_id, g.tenant_id),
    ).fetchall()


def _components(db, package_id):
    return db.execute(
        """SELECT pc.*, s.service_code, s.description AS service_description,
                  p.product_code, p.product_name,
                  sup.supplier_name,
                  rs.sequence_number AS stop_sequence,
                  d.day_number, d.title AS day_title
           FROM package_components pc
           LEFT JOIN services s ON s.service_id = pc.service_id
           LEFT JOIN products p ON p.product_id = pc.product_id
           LEFT JOIN suppliers sup ON sup.supplier_id = pc.supplier_id
           LEFT JOIN package_route_stops rs ON rs.stop_id = pc.route_stop_id
           LEFT JOIN package_days d ON d.day_id = pc.day_id
           WHERE pc.package_id = ? AND pc.tenant_id = ?
           ORDER BY pc.sequence_number""",
        (package_id, g.tenant_id),
    ).fetchall()


def _price_tiers(db, package_id):
    return db.execute(
        "SELECT * FROM package_price_tiers WHERE package_id = ? AND tenant_id = ? ORDER BY min_pax",
        (package_id, g.tenant_id),
    ).fetchall()


def _active_services(db):
    return db.execute(
        """SELECT s.service_id, s.service_code, s.description,
                  c.category_id, c.category_code, c.category_name,
                  g.group_id, g.group_code, g.group_name,
                  sg.subgroup_id, sg.subgroup_code, sg.subgroup_name
           FROM services s
           JOIN service_subgroups sg ON sg.subgroup_id = s.subgroup_id
           JOIN service_groups g ON g.group_id = sg.group_id
           JOIN service_categories c ON c.category_id = g.category_id
           WHERE s.tenant_id = ? AND s.status != 'archived'
           ORDER BY s.service_code""",
        (g.tenant_id,),
    ).fetchall()


def _services_json(services):
    """Builds the [{id, code, description, category_id, category, group_id,
    group, subgroup_id, subgroup}] list the Add/Edit Component form's
    Category/Group/Sub-Group filter cascade uses to narrow the Service
    <select> client-side -- same convention as _pois_json(); subgroup_id
    is NOT NULL on services so every row here always carries a full
    category/group/subgroup."""
    return [
        {
            "id": s["service_id"],
            "code": s["service_code"],
            "description": s["description"],
            "category_id": s["category_id"],
            "category": s["category_name"],
            "group_id": s["group_id"],
            "group": s["group_name"],
            "subgroup_id": s["subgroup_id"],
            "subgroup": s["subgroup_name"],
        }
        for s in services
    ]


def _tour_package_services(db):
    """Services Inventory rows in the 'TP' (Tour Package) Category,
    joined up to their Group/Sub-Group names -- these are the only
    services a new Package can be created from (see new_package()). A
    service already linked to a package (packages.service_id) is
    excluded: the point of the link is one Package per Tour Package
    concept, so once 'TP-SN-MC-0001' has become a Package, the next new
    tour to that city gets its own new Service row (...-0002) rather
    than reusing this one -- matching how Services' own sequence numbers
    already work."""
    return db.execute(
        """SELECT s.service_id, s.service_code, s.description AS route_description,
                  g.group_name, sg.subgroup_name
           FROM services s
           JOIN service_subgroups sg ON sg.subgroup_id = s.subgroup_id
           JOIN service_groups g ON g.group_id = sg.group_id
           JOIN service_categories c ON c.category_id = g.category_id
           WHERE s.tenant_id = ? AND c.category_code = 'TP' AND s.status != 'archived'
             AND NOT EXISTS (
                 SELECT 1 FROM packages p WHERE p.tenant_id = s.tenant_id AND p.service_id = s.service_id
             )
           ORDER BY s.service_code""",
        (g.tenant_id,),
    ).fetchall()


def _tour_services_map(tour_services):
    """Builds the {service_id: {code, description, name}} dict the New
    Package form's JS uses for its live preview (see packages/form.html)
    -- keyed by string id since that's how it'll be looked up against
    the <select>'s value."""
    return {
        str(s["service_id"]): {
            "code": s["service_code"],
            "description": f"{s['group_name']}, {s['subgroup_name']}",
            "name": s["route_description"],
        }
        for s in tour_services
    }


def _resolve_tour_package_service(db, service_id):
    """Looks up one 'TP'-Category service by id, tenant-scoped and
    status-checked, for authoritative server-side derivation of
    package_code/package_name/description -- the client-side dropdown in
    packages/form.html is a convenience preview only, never trusted for
    the actual values (see new_package())."""
    if not service_id:
        return None
    return db.execute(
        """SELECT s.service_id, s.service_code, s.description AS route_description,
                  g.group_name, sg.subgroup_name
           FROM services s
           JOIN service_subgroups sg ON sg.subgroup_id = s.subgroup_id
           JOIN service_groups g ON g.group_id = sg.group_id
           JOIN service_categories c ON c.category_id = g.category_id
           WHERE s.service_id = ? AND s.tenant_id = ? AND c.category_code = 'TP' AND s.status != 'archived'""",
        (service_id, g.tenant_id),
    ).fetchone()


def _active_products(db):
    return db.execute(
        "SELECT product_id, product_code, product_name FROM products WHERE tenant_id = ? AND status != 'discontinued' ORDER BY product_code",
        (g.tenant_id,),
    ).fetchall()


def _active_suppliers(db):
    return db.execute(
        """SELECT sup.supplier_id, sup.supplier_name,
                  t.supplier_type_id, t.label AS type_label,
                  st.supplier_subtype_id, st.label AS subtype_label
           FROM suppliers sup
           LEFT JOIN supplier_types t ON t.supplier_type_id = sup.supplier_type_id
           LEFT JOIN supplier_subtypes st ON st.supplier_subtype_id = sup.supplier_subtype_id
           WHERE sup.tenant_id = ? AND sup.is_deleted = 0
           ORDER BY sup.supplier_name""",
        (g.tenant_id,),
    ).fetchall()


def _suppliers_json(suppliers):
    """Builds the [{id, name, type_id, type, subtype_id, subtype}] list the
    Add/Edit Component form's Type/Sub-Type filter cascade uses to narrow
    the Supplier <select> client-side -- same convention as _pois_json().
    supplier_type_id/supplier_subtype_id are nullable on suppliers, so
    type/subtype fall back to "" like _pois_json()'s type/city do."""
    return [
        {
            "id": s["supplier_id"],
            "name": s["supplier_name"],
            "type_id": s["supplier_type_id"],
            "type": s["type_label"] or "",
            "subtype_id": s["supplier_subtype_id"],
            "subtype": s["subtype_label"] or "",
        }
        for s in suppliers
    ]


def _pois(db):
    return db.execute(
        """SELECT p.poi_id, p.name, pt.label AS type_label,
                  COALESCE(ci.label, p.city_text) AS city_label
           FROM points_of_interest p
           LEFT JOIN poi_types pt ON pt.poi_type_id = p.poi_type_id
           LEFT JOIN cities ci ON ci.city_id = p.city_id
           WHERE p.tenant_id = ? AND p.is_deleted = 0
           ORDER BY p.name""",
        (g.tenant_id,),
    ).fetchall()


def _pois_json(pois):
    """Builds the [{id, name, type, city}] list the Add POI to Day form's
    Name/Type/City filter bar uses to filter the underlying <select>
    client-side, and to populate the Type/City filter dropdowns with
    whatever values actually appear -- 149 POIs is small enough to ship
    the whole list once rather than round-trip per filter change, same
    convention as the New Package form's service picker (see
    _tour_services_map())."""
    return [
        {"id": p["poi_id"], "name": p["name"], "type": p["type_label"] or "", "city": p["city_label"] or ""}
        for p in pois
    ]


# ----------------------------------------------------------- package CRUD

@packages_bp.route("/")
@login_required
def list_packages():
    db = get_db()
    q = request.args.get("q", "").strip()
    status = request.args.get("status", "").strip()
    show_archived = request.args.get("show_archived") == "1"

    sql = "SELECT * FROM packages WHERE tenant_id = ?"
    params = [g.tenant_id]
    if not show_archived:
        sql += " AND status != 'archived'"
    if q:
        sql += " AND (package_name LIKE ? OR package_code LIKE ?)"
        params += [f"%{q}%", f"%{q}%"]
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY package_code"
    packages = db.execute(sql, params).fetchall()

    return render_template(
        "packages/list.html", packages=packages, q=q, status=status, show_archived=show_archived,
    )


@packages_bp.route("/<int:package_id>")
@login_required
def view_package(package_id):
    db = get_db()
    package = _get_package(db, package_id)
    stops = _route_stops(db, package_id)
    days = _days(db, package_id)
    components = _components(db, package_id)
    # Each Day's card splits its Costing into two sub-parts alongside its
    # POI's -- Accommodation (is_accommodation) and Other Service -- per
    # Zeb's request. A component only shows up here if it's tied to this
    # specific Day (component.day_id).
    days_with_pois = [
        dict(
            d,
            pois=_day_pois(db, d["day_id"]),
            hotel_components=[c for c in components if c["day_id"] == d["day_id"] and c["is_accommodation"]],
            other_components=[c for c in components if c["day_id"] == d["day_id"] and not c["is_accommodation"]],
        )
        for d in days
    ]

    # Group Days under the Checkpoint (route stop) they belong to, per
    # Zeb's request -- a Day not tied to any stop, or tied to a stop that
    # isn't flagged is_checkpoint, falls back into "unassigned_days" so it
    # still shows up on the page instead of silently disappearing.
    checkpoints = _checkpoints(stops)
    checkpoint_stop_ids = {cp["stop_id"] for cp in checkpoints}
    for cp in checkpoints:
        cp["days"] = [d for d in days_with_pois if d["route_stop_id"] == cp["stop_id"]]
        # A service for the WHOLE Checkpoint stay (e.g. a Car Rental or
        # Guide covering every day at this stop) rather than one specific
        # Day -- tied to this Checkpoint's route_stop_id with no day_id.
        # Per Zeb's request, shown once per Checkpoint above its Days
        # rather than needing "Repeat for Checkpoint" re-entered per Day.
        cp["services"] = [c for c in components if c["route_stop_id"] == cp["stop_id"] and c["day_id"] is None]

    # "Repeat for Checkpoint": an Accommodation or Other Service line flagged
    # this way is entered on one Day but shows on every Day within that
    # same Checkpoint (not just the Day it belongs to) -- per Zeb's
    # request, so a multi-night item like a hotel room doesn't need to be
    # re-entered per night. It's the same underlying row everywhere it
    # appears (same component_id); day_card marks the copies shown on a
    # Day other than their own with is_inherited so the template can note
    # where they actually live. Only components tied to a Day that's
    # itself inside a Checkpoint participate -- an unassigned Day has no
    # "siblings" to repeat across.
    for cp in checkpoints:
        cp_day_ids = {d["day_id"] for d in cp["days"]}
        repeating = [c for c in components if c["day_id"] in cp_day_ids and c["repeat_for_checkpoint"]]
        for d in cp["days"]:
            inherited = [dict(c, is_inherited=True) for c in repeating if c["day_id"] != d["day_id"]]
            d["hotel_components"] = d["hotel_components"] + [c for c in inherited if c["is_accommodation"]]
            d["other_components"] = d["other_components"] + [c for c in inherited if not c["is_accommodation"]]

    unassigned_days = [d for d in days_with_pois if d["route_stop_id"] not in checkpoint_stop_ids]

    # The flat, package-wide Components table is gone -- per Zeb's
    # request, every component now shows up exactly where it applies:
    # under its Day (above), under its Checkpoint (cp["services"], above),
    # or here, at the two levels that sit outside any single Checkpoint:
    #   - entire_tour_components: no Route Stop and no Day at all -- a
    #     cost for the whole trip (e.g. trip insurance, a tour leader).
    #   - other_route_stop_components: tied to a Route Stop that ISN'T
    #     flagged as a Checkpoint, with no Day -- an edge case (most
    #     costed stops are Checkpoints) kept visible rather than silently
    #     dropped, same "never hide it" precedent as unassigned_days.
    entire_tour_components = [c for c in components if c["route_stop_id"] is None and c["day_id"] is None]
    other_route_stop_components = [
        c for c in components
        if c["route_stop_id"] is not None and c["route_stop_id"] not in checkpoint_stop_ids and c["day_id"] is None
    ]

    tiers = _price_tiers(db, package_id)
    return render_template(
        "packages/view.html", package=package, stops=stops, days=days_with_pois,
        checkpoints=checkpoints, unassigned_days=unassigned_days,
        entire_tour_components=entire_tour_components, other_route_stop_components=other_route_stop_components,
        tiers=tiers,
    )


@packages_bp.route("/new", methods=["GET", "POST"])
@login_required
def new_package():
    db = get_db()
    if request.method == "POST":
        form = request.form
        package_type = form.get("package_type") or "fixed_departure"
        if package_type not in ("fixed_departure", "custom"):
            package_type = "fixed_departure"

        # Package Number, Package Description, and Package Name are never
        # taken from the submitted form directly -- only the chosen
        # service_id is trusted, and package_code/package_name/description
        # are derived from that service's own record here, server-side.
        # (The visible "auto filled" preview in packages/form.html is a
        # client-side JS convenience only; it is not what gets saved.)
        service = _resolve_tour_package_service(db, form.get("service_id"))

        errors = []
        if service is None:
            errors.append(
                "Select a Tour Package Service to base this package on — Package Number, Description, "
                "and Name all come from it. If none appear below, add one under Inventory Management -> "
                "Services (Category 'Tour Package') first."
            )
        elif db.execute(
            "SELECT 1 FROM packages WHERE tenant_id = ? AND package_code = ?",
            (g.tenant_id, service["service_code"]),
        ).fetchone():
            # Belt-and-suspenders: _tour_package_services() already excludes
            # services already linked to a package, so this only fires on a
            # genuine race (two people submitting the same stale dropdown
            # at once).
            errors.append(f"A package has already been created from {service['service_code']}.")

        if errors:
            for e in errors:
                flash(e, "error")
            tour_services = _tour_package_services(db)
            return render_template(
                "packages/form.html", package=None, form_values=form, tour_services=tour_services,
                tour_services_map=_tour_services_map(tour_services),
            )

        package_code = service["service_code"]
        package_name = service["route_description"]
        description = f"{service['group_name']}, {service['subgroup_name']}"

        db.execute(
            """INSERT INTO packages (tenant_id, service_id, package_code, package_name, package_type,
                                      duration_days, duration_nights, min_pax, max_pax, difficulty_rating,
                                      minimum_age, status, description, inclusions, exclusions, base_currency,
                                      notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                g.tenant_id, service["service_id"], package_code, package_name, package_type,
                form.get("duration_days") or None, form.get("duration_nights") or None,
                form.get("min_pax") or None, form.get("max_pax") or None,
                form.get("difficulty_rating", "").strip() or None, form.get("minimum_age") or None,
                form.get("status") or "draft",
                description, form.get("inclusions", "").strip() or None,
                form.get("exclusions", "").strip() or None, form.get("base_currency", "").strip() or None,
                form.get("notes", "").strip() or None,
            ),
        )
        db.commit()
        package_id = db.execute("SELECT last_insert_rowid() id").fetchone()["id"]
        log_action("Create", "package", package_id, f"Created package {package_code}: {package_name}")
        flash(f"Package {package_code} created. Now add its route, days, and components below.", "success")
        return redirect(url_for("packages.view_package", package_id=package_id))

    tour_services = _tour_package_services(db)
    return render_template(
        "packages/form.html", package=None, form_values=None, tour_services=tour_services,
        tour_services_map=_tour_services_map(tour_services),
    )


@packages_bp.route("/<int:package_id>/edit", methods=["GET", "POST"])
@login_required
def edit_package(package_id):
    db = get_db()
    package = _get_package(db, package_id)

    if request.method == "POST":
        form = request.form
        package_name = form.get("package_name", "").strip()
        package_type = form.get("package_type") or "fixed_departure"
        if package_type not in ("fixed_departure", "custom"):
            package_type = "fixed_departure"
        status = form.get("status") or "draft"
        if status not in ("draft", "active", "archived"):
            status = "draft"

        if not package_name:
            flash("Package Name is required.", "error")
            return render_template("packages/form.html", package=package, form_values=form)

        db.execute(
            """UPDATE packages SET package_name=?, package_type=?, duration_days=?, duration_nights=?,
                                    min_pax=?, max_pax=?, difficulty_rating=?, minimum_age=?, status=?,
                                    description=?, inclusions=?, exclusions=?, base_currency=?, notes=?,
                                    updated_at=datetime('now')
               WHERE package_id=? AND tenant_id=?""",
            (
                package_name, package_type, form.get("duration_days") or None, form.get("duration_nights") or None,
                form.get("min_pax") or None, form.get("max_pax") or None,
                form.get("difficulty_rating", "").strip() or None, form.get("minimum_age") or None, status,
                form.get("description", "").strip() or None, form.get("inclusions", "").strip() or None,
                form.get("exclusions", "").strip() or None, form.get("base_currency", "").strip() or None,
                form.get("notes", "").strip() or None,
                package_id, g.tenant_id,
            ),
        )
        db.commit()
        log_action("Update", "package", package_id, f"Updated package {package['package_code']}")
        flash("Package updated.", "success")
        return redirect(url_for("packages.view_package", package_id=package_id))

    return render_template("packages/form.html", package=package, form_values=None)


@packages_bp.route("/<int:package_id>/delete", methods=["POST"])
@login_required
def delete_package(package_id):
    db = get_db()
    package = _get_package(db, package_id)
    db.execute(
        "UPDATE packages SET status = 'archived', updated_at = datetime('now') WHERE package_id = ? AND tenant_id = ?",
        (package_id, g.tenant_id),
    )
    db.commit()
    log_action("Delete", "package", package_id, f"Archived package {package['package_code']}")
    flash(f"'{package['package_code']}' archived.", "success")
    return redirect(url_for("packages.list_packages"))


@packages_bp.route("/<int:package_id>/restore", methods=["POST"])
@login_required
def restore_package(package_id):
    db = get_db()
    package = _get_package(db, package_id)
    db.execute(
        "UPDATE packages SET status = 'draft', updated_at = datetime('now') WHERE package_id = ? AND tenant_id = ?",
        (package_id, g.tenant_id),
    )
    db.commit()
    log_action("Update", "package", package_id, f"Restored package {package['package_code']} from archived")
    flash(f"'{package['package_code']}' restored to Draft.", "success")
    return redirect(request.referrer or url_for("packages.view_package", package_id=package_id))


# ------------------------------------------------------------ route stops

@packages_bp.route("/<int:package_id>/stops/new", methods=["GET", "POST"])
@login_required
def new_route_stop(package_id):
    db = get_db()
    package = _get_package(db, package_id)
    if request.method == "POST":
        form = request.form
        sequence_number = form.get("sequence_number") or None
        if not sequence_number:
            errors_next = db.execute(
                "SELECT COALESCE(MAX(sequence_number), 0) + 1 AS n FROM package_route_stops WHERE package_id = ?",
                (package_id,),
            ).fetchone()["n"]
            sequence_number = errors_next
        is_layover = 1 if form.get("is_layover") else 0
        is_checkpoint = 1 if form.get("is_checkpoint") else 0
        # Nights and Approximate Hrs are mutually exclusive on the form
        # (see stop_form.html's JS) -- enforce that server-side too, so a
        # stale/tampered submission can't leave both populated.
        nights = None if is_layover else (form.get("nights") or None)
        layover_hours = (form.get("layover_hours") or None) if is_layover else None
        db.execute(
            """INSERT INTO package_route_stops (tenant_id, package_id, sequence_number, region_id, country_id,
                                                  state_id, state_province_text, city_id, city_text, nights,
                                                  is_layover, layover_hours, is_checkpoint, border_crossing_notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                g.tenant_id, package_id, sequence_number, form.get("region_id") or None,
                form.get("country_id") or None, form.get("state_id") or None,
                form.get("state_province_text", "").strip() or None, form.get("city_id") or None,
                form.get("city_text", "").strip() or None,
                nights, is_layover, layover_hours, is_checkpoint,
                form.get("border_crossing_notes", "").strip() or None,
            ),
        )
        db.commit()
        log_action("Create", "package_route_stop", package_id, f"Added route stop to {package['package_code']}")
        flash("Route stop added.", "success")
        return redirect(url_for("packages.view_package", package_id=package_id))
    return render_template("packages/stop_form.html", package=package, stop=None)


@packages_bp.route("/<int:package_id>/stops/<int:stop_id>/edit", methods=["GET", "POST"])
@login_required
def edit_route_stop(package_id, stop_id):
    db = get_db()
    package = _get_package(db, package_id)
    stop = db.execute(
        "SELECT * FROM package_route_stops WHERE stop_id = ? AND package_id = ? AND tenant_id = ?",
        (stop_id, package_id, g.tenant_id),
    ).fetchone()
    if stop is None:
        abort(404)
    if request.method == "POST":
        form = request.form
        is_layover = 1 if form.get("is_layover") else 0
        is_checkpoint = 1 if form.get("is_checkpoint") else 0
        nights = None if is_layover else (form.get("nights") or None)
        layover_hours = (form.get("layover_hours") or None) if is_layover else None
        db.execute(
            """UPDATE package_route_stops SET sequence_number=?, region_id=?, country_id=?, state_id=?,
                                                state_province_text=?, city_id=?, city_text=?,
                                                nights=?, is_layover=?, layover_hours=?, is_checkpoint=?,
                                                border_crossing_notes=?
               WHERE stop_id=? AND tenant_id=?""",
            (
                form.get("sequence_number") or stop["sequence_number"], form.get("region_id") or None,
                form.get("country_id") or None, form.get("state_id") or None,
                form.get("state_province_text", "").strip() or None, form.get("city_id") or None,
                form.get("city_text", "").strip() or None,
                nights, is_layover, layover_hours, is_checkpoint,
                form.get("border_crossing_notes", "").strip() or None,
                stop_id, g.tenant_id,
            ),
        )
        db.commit()
        log_action("Update", "package_route_stop", package_id, f"Updated route stop #{stop_id}")
        flash("Route stop updated.", "success")
        return redirect(url_for("packages.view_package", package_id=package_id))
    return render_template("packages/stop_form.html", package=package, stop=stop)


@packages_bp.route("/<int:package_id>/stops/<int:stop_id>/delete", methods=["POST"])
@login_required
def delete_route_stop(package_id, stop_id):
    db = get_db()
    _get_package(db, package_id)
    stop = db.execute(
        "SELECT * FROM package_route_stops WHERE stop_id = ? AND package_id = ? AND tenant_id = ?",
        (stop_id, package_id, g.tenant_id),
    ).fetchone()
    if stop is None:
        abort(404)
    # Days and components can be tied to a route stop (route_stop_id), and
    # that FK has no ON DELETE clause -- unlike a day's package_day_pois
    # (which belong to the day and are removed with it), a day or a
    # component tied to this stop is its own standalone record elsewhere
    # in the package, so deleting the stop out from under it should be
    # blocked with a clear message rather than crashing on the FK
    # constraint (or silently orphaning the reference).
    day_count = db.execute(
        "SELECT COUNT(*) c FROM package_days WHERE route_stop_id = ? AND tenant_id = ?", (stop_id, g.tenant_id)
    ).fetchone()["c"]
    component_count = db.execute(
        "SELECT COUNT(*) c FROM package_components WHERE route_stop_id = ? AND tenant_id = ?",
        (stop_id, g.tenant_id),
    ).fetchone()["c"]
    if day_count or component_count:
        parts = []
        if day_count:
            parts.append(f"{day_count} day(s)")
        if component_count:
            parts.append(f"{component_count} component(s)")
        flash(
            f"This stop is still linked to {' and '.join(parts)}, so it can't be deleted. "
            f"Reassign or remove those first.",
            "error",
        )
        return redirect(url_for("packages.view_package", package_id=package_id))
    db.execute("DELETE FROM package_route_stops WHERE stop_id = ? AND tenant_id = ?", (stop_id, g.tenant_id))
    db.commit()
    log_action("Delete", "package_route_stop", package_id, f"Deleted route stop #{stop_id}")
    flash("Route stop deleted.", "success")
    return redirect(url_for("packages.view_package", package_id=package_id))


# ------------------------------------------------------------------- days

@packages_bp.route("/<int:package_id>/days/new", methods=["GET", "POST"])
@login_required
def new_package_day(package_id):
    db = get_db()
    package = _get_package(db, package_id)
    if request.method == "POST":
        form = request.form
        day_number = form.get("day_number") or None
        if not day_number:
            day_number = db.execute(
                "SELECT COALESCE(MAX(day_number), 0) + 1 AS n FROM package_days WHERE package_id = ?",
                (package_id,),
            ).fetchone()["n"]
        if db.execute(
            "SELECT 1 FROM package_days WHERE package_id = ? AND day_number = ?", (package_id, day_number)
        ).fetchone():
            flash(f"Day {day_number} already exists for this package.", "error")
            return render_template(
                "packages/day_form.html", package=package, day=None, stops=_route_stops(db, package_id),
                form_values=form, preselected_stop_id=form.get("route_stop_id") or None,
            )
        db.execute(
            """INSERT INTO package_days (tenant_id, package_id, route_stop_id, day_number, title, description)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                g.tenant_id, package_id, form.get("route_stop_id") or None, day_number,
                form.get("title", "").strip() or None, form.get("description", "").strip() or None,
            ),
        )
        db.commit()
        log_action("Create", "package_day", package_id, f"Added Day {day_number} to {package['package_code']}")
        flash(f"Day {day_number} added.", "success")
        return redirect(url_for("packages.view_package", package_id=package_id))
    # A "+ Add Day" link inside a Checkpoint's Itinerary section (see
    # view.html) passes ?route_stop_id=<stop_id> so the new Day starts out
    # tied to that Checkpoint instead of defaulting to "Not tied to a
    # stop" -- still just a pre-selected default, freely changeable on the
    # form itself.
    return render_template(
        "packages/day_form.html", package=package, day=None, stops=_route_stops(db, package_id), form_values=None,
        preselected_stop_id=request.args.get("route_stop_id") or None,
    )


@packages_bp.route("/<int:package_id>/days/<int:day_id>/edit", methods=["GET", "POST"])
@login_required
def edit_package_day(package_id, day_id):
    db = get_db()
    package = _get_package(db, package_id)
    day = db.execute(
        "SELECT * FROM package_days WHERE day_id = ? AND package_id = ? AND tenant_id = ?",
        (day_id, package_id, g.tenant_id),
    ).fetchone()
    if day is None:
        abort(404)
    if request.method == "POST":
        form = request.form
        db.execute(
            """UPDATE package_days SET route_stop_id=?, day_number=?, title=?, description=?
               WHERE day_id=? AND tenant_id=?""",
            (
                form.get("route_stop_id") or None, form.get("day_number") or day["day_number"],
                form.get("title", "").strip() or None, form.get("description", "").strip() or None,
                day_id, g.tenant_id,
            ),
        )
        db.commit()
        log_action("Update", "package_day", package_id, f"Updated Day {day['day_number']}")
        flash("Day updated.", "success")
        return redirect(url_for("packages.view_package", package_id=package_id))
    return render_template(
        "packages/day_form.html", package=package, day=day, stops=_route_stops(db, package_id), form_values=None,
    )


@packages_bp.route("/<int:package_id>/days/<int:day_id>/delete", methods=["POST"])
@login_required
def delete_package_day(package_id, day_id):
    db = get_db()
    _get_package(db, package_id)
    day = db.execute(
        "SELECT * FROM package_days WHERE day_id = ? AND package_id = ? AND tenant_id = ?",
        (day_id, package_id, g.tenant_id),
    ).fetchone()
    if day is None:
        abort(404)
    # POI's belong to the day and are removed with it (package_day_pois),
    # but a Costing component tied to this Day (day_id -- Accommodation or
    # Other Service, see view.html's day_card macro) is its own standalone
    # record, same as a component tied to a Route Stop -- block deletion
    # with a clear message instead of orphaning it, matching
    # delete_route_stop()'s precedent.
    component_count = db.execute(
        "SELECT COUNT(*) c FROM package_components WHERE day_id = ? AND tenant_id = ?", (day_id, g.tenant_id)
    ).fetchone()["c"]
    if component_count:
        flash(
            f"Day {day['day_number']} is still linked to {component_count} component(s), so it can't be deleted. "
            f"Reassign or remove those first.",
            "error",
        )
        return redirect(url_for("packages.view_package", package_id=package_id))
    db.execute("DELETE FROM package_day_pois WHERE day_id = ? AND tenant_id = ?", (day_id, g.tenant_id))
    db.execute("DELETE FROM package_days WHERE day_id = ? AND tenant_id = ?", (day_id, g.tenant_id))
    db.commit()
    log_action("Delete", "package_day", package_id, f"Deleted Day {day['day_number']}")
    flash(f"Day {day['day_number']} deleted.", "success")
    return redirect(url_for("packages.view_package", package_id=package_id))


# -------------------------------------------------------------- day POIs

@packages_bp.route("/<int:package_id>/days/<int:day_id>/pois/new", methods=["GET", "POST"])
@login_required
def new_day_poi(package_id, day_id):
    db = get_db()
    package = _get_package(db, package_id)
    day = db.execute(
        "SELECT * FROM package_days WHERE day_id = ? AND package_id = ? AND tenant_id = ?",
        (day_id, package_id, g.tenant_id),
    ).fetchone()
    if day is None:
        abort(404)
    if request.method == "POST":
        form = request.form
        poi_id = form.get("poi_id") or None
        if not poi_id:
            flash("Choose a Point of Interest.", "error")
            pois = _pois(db)
            return render_template(
                "packages/day_poi_form.html", package=package, day=day, pois=pois, pois_json=_pois_json(pois),
            )
        if db.execute(
            "SELECT 1 FROM package_day_pois WHERE day_id = ? AND poi_id = ?", (day_id, poi_id)
        ).fetchone():
            flash("That Point of Interest is already on this day.", "error")
            pois = _pois(db)
            return render_template(
                "packages/day_poi_form.html", package=package, day=day, pois=pois, pois_json=_pois_json(pois),
            )
        sequence_number = form.get("sequence_number") or None
        if not sequence_number:
            sequence_number = db.execute(
                "SELECT COALESCE(MAX(sequence_number), 0) + 1 AS n FROM package_day_pois WHERE day_id = ?",
                (day_id,),
            ).fetchone()["n"]
        db.execute(
            """INSERT INTO package_day_pois (tenant_id, day_id, poi_id, sequence_number, visit_notes)
               VALUES (?, ?, ?, ?, ?)""",
            (g.tenant_id, day_id, poi_id, sequence_number, form.get("visit_notes", "").strip() or None),
        )
        db.commit()
        log_action("Create", "package_day_poi", package_id, f"Added a POI to Day {day['day_number']}")
        flash("Point of Interest added to the day.", "success")
        return redirect(url_for("packages.view_package", package_id=package_id))
    pois = _pois(db)
    return render_template(
        "packages/day_poi_form.html", package=package, day=day, pois=pois, pois_json=_pois_json(pois),
    )


@packages_bp.route("/<int:package_id>/days/<int:day_id>/pois/<int:day_poi_id>/delete", methods=["POST"])
@login_required
def delete_day_poi(package_id, day_id, day_poi_id):
    db = get_db()
    _get_package(db, package_id)
    day_poi = db.execute(
        "SELECT * FROM package_day_pois WHERE day_poi_id = ? AND day_id = ? AND tenant_id = ?",
        (day_poi_id, day_id, g.tenant_id),
    ).fetchone()
    if day_poi is None:
        abort(404)
    db.execute("DELETE FROM package_day_pois WHERE day_poi_id = ? AND tenant_id = ?", (day_poi_id, g.tenant_id))
    db.commit()
    log_action("Delete", "package_day_poi", package_id, f"Removed a POI from day #{day_id}")
    flash("Point of Interest removed from the day.", "success")
    return redirect(url_for("packages.view_package", package_id=package_id))


# ------------------------------------------------------------ components

def _resolve_category_id(db, category_code):
    """Looks up a service_categories.category_id by its code (e.g. 'AR' for
    Accommodation / Rooms) for the "Add Accommodation to this day" quick-link
    (see view.html) -- lets that link pre-filter the Add Component form's
    Service Category dropdown without hard-coding a numeric id anywhere
    templates or routes."""
    if not category_code:
        return None
    row = db.execute(
        "SELECT category_id FROM service_categories WHERE category_code = ?", (category_code,)
    ).fetchone()
    return row["category_id"] if row else None


@packages_bp.route("/<int:package_id>/components/new", methods=["GET", "POST"])
@login_required
def new_component(package_id):
    db = get_db()
    package = _get_package(db, package_id)
    if request.method == "POST":
        form, errors = request.form, []
        component_type = form.get("component_type") or "custom"
        if component_type not in ("service", "product", "custom"):
            component_type = "custom"
        service_id = form.get("service_id") or None
        product_id = form.get("product_id") or None
        description = form.get("description", "").strip() or None

        if component_type == "service" and not service_id:
            errors.append("Choose a Service.")
        if component_type == "product" and not product_id:
            errors.append("Choose a Product.")
        if component_type == "custom" and not description:
            errors.append("Description is required for a custom line item.")
        if component_type != "service":
            service_id = None
        if component_type != "product":
            product_id = None

        try:
            unit_cost = float(form.get("unit_cost")) if form.get("unit_cost") else None
        except ValueError:
            errors.append("Unit Cost must be a number.")
            unit_cost = None
        try:
            unit_price = float(form.get("unit_price")) if form.get("unit_price") else None
        except ValueError:
            errors.append("Unit Price must be a number.")
            unit_price = None
        try:
            quantity = float(form.get("quantity")) if form.get("quantity") not in (None, "") else 1
        except ValueError:
            errors.append("Qty must be a number.")
            quantity = 1

        if errors:
            for e in errors:
                flash(e, "error")
            services = _active_services(db)
            suppliers = _active_suppliers(db)
            return render_template(
                "packages/component_form.html", package=package, component=None,
                stops=_route_stops(db, package_id), days=_days(db, package_id),
                services=services, services_json=_services_json(services),
                products=_active_products(db), suppliers=suppliers, suppliers_json=_suppliers_json(suppliers),
                form_values=form, preselected_day_id=None, preselected_is_accommodation=False,
                preselect_category_id=None, preselected_route_stop_id=form.get("route_stop_id") or None,
                is_accommodation_form=bool(form.get("is_accommodation")),
            )

        sequence_number = form.get("sequence_number") or None
        if not sequence_number:
            sequence_number = db.execute(
                "SELECT COALESCE(MAX(sequence_number), 0) + 1 AS n FROM package_components WHERE package_id = ?",
                (package_id,),
            ).fetchone()["n"]

        db.execute(
            """INSERT INTO package_components (tenant_id, package_id, route_stop_id, day_id, is_accommodation,
                                                 repeat_for_checkpoint, component_type, service_id, product_id,
                                                 supplier_id, description, quantity, unit, unit_cost, unit_price,
                                                 currency, notes, sequence_number)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                g.tenant_id, package_id, form.get("route_stop_id") or None, form.get("day_id") or None,
                1 if form.get("is_accommodation") else 0, 1 if form.get("repeat_for_checkpoint") else 0,
                component_type, service_id, product_id, form.get("supplier_id") or None, description,
                quantity, form.get("unit", "").strip() or None, unit_cost, unit_price,
                form.get("currency", "").strip() or None, form.get("notes", "").strip() or None, sequence_number,
            ),
        )
        db.commit()
        log_action("Create", "package_component", package_id, f"Added a component to {package['package_code']}")
        flash("Component added.", "success")
        return redirect(url_for("packages.view_package", package_id=package_id))

    services = _active_services(db)
    suppliers = _active_suppliers(db)
    # A Day card's "Add Accommodation to this day" / "Add other Service to
    # this day" links (see view.html) pass day_id (and, for Accommodation,
    # is_accommodation=1 plus category=AR) so the form opens pre-tied to
    # that Day, pre-checked, and pre-filtered to the Accommodation / Rooms
    # Service Category -- all just pre-selected defaults, freely changeable
    # on the form itself, same convention as the Route Stop form's
    # preselected_stop_id (day_form.html). A "+ Add Service for this
    # Checkpoint" / "+ Add Entire Tour Service" link instead (or also)
    # passes route_stop_id so a whole-checkpoint or whole-trip service
    # opens pre-tied to the right scope.
    return render_template(
        "packages/component_form.html", package=package, component=None, stops=_route_stops(db, package_id),
        days=_days(db, package_id), services=services, services_json=_services_json(services),
        products=_active_products(db), suppliers=suppliers, suppliers_json=_suppliers_json(suppliers),
        form_values=None, preselected_day_id=request.args.get("day_id") or None,
        preselected_is_accommodation=request.args.get("is_accommodation") == "1",
        preselect_category_id=_resolve_category_id(db, request.args.get("category")),
        preselected_route_stop_id=request.args.get("route_stop_id") or None,
        is_accommodation_form=(request.args.get("is_accommodation") == "1"),
    )


@packages_bp.route("/<int:package_id>/components/<int:component_id>/edit", methods=["GET", "POST"])
@login_required
def edit_component(package_id, component_id):
    db = get_db()
    package = _get_package(db, package_id)
    component = db.execute(
        "SELECT * FROM package_components WHERE component_id = ? AND package_id = ? AND tenant_id = ?",
        (component_id, package_id, g.tenant_id),
    ).fetchone()
    if component is None:
        abort(404)

    if request.method == "POST":
        form, errors = request.form, []
        component_type = form.get("component_type") or "custom"
        if component_type not in ("service", "product", "custom"):
            component_type = "custom"
        service_id = form.get("service_id") or None
        product_id = form.get("product_id") or None
        description = form.get("description", "").strip() or None

        if component_type == "service" and not service_id:
            errors.append("Choose a Service.")
        if component_type == "product" and not product_id:
            errors.append("Choose a Product.")
        if component_type == "custom" and not description:
            errors.append("Description is required for a custom line item.")
        if component_type != "service":
            service_id = None
        if component_type != "product":
            product_id = None

        try:
            unit_cost = float(form.get("unit_cost")) if form.get("unit_cost") else None
        except ValueError:
            errors.append("Unit Cost must be a number.")
            unit_cost = None
        try:
            unit_price = float(form.get("unit_price")) if form.get("unit_price") else None
        except ValueError:
            errors.append("Unit Price must be a number.")
            unit_price = None
        try:
            quantity = float(form.get("quantity")) if form.get("quantity") not in (None, "") else 1
        except ValueError:
            errors.append("Qty must be a number.")
            quantity = 1

        if errors:
            for e in errors:
                flash(e, "error")
            services = _active_services(db)
            suppliers = _active_suppliers(db)
            return render_template(
                "packages/component_form.html", package=package, component=component,
                stops=_route_stops(db, package_id), days=_days(db, package_id),
                services=services, services_json=_services_json(services),
                products=_active_products(db), suppliers=suppliers, suppliers_json=_suppliers_json(suppliers),
                form_values=form, preselected_day_id=None, preselected_is_accommodation=False,
                preselect_category_id=None, preselected_route_stop_id=None,
                is_accommodation_form=bool(form.get("is_accommodation")),
            )

        db.execute(
            """UPDATE package_components SET route_stop_id=?, day_id=?, is_accommodation=?,
                                              repeat_for_checkpoint=?, component_type=?, service_id=?, product_id=?,
                                              supplier_id=?, description=?, quantity=?, unit=?, unit_cost=?,
                                              unit_price=?, currency=?, notes=?, sequence_number=?,
                                              updated_at=datetime('now')
               WHERE component_id=? AND tenant_id=?""",
            (
                form.get("route_stop_id") or None, form.get("day_id") or None,
                1 if form.get("is_accommodation") else 0, 1 if form.get("repeat_for_checkpoint") else 0,
                component_type, service_id, product_id, form.get("supplier_id") or None, description,
                quantity, form.get("unit", "").strip() or None, unit_cost, unit_price,
                form.get("currency", "").strip() or None,
                form.get("notes", "").strip() or None, form.get("sequence_number") or component["sequence_number"],
                component_id, g.tenant_id,
            ),
        )
        db.commit()
        log_action("Update", "package_component", package_id, f"Updated component #{component_id}")
        flash("Component updated.", "success")
        return redirect(url_for("packages.view_package", package_id=package_id))

    services = _active_services(db)
    suppliers = _active_suppliers(db)
    return render_template(
        "packages/component_form.html", package=package, component=component, stops=_route_stops(db, package_id),
        days=_days(db, package_id), services=services, services_json=_services_json(services),
        products=_active_products(db), suppliers=suppliers, suppliers_json=_suppliers_json(suppliers),
        form_values=None, preselected_day_id=None, preselected_is_accommodation=False, preselect_category_id=None,
        preselected_route_stop_id=None, is_accommodation_form=bool(component["is_accommodation"]),
    )


@packages_bp.route("/<int:package_id>/components/<int:component_id>/delete", methods=["POST"])
@login_required
def delete_component(package_id, component_id):
    db = get_db()
    _get_package(db, package_id)
    component = db.execute(
        "SELECT * FROM package_components WHERE component_id = ? AND package_id = ? AND tenant_id = ?",
        (component_id, package_id, g.tenant_id),
    ).fetchone()
    if component is None:
        abort(404)
    db.execute("DELETE FROM package_components WHERE component_id = ? AND tenant_id = ?", (component_id, g.tenant_id))
    db.commit()
    log_action("Delete", "package_component", package_id, f"Deleted component #{component_id}")
    flash("Component deleted.", "success")
    return redirect(url_for("packages.view_package", package_id=package_id))


# ----------------------------------------------------------- price tiers

@packages_bp.route("/<int:package_id>/tiers/new", methods=["GET", "POST"])
@login_required
def new_price_tier(package_id):
    db = get_db()
    package = _get_package(db, package_id)
    if request.method == "POST":
        form, errors = request.form, []
        min_pax = form.get("min_pax") or None
        price_per_pax = form.get("price_per_pax") or None
        if not min_pax:
            errors.append("Min Pax is required.")
        if not price_per_pax:
            errors.append("Price per Pax is required.")
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("packages/tier_form.html", package=package, tier=None, form_values=form)

        db.execute(
            """INSERT INTO package_price_tiers (tenant_id, package_id, min_pax, max_pax, price_per_pax, currency, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                g.tenant_id, package_id, min_pax, form.get("max_pax") or None, price_per_pax,
                form.get("currency", "").strip() or None, form.get("notes", "").strip() or None,
            ),
        )
        db.commit()
        log_action("Create", "package_price_tier", package_id, f"Added a price tier to {package['package_code']}")
        flash("Price tier added.", "success")
        return redirect(url_for("packages.view_package", package_id=package_id))
    return render_template("packages/tier_form.html", package=package, tier=None, form_values=None)


@packages_bp.route("/<int:package_id>/tiers/<int:tier_id>/edit", methods=["GET", "POST"])
@login_required
def edit_price_tier(package_id, tier_id):
    db = get_db()
    package = _get_package(db, package_id)
    tier = db.execute(
        "SELECT * FROM package_price_tiers WHERE tier_id = ? AND package_id = ? AND tenant_id = ?",
        (tier_id, package_id, g.tenant_id),
    ).fetchone()
    if tier is None:
        abort(404)
    if request.method == "POST":
        form, errors = request.form, []
        min_pax = form.get("min_pax") or None
        price_per_pax = form.get("price_per_pax") or None
        if not min_pax:
            errors.append("Min Pax is required.")
        if not price_per_pax:
            errors.append("Price per Pax is required.")
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("packages/tier_form.html", package=package, tier=tier, form_values=form)

        db.execute(
            """UPDATE package_price_tiers SET min_pax=?, max_pax=?, price_per_pax=?, currency=?, notes=?
               WHERE tier_id=? AND tenant_id=?""",
            (
                min_pax, form.get("max_pax") or None, price_per_pax, form.get("currency", "").strip() or None,
                form.get("notes", "").strip() or None, tier_id, g.tenant_id,
            ),
        )
        db.commit()
        log_action("Update", "package_price_tier", package_id, f"Updated price tier #{tier_id}")
        flash("Price tier updated.", "success")
        return redirect(url_for("packages.view_package", package_id=package_id))
    return render_template("packages/tier_form.html", package=package, tier=tier, form_values=None)


@packages_bp.route("/<int:package_id>/tiers/<int:tier_id>/delete", methods=["POST"])
@login_required
def delete_price_tier(package_id, tier_id):
    db = get_db()
    _get_package(db, package_id)
    tier = db.execute(
        "SELECT * FROM package_price_tiers WHERE tier_id = ? AND package_id = ? AND tenant_id = ?",
        (tier_id, package_id, g.tenant_id),
    ).fetchone()
    if tier is None:
        abort(404)
    db.execute("DELETE FROM package_price_tiers WHERE tier_id = ? AND tenant_id = ?", (tier_id, g.tenant_id))
    db.commit()
    log_action("Delete", "package_price_tier", package_id, f"Deleted price tier #{tier_id}")
    flash("Price tier deleted.", "success")
    return redirect(url_for("packages.view_package", package_id=package_id))
