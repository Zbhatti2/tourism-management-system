"""
Module — Human Resources (Human_Resource_Module1a.docx). Sits below
Suppliers, above Points of Interest on the sidebar (app.py MODULES).

Covers Internal Human Resources (Employees — salaried staff) plus one
Roles lookup (hr_roles/hr_role_assignments) originally shared with
External Resources. External Resources (non-salaried/contract people —
tours, transport, IT, cleaning, security, ...) used to be their own record
type here; they were retired in favor of Suppliers with
suppliers.is_external_resource = 1 set (see blueprints/suppliers.py and
the retirement note above update_employee_roles at the bottom of this
file) — a contractor is invoiced like any other vendor, so it made more
sense to model it as a flagged Supplier than as a separate HR entity.

Employees get the same History + Archive + Purge/Retention treatment as
Contacts (see blueprints/contacts.py and system_mgmt.py's
_purge_eligible/_purge_eligible_hr) — HR data (salary, department, job
title) is exactly the kind of record worth a change trail and a retention
policy. Deletion here is therefore a soft-delete (is_deleted=1) plus an
archive snapshot, never an immediate hard delete, mirroring
delete_contact() in blueprints/contacts.py.

Host Organization (the tenant's own company — where an Employee's Office
Work Address is picked from) is managed from System Management, not here —
see blueprints/system_mgmt.py's host-organization routes — since it's
tenant infrastructure rather than a person record.
"""
import json

from flask import Blueprint, abort, flash, g, jsonify, redirect, render_template, request, send_from_directory, url_for
from werkzeug.utils import secure_filename
import os
import uuid

from auth.decorators import login_required
from config import Config
from db import get_db, log_action
from utils import open_local_path

hr_bp = Blueprint("hr", __name__)


# =============================================================== lookups

def _genders(db):
    return db.execute(
        "SELECT * FROM genders WHERE is_active = 1 AND tenant_id = ? ORDER BY label COLLATE NOCASE", (g.tenant_id,)
    ).fetchall()


def _employee_types(db):
    return db.execute(
        "SELECT * FROM employee_types WHERE is_active = 1 AND tenant_id = ? ORDER BY label COLLATE NOCASE", (g.tenant_id,)
    ).fetchall()


def _departments(db):
    return db.execute(
        "SELECT * FROM departments WHERE is_active = 1 AND tenant_id = ? ORDER BY label COLLATE NOCASE", (g.tenant_id,)
    ).fetchall()


def _job_titles(db):
    return db.execute(
        "SELECT * FROM job_titles WHERE is_active = 1 AND tenant_id = ? ORDER BY label COLLATE NOCASE", (g.tenant_id,)
    ).fetchall()


def _employee_phone_types(db):
    return db.execute(
        "SELECT * FROM employee_phone_types WHERE is_active = 1 AND tenant_id = ? ORDER BY label COLLATE NOCASE", (g.tenant_id,)
    ).fetchall()


def _hr_roles(db):
    return db.execute(
        "SELECT * FROM hr_roles WHERE is_active = 1 AND tenant_id = ? ORDER BY label COLLATE NOCASE", (g.tenant_id,)
    ).fetchall()


def _manager_options(db, exclude_employee_id=None):
    """Every other active Employee at this tenant, alphabetical — what the
    "Reports to" picker on the Employee form offers. Excludes the employee
    being edited so nobody can be set as their own manager; deeper cycles
    (A reports to B who reports to A) aren't checked."""
    sql = "SELECT employee_id, full_name FROM employees WHERE tenant_id = ? AND is_deleted = 0"
    params = [g.tenant_id]
    if exclude_employee_id:
        sql += " AND employee_id != ?"
        params.append(exclude_employee_id)
    sql += " ORDER BY full_name COLLATE NOCASE"
    return db.execute(sql, params).fetchall()


def _countries(db):
    return db.execute("SELECT * FROM countries WHERE is_active = 1 ORDER BY label").fetchall()


def _host_address_options(db):
    """Every Host Organization office/location address, tagged for display
    with its type and a City/Country summary — this is what an Employee's
    Office Work Address is picked from (see schema.sql MODULE H)."""
    return db.execute(
        """SELECT ha.host_address_id, hat.label AS type_label,
                  COALESCE(c.label, ha.city_text) AS city_label,
                  co.label AS country_label
           FROM host_addresses ha
           LEFT JOIN host_address_types hat ON hat.address_type_id = ha.address_type_id
           LEFT JOIN cities c ON c.city_id = ha.city_id
           LEFT JOIN countries co ON co.country_id = ha.country_id
           WHERE ha.tenant_id = ?
           ORDER BY hat.sort_order, ha.host_address_id""",
        (g.tenant_id,),
    ).fetchall()


def _role_ids_for(db, owner_type, owner_id):
    rows = db.execute(
        "SELECT role_id FROM hr_role_assignments WHERE owner_type = ? AND owner_id = ? AND tenant_id = ?",
        (owner_type, owner_id, g.tenant_id),
    ).fetchall()
    return {r["role_id"] for r in rows}


def _set_roles(db, owner_type, owner_id, role_ids):
    """Replace the full set of role assignments for one Employee/External
    Resource with `role_ids` — the multi-select role checklist on the
    edit/view form posts the complete desired set each time, so this is a
    clear-and-reinsert rather than a diff."""
    db.execute(
        "DELETE FROM hr_role_assignments WHERE owner_type = ? AND owner_id = ? AND tenant_id = ?",
        (owner_type, owner_id, g.tenant_id),
    )
    for role_id in role_ids:
        db.execute(
            "INSERT INTO hr_role_assignments (tenant_id, owner_type, owner_id, role_id) VALUES (?, ?, ?, ?)",
            (g.tenant_id, owner_type, owner_id, role_id),
        )


def _save_profile_photo(file_storage):
    if not file_storage or not file_storage.filename:
        return None
    ext = file_storage.filename.rsplit(".", 1)[-1].lower() if "." in file_storage.filename else ""
    if ext not in Config.ALLOWED_IMAGE_EXTENSIONS:
        flash(f"Profile image must be one of: {', '.join(sorted(Config.ALLOWED_IMAGE_EXTENSIONS))}.", "error")
        return None
    filename = secure_filename(f"{uuid.uuid4().hex}.{ext}")
    file_storage.save(os.path.join(Config.EMPLOYEE_UPLOADS_DIR, filename))
    return filename


def _delete_profile_photo(filename):
    if not filename:
        return
    path = os.path.join(Config.EMPLOYEE_UPLOADS_DIR, filename)
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


@hr_bp.route("/employees/<int:employee_id>/photo")
@login_required
def employee_photo(employee_id):
    db = get_db()
    row = db.execute(
        "SELECT profile_image_path FROM employees WHERE employee_id = ? AND tenant_id = ?", (employee_id, g.tenant_id)
    ).fetchone()
    if row is None or not row["profile_image_path"]:
        abort(404)
    return send_from_directory(Config.EMPLOYEE_UPLOADS_DIR, row["profile_image_path"])


# =============================================================== index

@hr_bp.route("/")
@login_required
def index():
    db = get_db()
    employee_count = db.execute(
        "SELECT COUNT(*) c FROM employees WHERE tenant_id = ? AND is_deleted = 0", (g.tenant_id,)
    ).fetchone()["c"]
    resource_count = db.execute(
        "SELECT COUNT(*) c FROM suppliers WHERE tenant_id = ? AND is_deleted = 0 AND is_external_resource = 1", (g.tenant_id,)
    ).fetchone()["c"]
    return render_template("hr/index.html", employee_count=employee_count, resource_count=resource_count)


# =============================================================== employees

def _get_employee(db, employee_id):
    emp = db.execute(
        """SELECT e.*, et.label AS type_label, d.label AS department_label, jt.label AS job_title_label,
                  cc.label AS citizenship_label, gn.label AS gender_label,
                  pr.label AS primary_role_label, pr.description AS primary_role_duties,
                  sr.label AS secondary_role_label, sr.description AS secondary_role_duties,
                  mgr.full_name AS manager_name
           FROM employees e
           LEFT JOIN employee_types et ON et.employee_type_id = e.employee_type_id
           LEFT JOIN departments d ON d.department_id = e.department_id
           LEFT JOIN job_titles jt ON jt.job_title_id = e.job_title_id
           LEFT JOIN countries cc ON cc.country_id = e.citizenship_country_id
           LEFT JOIN genders gn ON gn.gender_id = e.gender_id
           LEFT JOIN hr_roles pr ON pr.role_id = e.primary_role_id
           LEFT JOIN hr_roles sr ON sr.role_id = e.secondary_role_id
           LEFT JOIN employees mgr ON mgr.employee_id = e.manager_id AND mgr.is_deleted = 0
           WHERE e.employee_id = ? AND e.tenant_id = ? AND e.is_deleted = 0""",
        (employee_id, g.tenant_id),
    ).fetchone()
    if emp is None:
        abort(404)
    return emp


@hr_bp.route("/employees")
@login_required
def list_employees():
    db = get_db()
    q = request.args.get("q", "").strip()
    department_id = request.args.get("department_id", "").strip()
    employee_type_id = request.args.get("employee_type_id", "").strip()
    sql = """
        SELECT e.*, et.label AS type_label, d.label AS department_label, jt.label AS job_title_label,
               pe.email_address AS primary_email,
               pp.country_code AS primary_phone_country_code, pp.area_code AS primary_phone_area_code,
               pp.number AS primary_phone_number, pp.extension AS primary_phone_extension
        FROM employees e
        LEFT JOIN employee_types et ON et.employee_type_id = e.employee_type_id
        LEFT JOIN departments d ON d.department_id = e.department_id
        LEFT JOIN job_titles jt ON jt.job_title_id = e.job_title_id
        LEFT JOIN employee_emails pe ON pe.employee_email_id = (
            SELECT em.employee_email_id FROM employee_emails em
            WHERE em.employee_id = e.employee_id AND em.tenant_id = e.tenant_id
            ORDER BY em.is_primary DESC, em.employee_email_id ASC LIMIT 1
        )
        LEFT JOIN employee_phones pp ON pp.employee_phone_id = (
            SELECT ph.employee_phone_id FROM employee_phones ph
            WHERE ph.employee_id = e.employee_id AND ph.tenant_id = e.tenant_id
            ORDER BY ph.is_primary DESC, ph.employee_phone_id ASC LIMIT 1
        )
        WHERE e.tenant_id = ? AND e.is_deleted = 0
    """
    params = [g.tenant_id]
    if q:
        sql += " AND e.full_name LIKE ?"
        params.append(f"%{q}%")
    if department_id:
        sql += " AND e.department_id = ?"
        params.append(department_id)
    if employee_type_id:
        sql += " AND e.employee_type_id = ?"
        params.append(employee_type_id)
    sql += " ORDER BY e.full_name"
    rows = db.execute(sql, params).fetchall()
    return render_template(
        "hr/employees_list.html", employees=rows, q=q, department_id=department_id,
        employee_type_id=employee_type_id, departments=_departments(db), employee_types=_employee_types(db),
    )


@hr_bp.route("/employees/new", methods=["GET", "POST"])
@login_required
def new_employee():
    db = get_db()
    if request.method == "POST":
        form = request.form
        name = form.get("full_name", "").strip()
        primary_role_id = form.get("primary_role_id") or None
        secondary_role_id = form.get("secondary_role_id") or None
        manager_id = form.get("manager_id") or None
        error = None
        if not name:
            error = "Full name is required."
        elif not primary_role_id:
            error = "Primary Role is required."
        elif secondary_role_id and secondary_role_id == primary_role_id:
            error = "Secondary Role must be different from Primary Role."
        if error:
            flash(error, "error")
            return render_template(
                "hr/employee_form.html", employee=None, employee_types=_employee_types(db),
                departments=_departments(db), job_titles=_job_titles(db), countries=_countries(db),
                host_addresses=_host_address_options(db), all_roles=_hr_roles(db),
                manager_options=_manager_options(db), genders=_genders(db),
            )
        db.execute(
            """INSERT INTO employees
               (tenant_id, full_name, gender_id, employee_type_id, department_id, job_title_id,
                primary_role_id, secondary_role_id, manager_id, date_of_birth,
                host_address_id, date_of_hire, monthly_salary, citizenship_country_id, passport_number,
                visa_status, tax_id, national_id_number, emergency_contact_name, emergency_contact_address,
                emergency_contact_phone, emergency_contact_email, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                g.tenant_id, name, form.get("gender_id") or None,
                form.get("employee_type_id") or None, form.get("department_id") or None,
                form.get("job_title_id") or None,
                primary_role_id, secondary_role_id, manager_id,
                form.get("date_of_birth") or None, form.get("host_address_id") or None,
                form.get("date_of_hire") or None, form.get("monthly_salary") or None,
                form.get("citizenship_country_id") or None, form.get("passport_number", "").strip() or None,
                form.get("visa_status", "").strip() or None, form.get("tax_id", "").strip() or None,
                form.get("national_id_number", "").strip() or None,
                form.get("emergency_contact_name", "").strip() or None,
                form.get("emergency_contact_address", "").strip() or None,
                form.get("emergency_contact_phone", "").strip() or None,
                form.get("emergency_contact_email", "").strip() or None,
                form.get("notes", "").strip() or None,
            ),
        )
        db.commit()
        employee_id = db.execute("SELECT last_insert_rowid() id").fetchone()["id"]
        log_action("Create", "employee", employee_id, f"Created employee {name}")
        flash("Employee created.", "success")
        return redirect(url_for("hr.view_employee", employee_id=employee_id))
    return render_template(
        "hr/employee_form.html", employee=None, employee_types=_employee_types(db),
        departments=_departments(db), job_titles=_job_titles(db), countries=_countries(db),
        host_addresses=_host_address_options(db), all_roles=_hr_roles(db),
        manager_options=_manager_options(db), genders=_genders(db),
    )


# Fields tracked in employees_history when changed (see schema.sql).
_EMPLOYEE_HISTORY_FIELDS = [
    "department_id", "job_title_id", "employee_type_id", "monthly_salary", "host_address_id",
    "primary_role_id", "secondary_role_id", "manager_id", "gender_id",
]


@hr_bp.route("/employees/<int:employee_id>/edit", methods=["GET", "POST"])
@login_required
def edit_employee(employee_id):
    db = get_db()
    employee = _get_employee(db, employee_id)
    if request.method == "POST":
        form = request.form
        name = form.get("full_name", "").strip()
        primary_role_id = form.get("primary_role_id") or None
        secondary_role_id = form.get("secondary_role_id") or None
        manager_id = form.get("manager_id") or None
        error = None
        if not name:
            error = "Full name is required."
        elif not primary_role_id:
            error = "Primary Role is required."
        elif secondary_role_id and secondary_role_id == primary_role_id:
            error = "Secondary Role must be different from Primary Role."
        elif manager_id and int(manager_id) == employee_id:
            error = "An employee cannot report to themselves."
        if error:
            flash(error, "error")
            return render_template(
                "hr/employee_form.html", employee=employee, employee_types=_employee_types(db),
                departments=_departments(db), job_titles=_job_titles(db), countries=_countries(db),
                host_addresses=_host_address_options(db), all_roles=_hr_roles(db),
                manager_options=_manager_options(db, exclude_employee_id=employee_id), genders=_genders(db),
            )

        new_values = {
            "department_id": form.get("department_id") or None,
            "job_title_id": form.get("job_title_id") or None,
            "employee_type_id": form.get("employee_type_id") or None,
            "monthly_salary": form.get("monthly_salary") or None,
            "host_address_id": form.get("host_address_id") or None,
            "primary_role_id": primary_role_id,
            "secondary_role_id": secondary_role_id,
            "manager_id": manager_id,
            "gender_id": form.get("gender_id") or None,
        }
        for field in _EMPLOYEE_HISTORY_FIELDS:
            if str(employee[field] or "") != str(new_values[field] or ""):
                db.execute(
                    "INSERT INTO employees_history (tenant_id, employee_id, field_name, previous_value) VALUES (?, ?, ?, ?)",
                    (g.tenant_id, employee_id, field, str(employee[field] or "")),
                )

        db.execute(
            """UPDATE employees SET full_name=?, gender_id=?, employee_type_id=?, department_id=?, job_title_id=?,
               primary_role_id=?, secondary_role_id=?, manager_id=?,
               date_of_birth=?, host_address_id=?, date_of_hire=?, monthly_salary=?, citizenship_country_id=?,
               passport_number=?, visa_status=?, tax_id=?, national_id_number=?, emergency_contact_name=?,
               emergency_contact_address=?, emergency_contact_phone=?, emergency_contact_email=?, notes=?,
               updated_at=datetime('now') WHERE employee_id=? AND tenant_id=?""",
            (
                name, new_values["gender_id"], new_values["employee_type_id"], new_values["department_id"],
                new_values["job_title_id"],
                new_values["primary_role_id"], new_values["secondary_role_id"], new_values["manager_id"],
                form.get("date_of_birth") or None,
                new_values["host_address_id"], form.get("date_of_hire") or None, new_values["monthly_salary"],
                form.get("citizenship_country_id") or None, form.get("passport_number", "").strip() or None,
                form.get("visa_status", "").strip() or None, form.get("tax_id", "").strip() or None,
                form.get("national_id_number", "").strip() or None,
                form.get("emergency_contact_name", "").strip() or None,
                form.get("emergency_contact_address", "").strip() or None,
                form.get("emergency_contact_phone", "").strip() or None,
                form.get("emergency_contact_email", "").strip() or None,
                form.get("notes", "").strip() or None,
                employee_id, g.tenant_id,
            ),
        )

        if form.get("remove_photo"):
            _delete_profile_photo(employee["profile_image_path"])
            db.execute("UPDATE employees SET profile_image_path = NULL WHERE employee_id = ? AND tenant_id = ?", (employee_id, g.tenant_id))
        else:
            photo_filename = _save_profile_photo(request.files.get("profile_image"))
            if photo_filename:
                _delete_profile_photo(employee["profile_image_path"])
                db.execute(
                    "UPDATE employees SET profile_image_path = ? WHERE employee_id = ? AND tenant_id = ?",
                    (photo_filename, employee_id, g.tenant_id),
                )

        db.commit()
        log_action("Update", "employee", employee_id, "Updated core employee fields")
        flash("Employee updated.", "success")
        return redirect(url_for("hr.view_employee", employee_id=employee_id))
    return render_template(
        "hr/employee_form.html", employee=employee, employee_types=_employee_types(db),
        departments=_departments(db), job_titles=_job_titles(db), countries=_countries(db),
        host_addresses=_host_address_options(db), all_roles=_hr_roles(db),
        manager_options=_manager_options(db, exclude_employee_id=employee_id), genders=_genders(db),
    )


def _snapshot_employee(db, employee_id):
    employee = db.execute("SELECT * FROM employees WHERE employee_id = ?", (employee_id,)).fetchone()
    return {
        "employee": dict(employee),
        "emails": [dict(r) for r in db.execute("SELECT * FROM employee_emails WHERE employee_id = ?", (employee_id,))],
        "phones": [dict(r) for r in db.execute("SELECT * FROM employee_phones WHERE employee_id = ?", (employee_id,))],
        "addresses": [dict(r) for r in db.execute("SELECT * FROM addresses WHERE owner_type='Employee' AND owner_id = ?", (employee_id,))],
        "education": [dict(r) for r in db.execute("SELECT * FROM employee_education WHERE employee_id = ?", (employee_id,))],
        "links": [dict(r) for r in db.execute("SELECT * FROM employee_document_links WHERE employee_id = ?", (employee_id,))],
        "roles": [dict(r) for r in db.execute(
            "SELECT * FROM hr_role_assignments WHERE owner_type='Employee' AND owner_id = ?", (employee_id,)
        )],
    }


@hr_bp.route("/employees/<int:employee_id>/delete", methods=["POST"])
@login_required
def delete_employee(employee_id):
    db = get_db()
    employee = _get_employee(db, employee_id)
    snapshot = _snapshot_employee(db, employee_id)

    retention_row = db.execute("SELECT data_retention_days FROM tenants WHERE tenant_id = ?", (g.tenant_id,)).fetchone()
    retention_days = retention_row["data_retention_days"] if retention_row else None
    if retention_days is not None:
        db.execute(
            "INSERT INTO employees_archive (employee_id, tenant_id, snapshot, purge_eligible_at) "
            "VALUES (?, ?, ?, datetime(?, ?))",
            (employee_id, g.tenant_id, json.dumps(snapshot, default=str), employee["created_at"], f"+{retention_days} days"),
        )
    else:
        db.execute(
            "INSERT INTO employees_archive (employee_id, tenant_id, snapshot, purge_eligible_at) VALUES (?, ?, ?, NULL)",
            (employee_id, g.tenant_id, json.dumps(snapshot, default=str)),
        )
    db.execute(
        "UPDATE employees SET is_deleted = 1, updated_at = datetime('now') WHERE employee_id = ? AND tenant_id = ?",
        (employee_id, g.tenant_id),
    )
    db.commit()
    log_action("Delete", "employee", employee_id, f"Archived employee {employee['full_name']}")
    flash("Employee deleted (moved to archive).", "success")
    return redirect(url_for("hr.list_employees"))


@hr_bp.route("/employees/<int:employee_id>")
@login_required
def view_employee(employee_id):
    db = get_db()
    employee = _get_employee(db, employee_id)
    host_address = None
    if employee["host_address_id"]:
        host_address = db.execute(
            """SELECT ha.*, hat.label AS type_label, COALESCE(c.label, ha.city_text) AS city_label,
                      co.label AS country_label
               FROM host_addresses ha
               LEFT JOIN host_address_types hat ON hat.address_type_id = ha.address_type_id
               LEFT JOIN cities c ON c.city_id = ha.city_id
               LEFT JOIN countries co ON co.country_id = ha.country_id
               WHERE ha.host_address_id = ? AND ha.tenant_id = ?""",
            (employee["host_address_id"], g.tenant_id),
        ).fetchone()
    address = db.execute(
        """SELECT a.*, COALESCE(c.label, a.city_text) AS city_label,
                  COALESCE(st.label, a.state_province_text) AS state_label, co.label AS country_label
           FROM addresses a
           LEFT JOIN cities c ON c.city_id = a.city_id
           LEFT JOIN states st ON st.state_id = a.state_id
           LEFT JOIN countries co ON co.country_id = a.country_id
           WHERE a.owner_type = 'Employee' AND a.owner_id = ? AND a.tenant_id = ?""",
        (employee_id, g.tenant_id),
    ).fetchone()
    emails = db.execute(
        "SELECT * FROM employee_emails WHERE employee_id = ? AND tenant_id = ? ORDER BY is_primary DESC, email_address",
        (employee_id, g.tenant_id),
    ).fetchall()
    phones = db.execute(
        """SELECT p.*, pt.label AS phone_type_label FROM employee_phones p
           LEFT JOIN employee_phone_types pt ON pt.phone_type_id = p.phone_type_id
           WHERE p.employee_id = ? AND p.tenant_id = ? ORDER BY p.is_primary DESC, pt.sort_order""",
        (employee_id, g.tenant_id),
    ).fetchall()
    education = db.execute(
        "SELECT * FROM employee_education WHERE employee_id = ? AND tenant_id = ? ORDER BY education_id",
        (employee_id, g.tenant_id),
    ).fetchall()
    links = db.execute(
        "SELECT * FROM employee_document_links WHERE employee_id = ? AND tenant_id = ? ORDER BY link_id",
        (employee_id, g.tenant_id),
    ).fetchall()
    # Note: the general multi-role checklist (hr_role_assignments, via
    # _hr_roles/_role_ids_for) is no longer shown on this page -- Primary
    # Role/Secondary Role above supersede it for Employees. External
    # Resources (view_resource() below) still use it; existing Employee
    # role-assignment rows, if any, are left in the database untouched.
    history = db.execute(
        "SELECT * FROM employees_history WHERE employee_id = ? AND tenant_id = ? ORDER BY superseded_at DESC",
        (employee_id, g.tenant_id),
    ).fetchall()

    # Business card fields: Employee's own primary phone/email, the Host
    # Organization's name/website, the assigned office's address, and that
    # office's Fax number (spec: "sufficient to meet a business card
    # requirement" — data only, no print feature in this pass).
    host_org = db.execute("SELECT * FROM host_organizations WHERE tenant_id = ?", (g.tenant_id,)).fetchone()
    office_fax = None
    if employee["host_address_id"]:
        office_fax = db.execute(
            """SELECT hp.* FROM host_phones hp JOIN host_phone_types hpt ON hpt.phone_type_id = hp.phone_type_id
               WHERE hp.host_address_id = ? AND hp.tenant_id = ? AND hpt.label = 'Fax'
               ORDER BY hp.is_primary DESC LIMIT 1""",
            (employee["host_address_id"], g.tenant_id),
        ).fetchone()
    business_card_phone = next((e for e in phones if e["is_primary"]), phones[0] if phones else None)
    business_card_email = next((e for e in emails if e["is_primary"]), emails[0] if emails else None)

    return render_template(
        "hr/employee_view.html", employee=employee, host_address=host_address, address=address, emails=emails,
        phones=phones, education=education, links=links,
        history=history, host_org=host_org, office_fax=office_fax,
        business_card_phone=business_card_phone, business_card_email=business_card_email,
    )


# --- employee address (single) ---

@hr_bp.route("/employees/<int:employee_id>/address/edit", methods=["GET", "POST"])
@login_required
def edit_employee_address(employee_id):
    db = get_db()
    employee = _get_employee(db, employee_id)
    address = db.execute(
        "SELECT * FROM addresses WHERE owner_type = 'Employee' AND owner_id = ? AND tenant_id = ?",
        (employee_id, g.tenant_id),
    ).fetchone()
    if request.method == "POST":
        form = request.form
        values = (
            form.get("street", "").strip() or None, form.get("unit", "").strip() or None,
            form.get("region_id") or None, form.get("country_id") or None,
            form.get("state_id") or None, form.get("state_province_text", "").strip() or None,
            form.get("city_id") or None, form.get("city_text", "").strip() or None,
            form.get("postal_code", "").strip() or None,
        )
        if address is None:
            db.execute(
                """INSERT INTO addresses
                   (tenant_id, owner_type, owner_id, address_type, street, unit, region_id, country_id,
                    state_id, state_province_text, city_id, city_text, postal_code, is_primary)
                   VALUES (?, 'Employee', ?, 'Home', ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
                (g.tenant_id, employee_id) + values,
            )
        else:
            db.execute(
                """UPDATE addresses SET street=?, unit=?, region_id=?, country_id=?, state_id=?, state_province_text=?,
                   city_id=?, city_text=?, postal_code=?, updated_at=datetime('now')
                   WHERE address_id=? AND tenant_id=?""",
                values + (address["address_id"], g.tenant_id),
            )
        db.commit()
        log_action("Update", "employee_address", employee_id, "Updated employee address")
        flash("Address saved.", "success")
        return redirect(url_for("hr.view_employee", employee_id=employee_id))
    return render_template("hr/employee_address_form.html", employee=employee, address=address)


# --- employee emails (multiple, with primary) ---

@hr_bp.route("/employees/<int:employee_id>/emails/new", methods=["GET", "POST"])
@login_required
def new_employee_email(employee_id):
    db = get_db()
    employee = _get_employee(db, employee_id)
    if request.method == "POST":
        form = request.form
        if form.get("is_primary"):
            db.execute("UPDATE employee_emails SET is_primary = 0 WHERE employee_id = ? AND tenant_id = ?", (employee_id, g.tenant_id))
        db.execute(
            "INSERT INTO employee_emails (tenant_id, employee_id, email_address, is_primary) VALUES (?, ?, ?, ?)",
            (g.tenant_id, employee_id, form["email_address"].strip(), 1 if form.get("is_primary") else 0),
        )
        db.commit()
        log_action("Create", "employee_email", employee_id, f"Added email {form['email_address']}")
        flash("Email added.", "success")
        return redirect(url_for("hr.view_employee", employee_id=employee_id))
    return render_template("hr/employee_email_form.html", employee=employee, email=None)


@hr_bp.route("/employees/<int:employee_id>/emails/<int:email_id>/edit", methods=["GET", "POST"])
@login_required
def edit_employee_email(employee_id, email_id):
    db = get_db()
    employee = _get_employee(db, employee_id)
    email = db.execute(
        "SELECT * FROM employee_emails WHERE employee_email_id = ? AND employee_id = ? AND tenant_id = ?",
        (email_id, employee_id, g.tenant_id),
    ).fetchone()
    if email is None:
        abort(404)
    if request.method == "POST":
        form = request.form
        if form.get("is_primary"):
            db.execute("UPDATE employee_emails SET is_primary = 0 WHERE employee_id = ? AND tenant_id = ?", (employee_id, g.tenant_id))
        db.execute(
            "UPDATE employee_emails SET email_address=?, is_primary=?, updated_at=datetime('now') WHERE employee_email_id=? AND tenant_id=?",
            (form["email_address"].strip(), 1 if form.get("is_primary") else 0, email_id, g.tenant_id),
        )
        db.commit()
        log_action("Update", "employee_email", employee_id, f"Updated email #{email_id}")
        flash("Email updated.", "success")
        return redirect(url_for("hr.view_employee", employee_id=employee_id))
    return render_template("hr/employee_email_form.html", employee=employee, email=email)


@hr_bp.route("/employees/<int:employee_id>/emails/<int:email_id>/delete", methods=["POST"])
@login_required
def delete_employee_email(employee_id, email_id):
    db = get_db()
    _get_employee(db, employee_id)
    db.execute("DELETE FROM employee_emails WHERE employee_email_id = ? AND employee_id = ? AND tenant_id = ?", (email_id, employee_id, g.tenant_id))
    db.commit()
    log_action("Delete", "employee_email", employee_id, f"Deleted email #{email_id}")
    flash("Email deleted.", "success")
    return redirect(url_for("hr.view_employee", employee_id=employee_id))


# --- employee phones (multiple, with primary) ---

@hr_bp.route("/employees/<int:employee_id>/phones/new", methods=["GET", "POST"])
@login_required
def new_employee_phone(employee_id):
    db = get_db()
    employee = _get_employee(db, employee_id)
    if request.method == "POST":
        form = request.form
        if form.get("is_primary"):
            db.execute("UPDATE employee_phones SET is_primary = 0 WHERE employee_id = ? AND tenant_id = ?", (employee_id, g.tenant_id))
        db.execute(
            """INSERT INTO employee_phones (tenant_id, employee_id, phone_type_id, country_code, area_code, number, extension, is_primary)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                g.tenant_id, employee_id, form.get("phone_type_id") or None,
                form.get("country_code", "").strip() or None, form.get("area_code", "").strip() or None,
                form["number"].strip(), form.get("extension", "").strip() or None, 1 if form.get("is_primary") else 0,
            ),
        )
        db.commit()
        log_action("Create", "employee_phone", employee_id, "Added employee phone")
        flash("Phone added.", "success")
        return redirect(url_for("hr.view_employee", employee_id=employee_id))
    return render_template("hr/employee_phone_form.html", employee=employee, phone=None, phone_types=_employee_phone_types(db))


@hr_bp.route("/employees/<int:employee_id>/phones/<int:phone_id>/edit", methods=["GET", "POST"])
@login_required
def edit_employee_phone(employee_id, phone_id):
    db = get_db()
    employee = _get_employee(db, employee_id)
    phone = db.execute(
        "SELECT * FROM employee_phones WHERE employee_phone_id = ? AND employee_id = ? AND tenant_id = ?",
        (phone_id, employee_id, g.tenant_id),
    ).fetchone()
    if phone is None:
        abort(404)
    if request.method == "POST":
        form = request.form
        if form.get("is_primary"):
            db.execute("UPDATE employee_phones SET is_primary = 0 WHERE employee_id = ? AND tenant_id = ?", (employee_id, g.tenant_id))
        db.execute(
            """UPDATE employee_phones SET phone_type_id=?, country_code=?, area_code=?, number=?, extension=?,
               is_primary=?, updated_at=datetime('now') WHERE employee_phone_id=? AND tenant_id=?""",
            (
                form.get("phone_type_id") or None, form.get("country_code", "").strip() or None,
                form.get("area_code", "").strip() or None, form["number"].strip(),
                form.get("extension", "").strip() or None, 1 if form.get("is_primary") else 0, phone_id, g.tenant_id,
            ),
        )
        db.commit()
        log_action("Update", "employee_phone", employee_id, f"Updated employee phone #{phone_id}")
        flash("Phone updated.", "success")
        return redirect(url_for("hr.view_employee", employee_id=employee_id))
    return render_template("hr/employee_phone_form.html", employee=employee, phone=phone, phone_types=_employee_phone_types(db))


@hr_bp.route("/employees/<int:employee_id>/phones/<int:phone_id>/delete", methods=["POST"])
@login_required
def delete_employee_phone(employee_id, phone_id):
    db = get_db()
    _get_employee(db, employee_id)
    db.execute("DELETE FROM employee_phones WHERE employee_phone_id = ? AND employee_id = ? AND tenant_id = ?", (phone_id, employee_id, g.tenant_id))
    db.commit()
    log_action("Delete", "employee_phone", employee_id, f"Deleted employee phone #{phone_id}")
    flash("Phone deleted.", "success")
    return redirect(url_for("hr.view_employee", employee_id=employee_id))


# --- employee education (repeatable) ---

@hr_bp.route("/employees/<int:employee_id>/education/new", methods=["GET", "POST"])
@login_required
def new_employee_education(employee_id):
    db = get_db()
    employee = _get_employee(db, employee_id)
    if request.method == "POST":
        form = request.form
        title = form.get("title", "").strip()
        credential_type = form.get("credential_type") if form.get("credential_type") in ("Degree", "Certificate") else None
        if not title or not credential_type:
            flash("Type and Title are required.", "error")
            return render_template("hr/employee_education_form.html", employee=employee, item=None)
        db.execute(
            "INSERT INTO employee_education (tenant_id, employee_id, credential_type, title, institution, year_completed, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                g.tenant_id, employee_id, credential_type, title,
                form.get("institution", "").strip() or None, form.get("year_completed", "").strip() or None,
                form.get("notes", "").strip() or None,
            ),
        )
        db.commit()
        log_action("Create", "employee_education", employee_id, f"Added {credential_type.lower()} '{title}'")
        flash("Education entry added.", "success")
        return redirect(url_for("hr.view_employee", employee_id=employee_id))
    return render_template("hr/employee_education_form.html", employee=employee, item=None)


@hr_bp.route("/employees/<int:employee_id>/education/<int:education_id>/edit", methods=["GET", "POST"])
@login_required
def edit_employee_education(employee_id, education_id):
    db = get_db()
    employee = _get_employee(db, employee_id)
    item = db.execute(
        "SELECT * FROM employee_education WHERE education_id = ? AND employee_id = ? AND tenant_id = ?",
        (education_id, employee_id, g.tenant_id),
    ).fetchone()
    if item is None:
        abort(404)
    if request.method == "POST":
        form = request.form
        title = form.get("title", "").strip()
        credential_type = form.get("credential_type") if form.get("credential_type") in ("Degree", "Certificate") else None
        if not title or not credential_type:
            flash("Type and Title are required.", "error")
            return render_template("hr/employee_education_form.html", employee=employee, item=item)
        db.execute(
            "UPDATE employee_education SET credential_type=?, title=?, institution=?, year_completed=?, notes=? "
            "WHERE education_id=? AND tenant_id=?",
            (
                credential_type, title, form.get("institution", "").strip() or None,
                form.get("year_completed", "").strip() or None, form.get("notes", "").strip() or None,
                education_id, g.tenant_id,
            ),
        )
        db.commit()
        log_action("Update", "employee_education", employee_id, f"Updated education entry #{education_id}")
        flash("Education entry updated.", "success")
        return redirect(url_for("hr.view_employee", employee_id=employee_id))
    return render_template("hr/employee_education_form.html", employee=employee, item=item)


@hr_bp.route("/employees/<int:employee_id>/education/<int:education_id>/delete", methods=["POST"])
@login_required
def delete_employee_education(employee_id, education_id):
    db = get_db()
    _get_employee(db, employee_id)
    db.execute("DELETE FROM employee_education WHERE education_id = ? AND employee_id = ? AND tenant_id = ?", (education_id, employee_id, g.tenant_id))
    db.commit()
    log_action("Delete", "employee_education", employee_id, f"Deleted education entry #{education_id}")
    flash("Education entry deleted.", "success")
    return redirect(url_for("hr.view_employee", employee_id=employee_id))


# --- employee document links ---

@hr_bp.route("/employees/<int:employee_id>/links/new", methods=["GET", "POST"])
@login_required
def new_employee_link(employee_id):
    db = get_db()
    employee = _get_employee(db, employee_id)
    if request.method == "POST":
        form = request.form
        db.execute(
            "INSERT INTO employee_document_links (tenant_id, employee_id, url, document_path, description, notes) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                g.tenant_id, employee_id, form.get("url", "").strip() or None, form.get("document_path", "").strip() or None,
                form.get("description", "").strip() or None, form.get("notes", "").strip() or None,
            ),
        )
        db.commit()
        log_action("Create", "employee_document_link", employee_id, "Added document link")
        flash("Document link added.", "success")
        return redirect(url_for("hr.view_employee", employee_id=employee_id))
    return render_template("hr/employee_link_form.html", employee=employee, link=None)


@hr_bp.route("/employees/<int:employee_id>/links/<int:link_id>/edit", methods=["GET", "POST"])
@login_required
def edit_employee_link(employee_id, link_id):
    db = get_db()
    employee = _get_employee(db, employee_id)
    link = db.execute(
        "SELECT * FROM employee_document_links WHERE link_id = ? AND employee_id = ? AND tenant_id = ?",
        (link_id, employee_id, g.tenant_id),
    ).fetchone()
    if link is None:
        abort(404)
    if request.method == "POST":
        form = request.form
        db.execute(
            "UPDATE employee_document_links SET url=?, document_path=?, description=?, notes=? WHERE link_id=? AND employee_id=? AND tenant_id=?",
            (
                form.get("url", "").strip() or None, form.get("document_path", "").strip() or None,
                form.get("description", "").strip() or None, form.get("notes", "").strip() or None,
                link_id, employee_id, g.tenant_id,
            ),
        )
        db.commit()
        log_action("Update", "employee_document_link", employee_id, f"Updated document link #{link_id}")
        flash("Document link updated.", "success")
        return redirect(url_for("hr.view_employee", employee_id=employee_id))
    return render_template("hr/employee_link_form.html", employee=employee, link=link)


@hr_bp.route("/employees/<int:employee_id>/links/<int:link_id>/delete", methods=["POST"])
@login_required
def delete_employee_link(employee_id, link_id):
    db = get_db()
    _get_employee(db, employee_id)
    db.execute("DELETE FROM employee_document_links WHERE link_id = ? AND employee_id = ? AND tenant_id = ?", (link_id, employee_id, g.tenant_id))
    db.commit()
    log_action("Delete", "employee_document_link", employee_id, f"Deleted document link #{link_id}")
    flash("Document link deleted.", "success")
    return redirect(url_for("hr.view_employee", employee_id=employee_id))


@hr_bp.route("/employees/<int:employee_id>/links/<int:link_id>/open")
@login_required
def open_employee_link(employee_id, link_id):
    db = get_db()
    link = db.execute(
        "SELECT * FROM employee_document_links WHERE link_id = ? AND employee_id = ? AND tenant_id = ?",
        (link_id, employee_id, g.tenant_id),
    ).fetchone()
    if link is None:
        return jsonify(ok=False, error="Document link not found."), 404
    path = link["document_path"]
    if not path:
        return jsonify(ok=False, error="No document path on this link.")
    if not os.path.exists(path):
        return jsonify(ok=False, error=f"File not found on disk: {path}")
    try:
        open_local_path(path)
    except Exception as e:
        return jsonify(ok=False, error=f"Couldn't open the file: {e}")
    log_action("Open", "employee_document_link", employee_id, f"Opened {path}")
    return jsonify(ok=True)


# --- employee roles (multi-select) ---

@hr_bp.route("/employees/<int:employee_id>/roles", methods=["POST"])
@login_required
def update_employee_roles(employee_id):
    db = get_db()
    _get_employee(db, employee_id)
    role_ids = [int(v) for v in request.form.getlist("role_ids")]
    _set_roles(db, "Employee", employee_id, role_ids)
    db.commit()
    log_action("Update", "employee_roles", employee_id, f"Set roles ({len(role_ids)})")
    flash("Roles updated.", "success")
    return redirect(url_for("hr.view_employee", employee_id=employee_id))



# =============================================================== external resources (retired)
#
# External Resources used to be their own record type here (full_name,
# resource_type_id, phone, email, tax_id, hourly_rate, its own Address via
# the shared `addresses` table, and Roles via hr_role_assignments) --
# retired per the request: "an external resource is also like a supplier
# ... it makes sense to have their organization in the Supplier table."
# A contractor is now just a Supplier row with suppliers.is_external_resource
# = 1 (see blueprints/suppliers.py), getting Suppliers' own address
# (supplier_addresses, full Country/Province/City cascade, multiple
# locations) and contact/phone handling (supplier_contacts /
# supplier_contact_phones) instead of the HR-specific tables above -- the
# request's own caveat ("they must be flagged... since the contacts for
# Suppliers is a different table") is exactly this.
#
# The external_resources / external_resources_history / external_resources_
# archive tables are left in schema.sql, unused, rather than dropped --
# same "never destroy, just stop using" treatment as employees.job_role --
# though in this case there was nothing in them to begin with (confirmed
# empty before this change). No route below reads or writes them anymore.
