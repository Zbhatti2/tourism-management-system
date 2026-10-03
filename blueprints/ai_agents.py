"""
Module — AI Agents: Agent Runs & Human Review Queue.

Foundations for Zeb's "First Agents" plan: narrowly-scoped AI agents that
discover and update Back-Office data — Hotels first, Points of Interest
second (see schema.sql's MODULE Z comment for the full design note) — with
mandatory Human Review & Approval on everything they touch except
high-volume/frequently-changing data (Room Pricing & Availability), which
is handled separately by blueprints/suppliers.py's
_save_room_price()/supplier_room_price_history — that data writes straight
through and relies on its own append-only log for audit/oversight instead
of a review gate.

Three building blocks:

  Agent Run  — a single narrowly-scoped invocation, e.g. "Hotel: Avari
  Lahore, discover" or "POI: Update Gurdwara Janamastan, Nankana Sahib".
  Scope is a required, human-entered label plus a task_type — there is no
  "discover everything" option, by design (this is the guardrail Zeb asked
  for: narrow parameters, not broad crawls). A run can optionally carry a
  list of specific source URLs/documents to point the work at
  (agent_run_sources — "The user should also be able to enter specific
  urls and sources").

  Review Queue item — one candidate change (a brand-new record, or an
  update to an existing one), broken down field-by-field so a reviewer can
  approve the fields an agent got right and reject/hold the ones it
  didn't, rather than an all-or-nothing gate on the whole record. Every
  field can carry a source citation and a confidence score.

AGENT_TYPES / _APPLY_HANDLERS below is the registry of what each agent
covers. Each agent_type maps to exactly one entity_type (see
AGENT_TYPE_ENTITY), and ENTITY_FIELDS lists the fields this module can
stage for review and write back for that entity — mostly simple scalars
that live directly on the entity's own row (Hotel Name/Company/Website/
Notes on suppliers, Name/Website/Phone/etc. on points_of_interest), plus
one exception: a Hotel's Address, which lives in its own supplier_addresses
table (typed, several rows per supplier, matched against the same Region/
Country/Province/City geography lookups the manual Address form's
dropdowns use) rather than a plain suppliers column. It's still listed in
ENTITY_FIELDS as a "pseudo-field" (see _PSEUDO_FIELDS) — a reviewer edits
it as one free-text line like any other field, and only at Approve & Apply
time is it parsed (address_parsing.parse_free_text_address, the same
parser the Suppliers CSV importer uses) into the structured columns —
because early on, an AI-proposed hotel address was landing as raw text in
Notes instead (Zeb: "The entire address fell into the Notes field. The
address should be parsed and entered into the address field", Sept 2026).
Geo-coordinates, amenities, room types & images (Hotels) and images &
structured reference links (POIs) are still out of scope for this first
slice, each already living in its own table, meant to be added here as
additional registry entries in a follow-on pass, once this loop (Agent Run
-> Review Queue -> field-level approve -> write-back) is proven end to
end. Likewise, Travel Advisories and Travel Document Requirements are
meant to become new agent_type values with their own entity_type
handlers, not a rewrite of this module.

The "Run Agent" button (run_agent() below) is the first automated caller
of that same write path: it fetches each of the run's Source URLs
(ai_extraction.fetch_source_text), sends their text to Claude constrained
to propose only whitelisted fields (ai_extraction.extract_fields_with_model),
and queues exactly one review item from the result — never applies
anything itself. It refuses to run at all without at least one Source URL
(no ungrounded guessing) and without ANTHROPIC_API_KEY configured (see
config.py / .env.example), explaining itself via a flash message rather
than failing obscurely either way. "New Review Item" (the manual form)
keeps working identically regardless of whether any of that is set up.

"Undo Apply" (undo_apply() below) is the safety net for an approved
change that turns out to be wrong after the fact: every Approve & Apply
now snapshots the entity's full previous row into
ai_review_items.applied_snapshot before writing, and Undo Apply restores
it -- plus, for a Hotel, a second snapshot
(ai_review_items.applied_address_snapshot) of its primary supplier_addresses
row, since Address lives outside the entity row itself (see above) and
needs its own before/after capture. Scoped to exactly one level, on
purpose -- only the single most recently applied change to a given record
can be reversed (_is_latest_applied_for_entity), not a full multi-version
history, and only for AI Agent applies specifically, not a system-wide
undo across every TMS module.

Agent Run Sources can be a URL (fetched as text) or an uploaded image
(Zeb: "Can the Agent be trained to pick relevant information from this
image?", Sept 2026, sharing a hotel-booking-listing screenshot) — bytes
stored right in agent_run_sources, same "stored in the database"
convention as Supplier/POI Images, reusing Config.ALLOWED_IMAGE_EXTENSIONS/
MAX_UPLOAD_BYTES (the same caps the Contacts profile-photo upload uses)
rather than Suppliers/POI's own wider IMAGE_FILE_EXTENSIONS list, since
these formats are exactly what Claude's vision input accepts. run_agent()
sends any image sources to the extraction model as vision input alongside
whatever text it fetches from URL sources -- see ai_extraction.py's
extract_fields_with_model. Sources can only be added to or removed from a
run while it's still 'pending' (before Run Agent has executed), so a
completed/failed run keeps an untouched record of exactly what it read.
"""
import json
import mimetypes
from io import BytesIO

from flask import Blueprint, abort, flash, g, redirect, render_template, request, send_file, url_for

from address_parsing import parse_free_text_address
import ai_usage
from ai_extraction import ExtractionError, SourceFetchError, extract_fields_with_model, fetch_source_text
from auth.decorators import login_required
from config import Config
from db import get_db, log_action

ai_agents_bp = Blueprint("ai_agents", __name__)


AGENT_TYPES = [
    ("hotel_intelligence", "Hotel Intelligence"),
    ("poi_intelligence", "Points of Interest Intelligence"),
]

# agent_type -> the one entity_type it stages review items for.
AGENT_TYPE_ENTITY = {
    "hotel_intelligence": "supplier_hotel",
    "poi_intelligence": "poi",
}

TASK_TYPES = [
    ("discover_new", "Discover a new record"),
    ("update_existing", "Update an existing record"),
]

# entity_type -> (table, primary key column) -- used to look up an existing
# record's current values, both for the "update" picker and for applying an
# update.
ENTITY_TABLES = {
    "supplier_hotel": ("suppliers", "supplier_id"),
    "poi": ("points_of_interest", "poi_id"),
}

# Columns never touched by undo_apply()'s snapshot restore, even though
# they're part of the full-row snapshot captured at apply time -- the
# primary key and tenant_id must never move, and is_deleted/created_at are
# deliberately left alone so undoing an update can never silently
# resurrect (or otherwise change the deleted-ness of) a record that was
# separately deleted after the apply, or rewrite its original creation
# timestamp.
_UNDO_RESTORE_EXCLUDED_COLUMNS = {"tenant_id", "is_deleted", "created_at"}

# entity_type -> ordered list of (field_name, field_label) this module knows
# how to stage for review and, once approved, write back. Keep this list to
# fields that live directly on the target row as simple scalars — anything
# structured (an address, a list of amenities, a set of images) belongs in
# its own future entity_type/handler rather than being force-fit here.
ENTITY_FIELDS = {
    "supplier_hotel": [
        ("supplier_name", "Hotel Name"),
        ("company_name", "Company Name"),
        ("address", "Address"),
        ("web_page", "Website"),
        ("notes", "Notes"),
    ],
    "poi": [
        ("name", "Name of Attraction"),
        ("year_established", "Year Established"),
        ("local_location", "Location"),
        ("phone", "Phone"),
        ("website", "Website"),
        ("historical_significance", "Historical Significance"),
        ("directions", "Directions"),
        ("map_coordinates", "Map Coordinates"),
        ("notes", "Notes"),
    ],
}

ENTITY_LABELS = {
    "supplier_hotel": "Hotel (Supplier)",
    "poi": "Point of Interest",
}

# Shorter form used inline in the review-item form's radio labels/headings
# ("A brand-new Hotel" reads better than "A brand-new Hotel (Supplier)").
ENTITY_SHORT_LABELS = {
    "supplier_hotel": "Hotel",
    "poi": "Point of Interest",
}

# entity_type -> set of ENTITY_FIELDS names that AREN'T a plain column on
# that entity's own table -- e.g. "address" for a Hotel, which lives in its
# own supplier_addresses table (typed, multiple rows, with region/country/
# state/city lookups), not a suppliers column like Hotel Name/Company/
# Website/Notes are. _entity_choices' generic "SELECT these column names"
# query must skip these; _current_field_value/_apply_supplier_hotel give
# each one its own read/write logic instead (see _format_supplier_address/
# _upsert_supplier_address below). Empty for every entity_type that has no
# such fields (currently just 'poi').
_PSEUDO_FIELDS = {
    "supplier_hotel": {"address"},
}


def _display_field(entity_type):
    """The column used as this entity's human-readable name."""
    return "supplier_name" if entity_type == "supplier_hotel" else "name"


def _get_entity_row(db, entity_type, entity_id):
    table, id_col = ENTITY_TABLES[entity_type]
    return db.execute(
        f"SELECT * FROM {table} WHERE {id_col} = ? AND tenant_id = ?", (entity_id, g.tenant_id)
    ).fetchone()


def _hotel_supplier_type_id(db):
    row = db.execute(
        "SELECT supplier_type_id FROM supplier_types WHERE tenant_id = ? AND template_key = 'hotel' LIMIT 1",
        (g.tenant_id,),
    ).fetchone()
    return row["supplier_type_id"] if row else None


def _format_supplier_address(db, supplier_id):
    """Renders a Hotel's current primary supplier_addresses row back into
    one free-text line -- the "Current Value" a reviewer sees for the
    Address field, and the display-side inverse of
    _upsert_supplier_address's parsing. None if the supplier has no
    address on file yet."""
    row = db.execute(
        """SELECT a.street, a.postal_code, a.state_province_text, a.city_text,
                  c.label AS city_label, s.label AS state_label, co.label AS country_label
           FROM supplier_addresses a
           LEFT JOIN cities c ON c.city_id = a.city_id
           LEFT JOIN states s ON s.state_id = a.state_id
           LEFT JOIN countries co ON co.country_id = a.country_id
           WHERE a.supplier_id = ? AND a.tenant_id = ?
           ORDER BY a.is_primary DESC, a.supplier_address_id ASC LIMIT 1""",
        (supplier_id, g.tenant_id),
    ).fetchone()
    if row is None:
        return None
    parts = [
        row["street"],
        row["city_label"] or row["city_text"],
        row["state_label"] or row["state_province_text"],
        row["country_label"],
        row["postal_code"],
    ]
    parts = [p for p in parts if p and str(p).strip()]
    return ", ".join(parts) or None


def _current_field_value(db, entity_type, existing, field_name):
    """A field's current value for review-item display -- ordinarily just
    a column read off `existing` (the live entity row), except a pseudo-
    field (see _PSEUDO_FIELDS) like a Hotel's Address, which isn't a
    column at all and needs its own lookup."""
    if existing is None:
        return None
    if field_name in _PSEUDO_FIELDS.get(entity_type, ()):
        if entity_type == "supplier_hotel" and field_name == "address":
            return _format_supplier_address(db, existing["supplier_id"])
        return None
    return existing[field_name]


def _upsert_supplier_address(db, supplier_id, address_text):
    """Parses free-text `address_text` (address_parsing.parse_free_text_address
    -- the same parser the Suppliers CSV importer uses, matching City/
    Province/Country against the same geography lookups the manual Address
    form's dropdowns use) and writes it into this supplier's primary
    "Main Office" supplier_addresses row -- updating one in place if it
    already has one, else inserting a new one. A part the parser can't
    match against a geography lookup still lands in that field's own free-
    text fallback column (city_text/state_province_text) rather than being
    dropped, exactly like the manual address form's "not on file"
    fallback. A blank address_text is a no-op -- an approved-but-empty
    Address field shouldn't wipe out a real address already on file."""
    address_text = (address_text or "").strip()
    if not address_text:
        return
    parsed = parse_free_text_address(db, address_text)
    existing = db.execute(
        """SELECT supplier_address_id FROM supplier_addresses
           WHERE supplier_id = ? AND tenant_id = ? ORDER BY is_primary DESC, supplier_address_id ASC LIMIT 1""",
        (supplier_id, g.tenant_id),
    ).fetchone()
    state_text = None if parsed.get("state_id") else (parsed.get("state_text") or None)
    city_text = None if parsed.get("city_id") else (parsed.get("city_text") or None)
    if existing:
        db.execute(
            """UPDATE supplier_addresses SET street = ?, region_id = ?, country_id = ?,
               state_id = ?, state_province_text = ?, city_id = ?, city_text = ?, postal_code = ?,
               updated_at = datetime('now')
               WHERE supplier_address_id = ? AND tenant_id = ?""",
            (
                parsed.get("street") or None, parsed.get("region_id"), parsed.get("country_id"),
                parsed.get("state_id"), state_text, parsed.get("city_id"), city_text,
                parsed.get("postal_code") or None,
                existing["supplier_address_id"], g.tenant_id,
            ),
        )
    else:
        main_office_type = db.execute(
            "SELECT address_type_id FROM supplier_address_types WHERE tenant_id = ? AND lower(label) = 'main office'",
            (g.tenant_id,),
        ).fetchone()
        db.execute(
            """INSERT INTO supplier_addresses
               (tenant_id, supplier_id, address_type_id, street, region_id, country_id,
                state_id, state_province_text, city_id, city_text, postal_code, is_primary)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
            (
                g.tenant_id, supplier_id, main_office_type["address_type_id"] if main_office_type else None,
                parsed.get("street") or None, parsed.get("region_id"), parsed.get("country_id"),
                parsed.get("state_id"), state_text, parsed.get("city_id"), city_text,
                parsed.get("postal_code") or None,
            ),
        )


def _snapshot_supplier_address(db, supplier_id):
    """The full current primary supplier_addresses row as a plain dict
    (JSON-ready), or None if the supplier has no address on file yet --
    captured right before an 'update' apply touches it, so undo_apply()
    can restore exactly this, the same "snapshot right before the write"
    approach applied_snapshot already uses for the entity row itself."""
    row = db.execute(
        """SELECT * FROM supplier_addresses WHERE supplier_id = ? AND tenant_id = ?
           ORDER BY is_primary DESC, supplier_address_id ASC LIMIT 1""",
        (supplier_id, g.tenant_id),
    ).fetchone()
    return {k: row[k] for k in row.keys()} if row is not None else None


def _restore_supplier_address(db, supplier_id, snapshot):
    """undo_apply()'s reversal of _upsert_supplier_address: restores the
    supplier's primary address row to exactly `snapshot` (a dict from
    _snapshot_supplier_address, or None meaning there was no address row
    before the apply this is undoing -- in which case whatever row exists
    now, if any, is removed, since it didn't exist before that write)."""
    existing = db.execute(
        """SELECT supplier_address_id FROM supplier_addresses
           WHERE supplier_id = ? AND tenant_id = ? ORDER BY is_primary DESC, supplier_address_id ASC LIMIT 1""",
        (supplier_id, g.tenant_id),
    ).fetchone()
    if snapshot is None:
        if existing:
            db.execute(
                "DELETE FROM supplier_addresses WHERE supplier_address_id = ? AND tenant_id = ?",
                (existing["supplier_address_id"], g.tenant_id),
            )
        return
    restore_cols = [c for c in snapshot.keys() if c not in ("supplier_address_id", "tenant_id", "supplier_id", "created_at")]
    if existing:
        set_clause = ", ".join(f"{c} = ?" for c in restore_cols)
        params = [snapshot[c] for c in restore_cols] + [existing["supplier_address_id"], g.tenant_id]
        db.execute(
            f"UPDATE supplier_addresses SET {set_clause}, updated_at = datetime('now') "
            f"WHERE supplier_address_id = ? AND tenant_id = ?",
            params,
        )
    else:
        insert_cols = ["tenant_id", "supplier_id"] + restore_cols
        placeholders = ", ".join(["?"] * len(insert_cols))
        params = [g.tenant_id, supplier_id] + [snapshot[c] for c in restore_cols]
        db.execute(f"INSERT INTO supplier_addresses ({', '.join(insert_cols)}) VALUES ({placeholders})", params)


def _entity_choices(db, entity_type):
    """Existing records of this entity_type for the "update an existing
    record" picker, each carrying its current core-field values (as a
    plain dict, ready for |tojson) so the form can populate "Current
    Value" client-side without a round trip (same pattern as the Room Type
    default-description autofill in templates/suppliers/room_form.html).
    Jinja has no dict-comprehension syntax, so that dict is built here in
    Python rather than in the template.

    A pseudo-field (see _PSEUDO_FIELDS) is skipped from the generic SQL
    column list -- it isn't one -- and filled in afterward via its own
    lookup instead."""
    table, id_col = ENTITY_TABLES[entity_type]
    display_col = _display_field(entity_type)
    field_names = [name for name, _ in ENTITY_FIELDS[entity_type]]
    pseudo_fields = _PSEUDO_FIELDS.get(entity_type, set())
    column_field_names = [n for n in field_names if n not in pseudo_fields]
    select_extra = (", " + ", ".join(column_field_names)) if column_field_names else ""

    if entity_type == "supplier_hotel":
        type_id = _hotel_supplier_type_id(db)
        if type_id is None:
            return []
        rows = db.execute(
            f"""SELECT {id_col} AS choice_id, {display_col} AS choice_label{select_extra}
                FROM {table} WHERE tenant_id = ? AND supplier_type_id = ? AND is_deleted = 0
                ORDER BY {display_col}""",
            (g.tenant_id, type_id),
        ).fetchall()
    else:
        rows = db.execute(
            f"""SELECT {id_col} AS choice_id, {display_col} AS choice_label{select_extra}
                FROM {table} WHERE tenant_id = ? AND is_deleted = 0
                ORDER BY {display_col}""",
            (g.tenant_id,),
        ).fetchall()

    choices = []
    for r in rows:
        fields = {name: r[name] for name in column_field_names}
        if entity_type == "supplier_hotel" and "address" in pseudo_fields:
            fields["address"] = _format_supplier_address(db, r["choice_id"])
        choices.append({"choice_id": r["choice_id"], "choice_label": r["choice_label"], "fields": fields})
    return choices


def _apply_supplier_hotel(db, item, approved):
    """approved: dict of field_name -> proposed_value, already restricted
    to the fields the reviewer approved. Returns the resulting supplier_id."""
    approved = dict(approved)
    address_text = approved.pop("address", None)  # not a suppliers column -- its own table, see _upsert_supplier_address

    if item["action_type"] == "create":
        type_id = _hotel_supplier_type_id(db)
        if type_id is None:
            raise ValueError("No Supplier Type with template_key='hotel' is configured for this tenant.")
        name = approved.get("supplier_name") or item["entity_label"]
        db.execute(
            """INSERT INTO suppliers (tenant_id, supplier_name, supplier_type_id, company_name, web_page, notes)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (g.tenant_id, name, type_id, approved.get("company_name"), approved.get("web_page"), approved.get("notes")),
        )
        db.commit()
        supplier_id = db.execute("SELECT last_insert_rowid() id").fetchone()["id"]
        if address_text:
            _upsert_supplier_address(db, supplier_id, address_text)
            db.commit()
        log_action("Create", "supplier", supplier_id,
                   f"Created via AI Agent run #{item['run_id']}: {item['entity_label']}")
        return supplier_id
    else:
        supplier_id = item["entity_id"]
        supplier = _get_entity_row(db, "supplier_hotel", supplier_id)
        if supplier is None:
            raise ValueError(f"Hotel #{supplier_id} no longer exists.")
        if address_text:
            _upsert_supplier_address(db, supplier_id, address_text)
            db.commit()
        if not approved:
            return supplier_id
        set_clause = ", ".join(f"{col} = ?" for col in approved)
        params = list(approved.values()) + [supplier_id, g.tenant_id]
        db.execute(
            f"UPDATE suppliers SET {set_clause}, updated_at = datetime('now') WHERE supplier_id = ? AND tenant_id = ?",
            params,
        )
        db.commit()
        log_action("Update", "supplier", supplier_id,
                   f"Updated via AI Agent run #{item['run_id']}: {', '.join(approved.keys())}")
        return supplier_id


def _apply_poi(db, item, approved):
    """approved: dict of field_name -> proposed_value, already restricted
    to the fields the reviewer approved. Returns the resulting poi_id."""
    if item["action_type"] == "create":
        fields_to_insert = dict(approved)
        fields_to_insert["name"] = fields_to_insert.get("name") or item["entity_label"]
        columns = list(fields_to_insert.keys())
        col_sql = ", ".join(["tenant_id"] + columns)
        placeholder_sql = ", ".join(["?"] * (len(columns) + 1))
        db.execute(
            f"INSERT INTO points_of_interest ({col_sql}) VALUES ({placeholder_sql})",
            [g.tenant_id] + [fields_to_insert[c] for c in columns],
        )
        db.commit()
        poi_id = db.execute("SELECT last_insert_rowid() id").fetchone()["id"]
        log_action("Create", "point_of_interest", poi_id,
                   f"Created via AI Agent run #{item['run_id']}: {item['entity_label']}")
        return poi_id
    else:
        poi_id = item["entity_id"]
        poi = _get_entity_row(db, "poi", poi_id)
        if poi is None:
            raise ValueError(f"Point of Interest #{poi_id} no longer exists.")
        if not approved:
            return poi_id
        set_clause = ", ".join(f"{col} = ?" for col in approved)
        params = list(approved.values()) + [poi_id, g.tenant_id]
        db.execute(
            f"UPDATE points_of_interest SET {set_clause}, updated_at = datetime('now') WHERE poi_id = ? AND tenant_id = ?",
            params,
        )
        db.commit()
        log_action("Update", "point_of_interest", poi_id,
                   f"Updated via AI Agent run #{item['run_id']}: {', '.join(approved.keys())}")
        return poi_id


_APPLY_HANDLERS = {
    "supplier_hotel": _apply_supplier_hotel,
    "poi": _apply_poi,
}


# ---------------------------------------------------------------- Agent Runs

def _save_one_source(db, run_id, sort_order, url, note, file_storage):
    """Inserts one agent_run_sources row from a single dynamic Source row —
    either a URL (url non-blank) or an uploaded image (file_storage has a
    filename), never both; the form gives each row one URL input and one
    file input, and whichever one was actually filled in wins (a row with
    neither is silently a no-op, same as before this feature existed).

    Returns None on success, or a short error string to flash if the row
    was invalid and had to be skipped (an unsupported image format, or one
    over Config.MAX_UPLOAD_BYTES) — never fails the whole request over one
    bad row, consistent with how a blank row is already just skipped."""
    url = (url or "").strip()
    note = (note or "").strip() or None
    has_file = file_storage is not None and (file_storage.filename or "").strip()

    if has_file:
        filename = file_storage.filename.strip()
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext not in Config.ALLOWED_IMAGE_EXTENSIONS:
            return (
                f"“{filename}” isn't a supported image type "
                f"(allowed: {', '.join(sorted(Config.ALLOWED_IMAGE_EXTENSIONS))})."
            )
        data = file_storage.read()
        if not data:
            return f"“{filename}” appears to be empty."
        if len(data) > Config.MAX_UPLOAD_BYTES:
            return f"“{filename}” is too large (max {Config.MAX_UPLOAD_BYTES // (1024 * 1024)} MB)."
        mime_type = file_storage.mimetype or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        db.execute(
            """INSERT INTO agent_run_sources
               (tenant_id, run_id, source_type, file_data, file_name, mime_type, file_size, note, sort_order)
               VALUES (?, ?, 'image', ?, ?, ?, ?, ?, ?)""",
            (g.tenant_id, run_id, data, filename, mime_type, len(data), note, sort_order),
        )
        return None

    if url:
        db.execute(
            """INSERT INTO agent_run_sources (tenant_id, run_id, source_type, url, note, sort_order)
               VALUES (?, ?, 'url', ?, ?, ?)""",
            (g.tenant_id, run_id, url, note, sort_order),
        )
        return None

    return None  # blank row -- nothing to save, not an error


def _save_run_sources(db, run_id):
    """Reads the New Agent Run form's dynamic Source rows (see
    templates/ai_agents/run_form.html's addSourceRow, mirroring
    templates/poi/form.html's addLinkRow) and inserts one agent_run_sources
    row per non-blank URL or uploaded image. Called once, right after the
    run itself is created. Per-row validation errors (a bad image format/
    size) are flashed but don't block the run itself from being created —
    that one row is just skipped."""
    urls = request.form.getlist("source_url")
    notes = request.form.getlist("source_note")
    files = request.files.getlist("source_image")
    row_count = max(len(urls), len(notes), len(files))
    for i in range(row_count):
        url = urls[i] if i < len(urls) else ""
        note = notes[i] if i < len(notes) else ""
        file_storage = files[i] if i < len(files) else None
        error = _save_one_source(db, run_id, i, url, note, file_storage)
        if error:
            flash(error, "error")
    db.commit()


def _create_review_item(db, run, entity_type, action_type, entity_id, entity_label, field_values):
    """field_values: list of dicts with field_name, field_label,
    proposed_value, confidence, source_citation. current_value is looked
    up here from the live entity row (or left NULL for a new record).
    Shared by the manual "New Review Item" form and run_agent()'s
    automated path, so both write through the exact same schema/whitelist
    logic — one INSERT shape, not two copies that can drift apart."""
    existing = _get_entity_row(db, entity_type, entity_id) if entity_id else None
    db.execute(
        """INSERT INTO ai_review_items (tenant_id, run_id, entity_type, entity_id, entity_label, action_type)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (g.tenant_id, run["run_id"], entity_type, entity_id, entity_label, action_type),
    )
    db.commit()
    review_id = db.execute("SELECT last_insert_rowid() id").fetchone()["id"]
    for i, fv in enumerate(field_values):
        current_value = _current_field_value(db, entity_type, existing, fv["field_name"])
        db.execute(
            """INSERT INTO ai_review_fields
               (tenant_id, review_id, field_name, field_label, current_value, proposed_value,
                confidence, source_citation, sort_order)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (g.tenant_id, review_id, fv["field_name"], fv["field_label"], current_value,
             fv.get("proposed_value"), fv.get("confidence"), fv.get("source_citation"), i),
        )
    db.commit()
    return review_id


def _is_latest_applied_for_entity(db, item):
    """True if no OTHER 'applied' review item exists for the same
    (entity_type, entity_id) that was applied more recently than this one
    -- i.e. undoing this item can't clobber a newer, presumably still-
    wanted change to the same record. This is what makes undo "one level":
    only ever the single most recent applied change to a given record is
    reversible. Ties on applied_at (only second-resolution, and tests/fast
    workflows can easily apply twice within the same second) are broken
    by review_id, which is monotonically increasing."""
    if item["entity_id"] is None:
        return True
    newer = db.execute(
        """SELECT 1 FROM ai_review_items
           WHERE tenant_id = ? AND entity_type = ? AND entity_id = ? AND status = 'applied'
             AND review_id != ?
             AND (applied_at > ? OR (applied_at = ? AND review_id > ?))
           LIMIT 1""",
        (g.tenant_id, item["entity_type"], item["entity_id"], item["review_id"],
         item["applied_at"], item["applied_at"], item["review_id"]),
    ).fetchone()
    return newer is None


@ai_agents_bp.route("/")
@login_required
def list_runs():
    db = get_db()
    runs = db.execute(
        """SELECT r.*,
                  SUM(CASE WHEN i.status = 'pending' THEN 1 ELSE 0 END) AS pending_count,
                  SUM(CASE WHEN i.status = 'applied' THEN 1 ELSE 0 END) AS applied_count,
                  SUM(CASE WHEN i.status = 'rejected' THEN 1 ELSE 0 END) AS rejected_count,
                  COUNT(i.review_id) AS item_count
           FROM agent_runs r
           LEFT JOIN ai_review_items i ON i.run_id = r.run_id
           WHERE r.tenant_id = ?
           GROUP BY r.run_id
           ORDER BY r.created_at DESC""",
        (g.tenant_id,),
    ).fetchall()
    pending_total = db.execute(
        "SELECT COUNT(*) n FROM ai_review_items WHERE tenant_id = ? AND status = 'pending'", (g.tenant_id,)
    ).fetchone()["n"]
    import tenant_agents
    return render_template("ai_agents/runs_list.html", runs=runs, pending_total=pending_total,
                           agents=tenant_agents.AGENTS, planned=tenant_agents.PLANNED,
                           stats=tenant_agents.agent_stats(db, g.tenant_id),
                           recent=tenant_agents.recent_runs(db, g.tenant_id), start_url=tenant_agents.start_url,
                           agent_labels={a["key"]: a["label"] for a in tenant_agents.AGENTS})


@ai_agents_bp.route("/new", methods=["GET", "POST"])
@login_required
def new_run():
    if request.method == "POST":
        agent_type = request.form.get("agent_type", "").strip()
        task_type = request.form.get("task_type", "").strip()
        scope_label = request.form.get("scope_label", "").strip()

        errors = []
        if agent_type not in dict(AGENT_TYPES):
            errors.append("Please choose an Agent Type.")
        if task_type not in dict(TASK_TYPES):
            errors.append("Please choose what this run should do.")
        if not scope_label:
            errors.append(
                "A specific scope is required — e.g. \"Avari Hotel, Lahore\" or "
                "\"All 5-star hotels in Singapore\". Broad/open-ended runs aren't supported by design."
            )
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template(
                "ai_agents/run_form.html", agent_types=AGENT_TYPES, task_types=TASK_TYPES, form_values=request.form,
            )

        db = get_db()
        db.execute(
            """INSERT INTO agent_runs (tenant_id, agent_type, task_type, scope_label, created_by)
               VALUES (?, ?, ?, ?, ?)""",
            (g.tenant_id, agent_type, task_type, scope_label, g.get("user_id") or None),
        )
        db.commit()
        run_id = db.execute("SELECT last_insert_rowid() id").fetchone()["id"]
        _save_run_sources(db, run_id)
        log_action("Create", "agent_run", run_id, f"Logged agent run: {scope_label}")
        flash(f"Agent run logged: “{scope_label}”.", "success")
        return redirect(url_for("ai_agents.view_run", run_id=run_id))

    preset = request.args if request.args.get("agent_type") in dict(AGENT_TYPES) else None
    return render_template("ai_agents/run_form.html", agent_types=AGENT_TYPES, task_types=TASK_TYPES, form_values=preset)


@ai_agents_bp.route("/<int:run_id>")
@login_required
def view_run(run_id):
    db = get_db()
    run = db.execute("SELECT * FROM agent_runs WHERE run_id = ? AND tenant_id = ?", (run_id, g.tenant_id)).fetchone()
    if run is None:
        abort(404)
    items = db.execute(
        "SELECT * FROM ai_review_items WHERE run_id = ? AND tenant_id = ? ORDER BY created_at DESC",
        (run_id, g.tenant_id),
    ).fetchall()
    sources = db.execute(
        "SELECT * FROM agent_run_sources WHERE run_id = ? AND tenant_id = ? ORDER BY sort_order, source_id",
        (run_id, g.tenant_id),
    ).fetchall()
    return render_template(
        "ai_agents/run_view.html", run=run, items=items, sources=sources,
        agent_types=dict(AGENT_TYPES), task_types=dict(TASK_TYPES),
        entity_short_labels=ENTITY_SHORT_LABELS, agent_type_entity=AGENT_TYPE_ENTITY,
        agent_ready=bool(Config.ANTHROPIC_API_KEY),
    )


@ai_agents_bp.route("/runs/<int:run_id>/execute", methods=["POST"])
@login_required
def run_agent(run_id):
    db = get_db()
    run = db.execute("SELECT * FROM agent_runs WHERE run_id = ? AND tenant_id = ?", (run_id, g.tenant_id)).fetchone()
    if run is None:
        abort(404)
    if run["status"] == "running":
        flash("This run is already in progress.", "error")
        return redirect(url_for("ai_agents.view_run", run_id=run_id))

    entity_type = AGENT_TYPE_ENTITY.get(run["agent_type"])
    if entity_type is None:
        flash(f"No entity type is registered for agent type '{run['agent_type']}'.", "error")
        return redirect(url_for("ai_agents.view_run", run_id=run_id))

    sources = db.execute(
        "SELECT * FROM agent_run_sources WHERE run_id = ? AND tenant_id = ? ORDER BY sort_order, source_id",
        (run_id, g.tenant_id),
    ).fetchall()
    if not sources:
        flash(
            "Add at least one Source URL or image before running the agent automatically — "
            "or use New Review Item to enter data by hand.", "error",
        )
        return redirect(url_for("ai_agents.view_run", run_id=run_id))

    if not Config.ANTHROPIC_API_KEY:
        flash("No ANTHROPIC_API_KEY is configured yet — see .env.example in the App folder.", "error")
        return redirect(url_for("ai_agents.view_run", run_id=run_id))
    over = ai_usage.check_limit(db, g.tenant_id)
    if over:
        flash(over, "error")
        return redirect(url_for("ai_agents.view_run", run_id=run_id))

    db.execute(
        "UPDATE agent_runs SET status = 'running', started_at = datetime('now') WHERE run_id = ? AND tenant_id = ?",
        (run_id, g.tenant_id),
    )
    db.commit()

    url_sources = [s for s in sources if s["source_type"] == "url"]
    image_sources = [s for s in sources if s["source_type"] == "image"]

    fetched, fetch_errors = [], []
    for s in url_sources:
        try:
            fetched.append((s["url"], fetch_source_text(s["url"])))
        except SourceFetchError as e:
            fetch_errors.append(str(e))

    images_payload = [
        {
            "label": s["file_name"] or f"Image source #{s['source_id']}",
            "mime_type": s["mime_type"] or "image/jpeg",
            "data": s["file_data"],
        }
        for s in image_sources
    ]

    if not fetched and not images_payload:
        summary = "Could not read any of this run's sources: " + "; ".join(fetch_errors)
        db.execute(
            "UPDATE agent_runs SET status = 'failed', summary = ?, completed_at = datetime('now') WHERE run_id = ? AND tenant_id = ?",
            (summary[:2000], run_id, g.tenant_id),
        )
        db.commit()
        flash("Agent run failed — see the run's summary for details.", "error")
        return redirect(url_for("ai_agents.view_run", run_id=run_id))

    field_specs = ENTITY_FIELDS[entity_type]
    short_label = ENTITY_SHORT_LABELS[entity_type]
    try:
        proposed = extract_fields_with_model(
            short_label, run["scope_label"], field_specs, fetched, images=images_payload,
            on_usage=lambda model, usage: ai_usage.record(
                db, g.tenant_id, "Agent Run", model, usage, user_id=g.user_id, ref_type="agent_runs", ref_id=run_id,
                note=run["scope_label"]),
        )
    except ExtractionError as e:
        db.execute(
            "UPDATE agent_runs SET status = 'failed', summary = ?, completed_at = datetime('now') WHERE run_id = ? AND tenant_id = ?",
            (str(e)[:2000], run_id, g.tenant_id),
        )
        db.commit()
        flash("Agent run failed — see the run's summary for details.", "error")
        return redirect(url_for("ai_agents.view_run", run_id=run_id))

    if not proposed:
        summary = "The extraction model didn't find any of the whitelisted fields in the given source(s)."
        db.execute(
            "UPDATE agent_runs SET status = 'completed', summary = ?, completed_at = datetime('now') WHERE run_id = ? AND tenant_id = ?",
            (summary, run_id, g.tenant_id),
        )
        db.commit()
        flash("Agent run completed, but nothing was found to queue for review.", "success")
        return redirect(url_for("ai_agents.view_run", run_id=run_id))

    # Resolve which existing record this is, for an update -- never guess across multiple candidates.
    notes = []
    entity_id = None
    entity_label = run["scope_label"]
    if run["task_type"] == "update_existing":
        choices = _entity_choices(db, entity_type)
        scope_lower = run["scope_label"].strip().lower()
        candidates = [c for c in choices if c["choice_label"].strip().lower() == scope_lower]
        if not candidates:
            candidates = [c for c in choices if scope_lower in c["choice_label"].strip().lower()]
        if len(candidates) == 1:
            entity_id = candidates[0]["choice_id"]
            entity_label = candidates[0]["choice_label"]
        else:
            notes.append(
                f"Couldn't confidently match \"{run['scope_label']}\" to exactly one existing {short_label} "
                f"({len(candidates)} possible match(es)) — queued as a new-record proposal instead."
            )

    action_type = "update" if entity_id else "create"
    field_label_by_name = dict(field_specs)
    field_values = [{**p, "field_label": field_label_by_name.get(p["field_name"], p["field_name"])} for p in proposed]
    review_id = _create_review_item(db, run, entity_type, action_type, entity_id, entity_label, field_values)

    if fetch_errors:
        notes.append(f"{len(fetch_errors)} source(s) could not be read: " + "; ".join(fetch_errors))
    source_summary = f"{len(fetched)} source(s)"
    if images_payload:
        source_summary += f" and {len(images_payload)} image source(s)"
    summary = f"Queued 1 review item with {len(proposed)} proposed field(s) from {source_summary}."
    if notes:
        summary += " " + " ".join(notes)
    db.execute(
        "UPDATE agent_runs SET status = 'completed', summary = ?, completed_at = datetime('now') WHERE run_id = ? AND tenant_id = ?",
        (summary[:2000], run_id, g.tenant_id),
    )
    db.commit()
    log_action("Update", "agent_run", run_id, "Agent run completed")
    flash("Agent run completed — see the queued review item below.", "success")
    return redirect(url_for("ai_agents.view_review_item", review_id=review_id))


@ai_agents_bp.route("/runs/<int:run_id>/delete", methods=["POST"])
@login_required
def delete_run(run_id):
    """Deletes an Agent Run outright -- only when it has zero Review Items
    attached (nothing was ever queued/applied from it) and it isn't
    currently running. Meant for cleaning up stray/orphan runs -- e.g. ones
    logged while testing the New Agent Run form before Run Agent existed
    or before an API key was configured, which will always sit at
    "Pending" with 0 Review Items forever otherwise. A run that actually
    produced review items is left alone even if every one of them was
    later rejected, since that's still a real record of what the agent
    proposed and what a human decided -- delete individual review items'
    worth of history isn't what this button is for."""
    db = get_db()
    run = db.execute("SELECT * FROM agent_runs WHERE run_id = ? AND tenant_id = ?", (run_id, g.tenant_id)).fetchone()
    if run is None:
        abort(404)
    if run["status"] == "running":
        flash("This run is currently in progress and can't be deleted.", "error")
        return redirect(url_for("ai_agents.view_run", run_id=run_id))
    item_count = db.execute(
        "SELECT COUNT(*) AS n FROM ai_review_items WHERE run_id = ? AND tenant_id = ?", (run_id, g.tenant_id)
    ).fetchone()["n"]
    if item_count > 0:
        flash(
            "This run has Review Items attached, so it can't be deleted — that's a record of what the "
            "agent proposed and what was decided. Reject any pending items first if you want, but the run "
            "itself stays.", "error",
        )
        return redirect(url_for("ai_agents.view_run", run_id=run_id))
    db.execute("DELETE FROM agent_run_sources WHERE run_id = ? AND tenant_id = ?", (run_id, g.tenant_id))
    db.execute("DELETE FROM agent_runs WHERE run_id = ? AND tenant_id = ?", (run_id, g.tenant_id))
    db.commit()
    log_action("Delete", "agent_run", run_id, f"Deleted empty agent run: {run['scope_label']}")
    flash(f"Deleted “{run['scope_label']}”.", "success")
    return redirect(url_for("ai_agents.list_runs"))


@ai_agents_bp.route("/runs/<int:run_id>/sources/new", methods=["GET", "POST"])
@login_required
def add_source(run_id):
    """Adds one more Source (URL or image) to an already-created run —
    only while it's still 'pending', so a run that's already been executed
    (or is mid-execution) keeps an untouched record of exactly what it
    read. Shares _save_one_source with the New Agent Run form itself."""
    db = get_db()
    run = db.execute("SELECT * FROM agent_runs WHERE run_id = ? AND tenant_id = ?", (run_id, g.tenant_id)).fetchone()
    if run is None:
        abort(404)
    if run["status"] != "pending":
        flash("Sources can only be added while a run is still pending (before Run Agent has executed).", "error")
        return redirect(url_for("ai_agents.view_run", run_id=run_id))

    if request.method == "POST":
        url = request.form.get("source_url", "")
        note = request.form.get("source_note", "")
        file_storage = request.files.get("source_image")
        has_file = file_storage is not None and (file_storage.filename or "").strip()
        if not (url or "").strip() and not has_file:
            flash("Enter a Source URL or choose an image to upload.", "error")
            return render_template("ai_agents/source_form.html", run=run)

        next_sort = db.execute(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM agent_run_sources WHERE run_id = ? AND tenant_id = ?",
            (run_id, g.tenant_id),
        ).fetchone()["n"]
        error = _save_one_source(db, run_id, next_sort, url, note, file_storage)
        db.commit()
        if error:
            flash(error, "error")
            return render_template("ai_agents/source_form.html", run=run)
        log_action("Update", "agent_run", run_id, "Added a source")
        flash("Source added.", "success")
        return redirect(url_for("ai_agents.view_run", run_id=run_id))

    return render_template("ai_agents/source_form.html", run=run)


@ai_agents_bp.route("/runs/<int:run_id>/sources/<int:source_id>/image")
@login_required
def source_image(run_id, source_id):
    """Serves an uploaded image Source's stored bytes -- same "stored in
    the database" pattern as Supplier/POI Images' own image routes."""
    db = get_db()
    source = db.execute(
        """SELECT * FROM agent_run_sources
           WHERE source_id = ? AND run_id = ? AND tenant_id = ? AND source_type = 'image'""",
        (source_id, run_id, g.tenant_id),
    ).fetchone()
    if source is None:
        abort(404)
    return send_file(
        BytesIO(source["file_data"]),
        mimetype=source["mime_type"] or "application/octet-stream",
        download_name=source["file_name"] or f"source-{source_id}",
    )


@ai_agents_bp.route("/runs/<int:run_id>/sources/<int:source_id>/delete", methods=["POST"])
@login_required
def delete_source(run_id, source_id):
    """Removes one Source from a run -- only while it's still 'pending',
    same guard as add_source (a run that's already executed keeps an
    untouched record of what it actually read)."""
    db = get_db()
    run = db.execute("SELECT * FROM agent_runs WHERE run_id = ? AND tenant_id = ?", (run_id, g.tenant_id)).fetchone()
    if run is None:
        abort(404)
    if run["status"] != "pending":
        flash("Sources can only be removed while a run is still pending (before Run Agent has executed).", "error")
        return redirect(url_for("ai_agents.view_run", run_id=run_id))
    source = db.execute(
        "SELECT * FROM agent_run_sources WHERE source_id = ? AND run_id = ? AND tenant_id = ?",
        (source_id, run_id, g.tenant_id),
    ).fetchone()
    if source is None:
        abort(404)
    db.execute("DELETE FROM agent_run_sources WHERE source_id = ? AND tenant_id = ?", (source_id, g.tenant_id))
    db.commit()
    log_action("Delete", "agent_run", run_id, "Removed a source")
    flash("Source removed.", "success")
    return redirect(url_for("ai_agents.view_run", run_id=run_id))


# ------------------------------------------------------------- Review Queue

@ai_agents_bp.route("/review")
@login_required
def list_review():
    db = get_db()
    status = request.args.get("status", "").strip()
    entity_type = request.args.get("entity_type", "").strip()

    sql = """SELECT i.*, r.agent_type, r.scope_label, r.run_id
              FROM ai_review_items i JOIN agent_runs r ON r.run_id = i.run_id
              WHERE i.tenant_id = ?"""
    params = [g.tenant_id]
    if status:
        sql += " AND i.status = ?"
        params.append(status)
    if entity_type:
        sql += " AND i.entity_type = ?"
        params.append(entity_type)
    sql += " ORDER BY i.created_at DESC"
    items = db.execute(sql, params).fetchall()
    return render_template(
        "ai_agents/review_list.html", items=items, status=status, entity_type=entity_type,
        entity_labels=ENTITY_LABELS,
    )


@ai_agents_bp.route("/runs/<int:run_id>/review/new", methods=["GET", "POST"])
@login_required
def new_review_item(run_id):
    db = get_db()
    run = db.execute("SELECT * FROM agent_runs WHERE run_id = ? AND tenant_id = ?", (run_id, g.tenant_id)).fetchone()
    if run is None:
        abort(404)

    entity_type = AGENT_TYPE_ENTITY.get(run["agent_type"])
    if entity_type is None:
        abort(400, description=f"No entity type is registered for agent type '{run['agent_type']}'.")
    short_label = ENTITY_SHORT_LABELS[entity_type]
    fields = ENTITY_FIELDS[entity_type]
    choices = _entity_choices(db, entity_type)
    sources = db.execute(
        "SELECT * FROM agent_run_sources WHERE run_id = ? AND tenant_id = ? ORDER BY sort_order, source_id",
        (run_id, g.tenant_id),
    ).fetchall()

    if request.method == "POST":
        action_type = request.form.get("action_type", "").strip()
        entity_id = request.form.get("entity_id", "").strip() or None
        entity_label = request.form.get("entity_label", "").strip()

        errors = []
        if action_type not in ("create", "update"):
            errors.append("Please choose Create or Update.")
        if action_type == "update" and not entity_id:
            errors.append(f"Please choose which existing {short_label} this update applies to.")
        if not entity_label:
            errors.append("A display label for this item is required.")

        existing = None
        if action_type == "update" and entity_id:
            existing = _get_entity_row(db, entity_type, entity_id)
            if existing is None:
                errors.append(f"That {short_label} could not be found.")

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template(
                "ai_agents/review_item_form.html", run=run, fields=fields, choices=choices, sources=sources,
                short_label=short_label, form_values=request.form,
            )

        field_values = []
        for field_name, field_label in fields:
            proposed = request.form.get(f"proposed__{field_name}", "").strip()
            citation = request.form.get(f"citation__{field_name}", "").strip() or None
            confidence_raw = request.form.get(f"confidence__{field_name}", "").strip()
            confidence = None
            if confidence_raw:
                try:
                    confidence = max(0.0, min(1.0, float(confidence_raw)))
                except ValueError:
                    confidence = None
            if not proposed and not citation and confidence is None:
                continue  # skip rows the user left entirely blank
            field_values.append({
                "field_name": field_name, "field_label": field_label,
                "proposed_value": proposed or None, "confidence": confidence, "source_citation": citation,
            })

        review_id = _create_review_item(db, run, entity_type, action_type, entity_id, entity_label, field_values)
        log_action("Create", "ai_review_item", review_id, f"Queued for review: {entity_label}")
        flash("Review item added to the queue.", "success")
        return redirect(url_for("ai_agents.view_review_item", review_id=review_id))

    return render_template(
        "ai_agents/review_item_form.html", run=run, fields=fields, choices=choices, sources=sources,
        short_label=short_label, form_values=None,
    )


def _review_item(db, review_id):
    return db.execute(
        """SELECT i.*, r.agent_type, r.scope_label, r.run_id
           FROM ai_review_items i JOIN agent_runs r ON r.run_id = i.run_id
           WHERE i.review_id = ? AND i.tenant_id = ?""",
        (review_id, g.tenant_id),
    ).fetchone()


@ai_agents_bp.route("/review/<int:review_id>")
@login_required
def view_review_item(review_id):
    db = get_db()
    item = _review_item(db, review_id)
    if item is None:
        abort(404)
    fields = db.execute(
        "SELECT * FROM ai_review_fields WHERE review_id = ? AND tenant_id = ? ORDER BY sort_order, field_id",
        (review_id, g.tenant_id),
    ).fetchall()
    can_undo = item["status"] == "applied" and _is_latest_applied_for_entity(db, item)
    return render_template(
        "ai_agents/review_item_view.html", item=item, fields=fields, entity_labels=ENTITY_LABELS,
        can_apply=item["entity_type"] in _APPLY_HANDLERS, can_undo=can_undo,
    )


@ai_agents_bp.route("/review/<int:review_id>/decide", methods=["POST"])
@login_required
def decide_review_item(review_id):
    db = get_db()
    item = _review_item(db, review_id)
    if item is None:
        abort(404)
    if item["status"] in ("applied", "undone"):
        flash(
            "This item has already been applied." if item["status"] == "applied"
            else "This item was undone, and can't be re-decided from here.", "error",
        )
        return redirect(url_for("ai_agents.view_review_item", review_id=review_id))

    fields = db.execute(
        "SELECT * FROM ai_review_fields WHERE review_id = ? AND tenant_id = ? ORDER BY sort_order, field_id",
        (review_id, g.tenant_id),
    ).fetchall()
    reviewer_notes = request.form.get("reviewer_notes", "").strip() or None
    action = request.form.get("decision", "").strip()  # 'reject_all' | 'save' | 'apply'

    if action == "reject_all":
        for f in fields:
            db.execute("UPDATE ai_review_fields SET field_status = 'rejected' WHERE field_id = ?", (f["field_id"],))
        db.execute(
            """UPDATE ai_review_items SET status = 'rejected', reviewer_notes = ?, reviewed_by = ?, reviewed_at = datetime('now')
               WHERE review_id = ? AND tenant_id = ?""",
            (reviewer_notes, g.get("user_id") or None, review_id, g.tenant_id),
        )
        db.commit()
        log_action("Reject", "ai_review_item", review_id, "Rejected in full")
        flash("Review item rejected.", "success")
        return redirect(url_for("ai_agents.list_review"))

    # 'save' and 'apply' both start by recording each field's individual decision
    allowed_names = {name for name, _ in ENTITY_FIELDS.get(item["entity_type"], [])}
    approved_values = {}
    any_rejected = False
    for f in fields:
        decision = request.form.get(f"field_status__{f['field_id']}", "pending")
        if decision not in ("approved", "rejected", "pending"):
            decision = "pending"
        db.execute("UPDATE ai_review_fields SET field_status = ? WHERE field_id = ?", (decision, f["field_id"]))
        if decision == "approved" and f["proposed_value"] is not None and f["field_name"] in allowed_names:
            approved_values[f["field_name"]] = f["proposed_value"]
        if decision == "rejected":
            any_rejected = True
    db.commit()

    if action == "apply":
        handler = _APPLY_HANDLERS.get(item["entity_type"])
        if handler is None:
            flash(f"No apply handler is registered yet for entity type '{item['entity_type']}'.", "error")
            return redirect(url_for("ai_agents.view_review_item", review_id=review_id))

        # Snapshot the full entity row exactly as it is right now, right
        # before the write -- this is the "previous record ... stored in
        # history" Undo Apply restores. Only meaningful for an 'update'
        # (a 'create' has no "before" row to snapshot; undoing a create
        # instead soft-deletes the record it made -- see undo_apply()).
        # Captured at apply time, not back when the review item was first
        # created, so it's an exact reversal regardless of how long the
        # item sat in the queue first.
        applied_snapshot = None
        applied_address_snapshot = None
        if item["action_type"] == "update" and item["entity_id"]:
            before_row = _get_entity_row(db, item["entity_type"], item["entity_id"])
            if before_row is not None:
                applied_snapshot = json.dumps({k: before_row[k] for k in before_row.keys()})
            # Address lives in its own supplier_addresses table, not a
            # suppliers column -- applied_snapshot above doesn't cover it,
            # so it gets its own snapshot alongside it (see
            # _snapshot_supplier_address / undo_apply()'s use of it below).
            # Captured unconditionally for every Hotel update, not only
            # when this particular apply approved the Address field -- if
            # it wasn't touched this time, "before" and "after" are simply
            # the same, so restoring it on undo is a harmless no-op.
            if item["entity_type"] == "supplier_hotel":
                applied_address_snapshot = json.dumps(_snapshot_supplier_address(db, item["entity_id"]))

        try:
            entity_id = handler(db, item, approved_values)
        except ValueError as e:
            flash(str(e), "error")
            return redirect(url_for("ai_agents.view_review_item", review_id=review_id))
        db.execute(
            """UPDATE ai_review_items SET status = 'applied', entity_id = ?, reviewer_notes = ?,
               reviewed_by = ?, reviewed_at = datetime('now'), applied_at = datetime('now'),
               applied_snapshot = ?, applied_address_snapshot = ?
               WHERE review_id = ? AND tenant_id = ?""",
            (entity_id, reviewer_notes, g.get("user_id") or None, applied_snapshot, applied_address_snapshot,
             review_id, g.tenant_id),
        )
        db.commit()
        log_action("Approve", "ai_review_item", review_id, "Approved fields applied")
        flash("Approved fields have been applied.", "success")
        return redirect(url_for("ai_agents.view_review_item", review_id=review_id))

    # action == 'save': persist field decisions without writing anything back yet
    new_status = "needs_changes" if any_rejected else "pending"
    db.execute(
        "UPDATE ai_review_items SET status = ?, reviewer_notes = ? WHERE review_id = ? AND tenant_id = ?",
        (new_status, reviewer_notes, review_id, g.tenant_id),
    )
    db.commit()
    flash("Field decisions saved.", "success")
    return redirect(url_for("ai_agents.view_review_item", review_id=review_id))


@ai_agents_bp.route("/review/<int:review_id>/undo", methods=["POST"])
@login_required
def undo_apply(review_id):
    """Reverses exactly one Applied review item's write -- the safety net
    for when an approved AI-proposed value turns out to be wrong after the
    fact ("reverse the most recent update in case of an error"). An
    action_type='update' item is undone by restoring the entity's columns
    from applied_snapshot, the full "previous record" captured at apply
    time; a 'create' item is undone by soft-deleting the record it made,
    since there's no "before" row to go back to.

    Only ever available on the MOST RECENT applied item for a given
    record (_is_latest_applied_for_entity) -- if a different, later apply
    has since touched the same Hotel/POI, this refuses rather than risk
    clobbering that newer, presumably still-wanted change. This is the
    "one level" of undo by design: once this item is 'undone', it can't be
    re-applied or undone again from here, and only ever the single most
    recent change to a record was ever reversible in the first place."""
    db = get_db()
    item = _review_item(db, review_id)
    if item is None:
        abort(404)
    if item["status"] != "applied":
        flash("Only an applied item can be undone.", "error")
        return redirect(url_for("ai_agents.view_review_item", review_id=review_id))
    if not _is_latest_applied_for_entity(db, item):
        flash(
            "A newer update has since been applied to this record — only the most recent "
            "one can be undone. Review that item instead.", "error",
        )
        return redirect(url_for("ai_agents.view_review_item", review_id=review_id))

    table, id_col = ENTITY_TABLES[item["entity_type"]]

    if item["action_type"] == "create":
        db.execute(
            f"UPDATE {table} SET is_deleted = 1, updated_at = datetime('now') WHERE {id_col} = ? AND tenant_id = ?",
            (item["entity_id"], g.tenant_id),
        )
    else:
        if not item["applied_snapshot"]:
            flash(
                "No prior version was captured for this item (it was applied before Undo Apply "
                "existed), so it can't be reversed automatically.", "error",
            )
            return redirect(url_for("ai_agents.view_review_item", review_id=review_id))
        snapshot = json.loads(item["applied_snapshot"])
        live_cols = {r["name"] for r in db.execute(f"PRAGMA table_info({table})").fetchall()}
        restore = {
            col: value for col, value in snapshot.items()
            if col in live_cols and col != id_col and col not in _UNDO_RESTORE_EXCLUDED_COLUMNS
        }
        if restore:
            set_clause = ", ".join(f"{col} = ?" for col in restore)
            params = list(restore.values()) + [item["entity_id"], g.tenant_id]
            db.execute(
                f"UPDATE {table} SET {set_clause}, updated_at = datetime('now') WHERE {id_col} = ? AND tenant_id = ?",
                params,
            )
        # Address lives outside the entity row itself (see decide_review_item's
        # apply branch) so it needs its own restore alongside the columns above.
        if item["entity_type"] == "supplier_hotel" and item["applied_address_snapshot"] is not None:
            _restore_supplier_address(db, item["entity_id"], json.loads(item["applied_address_snapshot"]))

    db.execute(
        """UPDATE ai_review_items SET status = 'undone', undone_at = datetime('now'), undone_by = ?
           WHERE review_id = ? AND tenant_id = ?""",
        (g.get("user_id") or None, review_id, g.tenant_id),
    )
    db.commit()
    log_action("Undo", "ai_review_item", review_id, f"Undid applied change to: {item['entity_label']}")
    flash(f"Undone — “{item['entity_label']}” has been reverted.", "success")
    return redirect(url_for("ai_agents.view_review_item", review_id=review_id))
