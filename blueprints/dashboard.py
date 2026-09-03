from flask import Blueprint, abort, g, render_template

from auth.decorators import login_required
from db import get_db

dashboard_bp = Blueprint("dashboard", __name__)

# The Dashboard's three not-yet-built tiles (Points of Interest is the
# fourth, but it's a real module -- see index() below, it links straight
# to poi.list_pois instead of here). Each gets its own slug so the URL/
# title is stable once a real module is built to replace it, without
# having to touch templates/dashboard.html's tile markup. Per the Main
# Dashboard Design mockup this replaces the old counts-grid dashboard.
COMING_SOON_TILES = {
    "product-design": {"title": "Product Design", "icon": "diagram-3", "description": "Build and price tour packages here once this module is built out."},
    "schedules": {"title": "Schedules (Flights, Rail, Ground)", "icon": "calendar3", "description": "Flight, rail, and ground transport schedules will live here once this module is built out."},
    "custom-tours": {"title": "Custom (Plan Your Tours)", "icon": "map", "description": "Build a custom, one-off tour itinerary here once this module is built out."},
}


@dashboard_bp.route("/")
@login_required
def index():
    db = get_db()
    tenant_id = g.tenant_id
    counts = {
        "contacts": db.execute(
            "SELECT COUNT(*) c FROM contacts WHERE tenant_id = ? AND is_deleted = 0", (tenant_id,)
        ).fetchone()["c"],
        "organizations": db.execute(
            "SELECT COUNT(*) c FROM organizations WHERE tenant_id = ?", (tenant_id,)
        ).fetchone()["c"],
        "suppliers": db.execute(
            "SELECT COUNT(*) c FROM suppliers WHERE tenant_id = ? AND is_deleted = 0", (tenant_id,)
        ).fetchone()["c"],
        "points_of_interest": db.execute(
            "SELECT COUNT(*) c FROM points_of_interest WHERE tenant_id = ? AND is_deleted = 0", (tenant_id,)
        ).fetchone()["c"],
        # Products, Tours in Process, and Tours Conducted have no tables
        # yet (Product Design and the tour-planning tiles above are all
        # still placeholders) -- shown as a real, honest 0 rather than a
        # made-up number, and wired up to real counts once those modules
        # exist.
        "products": 0,
        "tours_in_process": 0,
        "tours_conducted": 0,
    }
    return render_template("dashboard.html", counts=counts)


@dashboard_bp.route("/coming-soon/<slug>")
@login_required
def coming_soon(slug):
    tile = COMING_SOON_TILES.get(slug)
    if tile is None:
        abort(404)
    return render_template(
        "coming_soon.html", title=tile["title"], icon=tile["icon"], description=tile["description"]
    )
