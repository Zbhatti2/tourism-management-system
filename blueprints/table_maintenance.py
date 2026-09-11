"""
Module — Table Maintenance. Lets the user add/edit/deactivate/delete rows
in the shared-shape lookup tables (id, code, label, description,
sort_order, is_active — see schema.sql "MODULE E" comment) directly from
the UI, instead of only being settable via seed_data.py.

Two things worth calling out about how master-table data stays consistent
when a lookup table changes underneath it:

1. Renaming/re-spelling an entry (edit_entry) needs no propagation at all.
   Every master table stores a foreign key to the lookup row's id, not a
   copy of its text, so a relabel is reflected everywhere that row is used
   the instant it's saved.

2. Deleting an entry that's still referenced is where care is needed. The
   database itself already refuses that (PRAGMA foreign_keys = ON), so
   delete_entry() checks usage up front and, if the entry is in use, sends
   the user to reassign() instead of failing with a raw DB error. reassign()
   bulk-moves every master row currently pointing at the old entry over to
   a different entry the user picks, then optionally deletes the old one —
   this is also how two near-duplicate entries (e.g. "Accountant"/"CPA")
   get merged, independent of any delete attempt.
"""
import sqlite3

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from auth.decorators import login_required
from db import get_db, log_action

table_maintenance_bp = Blueprint("table_maintenance", __name__)


# Registry of the lookup tables this screen manages. Every table here shares
# the standard shape (id, code, label, description, sort_order, is_active),
# so one set of routes/templates serves all of them — add another entry
# here to bring a further lookup table under management later.
#
# "references" lists every master-table column that stores a foreign key
# into this lookup table; used both to count usage before a delete and to
# bulk-reassign rows during a merge.
#
# "parent" is optional, for a lookup table that's itself nested one level
# under another lookup table (e.g. knowledge_subdomains under
# knowledge_domains): {table, pk, fk, label_field, nav_label}, where "fk" is
# the column on THIS table that points at the parent's "pk". When set, the
# add/edit form gains a required parent picker and the list shows which
# parent each row belongs to.
TABLES = {
    "contact_categories": {
        "label": "Contact Categories",
        "table": "contact_categories",
        "pk": "contact_category_id",
        "references": [
            {"table": "contacts", "fk": "contact_category_id", "label": "contact(s)"},
        ],
    },
    "contact_titles": {
        "label": "Titles",
        "table": "contact_titles",
        "pk": "contact_title_id",
        "references": [
            {"table": "contacts", "fk": "title_id", "label": "contact(s)"},
        ],
    },
    "contact_suffixes": {
        "label": "Suffixes",
        "table": "contact_suffixes",
        "pk": "contact_suffix_id",
        "references": [
            {"table": "contacts", "fk": "suffix_id", "label": "contact(s)"},
        ],
    },
    "poi_types": {
        "label": "POI Types",
        "table": "poi_types",
        "pk": "poi_type_id",
        "references": [
            {"table": "points_of_interest", "fk": "poi_type_id", "label": "point(s) of interest"},
        ],
    },
    "organization_types": {
        "label": "Organization Types",
        "table": "organization_types",
        "pk": "organization_type_id",
        "references": [
            {"table": "organizations", "fk": "organization_type_id", "label": "organization(s)"},
        ],
    },
    "knowledge_domains": {
        "label": "Knowledge Domains",
        "table": "knowledge_domains",
        "pk": "knowledge_domain_id",
        "references": [
            {"table": "content_knowledge_domains", "fk": "knowledge_domain_id", "label": "content link(s)"},
        ],
    },
    "knowledge_subdomains": {
        "label": "Knowledge Sub-Domains",
        "table": "knowledge_subdomains",
        "pk": "knowledge_subdomain_id",
        "references": [],
        "parent": {
            "table": "knowledge_domains",
            "pk": "knowledge_domain_id",
            "fk": "knowledge_domain_id",
            "label_field": "label",
            "nav_label": "Domain",
        },
    },
    "content_types": {
        "label": "Content Types",
        "table": "content_types",
        "pk": "content_type_id",
        "references": [
            {"table": "content", "fk": "content_type_id", "label": "content item(s)"},
        ],
    },
    "content_subtypes": {
        "label": "Content Sub-Types",
        "table": "content_subtypes",
        "pk": "content_subtype_id",
        "references": [],
        "parent": {
            "table": "content_types",
            "pk": "content_type_id",
            "fk": "content_type_id",
            "label_field": "label",
            "nav_label": "Content Type",
        },
    },
    "professions": {
        "label": "Professions",
        "table": "professions",
        "pk": "profession_id",
        "references": [
            {"table": "contacts", "fk": "profession_id", "label": "contact(s)"},
        ],
        # Long, open-ended list (unlike e.g. Supplier Types) with no natural
        # grouping order of its own -- alphabetical by Label is the useful
        # default here rather than the drag-order "sort_order" every other
        # lookup table lists by. sort_order is still stored/editable (via
        # "New Entry"/edit) for the rare case an admin wants to hand-pin a
        # few entries to the top later; see the "manage" route's use of
        # cfg.get("default_sort").
        "default_sort": "label",
    },
    "contact_contexts": {
        "label": "Contexts",
        "table": "contact_contexts",
        "pk": "contact_context_id",
        "references": [
            {"table": "contacts", "fk": "context_id", "label": "contact(s)"},
        ],
    },
    "content_link_types": {
        "label": "Content Link Types",
        "table": "content_link_types",
        "pk": "content_link_type_id",
        "references": [
            {"table": "content_contact_links", "fk": "link_type_id", "label": "contacts link(s)"},
        ],
    },
    "supplier_types": {
        "label": "Supplier Types",
        "table": "supplier_types",
        "pk": "supplier_type_id",
        "references": [
            {"table": "suppliers", "fk": "supplier_type_id", "label": "supplier(s)"},
        ],
    },
    "supplier_subtypes": {
        "label": "Supplier Sub-Types",
        "table": "supplier_subtypes",
        "pk": "supplier_subtype_id",
        "references": [
            {"table": "suppliers", "fk": "supplier_subtype_id", "label": "supplier(s)"},
        ],
        "parent": {
            "table": "supplier_types",
            "pk": "supplier_type_id",
            "fk": "supplier_type_id",
            "label_field": "label",
            "nav_label": "Supplier Type",
        },
    },
    "supplier_address_types": {
        "label": "Supplier Address Types",
        "table": "supplier_address_types",
        "pk": "address_type_id",
        "references": [
            {"table": "supplier_addresses", "fk": "address_type_id", "label": "supplier address(es)"},
        ],
    },
    "organization_address_types": {
        "label": "Organization Address Types",
        "table": "organization_address_types",
        "pk": "address_type_id",
        "references": [
            {"table": "organization_addresses", "fk": "address_type_id", "label": "organization address(es)"},
        ],
    },
    "organization_phone_types": {
        "label": "Organization Phone Types",
        "table": "organization_phone_types",
        "pk": "phone_type_id",
        "references": [
            {"table": "organization_phones", "fk": "phone_type_id", "label": "organization phone(s)"},
        ],
    },
    "phone_types": {
        "label": "Phone Types",
        "table": "phone_types",
        "pk": "phone_type_id",
        "references": [
            {"table": "supplier_contact_phones", "fk": "phone_type_id", "label": "supplier contact phone(s)"},
        ],
    },
    "genders": {
        "label": "Genders",
        "table": "genders",
        "pk": "gender_id",
        "references": [
            {"table": "employees", "fk": "gender_id", "label": "employee(s)"},
        ],
    },
    "employee_types": {
        "label": "Employee Types",
        "table": "employee_types",
        "pk": "employee_type_id",
        "references": [
            {"table": "employees", "fk": "employee_type_id", "label": "employee(s)"},
        ],
    },
    "departments": {
        "label": "Departments",
        "table": "departments",
        "pk": "department_id",
        "references": [
            {"table": "employees", "fk": "department_id", "label": "employee(s)"},
        ],
    },
    "job_titles": {
        "label": "Job Titles",
        "table": "job_titles",
        "pk": "job_title_id",
        "references": [
            {"table": "employees", "fk": "job_title_id", "label": "employee(s)"},
        ],
    },
    "employee_phone_types": {
        "label": "Employee Phone Types",
        "table": "employee_phone_types",
        "pk": "phone_type_id",
        "references": [
            {"table": "employee_phones", "fk": "phone_type_id", "label": "employee phone(s)"},
        ],
    },
    "resource_types": {
        "label": "Resource Types",
        "table": "resource_types",
        "pk": "resource_type_id",
        "references": [
            {"table": "external_resources", "fk": "resource_type_id", "label": "external resource(s)"},
        ],
    },
    "hr_roles": {
        "label": "HR Roles",
        "table": "hr_roles",
        "pk": "role_id",
        "references": [
            {"table": "hr_role_assignments", "fk": "role_id", "label": "role assignment(s)"},
            {"table": "employees", "fk": "primary_role_id", "label": "employee(s) (as Primary Role)"},
            {"table": "employees", "fk": "secondary_role_id", "label": "employee(s) (as Secondary Role)"},
        ],
    },
    "host_address_types": {
        "label": "Host Address Types",
        "table": "host_address_types",
        "pk": "address_type_id",
        "references": [
            {"table": "host_addresses", "fk": "address_type_id", "label": "host address(es)"},
        ],
    },
    "host_phone_types": {
        "label": "Host Phone Types",
        "table": "host_phone_types",
        "pk": "phone_type_id",
        "references": [
            {"table": "host_phones", "fk": "phone_type_id", "label": "host phone(s)"},
        ],
    },
    "hotel_amenity_options": {
        "label": "Amenities & Facilities Options",
        "table": "hotel_amenity_options",
        "pk": "amenity_option_id",
        "references": [
            {"table": "supplier_amenities", "fk": "amenity_option_id", "label": "supplier amenity checklist entry(ies)"},
        ],
        "parent": {
            "table": "supplier_types",
            "pk": "supplier_type_id",
            "fk": "supplier_type_id",
            "label_field": "label",
            "nav_label": "Supplier Type",
        },
        "extra_fields": [
            {
                "name": "category",
                "label": "Category",
                "type": "select",
                "required": True,
                "options": ["In-Room", "Food & Drink", "Wellness", "Business", "Convenience"],
            },
            {"name": "icon", "label": "Icon", "type": "icon", "required": False},
        ],
    },
    "hotel_room_types": {
        "label": "Room Types",
        "table": "hotel_room_types",
        "pk": "room_type_id",
        "references": [
            {"table": "supplier_rooms", "fk": "room_type_id", "label": "supplier Room row(s)"},
        ],
        "parent": {
            "table": "supplier_types",
            "pk": "supplier_type_id",
            "fk": "supplier_type_id",
            "label_field": "label",
            "nav_label": "Supplier Type",
        },
    },
}


# Grouping + display order for the Tables screen (index() below). Every
# key here must exist in TABLES above; this list is what turns the flat
# TABLES registry into the grouped "Contacts / Content / Organization /
# Supplier / Knowledge" sections the Tables screen renders, so a table
# added to TABLES also needs a slot added here (index() asserts this in
# debug rather than silently dropping a table from the screen).
TABLE_GROUPS = [
    ("Contacts Group", [
        "contact_categories",
        "contact_titles",
        "professions",
        "contact_suffixes",
        "contact_contexts",
        "phone_types",
    ]),
    ("Content Group", [
        "content_types",
        "content_link_types",
        "content_subtypes",
    ]),
    ("Organization Group", [
        "organization_types",
        "organization_address_types",
        "organization_phone_types",
    ]),
    ("Supplier Group", [
        "supplier_address_types",
        "supplier_subtypes",
        "supplier_types",
        "hotel_amenity_options",
        "hotel_room_types",
    ]),
    ("Knowledge Group", [
        "knowledge_domains",
        "knowledge_subdomains",
        "poi_types",
    ]),
    ("Human Resources Group", [
        "genders",
        "employee_types",
        "departments",
        "job_titles",
        "employee_phone_types",
        "resource_types",
        "hr_roles",
        "host_address_types",
        "host_phone_types",
    ]),
]


def _table_config(table_key):
    cfg = TABLES.get(table_key)
    if cfg is None:
        abort(404)
    return cfg


def _safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _usage_count(db, cfg, entry_id):
    """Total rows across every master table that reference this lookup row,
    plus a per-table breakdown for display."""
    total = 0
    breakdown = []
    for ref in cfg["references"]:
        count = db.execute(
            f"SELECT COUNT(*) c FROM {ref['table']} WHERE {ref['fk']} = ?", (entry_id,)
        ).fetchone()["c"]
        if count:
            breakdown.append({"label": ref["label"], "count": count})
        total += count
    return total, breakdown


def _missing_required_extra_field(values, cfg):
    """Label of the first required extra_fields entry left blank, or None
    if all required ones are filled."""
    for field in cfg.get("extra_fields", []):
        if field.get("required") and not values.get(field["name"]):
            return field["label"]
    return None


def _parent_options(db, cfg):
    """Active rows from a table's parent lookup, for the picker on its
    add/edit form. None when the table isn't nested under anything."""
    p = cfg.get("parent")
    if not p:
        return None
    return db.execute(
        f"SELECT * FROM {p['table']} WHERE is_active = 1 AND tenant_id = ? ORDER BY sort_order, {p['label_field']}",
        (g.tenant_id,),
    ).fetchall()


def _row_values(form, cfg):
    """Column -> value for an insert/update, built from the submitted form.
    Includes the parent foreign key column too when this table is nested,
    plus any table-specific "extra_fields" (e.g. hotel_amenity_options'
    Category/Icon) beyond the standard code/label/description/sort_order/
    is_active shape every lookup table otherwise shares."""
    label = form.get("label", "").strip()
    values = {
        "code": form.get("code", "").strip() or None,
        "label": label,
        "description": form.get("description", "").strip() or None,
        "sort_order": _safe_int(form.get("sort_order"), 0),
        "is_active": 1 if form.get("is_active") else 0,
    }
    if cfg.get("parent"):
        values[cfg["parent"]["fk"]] = _safe_int(form.get("parent_id"), None)
    for field in cfg.get("extra_fields", []):
        values[field["name"]] = form.get(field["name"], "").strip() or None
    return label, values


@table_maintenance_bp.route("/")
@login_required
def index():
    """Step 1 of Table Maintenance: a "Tables" screen listing every
    registered lookup table so the user picks which one to work on next,
    instead of the old dropdown-on-the-list-page approach (which mixed
    "which table" and "what's in it" on one screen and read as cluttered).
    """
    db = get_db()

    def _table_entry(key):
        cfg = _table_config(key)
        # Every lookup table registered in TABLES carries a tenant_id column
        # (see MODULE E in schema.sql) — none of the GLOBAL geography
        # tables (regions/countries/states/cities) or country_phone_codes
        # are managed here. The geography ones have their own screen
        # (Geography Maintenance, linked above) since they're shared
        # across every tenant; country_phone_codes has no admin UI yet.
        total = db.execute(
            f"SELECT COUNT(*) c FROM {cfg['table']} WHERE tenant_id = ?", (g.tenant_id,)
        ).fetchone()["c"]
        active = db.execute(
            f"SELECT COUNT(*) c FROM {cfg['table']} WHERE is_active = 1 AND tenant_id = ?", (g.tenant_id,)
        ).fetchone()["c"]
        return {"key": key, "cfg": cfg, "total": total, "active": active}

    # Grouped into Contacts / Content / Organization / Supplier / Knowledge
    # sections (TABLE_GROUPS above), each in that group's stated display
    # order, rather than one flat A-Z list — this is the Tables screen's
    # grouped-by-module layout.
    groups = [{"label": label, "tables": [_table_entry(key) for key in keys]} for label, keys in TABLE_GROUPS]

    # Every table in TABLES must have a slot in TABLE_GROUPS, or it would
    # silently vanish from the Tables screen — catch that at request time
    # rather than leave a registered table unreachable from the UI.
    grouped_keys = {key for _, keys in TABLE_GROUPS for key in keys}
    missing = set(TABLES) - grouped_keys
    if missing:
        raise RuntimeError(f"Table(s) registered in TABLES but missing from TABLE_GROUPS: {sorted(missing)}")

    return render_template("table_maintenance/index.html", groups=groups)


@table_maintenance_bp.route("/<table_key>")
@login_required
def manage(table_key):
    cfg = _table_config(table_key)
    db = get_db()
    # Every lookup table lists by its manual drag-order (sort_order, with
    # label as a tiebreaker) by default. A table can opt into listing
    # alphabetically by Label instead by setting "default_sort": "label" in
    # its TABLES entry (see "professions" above) -- sort_order is still
    # stored and editable either way, it's just not what this screen orders
    # by for that table.
    order_by_label = cfg.get("default_sort") == "label"
    if cfg.get("parent"):
        p = cfg["parent"]
        entry_order = "t.label COLLATE NOCASE" if order_by_label else "t.sort_order, t.label"
        sql = (
            f"SELECT t.*, p.{p['label_field']} AS parent_label FROM {cfg['table']} t "
            f"LEFT JOIN {p['table']} p ON p.{p['pk']} = t.{p['fk']} "
            f"WHERE t.tenant_id = ? "
            f"ORDER BY p.{p['label_field']}, {entry_order}"
        )
    else:
        order_by = "t.label COLLATE NOCASE" if order_by_label else "t.sort_order, t.label"
        sql = f"SELECT t.* FROM {cfg['table']} t WHERE t.tenant_id = ? ORDER BY {order_by}"
    rows = db.execute(sql, (g.tenant_id,)).fetchall()
    entries = []
    for r in rows:
        count, _ = _usage_count(db, cfg, r[cfg["pk"]])
        entries.append({"row": r, "usage_count": count})
    return render_template(
        "table_maintenance/manage.html", table_key=table_key, cfg=cfg, entries=entries
    )


@table_maintenance_bp.route("/<table_key>/new", methods=["GET", "POST"])
@login_required
def new_entry(table_key):
    cfg = _table_config(table_key)
    db = get_db()
    if request.method == "POST":
        form = request.form
        label, values = _row_values(form, cfg)
        if not label:
            flash("Label is required.", "error")
            return render_template("table_maintenance/form.html", cfg=cfg, table_key=table_key, entry=None, parent_options=_parent_options(db, cfg))
        if cfg.get("parent") and not values[cfg["parent"]["fk"]]:
            flash(f"{cfg['parent']['nav_label']} is required.", "error")
            return render_template("table_maintenance/form.html", cfg=cfg, table_key=table_key, entry=None, parent_options=_parent_options(db, cfg))
        missing_field = _missing_required_extra_field(values, cfg)
        if missing_field:
            flash(f"{missing_field} is required.", "error")
            return render_template("table_maintenance/form.html", cfg=cfg, table_key=table_key, entry=None, parent_options=_parent_options(db, cfg))
        try:
            values = {"tenant_id": g.tenant_id, **values}
            cols = ", ".join(values.keys())
            qs = ", ".join(["?"] * len(values))
            db.execute(f"INSERT INTO {cfg['table']} ({cols}) VALUES ({qs})", tuple(values.values()))
            db.commit()
        except sqlite3.IntegrityError:
            flash("That code is already in use by another entry in this table — codes must be unique.", "error")
            return render_template("table_maintenance/form.html", cfg=cfg, table_key=table_key, entry=None, parent_options=_parent_options(db, cfg))

        new_id = db.execute("SELECT last_insert_rowid() id").fetchone()["id"]
        log_action("Create", cfg["table"], new_id, f"Added {cfg['label']} entry '{label}'")
        flash(f"'{label}' added.", "success")
        return redirect(url_for("table_maintenance.manage", table_key=table_key))
    return render_template("table_maintenance/form.html", cfg=cfg, table_key=table_key, entry=None, parent_options=_parent_options(db, cfg))


@table_maintenance_bp.route("/<table_key>/<int:entry_id>/edit", methods=["GET", "POST"])
@login_required
def edit_entry(table_key, entry_id):
    cfg = _table_config(table_key)
    db = get_db()
    entry = db.execute(
        f"SELECT * FROM {cfg['table']} WHERE {cfg['pk']} = ? AND tenant_id = ?", (entry_id, g.tenant_id)
    ).fetchone()
    if entry is None:
        abort(404)
    if request.method == "POST":
        form = request.form
        label, values = _row_values(form, cfg)
        if not label:
            flash("Label is required.", "error")
            return render_template("table_maintenance/form.html", cfg=cfg, table_key=table_key, entry=entry, parent_options=_parent_options(db, cfg))
        if cfg.get("parent") and not values[cfg["parent"]["fk"]]:
            flash(f"{cfg['parent']['nav_label']} is required.", "error")
            return render_template("table_maintenance/form.html", cfg=cfg, table_key=table_key, entry=entry, parent_options=_parent_options(db, cfg))
        missing_field = _missing_required_extra_field(values, cfg)
        if missing_field:
            flash(f"{missing_field} is required.", "error")
            return render_template("table_maintenance/form.html", cfg=cfg, table_key=table_key, entry=entry, parent_options=_parent_options(db, cfg))
        try:
            set_clause = ", ".join(f"{col}=?" for col in values.keys())
            db.execute(
                f"UPDATE {cfg['table']} SET {set_clause} WHERE {cfg['pk']}=? AND tenant_id=?",
                tuple(values.values()) + (entry_id, g.tenant_id),
            )
            db.commit()
        except sqlite3.IntegrityError:
            flash("That code is already in use by another entry in this table — codes must be unique.", "error")
            return render_template("table_maintenance/form.html", cfg=cfg, table_key=table_key, entry=entry, parent_options=_parent_options(db, cfg))

        log_action("Update", cfg["table"], entry_id, f"Updated {cfg['label']} entry '{label}'")
        flash(f"'{label}' updated — anywhere it's already used will show the new text right away.", "success")
        return redirect(url_for("table_maintenance.manage", table_key=table_key))
    return render_template("table_maintenance/form.html", cfg=cfg, table_key=table_key, entry=entry, parent_options=_parent_options(db, cfg))


@table_maintenance_bp.route("/<table_key>/<int:entry_id>/toggle-active", methods=["POST"])
@login_required
def toggle_active(table_key, entry_id):
    cfg = _table_config(table_key)
    db = get_db()
    entry = db.execute(
        f"SELECT * FROM {cfg['table']} WHERE {cfg['pk']} = ? AND tenant_id = ?", (entry_id, g.tenant_id)
    ).fetchone()
    if entry is None:
        abort(404)
    new_state = 0 if entry["is_active"] else 1
    db.execute(
        f"UPDATE {cfg['table']} SET is_active = ? WHERE {cfg['pk']} = ? AND tenant_id = ?",
        (new_state, entry_id, g.tenant_id),
    )
    db.commit()
    log_action("Update", cfg["table"], entry_id, f"{'Activated' if new_state else 'Deactivated'} '{entry['label']}'")
    flash(f"'{entry['label']}' {'activated' if new_state else 'deactivated'}.", "success")
    return redirect(url_for("table_maintenance.manage", table_key=table_key))


@table_maintenance_bp.route("/<table_key>/<int:entry_id>/delete", methods=["POST"])
@login_required
def delete_entry(table_key, entry_id):
    cfg = _table_config(table_key)
    db = get_db()
    entry = db.execute(
        f"SELECT * FROM {cfg['table']} WHERE {cfg['pk']} = ? AND tenant_id = ?", (entry_id, g.tenant_id)
    ).fetchone()
    if entry is None:
        abort(404)
    count, _ = _usage_count(db, cfg, entry_id)
    if count > 0:
        flash(
            f"'{entry['label']}' is still used by {count} record(s), so it can't be deleted outright. "
            f"Deactivate it, or reassign those records to a different entry first.",
            "error",
        )
        return redirect(url_for("table_maintenance.reassign", table_key=table_key, entry_id=entry_id))
    db.execute(f"DELETE FROM {cfg['table']} WHERE {cfg['pk']} = ? AND tenant_id = ?", (entry_id, g.tenant_id))
    db.commit()
    log_action("Delete", cfg["table"], entry_id, f"Deleted unused {cfg['label']} entry '{entry['label']}'")
    flash(f"'{entry['label']}' deleted.", "success")
    return redirect(url_for("table_maintenance.manage", table_key=table_key))


@table_maintenance_bp.route("/<table_key>/<int:entry_id>/reassign", methods=["GET", "POST"])
@login_required
def reassign(table_key, entry_id):
    """Bulk-move every master row that references `entry_id` over to a
    different entry in the same lookup table, then optionally delete the
    old entry. Reached either from a blocked delete, or directly from the
    manage list as a standalone "merge into..." action."""
    cfg = _table_config(table_key)
    db = get_db()
    entry = db.execute(
        f"SELECT * FROM {cfg['table']} WHERE {cfg['pk']} = ? AND tenant_id = ?", (entry_id, g.tenant_id)
    ).fetchone()
    if entry is None:
        abort(404)
    count, breakdown = _usage_count(db, cfg, entry_id)
    others = db.execute(
        f"SELECT * FROM {cfg['table']} WHERE {cfg['pk']} != ? AND tenant_id = ? ORDER BY label COLLATE NOCASE",
        (entry_id, g.tenant_id),
    ).fetchall()

    if request.method == "POST":
        target_id = _safe_int(request.form.get("target_id"), None)
        also_delete = bool(request.form.get("also_delete"))
        if not target_id:
            flash("Choose an entry to reassign these records to.", "error")
            return redirect(url_for("table_maintenance.reassign", table_key=table_key, entry_id=entry_id))
        target = db.execute(
            f"SELECT * FROM {cfg['table']} WHERE {cfg['pk']} = ? AND tenant_id = ?", (target_id, g.tenant_id)
        ).fetchone()
        if target is None:
            abort(404)

        moved = 0
        for ref in cfg["references"]:
            cur = db.execute(
                f"UPDATE {ref['table']} SET {ref['fk']} = ? WHERE {ref['fk']} = ? AND tenant_id = ?",
                (target_id, entry_id, g.tenant_id),
            )
            moved += cur.rowcount

        if also_delete:
            db.execute(f"DELETE FROM {cfg['table']} WHERE {cfg['pk']} = ? AND tenant_id = ?", (entry_id, g.tenant_id))
        db.commit()

        log_action(
            "Update", cfg["table"], entry_id,
            f"Reassigned {moved} record(s) from '{entry['label']}' to '{target['label']}'"
            + (" and deleted the old entry" if also_delete else ""),
        )
        flash(
            f"Moved {moved} record(s) from '{entry['label']}' to '{target['label']}'."
            + (
                f" '{entry['label']}' was then deleted."
                if also_delete
                else f" '{entry['label']}' is still in the list with nothing pointing at it — deactivate or delete it separately whenever you like."
            ),
            "success",
        )
        return redirect(url_for("table_maintenance.manage", table_key=table_key))

    return render_template(
        "table_maintenance/reassign.html",
        cfg=cfg, table_key=table_key, entry=entry, others=others,
        usage_count=count, usage_breakdown=breakdown,
    )
