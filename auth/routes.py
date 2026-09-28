import secrets

from flask import Blueprint, flash, g, redirect, render_template, request, session, url_for

from auth.decorators import get_current_dek
from db import get_db, is_configured, log_action
from security import crypto, session_keys
from security.passwords import hash_password, verify_password
from security.wordlist import verify_phrase
from tenant_provisioning import provision_tenant

auth_bp = Blueprint("auth_bp", __name__)


def _start_session(user_row, dek: bytes, tenant_name: str = None):
    token = secrets.token_urlsafe(32)
    session_keys.put(token, dek)
    session.clear()
    session["auth_token"] = token
    session["user_id"] = user_row["user_id"]
    session["tenant_id"] = user_row["tenant_id"]
    session["username"] = user_row["username"]
    session["display_name"] = user_row["display_name"]
    session["role"] = user_row["role"]
    session["must_change_password"] = bool(user_row["must_change_password"]) if "must_change_password" in user_row.keys() else False
    if tenant_name:
        session["tenant_name"] = tenant_name


@auth_bp.before_app_request
def _require_setup_first():
    """Anything other than /setup* must not be reachable until at least one
    tenant has been provisioned — there is nothing to log into otherwise."""
    if request.endpoint in (None, "static"):
        return
    if request.endpoint.startswith("auth_bp.setup"):
        return
    if not is_configured():
        return redirect(url_for("auth_bp.setup"))


@auth_bp.route("/setup", methods=["GET", "POST"])
def setup():
    """First-run (or new-tenant) provisioning: creates a tenant, its DEK,
    and its first user as TenantAdmin. TMS seeds its first tenant directly
    via `flask --app app seed-tenant`, so in normal use nobody hits this
    page for the first tenant — it exists for provisioning additional
    tenants later and as a manual fallback."""
    if is_configured():
        return redirect(url_for("auth_bp.login"))

    if request.method == "POST":
        tenant_name = request.form.get("tenant_name", "").strip()
        username = request.form.get("username", "").strip()
        display_name = request.form.get("display_name", "").strip() or username
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")

        errors = []
        if not tenant_name:
            errors.append("Company/organization name is required.")
        if not username:
            errors.append("User ID is required.")
        if len(password) < 8:
            errors.append("Password must be at least 8 characters.")
        if password != confirm:
            errors.append("Passwords do not match.")

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("setup.html")

        db = get_db()
        tenant_id, seed_phrase = provision_tenant(db, tenant_name, username, display_name, password)

        log_action("Setup", "tenants", tenant_id, f"Provisioned tenant {tenant_name!r} with admin {username!r}",
                   tenant_id=tenant_id)

        # Shown exactly once, on the next page — never persisted anywhere.
        session["_setup_seed_phrase"] = seed_phrase
        return redirect(url_for("auth_bp.setup_seed_phrase"))

    return render_template("setup.html")


@auth_bp.route("/setup/seed-phrase", methods=["GET", "POST"])
def setup_seed_phrase():
    phrase = session.get("_setup_seed_phrase")
    if not phrase:
        return redirect(url_for("auth_bp.login"))

    if request.method == "POST":
        session.pop("_setup_seed_phrase", None)
        flash("Setup complete. Please log in with your new password.", "success")
        return redirect(url_for("auth_bp.login"))

    return render_template("setup_seed_phrase.html", phrase=phrase)


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    # Already signed in with a live session? Send them straight to where
    # they belong instead of rendering the login form. base.html decides
    # whether to show its whole sidebar-shell layout purely off
    # session['auth_token'] being set -- and that signed cookie (plus the
    # on-disk secret key it's signed with) survives a server restart, so a
    # browser tab/cookie left over from an earlier run (e.g. the launcher
    # batch files always open a fresh tab at /login on every start) lands
    # here still authenticated. login.html only defines the LOGGED-OUT half
    # of base.html (block auth_content) -- without this guard, base.html
    # would take the logged-in branch instead and render an empty shell
    # (no login form, no page content, since login.html never defines
    # block content, and none of g.role/g.tenant_name/g.display_name are
    # set since /login isn't behind login_required either).
    #
    # get_current_dek() returns None both for "never logged in" and for "the
    # dev server restarted and wiped its in-memory session_keys store since
    # this cookie was issued" (see auth/decorators.py) -- in the latter case
    # the guard correctly falls through to the ordinary login form below,
    # same as login_required does for every other page.
    if get_current_dek() is not None:
        if session.get("role") == "SystemAdmin":
            return redirect(url_for("tenants_admin.list_tenants"))
        return redirect(url_for("dashboard.index"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        db = get_db()
        user = db.execute(
            "SELECT * FROM users WHERE username = ? AND is_active = 1", (username,)
        ).fetchone()

        if user is None or not verify_password(password, user["password_hash"]):
            flash("Incorrect username or password.", "error")
            log_action("Login", "users", None, f"Failed login attempt for username={username!r}",
                       tenant_id=None, user_id=None)
            return render_template("login.html")

        tenant_name = None
        dek = b""  # SystemAdmin accounts have no tenant DEK
        if user["role"] != "SystemAdmin":
            tenant = db.execute(
                "SELECT * FROM tenants WHERE tenant_id = ?", (user["tenant_id"],)
            ).fetchone()
            if tenant is None or tenant["status"] != "Active":
                flash("This account's organization is not active. Contact your administrator.", "error")
                return render_template("login.html")
            dek = crypto.unwrap_tenant_dek(tenant["dek_wrapped"])
            tenant_name = tenant["tenant_name"]

        _start_session(user, dek, tenant_name)
        db.execute("UPDATE users SET last_login_at = datetime('now') WHERE user_id = ?", (user["user_id"],))
        db.commit()
        log_action("Login", "users", user["user_id"], "Successful login",
                   tenant_id=user["tenant_id"], user_id=user["user_id"])

        # Opportunistic daily maintenance: this app has no background
        # scheduler of its own (it only runs while someone has it open), so
        # "once a day" retention purging is checked here, at the one point
        # every session is guaranteed to pass through. Never let a purge
        # problem block someone from logging in.
        try:
            from blueprints.system_mgmt import maybe_run_daily_purge
            maybe_run_daily_purge(db)
        except Exception as e:
            log_action("Purge", None, None, f"Daily purge check failed: {e}")

        # A SystemAdmin has no tenant_id of their own (schema.sql MODULE T),
        # so every tile on the regular Dashboard would just show empty,
        # meaningless zero-counts for them -- send them straight to the
        # platform-wide screen that's actually theirs to use instead.
        if user["role"] == "SystemAdmin":
            return redirect(url_for("tenants_admin.list_tenants"))
        return redirect(url_for("dashboard.index"))

    return render_template("login.html")


@auth_bp.route("/logout")
def logout():
    token = session.get("auth_token")
    if token:
        session_keys.drop(token)
        log_action("Logout", "users", session.get("user_id"))
    session.clear()
    return redirect(url_for("auth_bp.login"))


@auth_bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    """Self-service password reset via a per-user recovery seed phrase.
    This does NOT touch any encryption key — the tenant DEK is independent
    of user passwords (see security/crypto.py) — so a reset is just "verify
    the phrase, set a new password_hash." Nothing needs to be re-encrypted."""
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        phrase = request.form.get("seed_phrase", "")
        new_password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")

        db = get_db()
        user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()

        if user is None or not user["recovery_seed_hash"] or not verify_phrase(phrase, user["recovery_seed_hash"]):
            flash("That User ID and recovery phrase don't match our records.", "error")
            return render_template("forgot_password.html")
        if len(new_password) < 8:
            flash("Password must be at least 8 characters.", "error")
            return render_template("forgot_password.html")
        if new_password != confirm:
            flash("Passwords do not match.", "error")
            return render_template("forgot_password.html")

        db.execute(
            # A password the user chose themselves via this flow is never a
            # "temporary" one, even if must_change_password had been set
            # (e.g. a Tenant-Admin-issued temp password whose owner forgot
            # it before ever changing it) — clear that flag here so they
            # aren't immediately forced through Change Password again right
            # after logging in with the password they just picked.
            "UPDATE users SET password_hash = ?, must_change_password = 0, updated_at = datetime('now') WHERE user_id = ?",
            (hash_password(new_password), user["user_id"]),
        )
        db.commit()
        log_action("PasswordReset", "users", user["user_id"], "Password reset via recovery seed phrase",
                   tenant_id=user["tenant_id"], user_id=user["user_id"])

        flash("Password reset. Please log in with your new password.", "success")
        return redirect(url_for("auth_bp.login"))

    return render_template("forgot_password.html")
