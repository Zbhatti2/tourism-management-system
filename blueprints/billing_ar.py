"""
Module — Accounting / Finance: Billing & A/R. Placeholder sub-module,
one of three under the new "Accounting / Finance" sidebar group (see
app.py's MODULES and blueprints/purchasing_ap.py / accounts_gl.py for
its siblings), per the request:

    "Add a placeholder for 'Accounting & Finance' Module; with 3
    Sub-options, 'Billing & A/R': 'Purchasing & A/P': and 'Accounts &
    G/L': This will go below the Inventory Module."

Kept as its own blueprint (rather than one shared "accounting" blueprint
with three routes) so the sidebar highlights exactly which sub-option is
active, same convention as Organization Intelligence's Documents/
Intelligence children.
"""
from flask import Blueprint, render_template

from auth.decorators import login_required

billing_ar_bp = Blueprint("billing_ar", __name__)


@billing_ar_bp.route("/")
@login_required
def index():
    return render_template(
        "coming_soon.html", title="Billing & A/R", icon="receipt",
        description="Invoice customers and track accounts receivable here once this module is built out.",
    )
