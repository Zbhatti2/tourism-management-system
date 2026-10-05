"""
Minimal manual CSRF protection (no Flask-WTF dependency — kept out of
requirements.txt deliberately to minimize what this single-user local app
depends on). Every form includes a hidden csrf_token field; every POST is
checked against the token stored in the signed session cookie.
"""
import secrets

from flask import abort, request, session


def get_csrf_token() -> str:
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


def validate_csrf():
    token = request.form.get("csrf_token", "")
    expected = session.get("csrf_token", "")
    if not expected or not secrets.compare_digest(token, expected):
        abort(400, description="Invalid or missing CSRF token — please retry the form.")


# Public APIs called from other sites (no session cookie, no form token):
# the tenant Website API (blueprints/site_api.py) guards itself with its
# site key, allowed web addresses and per-visitor limits.
EXEMPT_PREFIXES = ("/api/site/",)


def init_app(app):
    app.jinja_env.globals["csrf_token"] = get_csrf_token

    @app.before_request
    def _check_csrf():
        if request.method == "POST" and not request.path.startswith(EXEMPT_PREFIXES):
            validate_csrf()
