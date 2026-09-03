"""
Module — Accounting / Finance: Purchasing & A/P. Placeholder sub-module —
see blueprints/billing_ar.py's docstring for the full story (same
"Accounting / Finance" sidebar group, same reason it's its own blueprint).
"""
from flask import Blueprint, render_template

from auth.decorators import login_required

purchasing_ap_bp = Blueprint("purchasing_ap", __name__)


@purchasing_ap_bp.route("/")
@login_required
def index():
    return render_template(
        "coming_soon.html", title="Purchasing & A/P", icon="cart-check",
        description="Manage purchase orders and accounts payable to suppliers here once this module is built out.",
    )
