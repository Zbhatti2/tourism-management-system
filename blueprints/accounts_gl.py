"""
Module — Accounting / Finance: Accounts & G/L. Placeholder sub-module —
see blueprints/billing_ar.py's docstring for the full story (same
"Accounting / Finance" sidebar group, same reason it's its own blueprint).
"""
from flask import Blueprint, render_template

from auth.decorators import login_required

accounts_gl_bp = Blueprint("accounts_gl", __name__)


@accounts_gl_bp.route("/")
@login_required
def index():
    return render_template(
        "coming_soon.html", title="Accounts & G/L", icon="calculator",
        description="Chart of accounts and general ledger entries will live here once this module is built out.",
    )
