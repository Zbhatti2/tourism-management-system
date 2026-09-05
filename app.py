"""
Tourism Management System — Flask app factory / entry point.

Run locally with:
    flask --app app init-db        # creates instance/tms.db from schema.sql
    flask --app app seed-tenant    # creates the Heritage Tours tenant + Zeb (TenantAdmin)
    flask --app app run --debug    # http://127.0.0.1:5000
"""
from flask import Flask, render_template

import db as db_module
from security import csrf
from utils import basename, format_date, format_phone, linkify, map_coordinates_link, orblank


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
    {"key": "dashboard", "label": "Dashboard", "icon": "speedometer2", "endpoint": "dashboard.index"},
    {"key": "contacts", "label": "Contacts", "icon": "people", "endpoint": "contacts.list_contacts"},
    {"key": "organizations", "label": "Organizations", "icon": "building", "endpoint": "organizations.list_organizations"},
    {"key": "suppliers", "label": "Suppliers", "icon": "truck", "endpoint": "suppliers.list_suppliers"},
    {"key": "hr", "label": "Human Resources", "icon": "person-badge", "endpoint": "hr.index"},
    {"key": "inventory", "label": "Inventory Management", "icon": "boxes", "children": [
        {"key": "services", "label": "Services", "icon": "list-check", "endpoint": "services.list_services"},
        {"key": "products", "label": "Products", "icon": "box-seam", "endpoint": "products.list_products"},
    ]},
    # First module off the "Next-Phase Blueprint" roadmap -- see
    # blueprints/packages.py's docstring. Tenant-scoped tour package
    # templates assembled from Services/Products/Suppliers/POI; no child
    # sub-modules yet (Departures/Bookings are later roadmap phases), so
    # this is a single top-level entry rather than a parent-with-children
    # group like Inventory Management above.
    {"key": "packages", "label": "Package Management", "icon": "map", "endpoint": "packages.list_packages"},
    # "Accounting / Finance" and "Tables & Utilities" below are parent nav
    # items with no page of their own, same pattern as "Organization
    # Intelligence" -- each just groups its sub-modules' blueprint keys in
    # the sidebar. See templates/base.html for how "children" is rendered
    # as a collapsible sub-menu.
    {"key": "accounting_finance", "label": "Accounting / Finance", "icon": "lightbulb", "children": [
        {"key": "billing_ar", "label": "Billing & A/R", "icon": "receipt", "endpoint": "billing_ar.index"},
        {"key": "purchasing_ap", "label": "Purchasing & A/P", "icon": "cart-check", "endpoint": "purchasing_ap.index"},
        {"key": "accounts_gl", "label": "Accounts & G/L", "icon": "calculator", "endpoint": "accounts_gl.index"},
    ]},
    {"key": "organization_intelligence", "label": "Organization Intelligence", "icon": "lightbulb", "children": [
        {"key": "documents", "label": "Documents", "icon": "folder2-open", "endpoint": "documents.index"},
        {"key": "intelligence", "label": "Intelligence", "icon": "journal-text", "endpoint": "intelligence.index"},
    ]},
    # Data Exchange and Table Maintenance are unchanged, existing
    # blueprints -- just moved off the top level and grouped under one
    # "Tables & Utilities" parent (matching the mockup's exact label).
    {"key": "tables_utilities", "label": "Tables & Utilities", "icon": "lightbulb", "children": [
        {"key": "data_exchange", "label": "Data Exchange", "icon": "arrow-left-right", "endpoint": "data_exchange.index"},
        {"key": "table_maintenance", "label": "Table Maintenance", "icon": "table", "endpoint": "table_maintenance.index"},
    ]},
    {"key": "system_mgmt", "label": "System Management", "icon": "gear", "endpoint": "system_mgmt.index"},
]


def create_app():
    app = Flask(__name__, instance_relative_config=False)

    from config import Config
    app.config["SECRET_KEY"] = Config.get_secret_key()
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    # Generous cap covering the largest legitimate upload (a profile photo,
    # capped separately at Config.MAX_UPLOAD_BYTES) plus CSV imports.
    app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024

    db_module.init_app(app)
    csrf.init_app(app)

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

    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
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

    app.jinja_env.globals["modules"] = MODULES
    app.jinja_env.filters["format_phone"] = format_phone
    app.jinja_env.filters["format_date"] = format_date
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
