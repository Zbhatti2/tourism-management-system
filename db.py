"""
SQLite connection management (Flask application-context pattern) and audit
logging helper. Deliberately plain sqlite3 — no ORM — schema.sql is the
single source of truth for structure.
"""
import sqlite3

import click
from flask import current_app, g, session

from config import Config


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(Config.DATABASE_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = get_db()
    with open(Config.SCHEMA_PATH, "r") as f:
        db.executescript(f.read())
    db.commit()


def log_action(action: str, entity_type: str = None, entity_id: int = None, detail: str = None,
                tenant_id: int = None, user_id: int = None):
    """Write one row to audit_log. Call this after any create/update/delete/
    import/export/login/purge — commits immediately so the audit trail
    survives even if the surrounding request later fails.

    tenant_id/user_id default to the current session's values when omitted,
    so most call sites inside a logged-in request don't need to pass them
    explicitly. Pass them explicitly for pre-login events (e.g. a failed
    login attempt, where the tenant may or may not be known yet).
    """
    if tenant_id is None:
        tenant_id = session.get("tenant_id")
    if user_id is None:
        user_id = session.get("user_id")
    db = get_db()
    db.execute(
        "INSERT INTO audit_log (tenant_id, user_id, action, entity_type, entity_id, detail) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (tenant_id, user_id, action, entity_type, entity_id, detail),
    )
    db.commit()


def is_configured() -> bool:
    """Has at least one tenant been provisioned? Phase 1.1 seeds the first
    tenant (Heritage Tours) directly via seed_data.py, so in normal use this
    is already true by the time anyone hits the app; the /setup wizard
    (auth/routes.py) exists for provisioning additional tenants later and as
    a fallback if the database was created without seeding."""
    db = get_db()
    row = db.execute("SELECT 1 FROM tenants LIMIT 1").fetchone()
    return row is not None


def init_app(app):
    app.teardown_appcontext(close_db)

    @app.cli.command("init-db")
    def init_db_command():
        """Flask CLI: `flask --app app init-db` — (re)creates all tables from schema.sql."""
        init_db()
        click.echo("Initialized the database from schema.sql.")

    @app.cli.command("seed-lookups")
    @click.argument("tenant_code")
    def seed_lookups_command(tenant_code):
        """Flask CLI: `flask --app app seed-lookups HERITAGE` — populates
        Module E lookup tables for one tenant."""
        from seed_data import seed_lookup_tables

        db = get_db()
        row = db.execute("SELECT tenant_id FROM tenants WHERE tenant_code = ?", (tenant_code,)).fetchone()
        if row is None:
            click.echo(f"No tenant with code {tenant_code!r}. Run `flask --app app seed-tenant` first.")
            return
        seed_lookup_tables(db, row["tenant_id"])
        click.echo(f"Seeded lookup tables for tenant {tenant_code}.")

    @app.cli.command("migrate-add-organization-notes")
    def migrate_add_organization_notes_command():
        """Flask CLI: `flask --app app migrate-add-organization-notes` — adds
        organizations.notes to a database created before that column
        existed. Safe to re-run; a no-op if already present. See
        migrate_add_organization_notes.py for details."""
        from migrate_add_organization_notes import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-add-geography-hierarchy")
    def migrate_add_geography_hierarchy_command():
        """Flask CLI: `flask --app app migrate-add-geography-hierarchy` —
        adds the Region -> Country -> Province/State -> City hierarchy
        (regions/cities tables, countries.region_id, addresses.region_id/
        city_id, renames addresses.city -> addresses.city_text) to a
        database created before that change, and seeds the 6 regions +
        Pakistan provinces/cities. Safe to re-run; does not touch existing
        tenant data. See migrate_add_geography_hierarchy.py for details."""
        from migrate_add_geography_hierarchy import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-poi-remove-platforms-accounts")
    def migrate_poi_remove_platforms_accounts_command():
        """Flask CLI: `flask --app app migrate-poi-remove-platforms-accounts`
        — archive-renames (never drops) the removed "Platforms &
        Subscriptions" and "Accounts" modules' tables, creates the new
        poi_types/points_of_interest tables, adds the Pakistan cities the
        Sikh Gurdwara seed data needs, and seeds POI Types (+ the 149
        Gurdwara POIs for Heritage Tours specifically) for every existing
        tenant. Safe to re-run. See migrate_poi_remove_platforms_accounts.py
        for details."""
        from migrate_poi_remove_platforms_accounts import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-drop-platforms-subscriptions")
    def migrate_drop_platforms_subscriptions_command():
        """Flask CLI: `flask --app app migrate-drop-platforms-subscriptions`
        — per Zeb's request to remove all traces of the "Platforms &
        Subscriptions" module (confirmed not needed for a Tourism/Travel
        Management System), permanently DROPs the 16 _archived_* tables
        that migrate-poi-remove-platforms-accounts left behind for that
        module specifically (every one confirmed empty or holding only
        unused lookup seed values -- no real data lost). Does NOT touch
        the separate Accounts module's archived tables. Safe to re-run.
        See migrate_drop_platforms_subscriptions_module.py for details."""
        from migrate_drop_platforms_subscriptions_module import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-add-suppliers")
    def migrate_add_suppliers_command():
        """Flask CLI: `flask --app app migrate-add-suppliers` — adds the new
        "Suppliers" module (suppliers, supplier_addresses, supplier_contacts,
        supplier_contact_phones, plus the address_types/phone_types/
        supplier_types/supplier_subtypes lookups — address_types was later
        renamed to supplier_address_types, see migrate-organization-addresses
        below) to a database created before that change, seeds those lookups
        for every existing tenant, and narrows addresses.owner_type's CHECK
        constraint (dropping the already-dead 'PersonalAccount' branch).
        Safe to re-run; does not touch existing tenant data. See
        migrate_add_suppliers.py for details."""
        from migrate_add_suppliers import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-add-supplier-preference")
    def migrate_add_supplier_preference_command():
        """Flask CLI: `flask --app app migrate-add-supplier-preference` —
        adds suppliers.preference ('Primary'/'Secondary', NULL by default)
        so a Top and a Secondary choice can be marked among many suppliers
        of the same Type in the same City (e.g. 100+ Hotels in Lahore).
        Safe to re-run; does not touch existing tenant data. See
        migrate_add_supplier_preference.py for details."""
        from migrate_add_supplier_preference import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-organization-addresses")
    def migrate_organization_addresses_command():
        """Flask CLI: `flask --app app migrate-organization-addresses` —
        renames the address_types table to supplier_address_types (if still
        present under its old name), adds the new organization_address_types
        lookup and organization_addresses table, seeds organization_address_
        types for every existing tenant, and backfills one organization_
        addresses row per existing organization that has a non-blank
        full_address, parsed into Street/City/Province/Country via
        address_parsing.py (recovering any phone numbers found embedded in
        that text into organizations.phone when it's currently blank).
        organizations.full_address itself is left in place, untouched, as a
        legacy fallback. Safe to re-run — organizations that already have an
        organization_addresses row are skipped. See
        migrate_organization_addresses.py for details."""
        from migrate_organization_addresses import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-organization-contact-info")
    def migrate_organization_contact_info_command():
        """Flask CLI: `flask --app app migrate-organization-contact-info` —
        adds the "Multiple Email and Phone" and "Documents Link" features to
        the Organizations module: creates the organization_phone_types
        lookup and the organization_emails/organization_phones/
        organization_reference_links tables (its own, separate phone-type
        lookup from Suppliers'), seeds organization_phone_types for every
        existing tenant, and backfills organization_emails/organization_
        phones rows from each organization's existing single-value email/
        phone columns (splitting on ";" where more than one value was
        jammed into one column). organizations.email/.phone themselves are
        left in place, untouched, as a legacy fallback.
        organization_reference_links has nothing to backfill — it starts
        empty, same as a fresh install. Safe to re-run — organizations that
        already have rows in a given new table are skipped for that table.
        See migrate_organization_contact_info.py for details."""
        from migrate_organization_contact_info import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-organization-intelligence")
    def migrate_organization_intelligence_command():
        """Flask CLI: `flask --app app migrate-organization-intelligence` —
        adds the new "Organization Intelligence" module: creates the
        organization_intelligence table (a freeform, dated, append-only
        journal — see schema.sql's MODULE G comment), and seeds the 7
        sample entries for the Heritage Tours tenant specifically (not for
        every tenant — this is Heritage Tours' own demo data, same as the
        Gurdwara POI seed). Safe to re-run — no-ops on the table if it
        already exists, and no-ops on the seed if Heritage Tours already
        has any Organization Intelligence entries. See
        migrate_organization_intelligence.py for details."""
        from migrate_organization_intelligence import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-human-resources")
    def migrate_human_resources_command():
        """Flask CLI: `flask --app app migrate-human-resources` — adds the
        Human Resources module (Employees, External Resources, shared HR
        Roles, and the Host Organization) to a database created before that
        change: widens addresses.owner_type to also allow 'Employee'/
        'ExternalResource', creates the 22 new tables, and seeds the
        generic starter lookups + one Host Organization record per existing
        tenant. Safe to re-run; does not touch any existing data. See
        migrate_human_resources.py for details."""
        from migrate_human_resources import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-fix-history-fk")
    def migrate_fix_history_fk_command():
        """Flask CLI: `flask --app app migrate-fix-history-fk` — drops the
        FOREIGN KEY constraint on contact_phones_history.phone_id,
        contact_emails_history.email_id, and addresses_history.address_id.
        Those FKs made it impossible to ever finish deleting a contact
        phone/email/address: the "Deleted" history row inserted just before
        the live row is removed pointed at that same row, so the DELETE
        that followed always failed with "FOREIGN KEY constraint failed".
        Safe to re-run; does not touch any existing data. See
        migrate_fix_history_fk.py for the full story."""
        from migrate_fix_history_fk import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-add-employee-role-manager")
    def migrate_add_employee_role_manager_command():
        """Flask CLI: `flask --app app migrate-add-employee-role-manager` —
        adds employees.primary_role_id, employees.secondary_role_id, and
        employees.manager_id (the Employee form's Primary Role, Secondary
        Role, and Reports To fields) to a database created before they
        existed. Safe to re-run; does not touch any existing data or assign
        a role to any existing employee. See
        migrate_add_employee_role_manager.py for the full story."""
        from migrate_add_employee_role_manager import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-add-employee-gender")
    def migrate_add_employee_gender_command():
        """Flask CLI: `flask --app app migrate-add-employee-gender` — adds
        the genders lookup table and employees.gender_id (the Employee
        form's Gender field), and seeds a starter Genders list (Male /
        Female / Other) for every existing tenant. Safe to re-run; does not
        touch any existing data. Also see migrate_add_employee_gender.py's
        docstring for the removal of the redundant freeform "Job role"
        field from the Employee form — the employees.job_role column
        itself is left untouched. See migrate_add_employee_gender.py for
        the full story."""
        from migrate_add_employee_gender import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-add-resource-organization")
    def migrate_add_resource_organization_command():
        """Flask CLI: `flask --app app migrate-add-resource-organization` —
        RETIRED: adds external_resources.organization_id, a column on a
        table the app no longer reads or writes (see
        migrate-add-supplier-external-resource below, and blueprints/hr.py's
        retirement note). Kept only so a database that already ran this
        once stays consistent; there's no reason to run it against a new
        database. Safe to re-run; does not touch any existing data. See
        migrate_add_resource_organization.py for the full story."""
        from migrate_add_resource_organization import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-add-supplier-external-resource")
    def migrate_add_supplier_external_resource_command():
        """Flask CLI: `flask --app app migrate-add-supplier-external-resource`
        — adds suppliers.is_external_resource, suppliers.tax_id,
        suppliers.national_id_number, and suppliers.hourly_rate (the
        Supplier form's "External Resource" flag and the fields that go
        with it) to a database created before External Resources were
        retired from Human Resources in favor of Suppliers. Safe to
        re-run; does not touch any existing data, and does not migrate
        anything out of the old external_resources table (which was
        confirmed empty). See migrate_add_supplier_external_resource.py
        for the full story."""
        from migrate_add_supplier_external_resource import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-add-supplier-resource-contact-info")
    def migrate_add_supplier_resource_contact_info_command():
        """Flask CLI: `flask --app app migrate-add-supplier-resource-contact-info`
        — adds suppliers.company_name and three new tables (supplier_emails,
        supplier_phones, supplier_reference_links) backing the redesigned,
        dedicated External Resource form/view (blueprints/suppliers.py's
        new_external_resource()) — a Company field, multiple phones/emails/
        reference links directly on the resource's own record, and (via app
        code, not a schema change) a single billing address instead of the
        generic Supplier form's multi-address/contact-person model. Safe to
        re-run; does not touch any existing data. See
        migrate_add_supplier_resource_contact_info.py for the full story."""
        from migrate_add_supplier_resource_contact_info import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("import-external-resources")
    @click.argument("tenant_code")
    @click.argument("csv_path")
    def import_external_resources_command(tenant_code, csv_path):
        """Flask CLI: `flask --app app import-external-resources TENANT_CODE
        path/to/file.csv` — bulk-creates External Resources
        (suppliers.is_external_resource=1) from a "Non-Employee Resources
        Contact" CSV export (Title/First Name/Last Name/Company/Job Title/
        Street/City/State/Postal Code/Country/Fax/Phone 1/Mobile Phone/
        E-mail 1/E-mail 2/Web Page columns) — one resource per row, with a
        billing address, up to three phones (Office/Cell/Fax), up to two
        emails, and a Web Page reference link, matching the redesigned
        External Resource model. Safe to re-run against the same file or a
        later batch that repeats a row — see import_external_resources.py
        for the full story and the skip-if-already-imported rule."""
        from import_external_resources import import_csv

        import_csv(csv_path, tenant_code)

    @app.cli.command("migrate-normalize-poi-coordinates")
    def migrate_normalize_poi_coordinates_command():
        """Flask CLI: `flask --app app migrate-normalize-poi-coordinates` —
        rewrites every existing Points of Interest "Map coordinates" value
        (points_of_interest.map_coordinates) into the canonical DMS display
        format ('31°35′17″N 74°18′34″E'), matching what new/edited POIs are
        normalized to automatically at save time (see utils.normalize_map_
        coordinates). Only touches rows that parse as real coordinates; a
        row that doesn't is left completely untouched. Safe to re-run —
        idempotent. See migrate_normalize_poi_coordinates.py for the full
        story."""
        from migrate_normalize_poi_coordinates import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-add-poi-knowledge-graph")
    def migrate_add_poi_knowledge_graph_command():
        """Flask CLI: `flask --app app migrate-add-poi-knowledge-graph` —
        adds points_of_interest.knowledge_graph_data (a freeform, ';'-
        delimited field, same convention as contacts.knowledge_graph_data)
        to a database created before this column existed. Safe to re-run;
        does not touch any existing data. See
        migrate_add_poi_knowledge_graph.py for the full story."""
        from migrate_add_poi_knowledge_graph import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-add-services")
    def migrate_add_services_command():
        """Flask CLI: `flask --app app migrate-add-services` — adds the new
        "Services" sub-module (Inventory Management -> Services): the 3
        GLOBAL Service Coding System taxonomy tables (service_categories/
        service_groups/service_subgroups, seeded with the full Category ->
        Group -> Sub-Group vocabulary and validity flags from
        Service_Coding_System_Schema.md), the tenant-scoped `services`
        catalog table, its two deprecated-Sub-Group enforcement triggers,
        and a handful of representative demo Services per existing tenant.
        Safe to re-run; does not touch any existing data. See
        migrate_add_services.py for the full story."""
        from migrate_add_services import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-add-products")
    def migrate_add_products_command():
        """Flask CLI: `flask --app app migrate-add-products` — adds the new
        "Products" sub-module (Inventory Management -> Products): the 3
        GLOBAL Product Coding System taxonomy tables (product_categories/
        product_groups/product_subgroups, seeded with the full Category ->
        Group -> Sub-Group vocabulary from TTMS_Products_Seed_Data.csv), the
        tenant-scoped `products` catalog table (166 seeded rows per
        tenant), the `product_attributes` key-value table, its two
        deprecated-Sub-Group enforcement triggers, and the full Product
        catalog per existing tenant. A separate, independent registry from
        Services -- never joined against or constrained with
        service_categories/service_groups/service_subgroups/services. Safe
        to re-run; does not touch any existing data. See
        migrate_add_products.py for the full story, including the
        deviations from the original prompt's generic template."""
        from migrate_add_products import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-add-packages")
    def migrate_add_packages_command():
        """Flask CLI: `flask --app app migrate-add-packages` — adds the new
        "Package Management" module (Package Management & Itinerary
        Builder): packages, package_route_stops, package_days,
        package_day_pois, package_components, and package_price_tiers.
        Unlike Services/Products, all six tables are tenant-scoped — no
        GLOBAL taxonomy to seed, since a package is one operator's own
        product, not shared coding vocabulary. package_components
        references services/products/suppliers by FK but writes no rows
        to those tables. Safe to re-run; does not touch any existing data.
        See migrate_add_packages.py for the full story."""
        from migrate_add_packages import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-add-package-service-link")
    def migrate_add_package_service_link_command():
        """Flask CLI: `flask --app app migrate-add-package-service-link` —
        adds packages.service_id, linking every Package back to the 'TP'
        (Tour Package) Category Services Inventory row it was created
        from, so the New Package form can auto-fill Package Number,
        Package Description, and Package Name from an existing Service
        instead of free-typing them. Safe to re-run; does not touch any
        existing data. See migrate_add_package_service_link.py for the
        full story."""
        from migrate_add_package_service_link import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-add-route-stop-layover")
    def migrate_add_route_stop_layover_command():
        """Flask CLI: `flask --app app migrate-add-route-stop-layover` —
        adds package_route_stops.is_layover and .layover_hours, so a
        transit/connection stop on the Route Planning form can be flagged
        and its irrelevant "Nights" field masked in favor of an
        "Approximate Hrs" field. Safe to re-run; does not touch any
        existing data. See migrate_add_route_stop_layover.py for the
        full story."""
        from migrate_add_route_stop_layover import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-add-route-stop-geography")
    def migrate_add_route_stop_geography_command():
        """Flask CLI: `flask --app app migrate-add-route-stop-geography` —
        adds package_route_stops.region_id, .state_id, and
        .state_province_text, so the Route Stop form's Country/City picker
        becomes the same Region -> Country -> Province/State -> City
        cascade used everywhere else in the app (see
        templates/_geography_fields.html), instead of skipping the
        Province/State level entirely. Safe to re-run; does not touch any
        existing data. See migrate_add_route_stop_geography.py for the
        full story."""
        from migrate_add_route_stop_geography import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-add-route-stop-checkpoint")
    def migrate_add_route_stop_checkpoint_command():
        """Flask CLI: `flask --app app migrate-add-route-stop-checkpoint` —
        adds package_route_stops.is_checkpoint, flagging a Route Stop as a
        place the group needs accommodations arranged for -- a planning
        datapoint independent of is_layover -- shown as its own Checkpoint
        column/badge on the Route table. Safe to re-run; does not touch
        any existing data. See migrate_add_route_stop_checkpoint.py for
        the full story."""
        from migrate_add_route_stop_checkpoint import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-add-component-day-costing")
    def migrate_add_component_day_costing_command():
        """Flask CLI: `flask --app app migrate-add-component-day-costing` —
        adds package_components.day_id and .is_accommodation, so a Costing
        line can be itemized against one specific Day (Hotel/Room vs Other
        Service, the two Costing sub-parts shown on each Day's card
        alongside its POI's). Safe to re-run; does not touch any existing
        data. See migrate_add_component_day_costing.py for the full
        story."""
        from migrate_add_component_day_costing import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-add-component-quantity-repeat")
    def migrate_add_component_quantity_repeat_command():
        """Flask CLI: `flask --app app migrate-add-component-quantity-repeat`
        — adds package_components.quantity and .repeat_for_checkpoint, for
        the Day card's Hotel/Room and Other Service Costing tables (QTY x
        Unit Cost/Unit Price -> Total Cost/Total Price/Profit Margin, plus
        a flag that repeats a line across every Day in its Checkpoint).
        Safe to re-run; does not touch any existing data. See
        migrate_add_component_quantity_repeat.py for the full story."""
        from migrate_add_component_quantity_repeat import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-add-component-airline-ticket")
    def migrate_add_component_airline_ticket_command():
        """Flask CLI: `flask --app app migrate-add-component-airline-ticket`
        — adds package_components.is_airline_ticket, so a whole-trip
        component can be flagged an Airline Ticket and shown in its own
        section on the package page (between Route and Entire Tour
        Services) instead of folded into the generic Entire Tour Services
        list. Safe to re-run; does not touch any existing data. See
        migrate_add_component_airline_ticket.py for the full story."""
        from migrate_add_component_airline_ticket import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-add-ai-agent-lookups")
    def migrate_add_ai_agent_lookups_command():
        """Flask CLI: `flask --app app migrate-add-ai-agent-lookups` — adds
        the new lookup values for the "Sikh Pilgrimage Sector" AI Agent /
        Data Enrichment pilot: 4 new POI Types (Airport, Railway Station,
        Hospital, Police Station) and 1 new Supplier Type (Tour Guide,
        with sub-types), for every existing tenant. Safe to re-run; does
        not touch any existing data. See migrate_add_ai_agent_lookups.py
        for the full story."""
        from migrate_add_ai_agent_lookups import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-add-knowledge-graph-edges")
    def migrate_add_knowledge_graph_edges_command():
        """Flask CLI: `flask --app app migrate-add-knowledge-graph-edges` —
        creates the new knowledge_graph_edges table, the structured
        replacement for the freeform knowledge_graph_data text field (see
        knowledge_graph.py). Safe to re-run — CREATE TABLE IF NOT EXISTS.
        See migrate_add_knowledge_graph_edges.py for the full story."""
        from migrate_add_knowledge_graph_edges import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-seed-nankana-sahib-pilot")
    def migrate_seed_nankana_sahib_pilot_command():
        """Flask CLI: `flask --app app migrate-seed-nankana-sahib-pilot` —
        populates the Nankana Sahib pilot-city data (real map coordinates
        on the 9 existing Gurdwara POIs, new non-Gurdwara POIs, new
        Suppliers, and Knowledge Graph edges linking them) for every
        existing tenant. Requires migrate-add-ai-agent-lookups and
        migrate-add-knowledge-graph-edges to have already run. Safe to
        re-run — idempotent. See migrate_seed_nankana_sahib_pilot.py for
        the full story."""
        from migrate_seed_nankana_sahib_pilot import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-add-hotel-template")
    def migrate_add_hotel_template_command():
        """Flask CLI: `flask --app app migrate-add-hotel-template` — adds
        the "Hotel" Supplier Type Template: supplier_types.template_key,
        the hotel_amenity_options / supplier_amenities / hotel_room_types /
        supplier_rooms tables, and seeds the Amenities & Facilities +
        Room Types master lists for every existing tenant, flagging their
        'Hotel' Supplier Type. Safe to re-run; does not touch any existing
        data. See migrate_add_hotel_template.py for the full story."""
        from migrate_add_hotel_template import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("migrate-add-hotel-lookup-parent")
    def migrate_add_hotel_lookup_parent_command():
        """Flask CLI: `flask --app app migrate-add-hotel-lookup-parent` —
        nests hotel_amenity_options and hotel_room_types under
        supplier_types (parent = 'Hotel'), so both are manageable from
        Table Maintenance, and renames hotel_room_types.default_description
        to description to match the standard lookup-table shape. Safe to
        re-run; does not touch any Amenities/Room Types data already on
        file. See migrate_add_hotel_lookup_parent.py for the full story."""
        from migrate_add_hotel_lookup_parent import migrate

        migrate()
        click.echo("Done.")

    @app.cli.command("seed-tenant")
    def seed_tenant_command():
        """Flask CLI: `flask --app app seed-tenant` — creates the first
        tenant (Heritage Tours) with its Tenant Admin (Zeb), plus that
        tenant's lookup tables. Safe to re-run; does nothing if Heritage
        Tours already exists."""
        from seed_data import seed_first_tenant

        db = get_db()
        seed_first_tenant(db)
        click.echo("Seeded the Heritage Tours tenant and its admin user.")
