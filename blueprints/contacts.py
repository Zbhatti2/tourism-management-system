"""
Module A — Personal Contacts Management. The full reference implementation:
copy this file's patterns (list/view/edit, multi-value child records with
history tracking, soft-delete to archive) when building out Modules B-D.
"""
import json
import os
import uuid
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from flask import Blueprint, abort, flash, g, jsonify, redirect, render_template, request, send_from_directory, url_for
from werkzeug.utils import secure_filename

from auth.decorators import login_required
from config import Config
from db import get_db, log_action
from utils import open_local_path

contacts_bp = Blueprint("contacts", __name__)


# --------------------------------------------------------------- profile photo

def _save_profile_photo(file_storage):
    """Validate and save an uploaded profile photo; return the stored
    filename (not the full path — that's reconstructed from Config.UPLOADS_DIR
    when serving) or None if no valid file was provided."""
    if not file_storage or not file_storage.filename:
        return None
    ext = file_storage.filename.rsplit(".", 1)[-1].lower() if "." in file_storage.filename else ""
    if ext not in Config.ALLOWED_IMAGE_EXTENSIONS:
        flash(f"Profile image must be one of: {', '.join(sorted(Config.ALLOWED_IMAGE_EXTENSIONS))}.", "error")
        return None
    filename = secure_filename(f"{uuid.uuid4().hex}.{ext}")
    file_storage.save(os.path.join(Config.UPLOADS_DIR, filename))
    return filename


def _delete_profile_photo(filename):
    if not filename:
        return
    path = os.path.join(Config.UPLOADS_DIR, filename)
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


@contacts_bp.route("/<int:contact_id>/photo")
@login_required
def contact_photo(contact_id):
    db = get_db()
    contact = db.execute(
        "SELECT profile_image_path FROM contacts WHERE contact_id = ? AND tenant_id = ?", (contact_id, g.tenant_id)
    ).fetchone()
    if contact is None or not contact["profile_image_path"]:
        abort(404)
    return send_from_directory(Config.UPLOADS_DIR, contact["profile_image_path"])


GENDERS = ["Male", "Female", "Other"]


def _lookups(db):
    tenant_id = g.tenant_id
    return {
        "categories": db.execute(
            "SELECT * FROM contact_categories WHERE is_active = 1 AND tenant_id = ? ORDER BY label COLLATE NOCASE", (tenant_id,)
        ).fetchall(),
        "organizations": db.execute(
            "SELECT * FROM organizations WHERE tenant_id = ? ORDER BY organization_name", (tenant_id,)
        ).fetchall(),
        "countries": db.execute("SELECT * FROM countries WHERE is_active = 1 ORDER BY label").fetchall(),
        "states": db.execute("SELECT * FROM states WHERE is_active = 1 ORDER BY label").fetchall(),
        "titles": db.execute(
            "SELECT * FROM contact_titles WHERE is_active = 1 AND tenant_id = ? ORDER BY label COLLATE NOCASE", (tenant_id,)
        ).fetchall(),
        "suffixes": db.execute(
            "SELECT * FROM contact_suffixes WHERE is_active = 1 AND tenant_id = ? ORDER BY label COLLATE NOCASE", (tenant_id,)
        ).fetchall(),
        "professions": db.execute(
            "SELECT * FROM professions WHERE is_active = 1 AND tenant_id = ? ORDER BY label COLLATE NOCASE", (tenant_id,)
        ).fetchall(),
        "contexts": db.execute(
            "SELECT * FROM contact_contexts WHERE is_active = 1 AND tenant_id = ? ORDER BY label COLLATE NOCASE", (tenant_id,)
        ).fetchall(),
        "genders": GENDERS,
        "assistant_candidates": db.execute(
            "SELECT contact_id, full_name, file_as FROM contacts WHERE is_deleted = 0 AND tenant_id = ? ORDER BY file_as, full_name",
            (tenant_id,),
        ).fetchall(),
    }


# ---------------------------------------------------------------- list/view

@contacts_bp.route("/")
@login_required
def list_contacts():
    db = get_db()
    q = request.args.get("q", "").strip()
    category_id = request.args.get("category", "").strip()

    sql = """
        SELECT c.*, o.organization_name, cc.label AS category_label,
               pe.email_address AS primary_email,
               pp.country_code AS primary_phone_country_code,
               pp.area_code AS primary_phone_area_code,
               pp.number AS primary_phone_number,
               pp.extension AS primary_phone_extension,
               COALESCE(pcity.label, pa.city_text) AS city_label,
               COALESCE(pstate.label, pa.state_province_text) AS state_label,
               pcountry.label AS country_label
        FROM contacts c
        LEFT JOIN organizations o ON o.organization_id = c.current_organization_id
        LEFT JOIN contact_categories cc ON cc.contact_category_id = c.contact_category_id
        LEFT JOIN contact_emails pe ON pe.email_id = (
            SELECT ce.email_id FROM contact_emails ce
            WHERE ce.contact_id = c.contact_id
            ORDER BY ce.is_primary DESC, ce.email_id ASC
            LIMIT 1
        )
        LEFT JOIN contact_phones pp ON pp.phone_id = (
            SELECT cp.phone_id FROM contact_phones cp
            WHERE cp.contact_id = c.contact_id
            ORDER BY cp.is_primary DESC, cp.phone_id ASC
            LIMIT 1
        )
        LEFT JOIN addresses pa ON pa.address_id = (
            SELECT a.address_id FROM addresses a
            WHERE a.owner_type = 'Contact' AND a.owner_id = c.contact_id AND a.tenant_id = c.tenant_id
            ORDER BY a.is_primary DESC, a.address_id ASC
            LIMIT 1
        )
        LEFT JOIN cities pcity ON pcity.city_id = pa.city_id
        LEFT JOIN states pstate ON pstate.state_id = pa.state_id
        LEFT JOIN countries pcountry ON pcountry.country_id = pa.country_id
        WHERE c.is_deleted = 0 AND c.tenant_id = ?
    """
    params = [g.tenant_id]
    if q:
        sql += " AND (c.full_name LIKE ? OR c.file_as LIKE ? OR o.organization_name LIKE ?)"
        like = f"%{q}%"
        params += [like, like, like]
    if category_id:
        sql += " AND c.contact_category_id = ?"
        params.append(category_id)
    sql += " ORDER BY c.priority_contact DESC, c.file_as, c.full_name"

    contacts = db.execute(sql, params).fetchall()
    categories = db.execute(
        "SELECT * FROM contact_categories WHERE is_active = 1 AND tenant_id = ? ORDER BY label COLLATE NOCASE", (g.tenant_id,)
    ).fetchall()
    return render_template("contacts/list.html", contacts=contacts, categories=categories, q=q, category_id=category_id)


@contacts_bp.route("/<int:contact_id>")
@login_required
def view_contact(contact_id):
    db = get_db()
    contact = db.execute(
        """SELECT c.*, o.organization_name, cc.label AS category_label,
                  ct.label AS title_label, cs.label AS suffix_label,
                  pr.label AS profession_label, ac.full_name AS assistant_name,
                  cx.label AS context_label
           FROM contacts c
           LEFT JOIN organizations o ON o.organization_id = c.current_organization_id
           LEFT JOIN contact_categories cc ON cc.contact_category_id = c.contact_category_id
           LEFT JOIN contact_titles ct ON ct.contact_title_id = c.title_id
           LEFT JOIN contact_suffixes cs ON cs.contact_suffix_id = c.suffix_id
           LEFT JOIN professions pr ON pr.profession_id = c.profession_id
           LEFT JOIN contacts ac ON ac.contact_id = c.assistant_contact_id
           LEFT JOIN contact_contexts cx ON cx.contact_context_id = c.context_id
           WHERE c.contact_id = ? AND c.is_deleted = 0 AND c.tenant_id = ?""",
        (contact_id, g.tenant_id),
    ).fetchone()
    if contact is None:
        abort(404)

    emails = db.execute("SELECT * FROM contact_emails WHERE contact_id = ? ORDER BY is_primary DESC, email_id", (contact_id,)).fetchall()
    phones = db.execute("SELECT * FROM contact_phones WHERE contact_id = ? ORDER BY is_primary DESC, phone_id", (contact_id,)).fetchall()
    addresses = db.execute(
        "SELECT a.*, r.label AS region_label, s.label AS state_label, co.label AS country_label, "
        "ci.label AS city_label FROM addresses a "
        "LEFT JOIN regions r ON r.region_id = a.region_id "
        "LEFT JOIN states s ON s.state_id = a.state_id "
        "LEFT JOIN countries co ON co.country_id = a.country_id "
        "LEFT JOIN cities ci ON ci.city_id = a.city_id "
        "WHERE a.owner_type = 'Contact' AND a.owner_id = ? ORDER BY a.is_primary DESC, a.address_id",
        (contact_id,),
    ).fetchall()
    links = db.execute("SELECT * FROM contact_reference_links WHERE contact_id = ? ORDER BY link_id", (contact_id,)).fetchall()

    return render_template("contacts/view.html", contact=contact, emails=emails, phones=phones, addresses=addresses, links=links)


@contacts_bp.route("/<int:contact_id>/history")
@login_required
def contact_history(contact_id):
    db = get_db()
    contact = db.execute(
        "SELECT * FROM contacts WHERE contact_id = ? AND tenant_id = ?", (contact_id, g.tenant_id)
    ).fetchone()
    if contact is None:
        abort(404)
    core = db.execute("SELECT * FROM contacts_history WHERE contact_id = ? ORDER BY superseded_at DESC", (contact_id,)).fetchall()
    emails = db.execute(
        "SELECT h.* FROM contact_emails_history h WHERE h.contact_id = ? ORDER BY superseded_at DESC", (contact_id,)
    ).fetchall()
    phones = db.execute(
        "SELECT h.* FROM contact_phones_history h WHERE h.contact_id = ? ORDER BY superseded_at DESC", (contact_id,)
    ).fetchall()
    addresses = db.execute(
        "SELECT h.* FROM addresses_history h WHERE h.owner_type = 'Contact' AND h.owner_id = ? ORDER BY superseded_at DESC",
        (contact_id,),
    ).fetchall()
    return render_template(
        "contacts/history.html", contact=contact, core=core, emails=emails, phones=phones, addresses=addresses
    )


# --------------------------------------------------------------- create/edit

@contacts_bp.route("/new", methods=["GET", "POST"])
@login_required
def new_contact():
    db = get_db()
    if request.method == "POST":
        form = request.form
        is_deceased = 1 if form.get("is_deceased") else 0
        # Date of death is only meaningful when the deceased flag is set —
        # the form masks/disables the field otherwise, but enforce it here
        # too in case the request didn't come from a JS-enabled browser.
        date_of_death = (form.get("date_of_death") or None) if is_deceased else None
        gender = form.get("gender") or None
        if gender not in GENDERS:
            gender = None
        db.execute(
            """INSERT INTO contacts
               (tenant_id, title_id, full_name, suffix_id, gender, file_as, current_organization_id, current_job_title, profession_id,
                web_page, contact_category_id, context_id, assistant_contact_id, priority_contact, date_of_birth, is_deceased,
                date_of_death, notes, knowledge_graph_data)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                g.tenant_id,
                form.get("title_id") or None,
                form["full_name"].strip(),
                form.get("suffix_id") or None,
                gender,
                form.get("file_as", "").strip() or None,
                form.get("current_organization_id") or None,
                form.get("current_job_title", "").strip() or None,
                form.get("profession_id") or None,
                form.get("web_page", "").strip() or None,
                form.get("contact_category_id") or None,
                form.get("context_id") or None,
                form.get("assistant_contact_id") or None,
                1 if form.get("priority_contact") else 0,
                form.get("date_of_birth") or None,
                is_deceased,
                date_of_death,
                form.get("notes", "").strip() or None,
                form.get("knowledge_graph_data", "").strip() or None,
            ),
        )
        db.commit()
        contact_id = db.execute("SELECT last_insert_rowid() id").fetchone()["id"]

        photo_filename = _save_profile_photo(request.files.get("profile_image"))
        if photo_filename:
            db.execute("UPDATE contacts SET profile_image_path = ? WHERE contact_id = ?", (photo_filename, contact_id))
            db.commit()

        log_action("Create", "contact", contact_id, f"Created contact {form['full_name']}")
        flash("Contact created.", "success")
        # When this form was reached via "+ Add Contact" on an Organization's
        # page (see organizations/view.html's Contacts card), `next` carries
        # that organization's own page so saving returns there instead of to
        # the new contact's page — same "next" convention new_organization/
        # new_assistant_contact below already use for their own quick-add-and-
        # return flows.
        next_url = request.form.get("next")
        if next_url:
            return redirect(next_url)
        return redirect(url_for("contacts.view_contact", contact_id=contact_id))

    preselect_assistant_id = request.args.get("assistant_id", type=int)
    preselect_organization_id = request.args.get("organization_id", type=int)
    return render_template(
        "contacts/form.html", contact=None,
        preselect_assistant_id=preselect_assistant_id,
        preselect_organization_id=preselect_organization_id,
        next_url=request.args.get("next"),
        **_lookups(db)
    )


@contacts_bp.route("/<int:contact_id>/edit", methods=["GET", "POST"])
@login_required
def edit_contact(contact_id):
    db = get_db()
    contact = db.execute(
        "SELECT * FROM contacts WHERE contact_id = ? AND is_deleted = 0 AND tenant_id = ?", (contact_id, g.tenant_id)
    ).fetchone()
    if contact is None:
        abort(404)

    if request.method == "POST":
        form = request.form
        new_org = form.get("current_organization_id") or None
        new_title = form.get("current_job_title", "").strip() or None

        # spec §2.1: mutable fields are never overwritten silently — the
        # previous value is snapshotted to *_history first, in the same
        # transaction as the update.
        if str(contact["current_organization_id"] or "") != str(new_org or ""):
            db.execute(
                "INSERT INTO contacts_history (tenant_id, contact_id, field_name, previous_value) VALUES (?, ?, ?, ?)",
                (g.tenant_id, contact_id, "current_organization_id", str(contact["current_organization_id"] or "")),
            )
        if (contact["current_job_title"] or "") != (new_title or ""):
            db.execute(
                "INSERT INTO contacts_history (tenant_id, contact_id, field_name, previous_value) VALUES (?, ?, ?, ?)",
                (g.tenant_id, contact_id, "current_job_title", contact["current_job_title"] or ""),
            )

        is_deceased = 1 if form.get("is_deceased") else 0
        date_of_death = (form.get("date_of_death") or None) if is_deceased else None

        new_assistant = form.get("assistant_contact_id") or None
        if new_assistant and int(new_assistant) == contact_id:
            flash("A contact can't be their own assistant.", "error")
            new_assistant = None

        gender = form.get("gender") or None
        if gender not in GENDERS:
            gender = None

        db.execute(
            """UPDATE contacts SET title_id=?, full_name=?, suffix_id=?, gender=?, file_as=?, current_organization_id=?, current_job_title=?,
               profession_id=?, web_page=?, contact_category_id=?, context_id=?, assistant_contact_id=?, priority_contact=?,
               date_of_birth=?, is_deceased=?, date_of_death=?, notes=?, knowledge_graph_data=?, updated_at=datetime('now')
               WHERE contact_id=? AND tenant_id=?""",
            (
                form.get("title_id") or None,
                form["full_name"].strip(),
                form.get("suffix_id") or None,
                gender,
                form.get("file_as", "").strip() or None,
                new_org,
                new_title,
                form.get("profession_id") or None,
                form.get("web_page", "").strip() or None,
                form.get("contact_category_id") or None,
                form.get("context_id") or None,
                new_assistant,
                1 if form.get("priority_contact") else 0,
                form.get("date_of_birth") or None,
                is_deceased,
                date_of_death,
                form.get("notes", "").strip() or None,
                form.get("knowledge_graph_data", "").strip() or None,
                contact_id,
                g.tenant_id,
            ),
        )

        if form.get("remove_photo"):
            _delete_profile_photo(contact["profile_image_path"])
            db.execute(
                "UPDATE contacts SET profile_image_path = NULL WHERE contact_id = ? AND tenant_id = ?",
                (contact_id, g.tenant_id),
            )
        else:
            photo_filename = _save_profile_photo(request.files.get("profile_image"))
            if photo_filename:
                _delete_profile_photo(contact["profile_image_path"])
                db.execute(
                    "UPDATE contacts SET profile_image_path = ? WHERE contact_id = ? AND tenant_id = ?",
                    (photo_filename, contact_id, g.tenant_id),
                )

        db.commit()
        log_action("Update", "contact", contact_id, "Updated core contact fields")
        flash("Contact updated.", "success")
        return redirect(url_for("contacts.view_contact", contact_id=contact_id))

    preselect_assistant_id = request.args.get("assistant_id", type=int)
    return render_template("contacts/form.html", contact=contact, preselect_assistant_id=preselect_assistant_id, **_lookups(db))


@contacts_bp.route("/<int:contact_id>/delete", methods=["POST"])
@login_required
def delete_contact(contact_id):
    db = get_db()
    contact = db.execute(
        "SELECT * FROM contacts WHERE contact_id = ? AND is_deleted = 0 AND tenant_id = ?", (contact_id, g.tenant_id)
    ).fetchone()
    if contact is None:
        abort(404)

    snapshot = {
        "contact": dict(contact),
        "emails": [dict(r) for r in db.execute("SELECT * FROM contact_emails WHERE contact_id = ?", (contact_id,))],
        "phones": [dict(r) for r in db.execute("SELECT * FROM contact_phones WHERE contact_id = ?", (contact_id,))],
        "addresses": [dict(r) for r in db.execute("SELECT * FROM addresses WHERE owner_type='Contact' AND owner_id = ?", (contact_id,))],
        "links": [dict(r) for r in db.execute("SELECT * FROM contact_reference_links WHERE contact_id = ?", (contact_id,))],
    }
    # Retention is per-tenant (tenants.data_retention_days — see
    # system_mgmt.py's "Retention & purge" box), not a global system_config
    # table (that table doesn't exist in this schema; this used to query it
    # and would 500 on every delete — fixed here).
    retention_row = db.execute("SELECT data_retention_days FROM tenants WHERE tenant_id = ?", (g.tenant_id,)).fetchone()
    retention_days = retention_row["data_retention_days"] if retention_row else None
    if retention_days is not None:
        # Purge-eligibility counts from the record's date of entry (created_at),
        # not from the moment it's archived.
        db.execute(
            "INSERT INTO contacts_archive (contact_id, tenant_id, snapshot, purge_eligible_at) "
            "VALUES (?, ?, ?, datetime(?, ?))",
            (contact_id, g.tenant_id, json.dumps(snapshot, default=str), contact["created_at"], f"+{retention_days} days"),
        )
    else:
        # No retention window configured — indefinite retention, never purge-eligible.
        db.execute(
            "INSERT INTO contacts_archive (contact_id, tenant_id, snapshot, purge_eligible_at) VALUES (?, ?, ?, NULL)",
            (contact_id, g.tenant_id, json.dumps(snapshot, default=str)),
        )
    db.execute(
        "UPDATE contacts SET is_deleted = 1, updated_at = datetime('now') WHERE contact_id = ? AND tenant_id = ?",
        (contact_id, g.tenant_id),
    )
    db.commit()
    log_action("Delete", "contact", contact_id, f"Archived contact {contact['full_name']}")
    flash("Contact deleted (moved to archive).", "success")
    return redirect(url_for("contacts.list_contacts"))


# --------------------------------------------------------------- quick org add

@contacts_bp.route("/organizations/new", methods=["GET", "POST"])
@login_required
def new_organization():
    db = get_db()
    next_url = request.values.get("next") or url_for("contacts.new_contact")
    if request.method == "POST":
        form = request.form
        db.execute(
            "INSERT INTO organizations (tenant_id, organization_name, full_address, phone, email, organization_type_id) VALUES (?, ?, ?, ?, ?, ?)",
            (
                g.tenant_id,
                form["organization_name"].strip(),
                form.get("full_address", "").strip() or None,
                form.get("phone", "").strip() or None,
                form.get("email", "").strip() or None,
                form.get("organization_type_id") or None,
            ),
        )
        db.commit()
        org_id = db.execute("SELECT last_insert_rowid() id").fetchone()["id"]
        log_action("Create", "organization", org_id, f"Created organization {form['organization_name']}")
        flash("Organization created.", "success")
        return redirect(request.form.get("next") or url_for("contacts.new_contact"))

    org_types = db.execute(
        "SELECT * FROM organization_types WHERE is_active = 1 AND tenant_id = ? ORDER BY label COLLATE NOCASE", (g.tenant_id,)
    ).fetchall()
    return render_template("contacts/organization_form.html", org_types=org_types, next_url=next_url)


@contacts_bp.route("/assistants/new", methods=["GET", "POST"])
@login_required
def new_assistant_contact():
    """Short quick-add form for a contact who is about to be selected as
    someone else's Assistant. Full name only is required; phone/email are
    optional convenience fields. Everything else can be filled in later by
    opening and editing the resulting contact record."""
    db = get_db()
    next_url = request.values.get("next") or url_for("contacts.new_contact")
    if request.method == "POST":
        form = request.form
        full_name = form.get("full_name", "").strip()
        if not full_name:
            flash("Full name is required.", "error")
            return render_template("contacts/assistant_form.html", next_url=request.form.get("next") or next_url)

        db.execute("INSERT INTO contacts (tenant_id, full_name) VALUES (?, ?)", (g.tenant_id, full_name))
        db.commit()
        new_contact_id = db.execute("SELECT last_insert_rowid() id").fetchone()["id"]

        email_address = form.get("email_address", "").strip()
        if email_address:
            db.execute(
                "INSERT INTO contact_emails (tenant_id, contact_id, email_address, is_primary) VALUES (?, ?, ?, 1)",
                (g.tenant_id, new_contact_id, email_address),
            )
        phone_number = form.get("phone_number", "").strip()
        if phone_number:
            db.execute(
                "INSERT INTO contact_phones (tenant_id, contact_id, phone_type, number, is_primary) VALUES (?, ?, 'Mobile', ?, 1)",
                (g.tenant_id, new_contact_id, phone_number),
            )
        db.commit()

        log_action("Create", "contact", new_contact_id, f"Quick-added contact {full_name}")
        flash(f"'{full_name}' added.", "success")

        target = request.form.get("next") or next_url
        parts = urlsplit(target)
        query = parse_qsl(parts.query)
        query.append(("assistant_id", str(new_contact_id)))
        return redirect(urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)))

    return render_template("contacts/assistant_form.html", next_url=next_url)


# --------------------------------------------------------------- emails

@contacts_bp.route("/<int:contact_id>/emails/new", methods=["GET", "POST"])
@login_required
def new_email(contact_id):
    db = get_db()
    contact = db.execute(
        "SELECT * FROM contacts WHERE contact_id = ? AND tenant_id = ?", (contact_id, g.tenant_id)
    ).fetchone()
    if contact is None:
        abort(404)
    if request.method == "POST":
        form = request.form
        if form.get("is_primary"):
            db.execute(
                "UPDATE contact_emails SET is_primary = 0 WHERE contact_id = ? AND tenant_id = ?",
                (contact_id, g.tenant_id),
            )
        db.execute(
            "INSERT INTO contact_emails (tenant_id, contact_id, email_address, is_primary) VALUES (?, ?, ?, ?)",
            (g.tenant_id, contact_id, form["email_address"].strip(), 1 if form.get("is_primary") else 0),
        )
        db.commit()
        log_action("Create", "contact_email", contact_id, f"Added email {form['email_address']}")
        return redirect(url_for("contacts.view_contact", contact_id=contact_id))
    return render_template("contacts/email_form.html", contact=contact, email=None)


@contacts_bp.route("/<int:contact_id>/emails/<int:email_id>/edit", methods=["GET", "POST"])
@login_required
def edit_email(contact_id, email_id):
    db = get_db()
    email = db.execute(
        "SELECT * FROM contact_emails WHERE email_id = ? AND contact_id = ? AND tenant_id = ?",
        (email_id, contact_id, g.tenant_id),
    ).fetchone()
    if email is None:
        abort(404)
    if request.method == "POST":
        form = request.form
        new_address = form["email_address"].strip()
        if new_address != email["email_address"]:
            db.execute(
                "INSERT INTO contact_emails_history (tenant_id, email_id, contact_id, previous_value) VALUES (?, ?, ?, ?)",
                (g.tenant_id, email_id, contact_id, email["email_address"]),
            )
        if form.get("is_primary"):
            db.execute(
                "UPDATE contact_emails SET is_primary = 0 WHERE contact_id = ? AND tenant_id = ?",
                (contact_id, g.tenant_id),
            )
        db.execute(
            "UPDATE contact_emails SET email_address = ?, is_primary = ?, updated_at = datetime('now') WHERE email_id = ? AND tenant_id = ?",
            (new_address, 1 if form.get("is_primary") else 0, email_id, g.tenant_id),
        )
        db.commit()
        log_action("Update", "contact_email", contact_id, f"Updated email #{email_id}")
        return redirect(url_for("contacts.view_contact", contact_id=contact_id))
    return render_template("contacts/email_form.html", contact={"contact_id": contact_id}, email=email)


@contacts_bp.route("/<int:contact_id>/emails/<int:email_id>/delete", methods=["POST"])
@login_required
def delete_email(contact_id, email_id):
    db = get_db()
    email = db.execute(
        "SELECT * FROM contact_emails WHERE email_id = ? AND contact_id = ? AND tenant_id = ?",
        (email_id, contact_id, g.tenant_id),
    ).fetchone()
    if email is None:
        abort(404)
    db.execute(
        "INSERT INTO contact_emails_history (tenant_id, email_id, contact_id, previous_value, superseded_reason) VALUES (?, ?, ?, ?, 'Deleted')",
        (g.tenant_id, email_id, contact_id, email["email_address"]),
    )
    db.execute("DELETE FROM contact_emails WHERE email_id = ? AND tenant_id = ?", (email_id, g.tenant_id))
    db.commit()
    log_action("Delete", "contact_email", contact_id, f"Deleted email #{email_id}")
    return redirect(url_for("contacts.view_contact", contact_id=contact_id))


# --------------------------------------------------------------- phones

PHONE_TYPES = ["Business", "Home", "Mobile", "WhatsApp", "Business Fax"]


@contacts_bp.route("/<int:contact_id>/phones/new", methods=["GET", "POST"])
@login_required
def new_phone(contact_id):
    db = get_db()
    contact = db.execute(
        "SELECT * FROM contacts WHERE contact_id = ? AND tenant_id = ?", (contact_id, g.tenant_id)
    ).fetchone()
    if contact is None:
        abort(404)
    if request.method == "POST":
        form = request.form
        if form.get("is_primary"):
            db.execute(
                "UPDATE contact_phones SET is_primary = 0 WHERE contact_id = ? AND tenant_id = ?",
                (contact_id, g.tenant_id),
            )
        db.execute(
            """INSERT INTO contact_phones (tenant_id, contact_id, phone_type, country_code, area_code, number, extension, is_primary)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                g.tenant_id, contact_id, form["phone_type"], form.get("country_code", "").strip() or None,
                form.get("area_code", "").strip() or None, form["number"].strip(),
                form.get("extension", "").strip() or None, 1 if form.get("is_primary") else 0,
            ),
        )
        db.commit()
        log_action("Create", "contact_phone", contact_id, f"Added {form['phone_type']} phone")
        return redirect(url_for("contacts.view_contact", contact_id=contact_id))
    return render_template("contacts/phone_form.html", contact=contact, phone=None, phone_types=PHONE_TYPES)


@contacts_bp.route("/<int:contact_id>/phones/<int:phone_id>/edit", methods=["GET", "POST"])
@login_required
def edit_phone(contact_id, phone_id):
    db = get_db()
    phone = db.execute(
        "SELECT * FROM contact_phones WHERE phone_id = ? AND contact_id = ? AND tenant_id = ?",
        (phone_id, contact_id, g.tenant_id),
    ).fetchone()
    if phone is None:
        abort(404)
    if request.method == "POST":
        form = request.form
        old_snapshot = json.dumps({
            "phone_type": phone["phone_type"], "country_code": phone["country_code"],
            "area_code": phone["area_code"], "number": phone["number"], "extension": phone["extension"],
        })
        new_number = form["number"].strip()
        new_country = form.get("country_code", "").strip() or None
        new_area = form.get("area_code", "").strip() or None
        new_ext = form.get("extension", "").strip() or None
        if (new_number, new_country, new_area, new_ext) != (phone["number"], phone["country_code"], phone["area_code"], phone["extension"]):
            db.execute(
                "INSERT INTO contact_phones_history (tenant_id, phone_id, contact_id, previous_value) VALUES (?, ?, ?, ?)",
                (g.tenant_id, phone_id, contact_id, old_snapshot),
            )
        if form.get("is_primary"):
            db.execute(
                "UPDATE contact_phones SET is_primary = 0 WHERE contact_id = ? AND tenant_id = ?",
                (contact_id, g.tenant_id),
            )
        db.execute(
            """UPDATE contact_phones SET phone_type=?, country_code=?, area_code=?, number=?, extension=?,
               is_primary=?, updated_at=datetime('now') WHERE phone_id=? AND tenant_id=?""",
            (form["phone_type"], new_country, new_area, new_number, new_ext, 1 if form.get("is_primary") else 0, phone_id, g.tenant_id),
        )
        db.commit()
        log_action("Update", "contact_phone", contact_id, f"Updated phone #{phone_id}")
        return redirect(url_for("contacts.view_contact", contact_id=contact_id))
    return render_template("contacts/phone_form.html", contact={"contact_id": contact_id}, phone=phone, phone_types=PHONE_TYPES)


@contacts_bp.route("/<int:contact_id>/phones/<int:phone_id>/delete", methods=["POST"])
@login_required
def delete_phone(contact_id, phone_id):
    db = get_db()
    phone = db.execute(
        "SELECT * FROM contact_phones WHERE phone_id = ? AND contact_id = ? AND tenant_id = ?",
        (phone_id, contact_id, g.tenant_id),
    ).fetchone()
    if phone is None:
        abort(404)
    snapshot = json.dumps({
        "phone_type": phone["phone_type"], "country_code": phone["country_code"],
        "area_code": phone["area_code"], "number": phone["number"], "extension": phone["extension"],
    })
    db.execute(
        "INSERT INTO contact_phones_history (tenant_id, phone_id, contact_id, previous_value, superseded_reason) VALUES (?, ?, ?, ?, 'Deleted')",
        (g.tenant_id, phone_id, contact_id, snapshot),
    )
    db.execute("DELETE FROM contact_phones WHERE phone_id = ? AND tenant_id = ?", (phone_id, g.tenant_id))
    db.commit()
    log_action("Delete", "contact_phone", contact_id, f"Deleted phone #{phone_id}")
    return redirect(url_for("contacts.view_contact", contact_id=contact_id))


# --------------------------------------------------------------- addresses

ADDRESS_TYPES = ["Home", "Business"]


@contacts_bp.route("/<int:contact_id>/addresses/new", methods=["GET", "POST"])
@login_required
def new_address(contact_id):
    db = get_db()
    contact = db.execute(
        "SELECT * FROM contacts WHERE contact_id = ? AND tenant_id = ?", (contact_id, g.tenant_id)
    ).fetchone()
    if contact is None:
        abort(404)
    if request.method == "POST":
        form = request.form
        if form.get("is_primary"):
            db.execute(
                "UPDATE addresses SET is_primary = 0 WHERE owner_type='Contact' AND owner_id = ? AND tenant_id = ?",
                (contact_id, g.tenant_id),
            )
        db.execute(
            """INSERT INTO addresses (tenant_id, owner_type, owner_id, address_type, street, unit,
               region_id, country_id, state_id, state_province_text, city_id, city_text, postal_code, is_primary)
               VALUES (?, 'Contact', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                g.tenant_id, contact_id, form.get("address_type") or None, form.get("street", "").strip() or None,
                form.get("unit", "").strip() or None,
                form.get("region_id") or None, form.get("country_id") or None,
                form.get("state_id") or None, form.get("state_province_text", "").strip() or None,
                form.get("city_id") or None, form.get("city_text", "").strip() or None,
                form.get("postal_code", "").strip() or None, 1 if form.get("is_primary") else 0,
            ),
        )
        db.commit()
        log_action("Create", "address", contact_id, "Added address")
        return redirect(url_for("contacts.view_contact", contact_id=contact_id))
    return render_template("contacts/address_form.html", contact=contact, address=None, address_types=ADDRESS_TYPES, **_lookups(db))


@contacts_bp.route("/<int:contact_id>/addresses/<int:address_id>/edit", methods=["GET", "POST"])
@login_required
def edit_address(contact_id, address_id):
    db = get_db()
    address = db.execute(
        "SELECT * FROM addresses WHERE address_id = ? AND owner_type='Contact' AND owner_id = ? AND tenant_id = ?",
        (address_id, contact_id, g.tenant_id),
    ).fetchone()
    if address is None:
        abort(404)
    if request.method == "POST":
        form = request.form
        old_snapshot = json.dumps(dict(address), default=str)
        if form.get("is_primary"):
            db.execute(
                "UPDATE addresses SET is_primary = 0 WHERE owner_type='Contact' AND owner_id = ? AND tenant_id = ?",
                (contact_id, g.tenant_id),
            )
        db.execute(
            "INSERT INTO addresses_history (tenant_id, address_id, owner_type, owner_id, previous_value) VALUES (?, ?, 'Contact', ?, ?)",
            (g.tenant_id, address_id, contact_id, old_snapshot),
        )
        db.execute(
            """UPDATE addresses SET address_type=?, street=?, unit=?, region_id=?, country_id=?, state_id=?,
               state_province_text=?, city_id=?, city_text=?, postal_code=?, is_primary=?,
               updated_at=datetime('now') WHERE address_id=? AND tenant_id=?""",
            (
                form.get("address_type") or None, form.get("street", "").strip() or None,
                form.get("unit", "").strip() or None,
                form.get("region_id") or None, form.get("country_id") or None,
                form.get("state_id") or None, form.get("state_province_text", "").strip() or None,
                form.get("city_id") or None, form.get("city_text", "").strip() or None,
                form.get("postal_code", "").strip() or None, 1 if form.get("is_primary") else 0,
                address_id, g.tenant_id,
            ),
        )
        db.commit()
        log_action("Update", "address", contact_id, f"Updated address #{address_id}")
        return redirect(url_for("contacts.view_contact", contact_id=contact_id))
    return render_template(
        "contacts/address_form.html", contact={"contact_id": contact_id}, address=address, address_types=ADDRESS_TYPES, **_lookups(db)
    )


@contacts_bp.route("/<int:contact_id>/addresses/<int:address_id>/delete", methods=["POST"])
@login_required
def delete_address(contact_id, address_id):
    db = get_db()
    address = db.execute(
        "SELECT * FROM addresses WHERE address_id = ? AND owner_type='Contact' AND owner_id = ? AND tenant_id = ?",
        (address_id, contact_id, g.tenant_id),
    ).fetchone()
    if address is None:
        abort(404)
    db.execute(
        "INSERT INTO addresses_history (tenant_id, address_id, owner_type, owner_id, previous_value, superseded_reason) VALUES (?, ?, 'Contact', ?, ?, 'Deleted')",
        (g.tenant_id, address_id, contact_id, json.dumps(dict(address), default=str)),
    )
    db.execute("DELETE FROM addresses WHERE address_id = ? AND tenant_id = ?", (address_id, g.tenant_id))
    db.commit()
    log_action("Delete", "address", contact_id, f"Deleted address #{address_id}")
    return redirect(url_for("contacts.view_contact", contact_id=contact_id))


# --------------------------------------------------------------- reference links

@contacts_bp.route("/<int:contact_id>/links/new", methods=["GET", "POST"])
@login_required
def new_link(contact_id):
    db = get_db()
    contact = db.execute(
        "SELECT * FROM contacts WHERE contact_id = ? AND tenant_id = ?", (contact_id, g.tenant_id)
    ).fetchone()
    if contact is None:
        abort(404)
    if request.method == "POST":
        form = request.form
        db.execute(
            "INSERT INTO contact_reference_links (tenant_id, contact_id, url, document_path, description, notes) VALUES (?, ?, ?, ?, ?, ?)",
            (
                g.tenant_id, contact_id, form.get("url", "").strip() or None, form.get("document_path", "").strip() or None,
                form.get("description", "").strip() or None, form.get("notes", "").strip() or None,
            ),
        )
        db.commit()
        log_action("Create", "contact_reference_link", contact_id, "Added reference link")
        return redirect(url_for("contacts.view_contact", contact_id=contact_id))
    return render_template("contacts/link_form.html", contact=contact, link=None)


@contacts_bp.route("/<int:contact_id>/links/<int:link_id>/edit", methods=["GET", "POST"])
@login_required
def edit_link(contact_id, link_id):
    db = get_db()
    contact = db.execute(
        "SELECT * FROM contacts WHERE contact_id = ? AND tenant_id = ?", (contact_id, g.tenant_id)
    ).fetchone()
    if contact is None:
        abort(404)
    link = db.execute(
        "SELECT * FROM contact_reference_links WHERE link_id = ? AND contact_id = ? AND tenant_id = ?",
        (link_id, contact_id, g.tenant_id),
    ).fetchone()
    if link is None:
        abort(404)
    if request.method == "POST":
        form = request.form
        db.execute(
            "UPDATE contact_reference_links SET url = ?, document_path = ?, description = ?, notes = ? "
            "WHERE link_id = ? AND contact_id = ? AND tenant_id = ?",
            (
                form.get("url", "").strip() or None, form.get("document_path", "").strip() or None,
                form.get("description", "").strip() or None, form.get("notes", "").strip() or None,
                link_id, contact_id, g.tenant_id,
            ),
        )
        db.commit()
        log_action("Update", "contact_reference_link", contact_id, f"Updated reference link #{link_id}")
        flash("Reference link updated.", "success")
        return redirect(url_for("contacts.view_contact", contact_id=contact_id))
    return render_template("contacts/link_form.html", contact=contact, link=link)


@contacts_bp.route("/<int:contact_id>/links/<int:link_id>/delete", methods=["POST"])
@login_required
def delete_link(contact_id, link_id):
    db = get_db()
    db.execute(
        "DELETE FROM contact_reference_links WHERE link_id = ? AND contact_id = ? AND tenant_id = ?",
        (link_id, contact_id, g.tenant_id),
    )
    db.commit()
    log_action("Delete", "contact_reference_link", contact_id, f"Deleted reference link #{link_id}")
    return redirect(url_for("contacts.view_contact", contact_id=contact_id))


@contacts_bp.route("/<int:contact_id>/links/<int:link_id>/open")
@login_required
def open_link(contact_id, link_id):
    db = get_db()
    link = db.execute(
        "SELECT * FROM contact_reference_links WHERE link_id = ? AND contact_id = ? AND tenant_id = ?",
        (link_id, contact_id, g.tenant_id),
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
    log_action("Open", "contact_reference_link", contact_id, f"Opened {path}")
    return jsonify(ok=True)
