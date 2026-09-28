"""
Module — Manage Users (TenantAdmin).

A TenantAdmin's own view of their organization's users: add a teammate,
edit their role/contact info, deactivate/reactivate them, or reset a
password they've lost. Every query here is scoped to g.tenant_id — a
TenantAdmin can only ever see or touch users in their own tenant, never
another tenant's (see auth/decorators.py's tenant_admin_required and
schema.sql MODULE T).

Setting a teammate's password is possible at all ONLY because tenant field
encryption is split from login (schema.sql MODULE T / security/crypto.py):
the tenant's DEK doesn't depend on any one user's password, so resetting
someone else's password re-encrypts nothing.

Every newly-created or password-reset user gets must_change_password = 1
and a fresh recovery seed phrase, shown exactly once on the page right
after — mirrors auth/routes.py's self-service /setup flow, just reached
from inside the app instead of before first login. auth/decorators.py's
login_required enforces the change (redirects to Change Password until
it's done) so a TenantAdmin-issued temp password is never used long-term.
"""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, session, url_for

from auth.decorators import tenant_admin_required
from db import get_db, log_action
from security.passwords import hash_password
from security.wordlist import generate_seed_phrase, hash_phrase

users_bp = Blueprint("users", __name__)

ROLES = ["User", "TenantAdmin"]  # a TenantAdmin can grant/revoke TenantAdmin, never SystemAdmin


def _get_user_or_404(db, user_id):
    user = db.execute(
        "SELECT * FROM users WHERE user_id = ? AND tenant_id = ?", (user_id, g.tenant_id)
    ).fetchone()
    if user is None:
        abort(404)
    return user


def _active_tenant_admin_count(db, exclude_user_id=None):
    row = db.execute(
        "SELECT COUNT(*) c FROM users WHERE tenant_id = ? AND role = 'TenantAdmin' AND is_active = 1 "
        "AND user_id != ?",
        (g.tenant_id, exclude_user_id or -1),
    ).fetchone()
    return row["c"]


# ---------------------------------------------------------------- delete
#
# Every column in the TMS schema that REFERENCES users(user_id), split in two:
#
# BLOCKING_REFERENCES -- live ownership that would be orphaned by a delete.
# TMS has none today (nothing is "assigned" to a user yet); the list is kept
# so future modules (e.g. bookings assigned to an agent) can add to it.
#
# HISTORICAL_REFERENCES -- "who did this" breadcrumbs on rows that stand on
# their own (an audit entry, an intelligence note, an AI agent run or review
# decision). All are nullable, so they are cleared just before the delete
# (the row survives, like a deleted account's old comments showing "ghost").
BLOCKING_REFERENCES = []
HISTORICAL_REFERENCES = [
    ("audit_log", "user_id"),
    ("organization_intelligence", "entered_by_user_id"),
    ("agent_runs", "created_by"),
    ("ai_review_items", "reviewed_by"),
    ("ai_review_items", "undone_by"),
]


def _usage_count(db, user_id):
    total = 0
    breakdown = []
    for ref in BLOCKING_REFERENCES:
        count = db.execute(
            f"SELECT COUNT(*) c FROM {ref['table']} WHERE {ref['fk']} = ? AND tenant_id = ?",
            (user_id, g.tenant_id),
        ).fetchone()["c"]
        if count:
            breakdown.append({"label": ref["label"], "count": count})
        total += count
    return total, breakdown


def _clear_historical_user_refs(db, user_id):
    for table, column in HISTORICAL_REFERENCES:
        db.execute(f"UPDATE {table} SET {column} = NULL WHERE {column} = ? AND tenant_id = ?", (user_id, g.tenant_id))


@users_bp.route("/")
@tenant_admin_required
def list_users():
    db = get_db()
    rows = db.execute(
        "SELECT * FROM users WHERE tenant_id = ? ORDER BY display_name COLLATE NOCASE", (g.tenant_id,)
    ).fetchall()
    return render_template("users/list.html", users=rows)


@users_bp.route("/new", methods=["GET", "POST"])
@tenant_admin_required
def new_user():
    db = get_db()
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        display_name = request.form.get("display_name", "").strip() or username
        email = request.form.get("email", "").strip() or None
        role = request.form.get("role", "User")
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")

        errors = []
        if not username:
            errors.append("User ID is required.")
        elif db.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
            errors.append("That User ID is already taken — it must be unique across the whole system.")
        if role not in ROLES:
            errors.append("Invalid role.")
        if len(password) < 8:
            errors.append("Password must be at least 8 characters.")
        if password != confirm:
            errors.append("Passwords do not match.")

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("users/form.html", user=None, roles=ROLES,
                                   form={"username": username, "display_name": display_name, "email": email, "role": role})

        seed_phrase = generate_seed_phrase()
        db.execute(
            """INSERT INTO users (tenant_id, username, display_name, email, password_hash, role,
               recovery_seed_hash, must_change_password)
               VALUES (?, ?, ?, ?, ?, ?, ?, 1)""",
            (g.tenant_id, username, display_name, email, hash_password(password), role, hash_phrase(seed_phrase)),
        )
        db.commit()
        new_id = db.execute("SELECT last_insert_rowid() id").fetchone()["id"]
        log_action("Create", "users", new_id, f"Added user '{username}' ({role})")

        session["_new_user_seed_phrase"] = seed_phrase
        session["_new_user_label"] = f"{display_name} ({username})"
        flash(f"'{display_name}' added. Give them their temporary password directly — "
              f"they'll be asked to change it on first login.", "success")
        return redirect(url_for("users.seed_phrase"))

    return render_template("users/form.html", user=None, roles=ROLES, form={})


@users_bp.route("/seed-phrase")
@tenant_admin_required
def seed_phrase():
    """One-time display of the recovery phrase just generated for a new or
    password-reset user — see new_user()/reset_password() below. Reads
    from the session rather than a URL param so the phrase itself is never
    visible in a link, browser history, or a server access log."""
    phrase = session.pop("_new_user_seed_phrase", None)
    label = session.pop("_new_user_label", None)
    if not phrase:
        return redirect(url_for("users.list_users"))
    return render_template("users/seed_phrase.html", phrase=phrase, label=label)


@users_bp.route("/<int:user_id>/edit", methods=["GET", "POST"])
@tenant_admin_required
def edit_user(user_id):
    db = get_db()
    user = _get_user_or_404(db, user_id)
    is_self = user_id == g.user_id

    if request.method == "POST":
        display_name = request.form.get("display_name", "").strip() or user["username"]
        email = request.form.get("email", "").strip() or None
        role = request.form.get("role", user["role"])

        if is_self and role != user["role"]:
            flash("You can't change your own role.", "error")
            return render_template("users/form.html", user=user, roles=ROLES, form=request.form)
        if role not in ROLES:
            flash("Invalid role.", "error")
            return render_template("users/form.html", user=user, roles=ROLES, form=request.form)
        if user["role"] == "TenantAdmin" and role == "User" and _active_tenant_admin_count(db, exclude_user_id=user_id) == 0:
            flash("This is the last active Tenant Admin — demote someone else to Tenant Admin first, "
                  "or your organization would be left with no one able to manage users.", "error")
            return render_template("users/form.html", user=user, roles=ROLES, form=request.form)

        db.execute(
            "UPDATE users SET display_name = ?, email = ?, role = ?, updated_at = datetime('now') WHERE user_id = ? AND tenant_id = ?",
            (display_name, email, role, user_id, g.tenant_id),
        )
        db.commit()
        log_action("Update", "users", user_id, f"Updated user '{user['username']}'")
        flash(f"'{display_name}' updated.", "success")
        return redirect(url_for("users.list_users"))

    return render_template("users/form.html", user=user, roles=ROLES, form=user, is_self=is_self)


@users_bp.route("/<int:user_id>/toggle-active", methods=["POST"])
@tenant_admin_required
def toggle_active(user_id):
    db = get_db()
    user = _get_user_or_404(db, user_id)

    if user_id == g.user_id:
        flash("You can't deactivate your own account.", "error")
        return redirect(url_for("users.list_users"))

    new_state = 0 if user["is_active"] else 1
    if new_state == 0 and user["role"] == "TenantAdmin" and _active_tenant_admin_count(db, exclude_user_id=user_id) == 0:
        flash("This is the last active Tenant Admin — your organization needs at least one.", "error")
        return redirect(url_for("users.list_users"))

    db.execute(
        "UPDATE users SET is_active = ?, updated_at = datetime('now') WHERE user_id = ? AND tenant_id = ?",
        (new_state, user_id, g.tenant_id),
    )
    db.commit()
    log_action("Update", "users", user_id, f"{'Activated' if new_state else 'Deactivated'} user '{user['username']}'")
    flash(f"'{user['display_name']}' {'activated' if new_state else 'deactivated'}.", "success")
    return redirect(url_for("users.list_users"))


@users_bp.route("/<int:user_id>/delete", methods=["POST"])
@tenant_admin_required
def delete_user(user_id):
    """Permanently removes a user — unlike Deactivate (toggle_active above),
    which just blocks their login and keeps everything about them on file,
    this erases the users row itself. Only allowed when nothing still
    depends on them being a real, ongoing person: see BLOCKING_REFERENCES/
    HISTORICAL_REFERENCES above for exactly what counts as "a record
    associated with the user" for this check and why."""
    db = get_db()
    user = _get_user_or_404(db, user_id)

    if user_id == g.user_id:
        flash("You can't delete your own account.", "error")
        return redirect(url_for("users.list_users"))
    if user["role"] == "TenantAdmin" and user["is_active"] and _active_tenant_admin_count(db, exclude_user_id=user_id) == 0:
        flash("This is the last active Tenant Admin — your organization needs at least one.", "error")
        return redirect(url_for("users.list_users"))

    count, breakdown = _usage_count(db, user_id)
    if count:
        parts = ", ".join(f"{b['count']} {b['label']}" for b in breakdown)
        flash(
            f"'{user['display_name']}' still has {parts} — reassign those "
            f"before deleting this user, or deactivate them instead.",
            "error",
        )
        return redirect(url_for("users.list_users"))

    _clear_historical_user_refs(db, user_id)
    db.execute("DELETE FROM users WHERE user_id = ? AND tenant_id = ?", (user_id, g.tenant_id))
    db.commit()
    log_action("Delete", "users", user_id, f"Deleted user '{user['username']}' ({user['role']})")
    flash(f"'{user['display_name']}' deleted.", "success")
    return redirect(url_for("users.list_users"))


@users_bp.route("/<int:user_id>/reset-password", methods=["GET", "POST"])
@tenant_admin_required
def reset_password(user_id):
    db = get_db()
    user = _get_user_or_404(db, user_id)

    if request.method == "POST":
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        if len(password) < 8:
            flash("Password must be at least 8 characters.", "error")
            return render_template("users/reset_password.html", user=user)
        if password != confirm:
            flash("Passwords do not match.", "error")
            return render_template("users/reset_password.html", user=user)

        seed_phrase_value = generate_seed_phrase()
        db.execute(
            """UPDATE users SET password_hash = ?, recovery_seed_hash = ?, must_change_password = 1,
               updated_at = datetime('now') WHERE user_id = ? AND tenant_id = ?""",
            (hash_password(password), hash_phrase(seed_phrase_value), user_id, g.tenant_id),
        )
        db.commit()
        log_action("PasswordReset", "users", user_id, f"Password reset for user '{user['username']}' by a Tenant Admin")

        session["_new_user_seed_phrase"] = seed_phrase_value
        session["_new_user_label"] = f"{user['display_name']} ({user['username']})"
        flash(f"Password reset for '{user['display_name']}'. Give them the new temporary password directly.", "success")
        return redirect(url_for("users.seed_phrase"))

    return render_template("users/reset_password.html", user=user)
