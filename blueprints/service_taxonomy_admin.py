"""
Module — Service Code Maintenance. Lets a Tenant Admin add/edit/deactivate/
delete rows in the three GLOBAL Service Coding System taxonomy tables
(service_categories / service_groups / service_subgroups — see
schema.sql's MODULE I and blueprints/services.py) directly from the UI,
instead of only being settable via seed_data.py. Per the request:

    "Please build the Table Maintenance feature for the Services Table."

Same reasoning as geography_admin.py for why this is its OWN screen rather
than folded into table_maintenance.py: these three tables have no
tenant_id column at all (see schema.sql's MODULE I design note) — they're
shared reference data across every tenant, the same treatment as
Regions/Countries/States/Cities, so an edit here (a relabel, a
deprecation) is visible to, and affects, every tenant, not just the
editing admin's own. table_maintenance.py's generic engine also assumes
every managed table shares one exact column shape (id/code/label/
description/sort_order/is_active); these three tables don't (category_
code/category_name instead of code/label, no sort_order, and Sub-Groups
carry an extra validity_status column table_maintenance has no concept
of) — a hand-written blueprint for just these three tables, following the
same list -> manage -> add/edit -> toggle -> delete shape as table_
maintenance.py/geography_admin.py, was simpler and safer than forcing a
third column shape through either existing generic engine.

Design decisions:

  * Category/Group/Sub-Group CODE is immutable once created (edit_*() only
    lets name/description/is_active/validity_status change, never the code
    itself) — a code is already denormalized into every Service Code
    string a tenant has issued against it (services.service_code, e.g.
    'TP-MN-MC-0001'); renaming 'TP' out from under existing services would
    leave their stored code text stale. Renaming the NAME is always safe
    and instant everywhere, same as every other lookup table in this app.

  * Deleting a row that still has children (a Category with Groups under
    it, a Group with Sub-Groups, a Sub-Group with Services issued against
    it) is blocked with a friendly message, same pattern as table_
    maintenance.py/geography_admin.py — deactivate instead. No "merge/
    reassign" flow like those two screens have: merging two different
    coding-system entries doesn't make sense the way merging two near-
    duplicate "Contact Titles" does, so that's intentionally not built
    here.

  * Deprecating a Sub-Group that already has Services issued against it is
    explicitly ALLOWED (not blocked) — matches the design doc's own model:
    deprecation blocks NEW codes from being issued (schema.sql's
    trg_block_deprecated_service_subgroup trigger only fires on INSERT/
    UPDATE OF subgroup_id on the `services` table, never on the taxonomy
    tables themselves), it doesn't retroactively touch anything already
    issued. The UI surfaces a warning count so the admin knows what
    they're grandfathering in, but never stops them.
"""
import sqlite3

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from auth.decorators import tenant_admin_required
from db import get_db, log_action

service_taxonomy_admin_bp = Blueprint("service_taxonomy_admin", __name__)


@service_taxonomy_admin_bp.route("/")
@tenant_admin_required
def index():
    db = get_db()
    cat_total = db.execute("SELECT COUNT(*) c FROM service_categories").fetchone()["c"]
    cat_active = db.execute("SELECT COUNT(*) c FROM service_categories WHERE is_active = 1").fetchone()["c"]
    grp_total = db.execute("SELECT COUNT(*) c FROM service_groups").fetchone()["c"]
    grp_active = db.execute("SELECT COUNT(*) c FROM service_groups WHERE is_active = 1").fetchone()["c"]
    sub_total = db.execute("SELECT COUNT(*) c FROM service_subgroups").fetchone()["c"]
    sub_active = db.execute("SELECT COUNT(*) c FROM service_subgroups WHERE validity_status = 'active'").fetchone()["c"]
    return render_template(
        "service_taxonomy_admin/index.html",
        cat_total=cat_total, cat_active=cat_active,
        grp_total=grp_total, grp_active=grp_active,
        sub_total=sub_total, sub_active=sub_active,
    )


# ---------------------------------------------------------------- Categories

@service_taxonomy_admin_bp.route("/categories")
@tenant_admin_required
def manage_categories():
    db = get_db()
    rows = db.execute(
        """SELECT c.*, (SELECT COUNT(*) FROM service_groups g WHERE g.category_id = c.category_id) AS group_count
           FROM service_categories c ORDER BY c.category_code"""
    ).fetchall()
    return render_template("service_taxonomy_admin/categories.html", categories=rows)


@service_taxonomy_admin_bp.route("/categories/new", methods=["GET", "POST"])
@tenant_admin_required
def new_category():
    db = get_db()
    if request.method == "POST":
        code = request.form.get("category_code", "").strip().upper()
        name = request.form.get("category_name", "").strip()
        description = request.form.get("description", "").strip() or None
        is_active = 1 if request.form.get("is_active") else 0
        if not code or not name:
            flash("Code and Name are both required.", "error")
            return render_template("service_taxonomy_admin/category_form.html", category=None, form_values=request.form)
        try:
            db.execute(
                "INSERT INTO service_categories (category_code, category_name, description, is_active) VALUES (?, ?, ?, ?)",
                (code, name, description, is_active),
            )
            db.commit()
        except sqlite3.IntegrityError:
            flash(f"Category code '{code}' is already in use — codes must be unique.", "error")
            return render_template("service_taxonomy_admin/category_form.html", category=None, form_values=request.form)
        new_id = db.execute("SELECT last_insert_rowid() id").fetchone()["id"]
        log_action("Create", "service_categories", new_id, f"Added Service Category '{code} — {name}' (global taxonomy table)")
        flash(f"Category '{code} — {name}' added.", "success")
        return redirect(url_for("service_taxonomy_admin.manage_categories"))
    return render_template("service_taxonomy_admin/category_form.html", category=None, form_values=None)


@service_taxonomy_admin_bp.route("/categories/<int:category_id>/edit", methods=["GET", "POST"])
@tenant_admin_required
def edit_category(category_id):
    db = get_db()
    category = db.execute("SELECT * FROM service_categories WHERE category_id = ?", (category_id,)).fetchone()
    if category is None:
        abort(404)
    if request.method == "POST":
        name = request.form.get("category_name", "").strip()
        description = request.form.get("description", "").strip() or None
        is_active = 1 if request.form.get("is_active") else 0
        if not name:
            flash("Name is required.", "error")
            return render_template("service_taxonomy_admin/category_form.html", category=category, form_values=None)
        db.execute(
            "UPDATE service_categories SET category_name=?, description=?, is_active=?, updated_at=datetime('now') WHERE category_id=?",
            (name, description, is_active, category_id),
        )
        db.commit()
        log_action("Update", "service_categories", category_id, f"Updated Service Category '{category['category_code']}' (global taxonomy table)")
        flash(f"'{category['category_code']} — {name}' updated — visible to every tenant right away.", "success")
        return redirect(url_for("service_taxonomy_admin.manage_categories"))
    return render_template("service_taxonomy_admin/category_form.html", category=category, form_values=None)


@service_taxonomy_admin_bp.route("/categories/<int:category_id>/toggle-active", methods=["POST"])
@tenant_admin_required
def toggle_category_active(category_id):
    db = get_db()
    category = db.execute("SELECT * FROM service_categories WHERE category_id = ?", (category_id,)).fetchone()
    if category is None:
        abort(404)
    new_state = 0 if category["is_active"] else 1
    db.execute("UPDATE service_categories SET is_active = ?, updated_at = datetime('now') WHERE category_id = ?", (new_state, category_id))
    db.commit()
    log_action("Update", "service_categories", category_id, f"{'Activated' if new_state else 'Deactivated'} Service Category '{category['category_code']}'")
    flash(f"'{category['category_code']} — {category['category_name']}' {'activated' if new_state else 'deactivated'}.", "success")
    return redirect(url_for("service_taxonomy_admin.manage_categories"))


@service_taxonomy_admin_bp.route("/categories/<int:category_id>/delete", methods=["POST"])
@tenant_admin_required
def delete_category(category_id):
    db = get_db()
    category = db.execute("SELECT * FROM service_categories WHERE category_id = ?", (category_id,)).fetchone()
    if category is None:
        abort(404)
    group_count = db.execute("SELECT COUNT(*) c FROM service_groups WHERE category_id = ?", (category_id,)).fetchone()["c"]
    if group_count:
        flash(
            f"'{category['category_code']}' still has {group_count} Group(s) under it, so it can't be deleted outright. "
            f"Deactivate it instead, or remove its Groups first.",
            "error",
        )
        return redirect(url_for("service_taxonomy_admin.manage_categories"))
    db.execute("DELETE FROM service_categories WHERE category_id = ?", (category_id,))
    db.commit()
    log_action("Delete", "service_categories", category_id, f"Deleted unused Service Category '{category['category_code']}'")
    flash(f"'{category['category_code']} — {category['category_name']}' deleted.", "success")
    return redirect(url_for("service_taxonomy_admin.manage_categories"))


# -------------------------------------------------------------------- Groups

def _category_options(db):
    return db.execute("SELECT * FROM service_categories WHERE is_active = 1 ORDER BY category_code").fetchall()


@service_taxonomy_admin_bp.route("/groups")
@tenant_admin_required
def manage_groups():
    db = get_db()
    category_id = request.args.get("category_id", "").strip()
    sql = """SELECT g.*, c.category_code, c.category_name,
                    (SELECT COUNT(*) FROM service_subgroups sg WHERE sg.group_id = g.group_id) AS subgroup_count
             FROM service_groups g JOIN service_categories c ON c.category_id = g.category_id"""
    params = []
    if category_id:
        sql += " WHERE g.category_id = ?"
        params.append(category_id)
    sql += " ORDER BY c.category_code, g.group_code"
    rows = db.execute(sql, params).fetchall()
    return render_template(
        "service_taxonomy_admin/groups.html", groups=rows, category_id=category_id,
        categories=_category_options(db),
    )


@service_taxonomy_admin_bp.route("/groups/new", methods=["GET", "POST"])
@tenant_admin_required
def new_group():
    db = get_db()
    if request.method == "POST":
        category_id = request.form.get("category_id") or None
        code = request.form.get("group_code", "").strip().upper()
        name = request.form.get("group_name", "").strip()
        description = request.form.get("description", "").strip() or None
        is_active = 1 if request.form.get("is_active") else 0
        if not category_id or not code or not name:
            flash("Category, Code, and Name are all required.", "error")
            return render_template("service_taxonomy_admin/group_form.html", group=None, categories=_category_options(db), form_values=request.form)
        try:
            db.execute(
                "INSERT INTO service_groups (category_id, group_code, group_name, description, is_active) VALUES (?, ?, ?, ?, ?)",
                (category_id, code, name, description, is_active),
            )
            db.commit()
        except sqlite3.IntegrityError:
            flash(f"Group code '{code}' is already in use within that Category — codes must be unique per Category.", "error")
            return render_template("service_taxonomy_admin/group_form.html", group=None, categories=_category_options(db), form_values=request.form)
        new_id = db.execute("SELECT last_insert_rowid() id").fetchone()["id"]
        log_action("Create", "service_groups", new_id, f"Added Service Group '{code} — {name}' (global taxonomy table)")
        flash(f"Group '{code} — {name}' added.", "success")
        return redirect(url_for("service_taxonomy_admin.manage_groups"))
    return render_template("service_taxonomy_admin/group_form.html", group=None, categories=_category_options(db), form_values=None)


@service_taxonomy_admin_bp.route("/groups/<int:group_id>/edit", methods=["GET", "POST"])
@tenant_admin_required
def edit_group(group_id):
    db = get_db()
    group = db.execute(
        """SELECT g.*, c.category_code, c.category_name FROM service_groups g
           JOIN service_categories c ON c.category_id = g.category_id WHERE g.group_id = ?""",
        (group_id,),
    ).fetchone()
    if group is None:
        abort(404)
    if request.method == "POST":
        name = request.form.get("group_name", "").strip()
        description = request.form.get("description", "").strip() or None
        is_active = 1 if request.form.get("is_active") else 0
        if not name:
            flash("Name is required.", "error")
            return render_template("service_taxonomy_admin/group_form.html", group=group, categories=None, form_values=None)
        db.execute(
            "UPDATE service_groups SET group_name=?, description=?, is_active=? WHERE group_id=?",
            (name, description, is_active, group_id),
        )
        db.commit()
        log_action("Update", "service_groups", group_id, f"Updated Service Group '{group['category_code']}-{group['group_code']}' (global taxonomy table)")
        flash(f"'{group['group_code']} — {name}' updated — visible to every tenant right away.", "success")
        return redirect(url_for("service_taxonomy_admin.manage_groups"))
    return render_template("service_taxonomy_admin/group_form.html", group=group, categories=None, form_values=None)


@service_taxonomy_admin_bp.route("/groups/<int:group_id>/toggle-active", methods=["POST"])
@tenant_admin_required
def toggle_group_active(group_id):
    db = get_db()
    group = db.execute("SELECT * FROM service_groups WHERE group_id = ?", (group_id,)).fetchone()
    if group is None:
        abort(404)
    new_state = 0 if group["is_active"] else 1
    db.execute("UPDATE service_groups SET is_active = ? WHERE group_id = ?", (new_state, group_id))
    db.commit()
    log_action("Update", "service_groups", group_id, f"{'Activated' if new_state else 'Deactivated'} Service Group '{group['group_code']}'")
    flash(f"'{group['group_code']} — {group['group_name']}' {'activated' if new_state else 'deactivated'}.", "success")
    return redirect(url_for("service_taxonomy_admin.manage_groups"))


@service_taxonomy_admin_bp.route("/groups/<int:group_id>/delete", methods=["POST"])
@tenant_admin_required
def delete_group(group_id):
    db = get_db()
    group = db.execute("SELECT * FROM service_groups WHERE group_id = ?", (group_id,)).fetchone()
    if group is None:
        abort(404)
    subgroup_count = db.execute("SELECT COUNT(*) c FROM service_subgroups WHERE group_id = ?", (group_id,)).fetchone()["c"]
    if subgroup_count:
        flash(
            f"'{group['group_code']}' still has {subgroup_count} Sub-Group(s) under it, so it can't be deleted outright. "
            f"Deactivate it instead, or remove its Sub-Groups first.",
            "error",
        )
        return redirect(url_for("service_taxonomy_admin.manage_groups"))
    db.execute("DELETE FROM service_groups WHERE group_id = ?", (group_id,))
    db.commit()
    log_action("Delete", "service_groups", group_id, f"Deleted unused Service Group '{group['group_code']}'")
    flash(f"'{group['group_code']} — {group['group_name']}' deleted.", "success")
    return redirect(url_for("service_taxonomy_admin.manage_groups"))


# ---------------------------------------------------------------- Sub-Groups

def _group_options(db):
    return db.execute(
        """SELECT g.*, c.category_code FROM service_groups g
           JOIN service_categories c ON c.category_id = g.category_id
           WHERE g.is_active = 1 AND c.is_active = 1 ORDER BY c.category_code, g.group_code"""
    ).fetchall()


@service_taxonomy_admin_bp.route("/subgroups")
@tenant_admin_required
def manage_subgroups():
    db = get_db()
    category_id = request.args.get("category_id", "").strip()
    validity_status = request.args.get("validity_status", "").strip()
    sql = """SELECT sg.*, g.group_code, g.group_name, c.category_id, c.category_code, c.category_name,
                    (SELECT COUNT(*) FROM services s WHERE s.subgroup_id = sg.subgroup_id) AS service_count
             FROM service_subgroups sg
             JOIN service_groups g ON g.group_id = sg.group_id
             JOIN service_categories c ON c.category_id = g.category_id
             WHERE 1=1"""
    params = []
    if category_id:
        sql += " AND c.category_id = ?"
        params.append(category_id)
    if validity_status:
        sql += " AND sg.validity_status = ?"
        params.append(validity_status)
    sql += " ORDER BY c.category_code, g.group_code, sg.subgroup_code"
    rows = db.execute(sql, params).fetchall()
    return render_template(
        "service_taxonomy_admin/subgroups.html", subgroups=rows, category_id=category_id,
        validity_status=validity_status, categories=_category_options(db),
    )


@service_taxonomy_admin_bp.route("/subgroups/new", methods=["GET", "POST"])
@tenant_admin_required
def new_subgroup():
    db = get_db()
    if request.method == "POST":
        group_id = request.form.get("group_id") or None
        code = request.form.get("subgroup_code", "").strip().upper()
        name = request.form.get("subgroup_name", "").strip()
        description = request.form.get("description", "").strip() or None
        validity_status = request.form.get("validity_status") or "active"
        is_active = 1 if request.form.get("is_active") else 0
        if validity_status not in ("active", "deprecated", "under_review"):
            validity_status = "active"
        if not group_id or not code or not name:
            flash("Group, Code, and Name are all required.", "error")
            return render_template("service_taxonomy_admin/subgroup_form.html", subgroup=None, groups=_group_options(db), form_values=request.form)
        try:
            db.execute(
                "INSERT INTO service_subgroups (group_id, subgroup_code, subgroup_name, description, validity_status, is_active) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (group_id, code, name, description, validity_status, is_active),
            )
            db.commit()
        except sqlite3.IntegrityError:
            flash(f"Sub-Group code '{code}' is already in use within that Group — codes must be unique per Group.", "error")
            return render_template("service_taxonomy_admin/subgroup_form.html", subgroup=None, groups=_group_options(db), form_values=request.form)
        new_id = db.execute("SELECT last_insert_rowid() id").fetchone()["id"]
        log_action("Create", "service_subgroups", new_id, f"Added Service Sub-Group '{code} — {name}' (global taxonomy table)")
        flash(f"Sub-Group '{code} — {name}' added.", "success")
        return redirect(url_for("service_taxonomy_admin.manage_subgroups"))
    return render_template("service_taxonomy_admin/subgroup_form.html", subgroup=None, groups=_group_options(db), form_values=None)


@service_taxonomy_admin_bp.route("/subgroups/<int:subgroup_id>/edit", methods=["GET", "POST"])
@tenant_admin_required
def edit_subgroup(subgroup_id):
    db = get_db()
    subgroup = db.execute(
        """SELECT sg.*, g.group_code, g.group_name, c.category_code, c.category_name
           FROM service_subgroups sg
           JOIN service_groups g ON g.group_id = sg.group_id
           JOIN service_categories c ON c.category_id = g.category_id
           WHERE sg.subgroup_id = ?""",
        (subgroup_id,),
    ).fetchone()
    if subgroup is None:
        abort(404)
    service_count = db.execute("SELECT COUNT(*) c FROM services WHERE subgroup_id = ?", (subgroup_id,)).fetchone()["c"]
    if request.method == "POST":
        name = request.form.get("subgroup_name", "").strip()
        description = request.form.get("description", "").strip() or None
        validity_status = request.form.get("validity_status") or "active"
        is_active = 1 if request.form.get("is_active") else 0
        if validity_status not in ("active", "deprecated", "under_review"):
            validity_status = "active"
        if not name:
            flash("Name is required.", "error")
            return render_template("service_taxonomy_admin/subgroup_form.html", subgroup=subgroup, groups=None, form_values=None, service_count=service_count)
        db.execute(
            "UPDATE service_subgroups SET subgroup_name=?, description=?, validity_status=?, is_active=? WHERE subgroup_id=?",
            (name, description, validity_status, is_active, subgroup_id),
        )
        db.commit()
        log_action(
            "Update", "service_subgroups", subgroup_id,
            f"Updated Service Sub-Group '{subgroup['category_code']}-{subgroup['group_code']}-{subgroup['subgroup_code']}' "
            f"(validity_status={validity_status}, global taxonomy table)",
        )
        flash(f"'{subgroup['subgroup_code']} — {name}' updated — visible to every tenant right away.", "success")
        return redirect(url_for("service_taxonomy_admin.manage_subgroups"))
    return render_template("service_taxonomy_admin/subgroup_form.html", subgroup=subgroup, groups=None, form_values=None, service_count=service_count)


@service_taxonomy_admin_bp.route("/subgroups/<int:subgroup_id>/toggle-active", methods=["POST"])
@tenant_admin_required
def toggle_subgroup_active(subgroup_id):
    db = get_db()
    subgroup = db.execute("SELECT * FROM service_subgroups WHERE subgroup_id = ?", (subgroup_id,)).fetchone()
    if subgroup is None:
        abort(404)
    new_state = 0 if subgroup["is_active"] else 1
    db.execute("UPDATE service_subgroups SET is_active = ? WHERE subgroup_id = ?", (new_state, subgroup_id))
    db.commit()
    log_action("Update", "service_subgroups", subgroup_id, f"{'Activated' if new_state else 'Deactivated'} Service Sub-Group '{subgroup['subgroup_code']}'")
    flash(f"'{subgroup['subgroup_code']} — {subgroup['subgroup_name']}' {'activated' if new_state else 'deactivated'}.", "success")
    return redirect(url_for("service_taxonomy_admin.manage_subgroups"))


@service_taxonomy_admin_bp.route("/subgroups/<int:subgroup_id>/delete", methods=["POST"])
@tenant_admin_required
def delete_subgroup(subgroup_id):
    db = get_db()
    subgroup = db.execute("SELECT * FROM service_subgroups WHERE subgroup_id = ?", (subgroup_id,)).fetchone()
    if subgroup is None:
        abort(404)
    service_count = db.execute("SELECT COUNT(*) c FROM services WHERE subgroup_id = ?", (subgroup_id,)).fetchone()["c"]
    if service_count:
        flash(
            f"'{subgroup['subgroup_code']}' still has {service_count} Service(s) issued against it, so it can't be "
            f"deleted outright. Deactivate it (or set it to Deprecated) instead — that blocks new services from "
            f"using it without touching the ones that already do.",
            "error",
        )
        return redirect(url_for("service_taxonomy_admin.manage_subgroups"))
    db.execute("DELETE FROM service_subgroups WHERE subgroup_id = ?", (subgroup_id,))
    db.commit()
    log_action("Delete", "service_subgroups", subgroup_id, f"Deleted unused Service Sub-Group '{subgroup['subgroup_code']}'")
    flash(f"'{subgroup['subgroup_code']} — {subgroup['subgroup_name']}' deleted.", "success")
    return redirect(url_for("service_taxonomy_admin.manage_subgroups"))
