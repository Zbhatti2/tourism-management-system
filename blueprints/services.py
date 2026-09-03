"""
Module — Inventory: Services. Tenant-scoped catalog of sellable services,
each identified by a Service Code (Category-Group-Sub-Group-Sequence, e.g.
'TP-MN-MC-0001') plus a free-text Description — the first of Inventory
Management's two planned sub-modules (Products is still a placeholder —
see blueprints/products.py), per the request:

    "There are two type of Inventory. The first is 'Services' which will
    have its own table since there are no Quantities On hand and Minimum
    Order Quantity type requirements. Services inventory is a Services
    Code and a Description. The services table will primarily be used for
    planning and formalizing a Tour package."

Built from the attached Service_Coding_System_Schema.md. The Category ->
Group -> Sub-Group vocabulary itself (service_categories/service_groups/
service_subgroups) is GLOBAL, shared reference data seeded by
seed_data.seed_service_taxonomy() — see schema.sql's MODULE I for the full
design note. This module only manages the tenant-scoped `services`
catalog built against that shared vocabulary.

Deprecated Sub-Groups (validity_status='deprecated' — combinations the
design doc identified as invalid, e.g. 'GT-BS-LN': no commercial operator
offers a self-drive coach) are blocked from ever being issued a code,
three ways (schema doc S9.3's defense-in-depth):

  1. UI layer — _taxonomy_tree() below excludes 'deprecated' Sub-Groups
     entirely, so the New Service form's picker never even offers one.
  2. This route layer — new_service() re-checks validity_status before
     the INSERT, in case the request was tampered with.
  3. Database layer — schema.sql's trg_block_deprecated_service_subgroup
     trigger is the last line of defense, catching anything that bypassed
     both layers above (a bulk script, a future API).

A Service's Category/Group/Sub-Group and sequence number are immutable
once issued (edit_service() only lets Description/Notes/Status change) —
matching the design doc's rationale that a code needs to stay a stable
identifier once something (eventually: an invoice line item, a Tour
Package) references it. "Delete" archives (status='archived') rather than
removing the row, same "never destroy, just stop using" philosophy as the
rest of this app — restore_service() below reverses it.
"""
import sqlite3

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from auth.decorators import login_required
from db import get_db, log_action

services_bp = Blueprint("services", __name__)


def _taxonomy_tree(db):
    """Categories -> Groups -> Sub-Groups, nested for the New Service
    form's cascading picker (see static/js/service_code_picker.js).
    Active categories/groups/subgroups only, and 'deprecated' Sub-Groups
    excluded outright (schema doc S9.3 layer 1) — 'under_review' ones are
    included (the picker flags them, doesn't block them; only the
    database trigger and the S9.3 layer-2 check below block 'deprecated')."""
    rows = db.execute(
        """SELECT c.category_id, c.category_code, c.category_name,
                  g.group_id, g.group_code, g.group_name,
                  sg.subgroup_id, sg.subgroup_code, sg.subgroup_name, sg.validity_status
           FROM service_categories c
           JOIN service_groups g ON g.category_id = c.category_id
           JOIN service_subgroups sg ON sg.group_id = g.group_id
           WHERE c.is_active = 1 AND g.is_active = 1 AND sg.is_active = 1
             AND sg.validity_status != 'deprecated'
           ORDER BY c.category_code, g.group_code, sg.subgroup_code"""
    ).fetchall()

    categories = {}
    order = []
    for r in rows:
        if r["category_id"] not in categories:
            categories[r["category_id"]] = {
                "category_id": r["category_id"], "category_code": r["category_code"],
                "category_name": r["category_name"], "groups": {}, "group_order": [],
            }
            order.append(r["category_id"])
        cat = categories[r["category_id"]]
        if r["group_id"] not in cat["groups"]:
            cat["groups"][r["group_id"]] = {
                "group_id": r["group_id"], "group_code": r["group_code"],
                "group_name": r["group_name"], "subgroups": [],
            }
            cat["group_order"].append(r["group_id"])
        cat["groups"][r["group_id"]]["subgroups"].append({
            "subgroup_id": r["subgroup_id"], "subgroup_code": r["subgroup_code"],
            "subgroup_name": r["subgroup_name"], "validity_status": r["validity_status"],
        })

    tree = []
    for cat_id in order:
        cat = categories[cat_id]
        cat["groups"] = [cat["groups"][gid] for gid in cat["group_order"]]
        del cat["group_order"]
        tree.append(cat)
    return tree


def _subgroup_full(db, subgroup_id):
    return db.execute(
        """SELECT sg.subgroup_id, sg.subgroup_code, sg.subgroup_name, sg.validity_status,
                  g.group_id, g.group_code, g.group_name,
                  c.category_id, c.category_code, c.category_name
           FROM service_subgroups sg
           JOIN service_groups g ON g.group_id = sg.group_id
           JOIN service_categories c ON c.category_id = g.category_id
           WHERE sg.subgroup_id = ?""",
        (subgroup_id,),
    ).fetchone()


def _service_row(db, service_id):
    return db.execute(
        """SELECT s.*, c.category_id, c.category_code, c.category_name,
                  g.group_code, g.group_name, sg.subgroup_code, sg.subgroup_name, sg.validity_status
           FROM services s
           JOIN service_subgroups sg ON sg.subgroup_id = s.subgroup_id
           JOIN service_groups g ON g.group_id = sg.group_id
           JOIN service_categories c ON c.category_id = g.category_id
           WHERE s.service_id = ? AND s.tenant_id = ?""",
        (service_id, g.tenant_id),
    ).fetchone()


@services_bp.route("/")
@login_required
def list_services():
    db = get_db()
    q = request.args.get("q", "").strip()
    category_id = request.args.get("category_id", "").strip()
    status = request.args.get("status", "").strip()
    show_archived = request.args.get("show_archived") == "1"

    sql = """
        SELECT s.*, c.category_id, c.category_code, c.category_name,
               g.group_code, g.group_name, sg.subgroup_code, sg.subgroup_name
        FROM services s
        JOIN service_subgroups sg ON sg.subgroup_id = s.subgroup_id
        JOIN service_groups g ON g.group_id = sg.group_id
        JOIN service_categories c ON c.category_id = g.category_id
        WHERE s.tenant_id = ?
    """
    params = [g.tenant_id]
    if not show_archived:
        sql += " AND s.status != 'archived'"
    if q:
        sql += " AND (s.description LIKE ? OR s.service_code LIKE ?)"
        params += [f"%{q}%", f"%{q}%"]
    if category_id:
        sql += " AND c.category_id = ?"
        params.append(category_id)
    if status:
        sql += " AND s.status = ?"
        params.append(status)
    sql += " ORDER BY s.service_code"
    rows = db.execute(sql, params).fetchall()

    categories = db.execute(
        "SELECT category_id, category_code, category_name FROM service_categories WHERE is_active = 1 ORDER BY category_code"
    ).fetchall()
    return render_template(
        "services/list.html", services=rows, q=q, category_id=category_id, status=status,
        show_archived=show_archived, categories=categories,
    )


@services_bp.route("/<int:service_id>")
@login_required
def view_service(service_id):
    db = get_db()
    service = _service_row(db, service_id)
    if service is None:
        abort(404)
    return render_template("services/view.html", service=service)


@services_bp.route("/new", methods=["GET", "POST"])
@login_required
def new_service():
    db = get_db()
    if request.method == "POST":
        subgroup_id = request.form.get("subgroup_id") or None
        description = request.form.get("description", "").strip()
        notes = request.form.get("notes", "").strip() or None
        status = request.form.get("status") or "active"
        if status not in ("draft", "active", "archived"):
            status = "active"

        subgroup = _subgroup_full(db, subgroup_id) if subgroup_id else None
        errors = []
        if subgroup is None:
            errors.append("Please choose a Category, Group, and Sub-Group.")
        elif subgroup["validity_status"] == "deprecated":
            # Layer 2 (see module docstring) — the picker already excludes
            # deprecated Sub-Groups, so reaching this means the request
            # was tampered with; the database trigger would catch it
            # anyway even without this check.
            errors.append(
                f"{subgroup['category_code']}-{subgroup['group_code']}-{subgroup['subgroup_code']} has been "
                "retired and can no longer be used for a new service."
            )
        if not description:
            errors.append("Description is required.")

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template(
                "services/form.html", service=None, taxonomy=_taxonomy_tree(db), form_values=request.form,
            )

        next_seq = db.execute(
            "SELECT COALESCE(MAX(sequence_number), 0) + 1 AS n FROM services WHERE tenant_id = ? AND subgroup_id = ?",
            (g.tenant_id, subgroup["subgroup_id"]),
        ).fetchone()["n"]
        service_code = f"{subgroup['category_code']}-{subgroup['group_code']}-{subgroup['subgroup_code']}-{next_seq:04d}"

        try:
            db.execute(
                """INSERT INTO services (tenant_id, subgroup_id, sequence_number, service_code, description, status, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (g.tenant_id, subgroup["subgroup_id"], next_seq, service_code, description, status, notes),
            )
            db.commit()
        except sqlite3.Error as e:
            # Layer 3 (see module docstring) — the database trigger.
            # Practically unreachable given the checks above, but a
            # deprecated-Sub-Group insert must never be silently swallowed.
            flash(f"Could not create the service: {e}", "error")
            return render_template(
                "services/form.html", service=None, taxonomy=_taxonomy_tree(db), form_values=request.form,
            )

        service_id = db.execute("SELECT last_insert_rowid() id").fetchone()["id"]
        log_action("Create", "service", service_id, f"Created service {service_code}: {description}")
        flash(f"Service {service_code} created.", "success")
        return redirect(url_for("services.view_service", service_id=service_id))

    return render_template("services/form.html", service=None, taxonomy=_taxonomy_tree(db), form_values=None)


@services_bp.route("/<int:service_id>/edit", methods=["GET", "POST"])
@login_required
def edit_service(service_id):
    db = get_db()
    service = _service_row(db, service_id)
    if service is None:
        abort(404)

    if request.method == "POST":
        description = request.form.get("description", "").strip()
        notes = request.form.get("notes", "").strip() or None
        status = request.form.get("status") or "active"
        if status not in ("draft", "active", "archived"):
            status = "active"
        if not description:
            flash("Description is required.", "error")
            return render_template("services/form.html", service=service, taxonomy=None, form_values=None)
        db.execute(
            "UPDATE services SET description=?, notes=?, status=?, updated_at=datetime('now') WHERE service_id=? AND tenant_id=?",
            (description, notes, status, service_id, g.tenant_id),
        )
        db.commit()
        log_action("Update", "service", service_id, f"Updated service {service['service_code']}")
        flash("Service updated.", "success")
        return redirect(url_for("services.view_service", service_id=service_id))

    return render_template("services/form.html", service=service, taxonomy=None, form_values=None)


@services_bp.route("/<int:service_id>/delete", methods=["POST"])
@login_required
def delete_service(service_id):
    db = get_db()
    service = db.execute(
        "SELECT * FROM services WHERE service_id = ? AND tenant_id = ?", (service_id, g.tenant_id)
    ).fetchone()
    if service is None:
        abort(404)
    db.execute(
        "UPDATE services SET status = 'archived', updated_at = datetime('now') WHERE service_id = ? AND tenant_id = ?",
        (service_id, g.tenant_id),
    )
    db.commit()
    log_action("Delete", "service", service_id, f"Archived service {service['service_code']}")
    flash(f"'{service['service_code']}' archived.", "success")
    return redirect(url_for("services.list_services"))


@services_bp.route("/<int:service_id>/restore", methods=["POST"])
@login_required
def restore_service(service_id):
    db = get_db()
    service = db.execute(
        "SELECT * FROM services WHERE service_id = ? AND tenant_id = ?", (service_id, g.tenant_id)
    ).fetchone()
    if service is None:
        abort(404)
    db.execute(
        "UPDATE services SET status = 'active', updated_at = datetime('now') WHERE service_id = ? AND tenant_id = ?",
        (service_id, g.tenant_id),
    )
    db.commit()
    log_action("Update", "service", service_id, f"Restored service {service['service_code']} from archived")
    flash(f"'{service['service_code']}' restored.", "success")
    return redirect(request.referrer or url_for("services.view_service", service_id=service_id))
