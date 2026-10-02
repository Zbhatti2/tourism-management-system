"""
Tourism Management System — Flask app factory / entry point.

Run locally with Start_TMS.bat (or `flask --app app run --debug`).
Schema changes are applied automatically at startup (db.py's
run_pending_migrations). A brand-new, empty database only:
    flask --app app init-db        # creates instance/tms.db from schema.sql
    flask --app app seed-tenant    # creates Ma Vie Tours + Zeb (TenantAdmin)
Platform-level login (once per database):
    flask --app app create-system-admin

Roles: "roles": [...] on a MODULES entry limits who sees it in the sidebar
(templates/base.html); omitted means every logged-in user. The SystemAdmin
belongs to no tenant, so every tenant module is hidden from it and Tenant
Management is the mirror image -- SystemAdmin only.
"""
from flask import Flask, render_template

import db as db_module
from security import csrf
from utils import basename, format_date, format_date_abbrev, format_phone, format_price, linkify, map_coordinates_link, orblank


# Points of Interest was removed from this sidebar list (its blueprint,
# routes and templates are all unchanged -- poi.list_pois/view_poi/etc.
# still work exactly as before) and replaced with Inventory Management,
# per the Main Dashboard Design mockup: POI now lives on the Dashboard as
# one of its four big tiles instead (see templates/dashboard.html),
# alongside three not-yet-built tiles (Product Design, Schedules, Custom)
# routed through dashboard.coming_soon.
#
# "Inventory Management" itself became a parent group (same shape as
# "Accounting / Finance" below) once its first sub-module, Services, was
# built out -- see blueprints/services.py. Products (blueprints/products.py)
# is the second sub-module, following the identical GLOBAL-taxonomy +
# tenant-scoped-catalog pattern.
MODULES = [
    {"key": "dashboard", "label": "Dashboard", "icon": "speedometer2", "roles": ["TenantAdmin", "User"], "endpoint": "dashboard.index"},
    {"key": "contacts", "label": "Contacts", "icon": "people", "roles": ["TenantAdmin", "User"], "endpoint": "contacts.list_contacts"},
    {"key": "organizations", "label": "Organizations", "icon": "building", "roles": ["TenantAdmin", "User"], "endpoint": "organizations.list_organizations"},
    {"key": "suppliers", "label": "Suppliers", "icon": "truck", "roles": ["TenantAdmin", "User"], "endpoint": "suppliers.list_suppliers"},
    {"key": "hr", "label": "Human Resources", "icon": "person-badge", "roles": ["TenantAdmin", "User"], "endpoint": "hr.index"},
    {"key": "inventory", "label": "Inventory Management", "icon": "boxes", "roles": ["TenantAdmin", "User"], "children": [
        {"key": "services", "label": "Services", "icon": "list-check", "endpoint": "services.list_services"},
        {"key": "products", "label": "Products", "icon": "box-seam", "endpoint": "products.list_products"},
    ]},
    # First module off the "Next-Phase Blueprint" roadmap -- see
    # blueprints/packages.py's docstring. Tenant-scoped tour package
    # templates assembled from Services/Products/Suppliers/POI; no child
    # sub-modules yet (Departures/Bookings are later roadmap phases), so
    # this is a single top-level entry rather than a parent-with-children
    # group like Inventory Management above.
    {"key": "packages", "label": "Package Management", "icon": "map", "roles": ["TenantAdmin", "User"], "endpoint": "packages.list_packages"},
    # Foundations for Zeb's "First Agents" plan (Sept 2026) -- Agent Runs +
    # Human Review Queue. Single top-level entry (Review Queue is a tab on
    # the Agent Runs page, not a separate sidebar child -- see
    # blueprints/ai_agents.py's module docstring for the full design note).
    {"key": "ai_agents", "label": "AI Agents", "icon": "robot", "roles": ["TenantAdmin", "User"], "endpoint": "ai_agents.list_runs"},
    # "Accounting / Finance" and "Tables & Utilities" below are parent nav
    # items with no page of their own, same pattern as "Organization
    # Intelligence" -- each just groups its sub-modules' blueprint keys in
    # the sidebar. See templates/base.html for how "children" is rendered
    # as a collapsible sub-menu.
    {"key": "accounting_finance", "label": "Accounting / Finance", "icon": "lightbulb", "roles": ["TenantAdmin", "User"], "children": [
        {"key": "billing_ar", "label": "Billing & A/R", "icon": "receipt", "endpoint": "billing_ar.index"},
        {"key": "purchasing_ap", "label": "Purchasing & A/P", "icon": "cart-check", "endpoint": "purchasing_ap.index"},
        {"key": "accounts_gl", "label": "Accounts & G/L", "icon": "calculator", "endpoint": "accounts_gl.index"},
    ]},
    {"key": "organization_intelligence", "label": "Organization Intelligence", "icon": "lightbulb", "roles": ["TenantAdmin", "User"], "children": [
        {"key": "documents", "label": "Documents", "icon": "folder2-open", "endpoint": "documents.index"},
        {"key": "intelligence", "label": "Intelligence", "icon": "journal-text", "endpoint": "intelligence.index"},
    ]},
    # Data Exchange and Table Maintenance are unchanged, existing
    # blueprints -- just moved off the top level and grouped under one
    # "Tables & Utilities" parent (matching the mockup's exact label).
    {"key": "tables_utilities", "label": "Tables & Utilities", "icon": "lightbulb", "roles": ["TenantAdmin", "User"], "children": [
        {"key": "data_exchange", "label": "Data Exchange", "icon": "arrow-left-right", "endpoint": "data_exchange.index"},
        {"key": "table_maintenance", "label": "Table Maintenance", "icon": "table", "endpoint": "table_maintenance.index"},
    ]},
    {"key": "users", "label": "Manage Users", "icon": "people-fill", "endpoint": "users.list_users",
     "roles": ["TenantAdmin"]},
    {"key": "tenants_admin", "label": "Tenant Management", "icon": "diagram-3", "endpoint": "tenants_admin.list_tenants",
     "roles": ["SystemAdmin"]},
    {"key": "system_mgmt", "label": "System Management", "icon": "gear", "endpoint": "system_mgmt.index"},
    # Platform Admin menu (SystemAdmin only) -- the shared platform data
    # every tenant draws on; see claude/TMS-Platform-Data-Plan.md. Listed
    # after System Management so a tenant's sidebar is unchanged (these are
    # all hidden from tenants) while the SystemAdmin's reads Tenant
    # Management, System Management, then the data screens. Entries whose
    # screen isn't built yet point at a platform_data "coming soon" page;
    # base.html highlights them by endpoint since they share a blueprint.
    {"key": "transport_hubs", "label": "Transport Hubs", "icon": "airplane", "endpoint": "transport_hubs.list_hubs",
     "roles": ["SystemAdmin"]},
    {"key": "platform_pois", "label": "Points of Interest", "icon": "geo-alt", "endpoint": "platform_data.pois",
     "roles": ["SystemAdmin"]},
    {"key": "geography_admin", "label": "Geography & Distances", "icon": "globe-americas", "endpoint": "geography_admin.index",
     "roles": ["SystemAdmin"]},
    {"key": "embassies", "label": "Embassies & Consulates", "icon": "flag", "endpoint": "platform_data.embassies",
     "roles": ["SystemAdmin"]},
    {"key": "accommodation", "label": "Accommodation", "icon": "building", "endpoint": "platform_data.accommodation",
     "roles": ["SystemAdmin"]},
    {"key": "restaurants", "label": "Restaurants", "icon": "cup-hot", "endpoint": "platform_data.restaurants",
     "roles": ["SystemAdmin"]},
    {"key": "currency_admin", "label": "Currencies", "icon": "currency-exchange", "endpoint": "currency_admin.index",
     "roles": ["SystemAdmin"]},
    # Locked lookup codes every tenant shares (platform_lookups.py).
    {"key": "platform_lookups", "label": "Platform Lookups", "icon": "tags", "endpoint": "platform_lookups.index",
     "roles": ["SystemAdmin"]},
]


def create_app():
    app = Flask(__name__, instance_relative_config=False)

    from config import Config
    app.config["SECRET_KEY"] = Config.get_secret_key()
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    # False for local http://127.0.0.1 (a Secure cookie is dropped over plain
    # HTTP and login would silently fail); set SESSION_COOKIE_SECURE=true on
    # the server, where TMS is only reached over HTTPS.
    app.config["SESSION_COOKIE_SECURE"] = Config.SESSION_COOKIE_SECURE
    # Generous cap covering the largest legitimate upload (a profile photo,
    # capped separately at Config.MAX_UPLOAD_BYTES) plus CSV imports.
    # Bulk image imports send many phone photos in one request (each capped
    # at utils.MAX_UPLOAD_FILE_BYTES).
    app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024

    db_module.init_app(app)
    csrf.init_app(app)

    # Upgrade the database with any schema changes shipped since it was last
    # opened -- automatic, additive, no data loss (see db.py MIGRATIONS).
    # A no-op on an uninitialized database or one already up to date.
    with app.app_context():
        db_module.run_pending_migrations()

    from auth.routes import auth_bp
    from blueprints.dashboard import dashboard_bp
    from blueprints.contacts import contacts_bp
    from blueprints.organizations import organizations_bp
    from blueprints.suppliers import suppliers_bp
    from blueprints.hr import hr_bp
    from blueprints.poi import poi_bp
    from blueprints.services import services_bp
    from blueprints.products import products_bp
    from blueprints.packages import packages_bp
    from blueprints.billing_ar import billing_ar_bp
    from blueprints.purchasing_ap import purchasing_ap_bp
    from blueprints.accounts_gl import accounts_gl_bp
    from blueprints.documents import documents_bp
    from blueprints.intelligence import intelligence_bp
    from blueprints.data_exchange import data_exchange_bp
    from blueprints.table_maintenance import table_maintenance_bp
    from blueprints.system_mgmt import system_mgmt_bp
    from blueprints.geography import geography_bp
    from blueprints.geography_admin import geography_admin_bp
    from blueprints.service_taxonomy_admin import service_taxonomy_admin_bp
    from blueprints.currency_admin import currency_admin_bp
    from blueprints.help import help_bp
    from blueprints.ai_agents import ai_agents_bp
    from blueprints.users import users_bp
    from blueprints.tenants_admin import tenants_admin_bp
    from blueprints.platform_lookups import platform_lookups_bp
    from blueprints.platform_data import platform_data_bp
    from blueprints.transport_hubs import transport_hubs_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    # No url_prefix: /help, /help/tree, /help/page/<id> are pages, not a
    # tenant-scoped module -- same as the reference project's help_bp.
    app.register_blueprint(help_bp)
    app.register_blueprint(contacts_bp, url_prefix="/contacts")
    app.register_blueprint(organizations_bp, url_prefix="/organizations")
    app.register_blueprint(suppliers_bp, url_prefix="/suppliers")
    app.register_blueprint(hr_bp, url_prefix="/hr")
    # poi_bp keeps its existing prefix/routes -- only its sidebar entry
    # moved (see the MODULES comment above).
    app.register_blueprint(poi_bp, url_prefix="/points-of-interest")
    app.register_blueprint(services_bp, url_prefix="/inventory/services")
    app.register_blueprint(products_bp, url_prefix="/inventory/products")
    app.register_blueprint(packages_bp, url_prefix="/packages")
    app.register_blueprint(ai_agents_bp, url_prefix="/ai-agents")
    app.register_blueprint(billing_ar_bp, url_prefix="/accounting/billing-ar")
    app.register_blueprint(purchasing_ap_bp, url_prefix="/accounting/purchasing-ap")
    app.register_blueprint(accounts_gl_bp, url_prefix="/accounting/accounts-gl")
    app.register_blueprint(documents_bp, url_prefix="/documents")
    app.register_blueprint(intelligence_bp, url_prefix="/intelligence")
    app.register_blueprint(data_exchange_bp, url_prefix="/data-exchange")
    app.register_blueprint(table_maintenance_bp, url_prefix="/table-maintenance")
    app.register_blueprint(system_mgmt_bp, url_prefix="/system")
    app.register_blueprint(geography_bp, url_prefix="/geography")
    app.register_blueprint(geography_admin_bp, url_prefix="/geography-maintenance")
    app.register_blueprint(service_taxonomy_admin_bp, url_prefix="/service-code-maintenance")
    app.register_blueprint(currency_admin_bp, url_prefix="/currency-maintenance")
    app.register_blueprint(users_bp, url_prefix="/users")
    app.register_blueprint(tenants_admin_bp, url_prefix="/platform/tenants")
    app.register_blueprint(platform_lookups_bp, url_prefix="/platform/lookups")
    app.register_blueprint(platform_data_bp, url_prefix="/platform")
    app.register_blueprint(transport_hubs_bp, url_prefix="/platform/transport-hubs")

    app.jinja_env.globals["modules"] = MODULES

    # True when the browser is on the same machine as the app (Start_TMS.bat
    # on your PC). Used to show the old "Browse…" buttons for reference-link
    # paths only there -- they open a file dialog on the SERVER, which is
    # meaningless on the hosted app.
    from flask import request as _request

    def is_local_request():
        host = (_request.host or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    app.jinja_env.globals["is_local_request"] = is_local_request
    app.jinja_env.filters["format_phone"] = format_phone
    app.jinja_env.filters["format_date"] = format_date
    app.jinja_env.filters["format_date_abbrev"] = format_date_abbrev
    app.jinja_env.filters["format_price"] = format_price
    app.jinja_env.filters["linkify"] = linkify
    app.jinja_env.filters["basename"] = basename
    app.jinja_env.filters["orblank"] = orblank
    app.jinja_env.filters["map_coordinates_link"] = map_coordinates_link

    @app.errorhandler(404)
    def not_found(_e):
        return render_template("error.html", code=404, message="Page not found"), 404

    @app.errorhandler(403)
    def forbidden(_e):
        return render_template("error.html", code=403, message="You don't have access to that page"), 403

    @app.errorhandler(500)
    def server_error(_e):
        return render_template("error.html", code=500, message="Something went wrong"), 500

    return app


app = create_app()

if __name__ == "__main__":
    app.run(debug=True)
