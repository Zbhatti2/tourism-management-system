"""
Module — Organizations. A standalone management screen for the shared
`organizations` table: it's used by more than just Contacts (spec §3.2) —
Personal Accounts and Desktop Software Licenses each carry their own
organization_id too. Given that shared use and its richer field set
(address, phone, email, type) compared to a plain code/label lookup, it
gets its own list/view/edit/delete pages here rather than living inside
Table Maintenance's simple lookup-table screen.

There's no organizations_archive table (unlike contacts_archive) — an
organization is closer to shared reference data than an owned record with
its own history, so deletion here follows the same pattern used for Table
Maintenance's lookup tables: blocked outright while anything still
references it (the database enforces this via PRAGMA foreign_keys = ON
regardless of what this code does), with a reassign/merge flow to
consolidate duplicates (e.g. "Acme Inc." vs "ACME, Inc." picked up from an
Outlook import) instead of a raw constraint error.
"""
import os

from flask import Blueprint, abort, flash, g, jsonify, redirect, render_template, request, url_for

from auth.decorators import login_required
from db import get_db, log_action
from utils import open_local_path

organizations_bp = Blueprint("organizations", __name__)

# Every master table that stores a foreign key to organizations.organization_id.
# (external_resources.organization_id existed briefly but is retired along
# with the rest of the external_resources table -- see blueprints/hr.py's
# retirement note -- so it's deliberately not listed here.)
REFERENCES = [
    {"table": "contacts", "fk": "current_organization_id", "label": "contact(s)"},
    {"table": "points_of_interest", "fk": "governing_authority_id", "label": "point(s) of interest"},
]


def _usage_count(db, org_id):
    total = 0
    breakdown = []
    for ref in REFERENCES:
        count = db.execute(
            f"SELECT COUNT(*) c FROM {ref['table']} WHERE {ref['fk']} = ?", (org_id,)
        ).fetchone()["c"]
        if count:
            breakdown.append({"label": ref["label"], "count": count})
        total += count
    return total, breakdown


def _org_types(db):
    return db.execute(
        "SELECT * FROM organization_types WHERE is_active = 1 AND tenant_id = ? ORDER BY label COLLATE NOCASE", (g.tenant_id,)
    ).fetchall()


def _org_address_types(db):
    return db.execute(
        "SELECT * FROM organization_address_types WHERE is_active = 1 AND tenant_id = ? ORDER BY label COLLATE NOCASE",
        (g.tenant_id,),
    ).fetchall()


def _org_phone_types(db):
    return db.execute(
        "SELECT * FROM organization_phone_types WHERE is_active = 1 AND tenant_id = ? ORDER BY label COLLATE NOCASE",
        (g.tenant_id,),
    ).fetchall()


def _get_organization(db, org_id):
    org = db.execute(
        """SELECT o.*, ot.label AS type_label FROM organizations o
           LEFT JOIN organization_types ot ON ot.organization_type_id = o.organization_type_id
           WHERE o.organization_id = ? AND o.tenant_id = ?""",
        (org_id, g.tenant_id),
    ).fetchone()
    if org is None:
        abort(404)
    return org


@organizations_bp.route("/")
@login_required
def list_organizations():
    db = get_db()
    q = request.args.get("q", "").strip()
    type_id = request.args.get("type_id", "").strip()
    city = request.args.get("city", "").strip()
    country_id = request.args.get("country_id", "").strip()
    sql = """
        SELECT o.*, ot.label AS type_label,
               COALESCE(pc.label, pa.city_text) AS city_label,
               COALESCE(ps.label, pa.state_province_text) AS state_label,
               pco.label AS country_label,
               pe.email_address AS primary_email,
               pp.country_code AS primary_phone_country_code,
               pp.area_code AS primary_phone_area_code,
               pp.number AS primary_phone_number,
               pp.extension AS primary_phone_extension
        FROM organizations o
        LEFT JOIN organization_types ot ON ot.organization_type_id = o.organization_type_id
        LEFT JOIN organization_addresses pa ON pa.organization_address_id = (
            SELECT a.organization_address_id FROM organization_addresses a
            WHERE a.organization_id = o.organization_id AND a.tenant_id = o.tenant_id
            ORDER BY a.is_primary DESC, a.organization_address_id ASC
            LIMIT 1
        )
        LEFT JOIN cities pc ON pc.city_id = pa.city_id
        LEFT JOIN states ps ON ps.state_id = pa.state_id
        LEFT JOIN countries pco ON pco.country_id = pa.country_id
        LEFT JOIN organization_emails pe ON pe.organization_email_id = (
            SELECT e.organization_email_id FROM organization_emails e
            WHERE e.organization_id = o.organization_id AND e.tenant_id = o.tenant_id
            ORDER BY e.is_primary DESC, e.organization_email_id ASC
            LIMIT 1
        )
        LEFT JOIN organization_phones pp ON pp.organization_phone_id = (
            SELECT ph.organization_phone_id FROM organization_phones ph
            WHERE ph.organization_id = o.organization_id AND ph.tenant_id = o.tenant_id
            ORDER BY ph.is_primary DESC, ph.organization_phone_id ASC
            LIMIT 1
        )
        WHERE o.tenant_id = ?
    """
    params = [g.tenant_id]
    if q:
        sql += " AND o.organization_name LIKE ?"
        params.append(f"%{q}%")
    if type_id:
        sql += " AND o.organization_type_id = ?"
        params.append(type_id)
    if city:
        # Matches the organization's primary address (same row the City
        # column on this list is built from) — a linked city_id (pc.label)
        # or, when the province/city isn't seeded in the geography lookups,
        # the free-text fallback (pa.city_text).
        sql += " AND (pc.label LIKE ? OR pa.city_text LIKE ?)"
        like = f"%{city}%"
        params += [like, like]
    if country_id:
        sql += " AND pa.country_id = ?"
        params.append(country_id)
    sql += " ORDER BY o.organization_name"
    rows = db.execute(sql, params).fetchall()

    orgs = []
    for r in rows:
        count, _ = _usage_count(db, r["organization_id"])
        orgs.append({"row": r, "usage_count": count})

    # Countries offered in the Country filter are limited to ones actually
    # in use on an organization's primary address, so the dropdown stays
    # short and every option is guaranteed to return results.
    country_options = db.execute(
        """SELECT DISTINCT co.country_id, co.label
           FROM organization_addresses oa
           JOIN countries co ON co.country_id = oa.country_id
           WHERE oa.tenant_id = ?
           ORDER BY co.label""",
        (g.tenant_id,),
    ).fetchall()

    return render_template(
        "organizations/list.html", orgs=orgs, q=q, org_types=_org_types(db),
        type_id=type_id, city=city, country_id=country_id, country_options=country_options,
    )


@organizations_bp.route("/<int:org_id>")
@login_required
def view_organization(org_id):
    db = get_db()
    org = _get_organization(db, org_id)
    contacts = db.execute(
        "SELECT contact_id, full_name, file_as FROM contacts "
        "WHERE current_organization_id = ? AND is_deleted = 0 ORDER BY file_as, full_name",
        (org_id,),
    ).fetchall()
    pois = db.execute(
        "SELECT poi_id, name FROM points_of_interest "
        "WHERE governing_authority_id = ? AND is_deleted = 0 ORDER BY name",
        (org_id,),
    ).fetchall()
    addresses = db.execute(
        """SELECT a.*, at.label AS address_type_label,
                  COALESCE(c.label, a.city_text) AS city_label,
                  COALESCE(st.label, a.state_province_text) AS state_label,
                  co.label AS country_label
           FROM organization_addresses a
           LEFT JOIN organization_address_types at ON at.address_type_id = a.address_type_id
           LEFT JOIN cities c ON c.city_id = a.city_id
           LEFT JOIN states st ON st.state_id = a.state_id
           LEFT JOIN countries co ON co.country_id = a.country_id
           WHERE a.organization_id = ? AND a.tenant_id = ?
           ORDER BY a.is_primary DESC, at.sort_order""",
        (org_id, g.tenant_id),
    ).fetchall()
    emails = db.execute(
        "SELECT * FROM organization_emails WHERE organization_id = ? AND tenant_id = ? ORDER BY is_primary DESC, email_address",
        (org_id, g.tenant_id),
    ).fetchall()
    phones = db.execute(
        """SELECT p.*, pt.label AS phone_type_label
           FROM organization_phones p
           LEFT JOIN organization_phone_types pt ON pt.phone_type_id = p.phone_type_id
           WHERE p.organization_id = ? AND p.tenant_id = ?
           ORDER BY p.is_primary DESC, pt.sort_order""",
        (org_id, g.tenant_id),
    ).fetchall()
    links = db.execute(
        "SELECT * FROM organization_reference_links WHERE organization_id = ? AND tenant_id = ? ORDER BY link_id",
        (org_id, g.tenant_id),
    ).fetchall()
    return render_template(
        "organizations/view.html", org=org, contacts=contacts, pois=pois, addresses=addresses,
        emails=emails, phones=phones, links=links,
    )


@organizations_bp.route("/new", methods=["GET", "POST"])
@login_required
def new_organization():
    db = get_db()
    if request.method == "POST":
        form = request.form
        name = form.get("organization_name", "").strip()
        if not name:
            flash("Organization name is required.", "error")
            return render_template("organizations/form.html", org=None, org_types=_org_types(db))
        db.execute(
            # full_address/phone/email are intentionally not set here — each
            # is superseded by its own child table (organization_addresses/
            # organization_emails/organization_phones — see the matching
            # cards on the view page) and kept only as a legacy free-text
            # column for pre-existing data.
            "INSERT INTO organizations (tenant_id, organization_name, website, organization_type_id, notes) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                g.tenant_id,
                name,
                form.get("website", "").strip() or None,
                form.get("organization_type_id") or None,
                form.get("notes", "").strip() or None,
            ),
        )
        db.commit()
        org_id = db.execute("SELECT last_insert_rowid() id").fetchone()["id"]
        log_action("Create", "organization", org_id, f"Created organization {name}")
        flash("Organization created.", "success")
        return redirect(url_for("organizations.view_organization", org_id=org_id))
    return render_template("organizations/form.html", org=None, org_types=_org_types(db))


@organizations_bp.route("/<int:org_id>/edit", methods=["GET", "POST"])
@login_required
def edit_organization(org_id):
    db = get_db()
    org = db.execute(
        "SELECT * FROM organizations WHERE organization_id = ? AND tenant_id = ?", (org_id, g.tenant_id)
    ).fetchone()
    if org is None:
        abort(404)
    if request.method == "POST":
        form = request.form
        name = form.get("organization_name", "").strip()
        if not name:
            flash("Organization name is required.", "error")
            return render_template("organizations/form.html", org=org, org_types=_org_types(db))
        db.execute(
            # full_address/phone/email are left untouched here (see the
            # matching note in new_organization above) — this form no
            # longer edits any of them.
            "UPDATE organizations SET organization_name=?, website=?, "
            "organization_type_id=?, notes=?, updated_at=datetime('now') WHERE organization_id=? AND tenant_id=?",
            (
                name,
                form.get("website", "").strip() or None,
                form.get("organization_type_id") or None,
                form.get("notes", "").strip() or None,
                org_id,
                g.tenant_id,
            ),
        )
        db.commit()
        log_action("Update", "organization", org_id, f"Updated organization {name}")
        flash("Organization updated — anywhere it's referenced will show the new details immediately.", "success")
        return redirect(url_for("organizations.view_organization", org_id=org_id))
    return render_template("organizations/form.html", org=org, org_types=_org_types(db))


@organizations_bp.route("/<int:org_id>/delete", methods=["POST"])
@login_required
def delete_organization(org_id):
    db = get_db()
    org = db.execute(
        "SELECT * FROM organizations WHERE organization_id = ? AND tenant_id = ?", (org_id, g.tenant_id)
    ).fetchone()
    if org is None:
        abort(404)
    count, _ = _usage_count(db, org_id)
    if count > 0:
        flash(
            f"'{org['organization_name']}' is still linked to {count} record(s), so it can't be deleted outright. "
            f"Reassign those records to a different organization first.",
            "error",
        )
        return redirect(url_for("organizations.reassign_organization", org_id=org_id))
    # An organization's addresses/emails/phones/reference links belong to
    # it (like its notes), not other records referencing it, so they're
    # removed along with it rather than counted in _usage_count/blocking
    # the delete.
    db.execute("DELETE FROM organization_addresses WHERE organization_id = ? AND tenant_id = ?", (org_id, g.tenant_id))
    db.execute("DELETE FROM organization_emails WHERE organization_id = ? AND tenant_id = ?", (org_id, g.tenant_id))
    db.execute("DELETE FROM organization_phones WHERE organization_id = ? AND tenant_id = ?", (org_id, g.tenant_id))
    db.execute("DELETE FROM organization_reference_links WHERE organization_id = ? AND tenant_id = ?", (org_id, g.tenant_id))
    db.execute("DELETE FROM organizations WHERE organization_id = ? AND tenant_id = ?", (org_id, g.tenant_id))
    db.commit()
    log_action("Delete", "organization", org_id, f"Deleted unused organization {org['organization_name']}")
    flash(f"'{org['organization_name']}' deleted.", "success")
    return redirect(url_for("organizations.list_organizations"))


@organizations_bp.route("/<int:org_id>/reassign", methods=["GET", "POST"])
@login_required
def reassign_organization(org_id):
    """Bulk-move every contact/personal account/software license that
    points at `org_id` over to a different organization, then optionally
    delete the old one. This is also the tool for merging duplicate
    organizations (e.g. "Acme Inc." vs "ACME, Inc." picked up from an
    import), independent of any delete attempt — reachable directly from
    the list as "Merge into..."."""
    db = get_db()
    org = db.execute(
        "SELECT * FROM organizations WHERE organization_id = ? AND tenant_id = ?", (org_id, g.tenant_id)
    ).fetchone()
    if org is None:
        abort(404)
    count, breakdown = _usage_count(db, org_id)
    others = db.execute(
        "SELECT * FROM organizations WHERE organization_id != ? AND tenant_id = ? ORDER BY organization_name",
        (org_id, g.tenant_id),
    ).fetchall()

    if request.method == "POST":
        target_id = request.form.get("target_id")
        also_delete = bool(request.form.get("also_delete"))
        if not target_id:
            flash("Choose an organization to reassign these records to.", "error")
            return redirect(url_for("organizations.reassign_organization", org_id=org_id))
        target_id = int(target_id)
        target = db.execute(
            "SELECT * FROM organizations WHERE organization_id = ? AND tenant_id = ?", (target_id, g.tenant_id)
        ).fetchone()
        if target is None:
            abort(404)

        moved = 0
        for ref in REFERENCES:
            cur = db.execute(
                f"UPDATE {ref['table']} SET {ref['fk']} = ? WHERE {ref['fk']} = ? AND tenant_id = ?",
                (target_id, org_id, g.tenant_id),
            )
            moved += cur.rowcount

        if also_delete:
            # Same reasoning as delete_organization above — the merged-away
            # organization's own addresses/emails/phones/reference links
            # aren't moved to the survivor (nothing else about it is,
            # either — notes are discarded the same way), just cleared so
            # the delete succeeds.
            db.execute("DELETE FROM organization_addresses WHERE organization_id = ? AND tenant_id = ?", (org_id, g.tenant_id))
            db.execute("DELETE FROM organization_emails WHERE organization_id = ? AND tenant_id = ?", (org_id, g.tenant_id))
            db.execute("DELETE FROM organization_phones WHERE organization_id = ? AND tenant_id = ?", (org_id, g.tenant_id))
            db.execute("DELETE FROM organization_reference_links WHERE organization_id = ? AND tenant_id = ?", (org_id, g.tenant_id))
            db.execute("DELETE FROM organizations WHERE organization_id = ? AND tenant_id = ?", (org_id, g.tenant_id))
        db.commit()

        log_action(
            "Update", "organization", org_id,
            f"Reassigned {moved} record(s) from '{org['organization_name']}' to '{target['organization_name']}'"
            + (" and deleted the old organization" if also_delete else ""),
        )
        flash(
            f"Moved {moved} record(s) from '{org['organization_name']}' to '{target['organization_name']}'."
            + (
                f" '{org['organization_name']}' was then deleted."
                if also_delete
                else f" '{org['organization_name']}' is still in the list with nothing pointing at it "
                     f"— delete it separately whenever you like."
            ),
            "success",
        )
        return redirect(url_for("organizations.list_organizations"))

    return render_template(
        "organizations/reassign.html",
        org=org, others=others, usage_count=count, usage_breakdown=breakdown,
    )


# --------------------------------------------------------------- addresses
#
# An organization can have more than one address (e.g. Mailing Address,
# Physical Address — see organization_address_types). Mirrors Suppliers'
# supplier_addresses CRUD (blueprints/suppliers.py) exactly, but against its
# own, separate organization_addresses table and organization_address_types
# lookup, per the standing Organizations/Suppliers separation rule.

@organizations_bp.route("/<int:org_id>/addresses/new", methods=["GET", "POST"])
@login_required
def new_organization_address(org_id):
    db = get_db()
    org = _get_organization(db, org_id)
    if request.method == "POST":
        form = request.form
        if form.get("is_primary"):
            db.execute(
                "UPDATE organization_addresses SET is_primary = 0 WHERE organization_id = ? AND tenant_id = ?",
                (org_id, g.tenant_id),
            )
        db.execute(
            """INSERT INTO organization_addresses
               (tenant_id, organization_id, address_type_id, street, unit, region_id, country_id,
                state_id, state_province_text, city_id, city_text, postal_code, is_primary)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                g.tenant_id, org_id, form.get("address_type_id") or None,
                form.get("street", "").strip() or None, form.get("unit", "").strip() or None,
                form.get("region_id") or None, form.get("country_id") or None,
                form.get("state_id") or None, form.get("state_province_text", "").strip() or None,
                form.get("city_id") or None, form.get("city_text", "").strip() or None,
                form.get("postal_code", "").strip() or None, 1 if form.get("is_primary") else 0,
            ),
        )
        db.commit()
        log_action("Create", "organization_address", org_id, "Added organization address")
        flash("Address added.", "success")
        return redirect(url_for("organizations.view_organization", org_id=org_id))
    return render_template(
        "organizations/address_form.html", org=org, address=None, address_types=_org_address_types(db)
    )


@organizations_bp.route("/<int:org_id>/addresses/<int:address_id>/edit", methods=["GET", "POST"])
@login_required
def edit_organization_address(org_id, address_id):
    db = get_db()
    org = _get_organization(db, org_id)
    address = db.execute(
        "SELECT * FROM organization_addresses WHERE organization_address_id = ? AND organization_id = ? AND tenant_id = ?",
        (address_id, org_id, g.tenant_id),
    ).fetchone()
    if address is None:
        abort(404)
    if request.method == "POST":
        form = request.form
        if form.get("is_primary"):
            db.execute(
                "UPDATE organization_addresses SET is_primary = 0 WHERE organization_id = ? AND tenant_id = ?",
                (org_id, g.tenant_id),
            )
        db.execute(
            """UPDATE organization_addresses SET address_type_id=?, street=?, unit=?, region_id=?, country_id=?,
               state_id=?, state_province_text=?, city_id=?, city_text=?, postal_code=?, is_primary=?,
               updated_at=datetime('now') WHERE organization_address_id=? AND tenant_id=?""",
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
        log_action("Update", "organization_address", org_id, f"Updated organization address #{address_id}")
        flash("Address updated.", "success")
        return redirect(url_for("organizations.view_organization", org_id=org_id))
    return render_template(
        "organizations/address_form.html", org=org, address=address, address_types=_org_address_types(db)
    )


@organizations_bp.route("/<int:org_id>/addresses/<int:address_id>/delete", methods=["POST"])
@login_required
def delete_organization_address(org_id, address_id):
    db = get_db()
    _get_organization(db, org_id)
    address = db.execute(
        "SELECT * FROM organization_addresses WHERE organization_address_id = ? AND organization_id = ? AND tenant_id = ?",
        (address_id, org_id, g.tenant_id),
    ).fetchone()
    if address is None:
        abort(404)
    db.execute(
        "DELETE FROM organization_addresses WHERE organization_address_id = ? AND tenant_id = ?",
        (address_id, g.tenant_id),
    )
    db.commit()
    log_action("Delete", "organization_address", org_id, f"Deleted organization address #{address_id}")
    flash("Address deleted.", "success")
    return redirect(url_for("organizations.view_organization", org_id=org_id))


# ------------------------------------------------------------------ emails
#
# An organization can have more than one email address. Mirrors Contacts'
# contact_emails CRUD (blueprints/contacts.py) but against its own,
# separate organization_emails table, and without history tracking —
# Organizations is master/reference data here, not an owned record with
# its own audit trail (see this module's docstring).

@organizations_bp.route("/<int:org_id>/emails/new", methods=["GET", "POST"])
@login_required
def new_organization_email(org_id):
    db = get_db()
    org = _get_organization(db, org_id)
    if request.method == "POST":
        form = request.form
        if form.get("is_primary"):
            db.execute(
                "UPDATE organization_emails SET is_primary = 0 WHERE organization_id = ? AND tenant_id = ?",
                (org_id, g.tenant_id),
            )
        db.execute(
            "INSERT INTO organization_emails (tenant_id, organization_id, email_address, is_primary) VALUES (?, ?, ?, ?)",
            (g.tenant_id, org_id, form["email_address"].strip(), 1 if form.get("is_primary") else 0),
        )
        db.commit()
        log_action("Create", "organization_email", org_id, f"Added email {form['email_address']}")
        flash("Email added.", "success")
        return redirect(url_for("organizations.view_organization", org_id=org_id))
    return render_template("organizations/email_form.html", org=org, email=None)


@organizations_bp.route("/<int:org_id>/emails/<int:email_id>/edit", methods=["GET", "POST"])
@login_required
def edit_organization_email(org_id, email_id):
    db = get_db()
    org = _get_organization(db, org_id)
    email = db.execute(
        "SELECT * FROM organization_emails WHERE organization_email_id = ? AND organization_id = ? AND tenant_id = ?",
        (email_id, org_id, g.tenant_id),
    ).fetchone()
    if email is None:
        abort(404)
    if request.method == "POST":
        form = request.form
        if form.get("is_primary"):
            db.execute(
                "UPDATE organization_emails SET is_primary = 0 WHERE organization_id = ? AND tenant_id = ?",
                (org_id, g.tenant_id),
            )
        db.execute(
            "UPDATE organization_emails SET email_address = ?, is_primary = ?, updated_at = datetime('now') "
            "WHERE organization_email_id = ? AND tenant_id = ?",
            (form["email_address"].strip(), 1 if form.get("is_primary") else 0, email_id, g.tenant_id),
        )
        db.commit()
        log_action("Update", "organization_email", org_id, f"Updated email #{email_id}")
        flash("Email updated.", "success")
        return redirect(url_for("organizations.view_organization", org_id=org_id))
    return render_template("organizations/email_form.html", org=org, email=email)


@organizations_bp.route("/<int:org_id>/emails/<int:email_id>/delete", methods=["POST"])
@login_required
def delete_organization_email(org_id, email_id):
    db = get_db()
    _get_organization(db, org_id)
    email = db.execute(
        "SELECT * FROM organization_emails WHERE organization_email_id = ? AND organization_id = ? AND tenant_id = ?",
        (email_id, org_id, g.tenant_id),
    ).fetchone()
    if email is None:
        abort(404)
    db.execute(
        "DELETE FROM organization_emails WHERE organization_email_id = ? AND tenant_id = ?", (email_id, g.tenant_id)
    )
    db.commit()
    log_action("Delete", "organization_email", org_id, f"Deleted email #{email_id}")
    flash("Email deleted.", "success")
    return redirect(url_for("organizations.view_organization", org_id=org_id))


# ------------------------------------------------------------------ phones
#
# An organization can have more than one phone number (Office, Mobile,
# Fax, ... — see organization_phone_types). Mirrors the addresses CRUD
# above in shape.

@organizations_bp.route("/<int:org_id>/phones/new", methods=["GET", "POST"])
@login_required
def new_organization_phone(org_id):
    db = get_db()
    org = _get_organization(db, org_id)
    if request.method == "POST":
        form = request.form
        if form.get("is_primary"):
            db.execute(
                "UPDATE organization_phones SET is_primary = 0 WHERE organization_id = ? AND tenant_id = ?",
                (org_id, g.tenant_id),
            )
        db.execute(
            """INSERT INTO organization_phones
               (tenant_id, organization_id, phone_type_id, country_code, area_code, number, extension, is_primary)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                g.tenant_id, org_id, form.get("phone_type_id") or None,
                form.get("country_code", "").strip() or None, form.get("area_code", "").strip() or None,
                form["number"].strip(), form.get("extension", "").strip() or None,
                1 if form.get("is_primary") else 0,
            ),
        )
        db.commit()
        log_action("Create", "organization_phone", org_id, "Added organization phone")
        flash("Phone added.", "success")
        return redirect(url_for("organizations.view_organization", org_id=org_id))
    return render_template("organizations/phone_form.html", org=org, phone=None, phone_types=_org_phone_types(db))


@organizations_bp.route("/<int:org_id>/phones/<int:phone_id>/edit", methods=["GET", "POST"])
@login_required
def edit_organization_phone(org_id, phone_id):
    db = get_db()
    org = _get_organization(db, org_id)
    phone = db.execute(
        "SELECT * FROM organization_phones WHERE organization_phone_id = ? AND organization_id = ? AND tenant_id = ?",
        (phone_id, org_id, g.tenant_id),
    ).fetchone()
    if phone is None:
        abort(404)
    if request.method == "POST":
        form = request.form
        if form.get("is_primary"):
            db.execute(
                "UPDATE organization_phones SET is_primary = 0 WHERE organization_id = ? AND tenant_id = ?",
                (org_id, g.tenant_id),
            )
        db.execute(
            """UPDATE organization_phones SET phone_type_id=?, country_code=?, area_code=?, number=?, extension=?,
               is_primary=?, updated_at=datetime('now') WHERE organization_phone_id=? AND tenant_id=?""",
            (
                form.get("phone_type_id") or None, form.get("country_code", "").strip() or None,
                form.get("area_code", "").strip() or None, form["number"].strip(),
                form.get("extension", "").strip() or None, 1 if form.get("is_primary") else 0,
                phone_id, g.tenant_id,
            ),
        )
        db.commit()
        log_action("Update", "organization_phone", org_id, f"Updated organization phone #{phone_id}")
        flash("Phone updated.", "success")
        return redirect(url_for("organizations.view_organization", org_id=org_id))
    return render_template("organizations/phone_form.html", org=org, phone=phone, phone_types=_org_phone_types(db))


@organizations_bp.route("/<int:org_id>/phones/<int:phone_id>/delete", methods=["POST"])
@login_required
def delete_organization_phone(org_id, phone_id):
    db = get_db()
    _get_organization(db, org_id)
    phone = db.execute(
        "SELECT * FROM organization_phones WHERE organization_phone_id = ? AND organization_id = ? AND tenant_id = ?",
        (phone_id, org_id, g.tenant_id),
    ).fetchone()
    if phone is None:
        abort(404)
    db.execute(
        "DELETE FROM organization_phones WHERE organization_phone_id = ? AND tenant_id = ?", (phone_id, g.tenant_id)
    )
    db.commit()
    log_action("Delete", "organization_phone", org_id, f"Deleted organization phone #{phone_id}")
    flash("Phone deleted.", "success")
    return redirect(url_for("organizations.view_organization", org_id=org_id))


# ------------------------------------------------------------- reference links
#
# "Documents Link" feature — an organization can have any number of
# reference links (a web URL and/or a local document path, each with a
# short description/notes). Mirrors Contacts' contact_reference_links CRUD
# (blueprints/contacts.py) against its own, separate
# organization_reference_links table.

@organizations_bp.route("/<int:org_id>/links/new", methods=["GET", "POST"])
@login_required
def new_organization_link(org_id):
    db = get_db()
    org = _get_organization(db, org_id)
    if request.method == "POST":
        form = request.form
        db.execute(
            "INSERT INTO organization_reference_links (tenant_id, organization_id, url, document_path, description, notes) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                g.tenant_id, org_id, form.get("url", "").strip() or None, form.get("document_path", "").strip() or None,
                form.get("description", "").strip() or None, form.get("notes", "").strip() or None,
            ),
        )
        db.commit()
        log_action("Create", "organization_reference_link", org_id, "Added reference link")
        flash("Reference link added.", "success")
        return redirect(url_for("organizations.view_organization", org_id=org_id))
    return render_template("organizations/link_form.html", org=org, link=None)


@organizations_bp.route("/<int:org_id>/links/<int:link_id>/edit", methods=["GET", "POST"])
@login_required
def edit_organization_link(org_id, link_id):
    db = get_db()
    org = _get_organization(db, org_id)
    link = db.execute(
        "SELECT * FROM organization_reference_links WHERE link_id = ? AND organization_id = ? AND tenant_id = ?",
        (link_id, org_id, g.tenant_id),
    ).fetchone()
    if link is None:
        abort(404)
    if request.method == "POST":
        form = request.form
        db.execute(
            "UPDATE organization_reference_links SET url = ?, document_path = ?, description = ?, notes = ? "
            "WHERE link_id = ? AND organization_id = ? AND tenant_id = ?",
            (
                form.get("url", "").strip() or None, form.get("document_path", "").strip() or None,
                form.get("description", "").strip() or None, form.get("notes", "").strip() or None,
                link_id, org_id, g.tenant_id,
            ),
        )
        db.commit()
        log_action("Update", "organization_reference_link", org_id, f"Updated reference link #{link_id}")
        flash("Reference link updated.", "success")
        return redirect(url_for("organizations.view_organization", org_id=org_id))
    return render_template("organizations/link_form.html", org=org, link=link)


@organizations_bp.route("/<int:org_id>/links/<int:link_id>/delete", methods=["POST"])
@login_required
def delete_organization_link(org_id, link_id):
    db = get_db()
    _get_organization(db, org_id)
    db.execute(
        "DELETE FROM organization_reference_links WHERE link_id = ? AND organization_id = ? AND tenant_id = ?",
        (link_id, org_id, g.tenant_id),
    )
    db.commit()
    log_action("Delete", "organization_reference_link", org_id, f"Deleted reference link #{link_id}")
    flash("Reference link deleted.", "success")
    return redirect(url_for("organizations.view_organization", org_id=org_id))


@organizations_bp.route("/<int:org_id>/links/<int:link_id>/open")
@login_required
def open_organization_link(org_id, link_id):
    db = get_db()
    link = db.execute(
        "SELECT * FROM organization_reference_links WHERE link_id = ? AND organization_id = ? AND tenant_id = ?",
        (link_id, org_id, g.tenant_id),
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
    log_action("Open", "organization_reference_link", org_id, f"Opened {path}")
    return jsonify(ok=True)
