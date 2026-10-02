"""
Module — Platform Data (SystemAdmin).

Home of the platform catalog screens in the Platform Admin sidebar
(see app.py MODULES): Transport Hubs, Points of Interest, Embassies &
Consulates, Accommodation and Restaurants. Each starts as a "coming soon"
page and is replaced by its real screen as it is built, in the order of
the platform master-data plan (claude/TMS-Platform-Data-Plan.md in the
TMS project). Geography & Distances, Currencies and Platform Lookups
already have their own blueprints.

PLATFORM_CATALOG below also drives the Platform Data card on the
SystemAdmin's System Management page: one row per catalog, with a live
record count once its table exists.
"""
from flask import Blueprint, render_template

from auth.decorators import system_admin_required

platform_data_bp = Blueprint("platform_data", __name__)

# key -> sidebar/card metadata. "table" is the platform table the catalog
# will live in; "endpoint" is where the sidebar and card link to.
PLATFORM_CATALOG = {
    "transport_hubs": {
        "title": "Transport Hubs", "icon": "airplane", "table": "transport_hubs",
        "endpoint": "platform_data.transport_hubs",
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


@platform_data_bp.route("/transport-hubs/")
@system_admin_required
def transport_hubs():
    return _coming_soon("transport_hubs")


@platform_data_bp.route("/points-of-interest/")
@system_admin_required
def pois():
    return _coming_soon("pois")


@platform_data_bp.route("/embassies/")
@system_admin_required
def embassies():
    return _coming_soon("embassies")


@platform_data_bp.route("/accommodation/")
@system_admin_required
def accommodation():
    return _coming_soon("accommodation")


@platform_data_bp.route("/restaurants/")
@system_admin_required
def restaurants():
    return _coming_soon("restaurants")
