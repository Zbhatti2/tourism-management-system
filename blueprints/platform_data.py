"""
Module — Platform Data (SystemAdmin).

Sidebar targets for the platform catalogs (see app.py MODULES): Points of
Interest, Embassies & Consulates, Accommodation and Restaurants. They
started as "coming soon" pages and now redirect to the generic catalog
screens (blueprints/platform_catalog.py). Transport Hubs, Geography & Distances, Currencies and
Platform Lookups already have their own blueprints.

PLATFORM_CATALOG below also drives the Platform Data card on the
SystemAdmin's System Management page: one row per catalog, with a live
record count once its table exists.
"""
from flask import Blueprint, redirect, render_template, url_for

from auth.decorators import system_admin_required

platform_data_bp = Blueprint("platform_data", __name__)

# key -> sidebar/card metadata. "table" is the platform table the catalog
# will live in; "endpoint" is where the sidebar and card link to.
PLATFORM_CATALOG = {
    "transport_hubs": {
        "title": "Transport Hubs", "icon": "airplane", "table": "transport_hubs",
        "endpoint": "transport_hubs.list_hubs",
        "description": "Airports, railway stations, bus terminals and seaports — one shared list every tenant "
                       "can pick arrival and departure points from.",
    },
    "pois": {
        "title": "Points of Interest", "icon": "geo-alt", "table": "platform_pois",
        "endpoint": "platform_data.pois",
        "description": "The platform catalog of places worth visiting — historic sites, museums, bridges, dams, "
                       "lakes, malls, stadiums and arenas — that tenants can adopt into their own POI list.",
    },
    "embassies": {
        "title": "Embassies & Consulates", "icon": "flag", "table": "embassies",
        "endpoint": "platform_data.embassies",
        "description": "Embassies, consulates and high commissions by host city, with contact and visa notes.",
    },
    "accommodation": {
        "title": "Accommodation", "icon": "building", "table": "platform_accommodation",
        "endpoint": "platform_data.accommodation",
        "description": "Hotels and resorts (property only, no rates) that tenants adopt as Suppliers of the "
                       "locked Hotel / Resort types.",
    },
    "restaurants": {
        "title": "Restaurants", "icon": "cup-hot", "table": "platform_restaurants",
        "endpoint": "platform_data.restaurants",
        "description": "Restaurants and eateries that tenants adopt as Suppliers of the locked Restaurant type.",
    },
}


def _coming_soon(key):
    item = PLATFORM_CATALOG[key]
    return render_template("coming_soon.html", title=item["title"], icon=item["icon"],
                           description=item["description"])


@platform_data_bp.route("/points-of-interest/")
@system_admin_required
def pois():
    return redirect(url_for("platform_catalog.list_records", entity="pois"))


@platform_data_bp.route("/embassies/")
@system_admin_required
def embassies():
    return redirect(url_for("platform_catalog.list_records", entity="embassies"))


@platform_data_bp.route("/accommodation/")
@system_admin_required
def accommodation():
    return redirect(url_for("platform_catalog.list_records", entity="accommodation"))


@platform_data_bp.route("/restaurants/")
@system_admin_required
def restaurants():
    return redirect(url_for("platform_catalog.list_records", entity="restaurants"))
