import functools

from flask import abort, flash, g, redirect, request, session, url_for

from security import session_keys

# Endpoints reachable even while must_change_password is set — the change-
# password form itself (obviously), plus logout (a stuck user must always
# be able to leave rather than be trapped in a redirect loop).
_MUST_CHANGE_PASSWORD_ALLOWED_ENDPOINTS = {"system_mgmt.change_password", "auth_bp.logout"}


def get_current_dek():
    """The unwrapped field-encryption key for the CURRENT TENANT, for this
    session, or None if not logged in. Shared by every user of that tenant —
    see schema.sql MODULE T and security/crypto.py."""
    token = session.get("auth_token")
    if not token:
        return None
    return session_keys.get(token)


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        dek = get_current_dek()
        if dek is None:
            # The session cookie may still carry an auth_token (e.g. the dev
            # server's --debug auto-reloader restarted the process and wiped
            # the in-memory session_keys store, but the signed cookie itself
            # survives since it's backed by the on-disk secret key). Clearing
            # the session here keeps the "logged in" flag and the actual
            # login state in sync.
            session.clear()
            return redirect(url_for("auth_bp.login"))
        g.dek = dek
        g.tenant_id = session.get("tenant_id")
        g.user_id = session.get("user_id")
        g.username = session.get("username")
        g.display_name = session.get("display_name")
        g.role = session.get("role")
        g.tenant_name = session.get("tenant_name")

        # A Tenant-Admin-issued temporary password (new user, or a reset —
        # see blueprints/users.py) forces a real password of the user's own
        # choosing before anything else is reachable. system_mgmt.
        # change_password clears the session flag once they've done that;
        # logout is always reachable so this can never become a dead end.
        if session.get("must_change_password") and request.endpoint not in _MUST_CHANGE_PASSWORD_ALLOWED_ENDPOINTS:
            flash("Your organization's admin set a temporary password for you — please choose a new one to continue.", "error")
            return redirect(url_for("system_mgmt.change_password"))

        return view(*args, **kwargs)
    return wrapped


def tenant_admin_required(view):
    """Like login_required, but also requires the TenantAdmin or SystemAdmin
    role. Use for user-management/whole-tenant screens."""
    @functools.wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if g.role not in ("TenantAdmin", "SystemAdmin"):
            flash("That page is restricted to Tenant Admins.", "error")
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def system_admin_required(view):
    """Like login_required, but also requires the SystemAdmin role (tenant
    provisioning, whole-database backups). SystemAdmin accounts have no
    tenant_id/DEK of their own, so pages behind this decorator must not rely
    on g.dek or g.tenant_id (left unset — templates already guard g.tenant_name
    with an {% if %}, same as they do for a SystemAdmin under login_required)."""
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        token = session.get("auth_token")
        if not token:
            # Not logged in at all (or a stale cookie from before login) --
            # same case login_required clears for the same reason.
            session.clear()
            return redirect(url_for("auth_bp.login"))
        if session.get("role") != "SystemAdmin":
            # Logged in, just not as a SystemAdmin (e.g. a TenantAdmin who
            # followed an old link or a stale bookmark) -- a clean 403,
            # same as tenant_admin_required, not a forced logout: being
            # logged in as the wrong role isn't a broken session.
            flash("That page is restricted to System Admins.", "error")
            abort(403)
        g.user_id = session.get("user_id")
        g.username = session.get("username")
        g.display_name = session.get("display_name")
        g.role = session.get("role")

        if session.get("must_change_password") and request.endpoint not in _MUST_CHANGE_PASSWORD_ALLOWED_ENDPOINTS:
            flash("Please choose a new password to continue.", "error")
            return redirect(url_for("system_mgmt.change_password"))

        return view(*args, **kwargs)
    return wrapped
