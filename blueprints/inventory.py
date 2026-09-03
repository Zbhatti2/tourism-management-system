"""
Module — Inventory Management. Placeholder, added to the sidebar in place
of the retired Points of Interest sidebar entry (POI itself isn't going
anywhere — its blueprint/routes/templates are unchanged; it just moved off
the left sidebar onto the Dashboard's "Points of Interest" tile, see
templates/dashboard.html), per the request:

    "Remove 'Points of Interest' from the menu and replace it with
    'Inventory Management' ... This will be a placeholder to wire the
    Inventory management module."

No tables of its own in schema.sql yet — those get added when this module
is actually built out (tracked/managed inventory items, stock levels,
supplier links, ...), following the same pattern every other module here
does (Module A/Contacts is the reference implementation).
"""
from flask import Blueprint, render_template

from auth.decorators import login_required

inventory_bp = Blueprint("inventory", __name__)


@inventory_bp.route("/")
@login_required
def index():
    return render_template(
        "coming_soon.html", title="Inventory Management", icon="boxes",
        description="Track inventory items, stock levels, and their supplier links here once this module is built out.",
    )
