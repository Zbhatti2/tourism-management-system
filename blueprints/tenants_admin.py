"""
Module — Tenant Management (SystemAdmin).

The platform-wide console for provisioning and overseeing every tenant —
distinct from System Management (blueprints/system_mgmt.py), which is each
tenant's OWN settings/backups/audit-log screen. A SystemAdmin has no
tenant_id of their own (schema.sql MODULE T), so nothing here is
tenant-scoped by g.tenant_id the way almost every other blueprint is.

Provisioning reuses tenant_provisioning.provision_tenant() — the exact same
operation the self-service /setup wizard (auth/routes.py) performs; this
screen is just the normal, logged-in way to do it once TMS is actually
running multi-tenant, rather than only a first-run/manual-fallback wizard.
"""
from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for

from auth.decorators import system_admin_required
from db import get_db, log_action
from tenant_provisioning import provision_tenant

tenants_admin_bp = Blueprint("tenants_admin", __name__)


@tenants_admin_bp.route("/")
@system_admin_required
def list_tenants():
    db = get_db()
    rows = db.execute(
        """SELECT t.*,
               (SELECT COUNT(*) FROM users u WHERE u.tenant_id = t.tenant_id) AS user_count,
               (SELECT COUNT(*) FROM users u WHERE u.tenant_id = t.tenant_id AND u.is_active = 1) AS active_user_count
           FROM tenants t WHERE t.is_platform = 0 ORDER BY t.tenant_name COLLATE NOCASE"""
    ).fetchall()
    return render_template("tenants_admin/list.html", tenants=rows)


@tenants_admin_bp.route("/new", methods=["GET", "POST"])
@system_admin_required
def new_tenant():
    db = get_db()
    if request.method == "POST":
        tenant_name = request.form.get("tenant_name", "").strip()
        username = request.form.get("username", "").strip()
        display_name = request.form.get("display_name", "").strip() or username
        website_domain = request.form.get("website_domain", "").strip().lower()
        for prefix in ("https://", "http://"):
            if website_domain.startswith(prefix):
                website_domain = website_domain[len(prefix):]
        website_domain = website_domain.rstrip("/") or None
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")

        errors = []
        if not tenant_name:
            errors.append("Organization name is required.")
        if not username:
            errors.append("Admin User ID is required.")
        elif db.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
            errors.append("That User ID is already taken — it must be unique across the whole system.")
        if len(password) < 8:
            errors.append("Password must be at least 8 characters.")
        if password != confirm:
            errors.append("Passwords do not match.")
        if db.execute("SELECT 1 FROM tenants WHERE tenant_name = ?", (tenant_name,)).fetchone():
            errors.append("A tenant with that organization name already exists.")

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("tenants_admin/form.html",
                                   form={"tenant_name": tenant_name, "username": username, "display_name": display_name,
                                         "website_domain": website_domain})

        tenant_id, seed_phrase_value = provision_tenant(
            db, tenant_name, username, display_name, password, must_change_password=True,
            website_domain=website_domain,
        )
        log_action("ProvisionTenant", "tenants", tenant_id,
                   f"Provisioned tenant {tenant_name!r} with admin {username!r} from Tenant Management",
                   tenant_id=tenant_id)

        session["_new_tenant_seed_phrase"] = seed_phrase_value
        session["_new_tenant_label"] = f"{display_name} ({username})"
        session["_new_tenant_org"] = tenant_name
        flash(f"Tenant '{tenant_name}' created. Give '{display_name}' their password directly.", "success")
        return redirect(url_for("tenants_admin.seed_phrase"))

    return render_template("tenants_admin/form.html", form={})


@tenants_admin_bp.route("/seed-phrase")
@system_admin_required
def seed_phrase():
    phrase = session.pop("_new_tenant_seed_phrase", None)
    label = session.pop("_new_tenant_label", None)
    org = session.pop("_new_tenant_org", None)
    if not phrase:
        return redirect(url_for("tenants_admin.list_tenants"))
    return render_template("tenants_admin/seed_phrase.html", phrase=phrase, label=label, org=org)


@tenants_admin_bp.route("/<int:tenant_id>/toggle-status", methods=["POST"])
@system_admin_required
def toggle_status(tenant_id):
    db = get_db()
    tenant = db.execute("SELECT * FROM tenants WHERE tenant_id = ?", (tenant_id,)).fetchone()
    if tenant is None:
        abort(404)
    if tenant["is_platform"]:
        # The reserved TMS Platform row isn't a real, operating tenant --
        # it has no users to suspend and Tenant Management's list already
        # hides it, so nothing should ever be able to reach this route for
        # it. Refuse rather than silently flipping a status nothing reads.
        abort(404)
    new_status = "Suspended" if tenant["status"] == "Active" else "Active"
    db.execute(
        "UPDATE tenants SET status = ?, updated_at = datetime('now') WHERE tenant_id = ?",
        (new_status, tenant_id),
    )
    db.commit()
    log_action("Update", "tenants", tenant_id, f"{tenant['tenant_name']} set to {new_status}", tenant_id=tenant_id)
    flash(f"'{tenant['tenant_name']}' is now {new_status}.{' Its users can no longer log in.' if new_status == 'Suspended' else ''}", "success")
    return redirect(url_for("tenants_admin.list_tenants"))
