"""
Geography lookup blueprint — serves the Region -> Country -> Province/State
-> City hierarchy as one JSON tree, fetched once client-side by the address
forms (see static/js/geography_picker.js) and filtered locally as the user
picks each level. This avoids a round trip per dropdown.

These tables (regions/countries/states/cities) are GLOBAL — not
tenant-scoped — so this endpoint returns the same data for every tenant.
Still behind @login_required since address forms are only ever reached
inside the authenticated app.
"""
from flask import Blueprint, jsonify

from auth.decorators import login_required
from db import get_db

geography_bp = Blueprint("geography", __name__)


@geography_bp.route("/tree")
@login_required
def tree():
    db = get_db()

    regions = [dict(r) for r in db.execute(
        "SELECT region_id, code, label FROM regions WHERE is_active = 1 ORDER BY label COLLATE NOCASE"
    ).fetchall()]
    countries = [dict(c) for c in db.execute(
        "SELECT country_id, region_id, code, label FROM countries WHERE is_active = 1 ORDER BY label COLLATE NOCASE"
    ).fetchall()]
    states = [dict(s) for s in db.execute(
        "SELECT state_id, country_id, code, label FROM states WHERE is_active = 1 ORDER BY label COLLATE NOCASE"
    ).fetchall()]
    cities = [dict(c) for c in db.execute(
        "SELECT city_id, state_id, label FROM cities WHERE is_active = 1 ORDER BY label COLLATE NOCASE"
    ).fetchall()]

    return jsonify({
        "regions": regions,
        "countries": countries,
        "states": states,
        "cities": cities,
    })
