import re
import secrets

from flask import Blueprint, flash, g, redirect, render_template, request, session, url_for

from db import get_db, is_configured, log_action
from security import crypto, session_keys
from security.passwords import hash_password, verify_password
from security.wordlist import generate_seed_phrase, hash_phrase, normalize_phrase, verify_phrase

auth_bp = Blueprint("auth_bp", __name__)


def _slugify_tenant_code(tenant_name: str) -> str:
    """'Heritage Tours' -> 'HERITAGE_TOURS'. Used as the tenant's short
    internal code when the setup form doesn't ask for one separately."""
    code = re.sub(r"[^A-Za-z0-9]+", "_", tenant_name.strip()).strip("_").upper()
    return code or secrets.token_hex(4).upper()


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
    and its first user as TenantAdmin. Phase 1.1 seeds Heritage Tours and
    its admin (Zeb) directly via `flask --app app seed-tenant`, so in normal
    use nobody hits this page for the first tenant — it exists for
    provisioning additional tenants later (Phase 1.3+) and as a manual
    fallback."""
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

        dek = crypto.new_tenant_dek()
        dek_wrapped = crypto.wrap_tenant_dek(dek)
        tenant_code = _slugify_tenant_code(tenant_name)

        cur = db.execute(
            "INSERT INTO tenants (tenant_code, tenant_name, dek_wrapped) VALUES (?, ?, ?)",
            (tenant_code, tenant_name, dek_wrapped),
        )
        tenant_id = cur.lastrowid

        seed_phrase = generate_seed_phrase()
        db.execute(
            """INSERT INTO users
               (tenant_id, username, display_name, password_hash, role, recovery_seed_hash)
               VALUES (?, ?, ?, ?, 'TenantAdmin', ?)""",
            (tenant_id, username, display_name, hash_password(password), hash_phrase(seed_phrase)),
        )
        db.commit()

        from seed_data import seed_lookup_tables
        seed_lookup_tables(db, tenant_id)

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
    Unlike PIMS's single-user version, this does NOT touch any encryption
    key — the tenant DEK is independent of user passwords (see
    security/crypto.py) — so a reset is just "verify the phrase, set a new
    password_hash." Nothing needs to be re-encrypted."""
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
            "UPDATE users SET password_hash = ?, updated_at = datetime('now') WHERE user_id = ?",
            (hash_password(new_password), user["user_id"]),
        )
        db.commit()
        log_action("PasswordReset", "users", user["user_id"], "Password reset via recovery seed phrase",
                   tenant_id=user["tenant_id"], user_id=user["user_id"])

        flash("Password reset. Please log in with your new password.", "success")
        return redirect(url_for("auth_bp.login"))

    return render_template("forgot_password.html")
