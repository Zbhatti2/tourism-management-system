"""
Module F — Data Exchange (Import / Export Staging).

Import: files are never written straight into the live tables. A CSV
upload becomes an import_batches row plus one import_staging_rows row per
source row (raw_data as JSON); each staging row is validated, the batch is
shown for review, and only on an explicit "Commit" does anything reach
contacts/contact_emails/contact_phones — following the same history-aware
insert pattern Module A uses. Contacts is the reference case (matching
Module A being the reference for plain CRUD); the same staging pattern
extends to other modules' importers later.

Export: every download is recorded as an export_jobs row with the file
written under instance/exports/, then served back for download — so
"what got exported and when" has an audit trail, not just an in-memory
CSV stream.

Sensitive fields ARE decrypted into JSON/CSV exports (this is your own
backup of your own data) — the UI warns that exported files are plaintext
and should be stored/deleted carefully.
"""
import csv
import io
import json
import os
import re

from flask import Blueprint, abort, flash, g, redirect, render_template, request, send_file, url_for

from address_parsing import (
    extract_trailing_phones, match_city, match_city_anywhere, match_country, match_state, parse_free_text_address,
)
from auth.decorators import login_required
from config import Config
from db import get_db, log_action
from security import crypto

data_exchange_bp = Blueprint("data_exchange", __name__)


@data_exchange_bp.route("/")
@login_required
def index():
    db = get_db()
    batches = db.execute(
        "SELECT * FROM import_batches WHERE tenant_id = ? ORDER BY batch_id DESC LIMIT 20", (g.tenant_id,)
    ).fetchall()
    jobs = db.execute(
        "SELECT * FROM export_jobs WHERE tenant_id = ? ORDER BY export_id DESC LIMIT 20", (g.tenant_id,)
    ).fetchall()
    return render_template("data_exchange/index.html", batches=batches, jobs=jobs)


# =============================================================== EXPORT ===

def _dec(ct, dek):
    try:
        return crypto.decrypt_value(ct, dek)
    except ValueError:
        return None


def _export_contacts_rows(db):
    contacts = db.execute(
        "SELECT * FROM contacts WHERE is_deleted = 0 AND tenant_id = ? ORDER BY full_name", (g.tenant_id,)
    ).fetchall()
    rows = []
    for c in contacts:
        emails = db.execute("SELECT email_address FROM contact_emails WHERE contact_id = ?", (c["contact_id"],)).fetchall()
        phones = db.execute("SELECT phone_type, country_code, area_code, number, extension FROM contact_phones WHERE contact_id = ?", (c["contact_id"],)).fetchall()
        rows.append({
            "full_name": c["full_name"], "file_as": c["file_as"], "job_title": c["current_job_title"],
            "web_page": c["web_page"], "date_of_birth": c["date_of_birth"], "notes": c["notes"],
            "emails": [e["email_address"] for e in emails],
            "phones": [f"{p['phone_type']}: {p['country_code'] or ''} {p['area_code'] or ''} {p['number']}".strip() for p in phones],
        })
    return rows


def _export_json(module, db, dek):
    if module == "contacts":
        return _export_contacts_rows(db)
    if module == "points_of_interest":
        pois = db.execute(
            """SELECT p.*, pt.label AS type_label,
                      COALESCE(c.label, p.city_text) AS city_label,
                      COALESCE(s.label, p.state_province_text) AS state_label,
                      co.label AS country_label
               FROM points_of_interest p
               LEFT JOIN poi_types pt ON pt.poi_type_id = p.poi_type_id
               LEFT JOIN cities c ON c.city_id = p.city_id
               LEFT JOIN states s ON s.state_id = p.state_id
               LEFT JOIN countries co ON co.country_id = p.country_id
               WHERE p.is_deleted = 0 AND p.tenant_id = ? ORDER BY p.name""",
            (g.tenant_id,),
        ).fetchall()
        return [dict(r) for r in pois]
    if module == "documents":
        items = db.execute(
            "SELECT * FROM content WHERE is_deleted = 0 AND tenant_id = ?", (g.tenant_id,)
        ).fetchall()
        out = []
        for i in items:
            d = dict(i)
            d["locations"] = [dict(r) for r in db.execute("SELECT location_type, path_or_url FROM content_locations WHERE content_id = ?", (i["content_id"],)).fetchall()]
            d["keywords"] = [r["term"] for r in db.execute("SELECT term FROM content_keywords WHERE content_id = ?", (i["content_id"],)).fetchall()]
            d["hashtags"] = [r["term"] for r in db.execute("SELECT term FROM content_hashtags WHERE content_id = ?", (i["content_id"],)).fetchall()]
            out.append(d)
        return out
    abort(404)


def _contacts_csv_bytes(db):
    rows = _export_contacts_rows(db)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["full_name", "file_as", "job_title", "web_page", "date_of_birth", "email", "phone", "notes"])
    for r in rows:
        writer.writerow([r["full_name"], r["file_as"] or "", r["job_title"] or "", r["web_page"] or "",
                          r["date_of_birth"] or "", "; ".join(r["emails"]), "; ".join(r["phones"]), r["notes"] or ""])
    return buf.getvalue().encode("utf-8")


def _contacts_vcard_bytes(db):
    contacts = db.execute(
        "SELECT * FROM contacts WHERE is_deleted = 0 AND tenant_id = ? ORDER BY full_name", (g.tenant_id,)
    ).fetchall()
    lines = []
    for c in contacts:
        emails = db.execute("SELECT email_address FROM contact_emails WHERE contact_id = ?", (c["contact_id"],)).fetchall()
        phones = db.execute("SELECT number FROM contact_phones WHERE contact_id = ?", (c["contact_id"],)).fetchall()
        lines.append("BEGIN:VCARD")
        lines.append("VERSION:3.0")
        lines.append(f"FN:{c['full_name']}")
        lines.append(f"N:{c['full_name']};;;;")
        for e in emails:
            lines.append(f"EMAIL:{e['email_address']}")
        for p in phones:
            lines.append(f"TEL:{p['number']}")
        if c["notes"]:
            lines.append(f"NOTE:{c['notes'].replace(chr(10), ' ')}")
        lines.append("END:VCARD")
    return "\r\n".join(lines).encode("utf-8")


EXPORT_FORMATS = {
    "contacts": ["json", "csv", "vcard"],
    "points_of_interest": ["json"],
    "documents": ["json"],
}
EXT = {"json": "json", "csv": "csv", "vcard": "vcf"}
MIME = {"json": "application/json", "csv": "text/csv", "vcard": "text/vcard"}
# schema.sql's export_jobs.format CHECK constraint expects this exact casing
# ('CSV','JSON','vCard','PDF') — the rest of this module works with the
# lowercase form throughout, so translate only at the point of insert.
FORMAT_DB_LABEL = {"json": "JSON", "csv": "CSV", "vcard": "vCard"}


@data_exchange_bp.route("/export", methods=["POST"])
@login_required
def create_export():
    module = request.form.get("module")
    fmt = request.form.get("format")
    if module not in EXPORT_FORMATS or fmt not in EXPORT_FORMATS[module]:
        abort(400)

    db = get_db()
    if fmt == "json":
        data = _export_json(module, db, g.dek)
        content = json.dumps(data, indent=2, default=str).encode("utf-8")
    elif fmt == "csv" and module == "contacts":
        content = _contacts_csv_bytes(db)
    elif fmt == "vcard" and module == "contacts":
        content = _contacts_vcard_bytes(db)
    else:
        abort(400)

    filename = f"{module}.{EXT[fmt]}"
    db.execute(
        "INSERT INTO export_jobs (tenant_id, module, format, file_path) VALUES (?, ?, ?, ?)",
        (g.tenant_id, module, FORMAT_DB_LABEL[fmt], filename),
    )
    db.commit()
    export_id = db.execute("SELECT last_insert_rowid() id").fetchone()["id"]

    file_path = os.path.join(Config.EXPORTS_DIR, f"{export_id}_{filename}")
    with open(file_path, "wb") as f:
        f.write(content)
    db.execute(
        "UPDATE export_jobs SET file_path = ? WHERE export_id = ? AND tenant_id = ?", (file_path, export_id, g.tenant_id)
    )
    db.commit()

    log_action("Export", "export_job", export_id, f"Exported {module} as {fmt}")
    return redirect(url_for("data_exchange.download_export", export_id=export_id))


@data_exchange_bp.route("/export/<int:export_id>/download")
@login_required
def download_export(export_id):
    db = get_db()
    job = db.execute(
        "SELECT * FROM export_jobs WHERE export_id = ? AND tenant_id = ?", (export_id, g.tenant_id)
    ).fetchone()
    if job is None or not job["file_path"] or not os.path.exists(job["file_path"]):
        abort(404)
    fmt = job["format"].lower()
    download_name = f"{job['module']}.{EXT[fmt]}"
    return send_file(job["file_path"], as_attachment=True, download_name=download_name, mimetype=MIME[fmt])


# =============================================================== IMPORT ===

def _decode_upload(file):
    """Decode an uploaded CSV's bytes to text, tolerating whatever encoding
    it was actually saved in — not just UTF-8. Excel's default "CSV (Comma
    delimited)" export uses the system codepage (Windows-1252 on most
    Windows installs), which mangles anything outside plain ASCII (curly
    quotes, em dashes, accented names) if read strictly as UTF-8; Sheets
    and most other tools use UTF-8, possibly with a BOM. Tries the common
    ones in order and falls back to Windows-1252 read as "replace" (which
    never raises) rather than rejecting the upload outright."""
    raw = file.read()
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("cp1252", errors="replace")


def _normalize_phone_digits(raw):
    """Digits only, so '(650) 496-2220' and '650-496-2220' compare equal;
    an 11-digit number starting with '1' also drops that leading digit
    (e.g. '1-562-607-9766' -> '5626079766'), so a US/Canada number entered
    with and without its country code still matches. Used only for the
    Contacts importer's same-person matching (see _resolve_contact_row's
    match_keys) — nowhere that a phone number is actually stored keeps
    this stripped form."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


# Header spellings this importer recognizes, mapped to the canonical field
# name the rest of this module (and batch_review.html) works with. Covers
# both a plain "full_name,email,phone,notes" file (this importer's
# original, simplest shape) and a full Outlook/Exchange contacts export
# (Title, First Name, Last Name, Suffix, Organization, Job Title, Street,
# City, State, Business Postal Code, Country, Business Fax, Business
# Phone, Home Phone, Home Phone 2, Mobile Phone, Categories, E-mail
# Address, E-mail 3 Address, Notes — the columns Sikh_ContactsBatch_21a.CSV
# was built against).
CONTACT_HEADER_ALIASES = {
    "title": ["title", "honorific"],
    "first_name": ["first name", "given name"],
    "last_name": ["last name", "surname", "family name"],
    "full_name": ["full_name", "full name", "name"],
    "suffix": ["suffix"],
    "organization": ["organization", "company", "company name"],
    "job_title": ["job title", "job_title", "position"],
    "street": ["street", "address", "street address"],
    "city": ["city"],
    "state": ["state", "province", "province/state"],
    "postal_code": ["business postal code", "postal code", "zip", "zip code"],
    "country": ["country"],
    "business_fax": ["business fax", "fax"],
    "business_phone": ["business phone", "work phone", "office phone"],
    "home_phone": ["home phone"],
    "home_phone_2": ["home phone 2", "home phone2"],
    "mobile_phone": ["mobile phone", "cell phone", "cell", "phone"],
    "categories": ["categories", "category"],
    "email": ["e-mail address", "email address", "email", "e-mail"],
    "email_3": ["e-mail 3 address", "email 3 address"],
    "web_page": ["web_page", "web page", "website", "url"],
    "date_of_birth": ["date_of_birth", "date of birth", "dob", "birthday"],
    "notes": ["notes", "note"],
}


def _resolve_contact_row(row):
    """Pulls the canonical fields out of a normalized CSV row by trying
    every known header spelling for each (see CONTACT_HEADER_ALIASES),
    then derives the fields contacts.py's own form always fills in but no
    single CSV column maps to directly:

    - full_name: used as-is when the file has one (the original simple
      format); otherwise First Name + Last Name joined, since contacts has
      no separate first/last columns of its own (see schema.sql).
    - file_as: "Last, First" (the standard Outlook file-as convention)
      when the source gave first/last separately — left blank for a
      plain-full_name file, same as leaving it blank on the manual form.
    - phone_entries: one entry per non-blank Business/Home/Home 2/Mobile/
      Fax column, each tagged with the contact_phones.phone_type it maps
      to (Home Phone 2 becomes a second 'Home'-typed entry — the CHECK
      constraint has no separate "Home 2" type, same as adding a second
      Home phone by hand).
    - email_list: Email Address plus E-mail 3 Address (Outlook's "E-mail 2
      Type"/"E-mail 2 Display Name" columns are metadata, not a second
      address, so there's nothing there to import).
    - match_keys: a same-person signature — normalized (lowercased) email
      addresses and (digits-only) phone numbers — used for duplicate
      detection instead of full_name alone. A person's name is NOT a safe
      uniqueness key on its own: this very file has two different "Raj
      Singh"s (a Virage Logic VP and a Redwood Ventures founder, different
      companies/phones/addresses) alongside several genuine repeats of the
      same person under one name with only the phone/email formatting
      differing. Matching requires a shared phone or email, not just a
      shared name, so distinct people who happen to share a name are never
      silently collapsed into one contact — see the duplicate checks in
      import_contacts and _commit_contacts_rows below."""
    resolved = dict(row)
    for field, aliases in CONTACT_HEADER_ALIASES.items():
        value = ""
        for alias in aliases:
            if row.get(alias):
                value = row[alias]
                break
        resolved[field] = value

    first = resolved.get("first_name", "").strip()
    last = resolved.get("last_name", "").strip()
    full_name = resolved.get("full_name", "").strip()
    if not full_name:
        full_name = " ".join(p for p in (first, last) if p)
    resolved["full_name"] = full_name

    if first and last:
        resolved["file_as"] = f"{last}, {first}"
    else:
        resolved["file_as"] = last or first or ""

    # A column value with more than one number/address crammed in
    # (";"-separated) is split into multiple entries — same convention
    # this importer's original simple "phone"/"email" columns always used.
    phone_entries = []
    for col, phone_type in (
        ("business_phone", "Business"), ("home_phone", "Home"),
        ("home_phone_2", "Home"), ("mobile_phone", "Mobile"),
        ("business_fax", "Business Fax"),
    ):
        raw = (resolved.get(col) or "").strip()
        for number in [n.strip() for n in raw.split(";") if n.strip()]:
            phone_entries.append({"number": number, "phone_type": phone_type})
    resolved["phone_entries"] = phone_entries

    email_list = []
    for col in ("email", "email_3"):
        raw = (resolved.get(col) or "").strip()
        email_list.extend(e.strip() for e in raw.split(";") if e.strip())
    resolved["email_list"] = email_list

    match_keys = set()
    for e in email_list:
        match_keys.add(f"email:{e.lower()}")
    for entry in phone_entries:
        digits = _normalize_phone_digits(entry["number"])
        if len(digits) >= 7:  # skip short/garbage fragments as a match signal
            match_keys.add(f"phone:{digits}")
    resolved["match_keys"] = sorted(match_keys)
    return resolved


def _resolve_contact_organization(db, name):
    """Find-or-create an Organization by name (case-insensitive,
    tenant-scoped) for a Contact's Organization column — per the request,
    a company named on the CSV that doesn't already exist in the
    Organizations table is created (bare: just a name — the same minimal
    row Table Maintenance-created lookups get; Organization Type, address,
    phone etc. can be filled in afterward on its own page), rather than
    left unlinked or silently duplicated when a second row names the same
    company."""
    name = (name or "").strip()
    if not name:
        return None
    row = db.execute(
        "SELECT organization_id FROM organizations WHERE lower(organization_name) = lower(?) AND tenant_id = ?",
        (name, g.tenant_id),
    ).fetchone()
    if row:
        return row["organization_id"]
    db.execute(
        "INSERT INTO organizations (tenant_id, organization_name) VALUES (?, ?)",
        (g.tenant_id, name),
    )
    db.commit()
    row = db.execute(
        "SELECT organization_id FROM organizations WHERE lower(organization_name) = lower(?) AND tenant_id = ?",
        (name, g.tenant_id),
    ).fetchone()
    return row["organization_id"]


def _resolve_or_create_lookup(db, table, pk, label):
    """Look up a row in one of the small Contacts lookup tables
    (contact_titles / contact_suffixes / contact_categories) by label
    (case-insensitive), creating one — auto-generated code, same
    convention as _resolve_supplier_type/_resolve_poi_type above — the
    first time a new label is seen, so an import doesn't silently drop a
    Title/Suffix/Category that isn't one of the ones already seeded.
    Table Maintenance is where it can be relabeled, merged, or reordered
    afterward.

    Two different labels can slugify to the same code — punctuation-only
    differences like "M.D." vs "MD", or "PhD" vs "PhD." (both real Suffix
    values in Sikh_ContactsBatch_21a.CSV) both collapse to "MD"/"PHD" once
    _slug() strips dots — which would otherwise crash on this table's
    UNIQUE(tenant_id, code) the moment the second one is created. A code
    that's already taken by some OTHER label gets a numeric suffix
    (_2, _3, ...) instead, so both distinct labels still get their own
    row."""
    label = (label or "").strip()
    if not label:
        return None
    row = db.execute(
        f"SELECT {pk} FROM {table} WHERE lower(label) = lower(?) AND tenant_id = ?", (label, g.tenant_id)
    ).fetchone()
    if row:
        return row[pk]
    from seed_data import _slug

    base_code = _slug(label) or "OTHER"
    code = base_code
    attempt = 2
    while db.execute(f"SELECT 1 FROM {table} WHERE code = ? AND tenant_id = ?", (code, g.tenant_id)).fetchone():
        code = f"{base_code}_{attempt}"[:40]
        attempt += 1
    db.execute(
        f"""INSERT INTO {table} (tenant_id, code, label, sort_order, is_active)
            VALUES (?, ?, ?, (SELECT COALESCE(MAX(sort_order), -1) + 1 FROM {table} WHERE tenant_id = ?), 1)""",
        (g.tenant_id, code, label, g.tenant_id),
    )
    db.commit()
    row = db.execute(f"SELECT {pk} FROM {table} WHERE code = ? AND tenant_id = ?", (code, g.tenant_id)).fetchone()
    return row[pk]


@data_exchange_bp.route("/import/contacts", methods=["GET", "POST"])
@login_required
def import_contacts():
    db = get_db()
    if request.method == "POST":
        file = request.files.get("file")
        if not file or not file.filename:
            flash("Please choose a CSV file.", "error")
            return render_template("data_exchange/import_contacts_form.html")

        text = _decode_upload(file)
        reader = csv.DictReader(io.StringIO(text))
        rows = []
        for raw_row in reader:
            resolved = _resolve_contact_row(_normalize_row(raw_row))
            resolved["address"] = {
                "street": (resolved.get("street") or "").strip() or None,
                "postal_code": (resolved.get("postal_code") or "").strip() or None,
                **_resolve_geo_components(db, resolved.get("country", ""), resolved.get("state", ""), resolved.get("city", "")),
            }
            rows.append(resolved)
        if not rows:
            flash("That CSV had no rows.", "error")
            return render_template("data_exchange/import_contacts_form.html")

        db.execute(
            "INSERT INTO import_batches (tenant_id, source_type, target_module, file_name, status, row_count) VALUES (?, 'CSV', 'contacts', ?, 'Staged', ?)",
            (g.tenant_id, file.filename, len(rows)),
        )
        db.commit()
        batch_id = db.execute("SELECT last_insert_rowid() id").fetchone()["id"]

        error_count = 0
        # Same-name-within-file is NOT enough on its own to call two rows
        # duplicates (see _resolve_contact_row's match_keys docstring) — a
        # match also needs an overlapping phone/email, so two different
        # people who happen to share a name both get staged.
        seen_by_name = {}
        for row in rows:
            full_name = (row.get("full_name") or "").strip()
            row_keys = set(row.get("match_keys") or [])
            if not full_name:
                status, errors = "Invalid", "A name is required (Full Name, or First/Last Name)"
            else:
                name_key = full_name.lower()
                prior_key_sets = seen_by_name.get(name_key, [])
                if row_keys and any(row_keys & prior for prior in prior_key_sets):
                    status, errors = "Invalid", "Duplicate contact within this file — same name and a matching phone/email as an earlier row"
                else:
                    status, errors = "Valid", None
                    seen_by_name.setdefault(name_key, []).append(row_keys)
            if status == "Invalid":
                error_count += 1
            db.execute(
                "INSERT INTO import_staging_rows (tenant_id, batch_id, raw_data, validation_status, validation_errors) VALUES (?, ?, ?, ?, ?)",
                (g.tenant_id, batch_id, json.dumps(row), status, errors),
            )
        db.execute(
            "UPDATE import_batches SET error_count = ?, status = 'Validated' WHERE batch_id = ? AND tenant_id = ?",
            (error_count, batch_id, g.tenant_id),
        )
        db.commit()
        log_action("Import", "import_batch", batch_id, f"Staged {len(rows)} rows from {file.filename} ({error_count} invalid)")
        return redirect(url_for("data_exchange.review_batch", batch_id=batch_id))

    return render_template("data_exchange/import_contacts_form.html")


# Labels for organization_type codes that don't already exist in
# organization_types and don't already match an existing lookup entry's
# auto-generated code (see seed_data._slug) — used only the first time a
# given code shows up in an import; after that it's just looked up.
ORG_TYPE_LABEL_OVERRIDES = {
    "EDU_COLLEGE": "College",
    "EDU_SCHOOL": "School",
    "EDU_UNIVERSITY": "University",
    "PAK_GOVT": "Pakistan Govt.",
}


def _resolve_organization_type(db, code):
    """Look up an organization_types row by code, creating one (with a
    readable label) the first time a new code is seen. Table Maintenance >
    Organization Types is where the label can be tidied up afterward — the
    code is what matters for matching future import rows."""
    code = (code or "").strip().upper()
    if not code:
        return None
    row = db.execute(
        "SELECT organization_type_id FROM organization_types WHERE code = ? AND tenant_id = ?", (code, g.tenant_id)
    ).fetchone()
    if row:
        return row["organization_type_id"]
    label = ORG_TYPE_LABEL_OVERRIDES.get(code) or code.replace("_", " ").title()
    db.execute(
        """INSERT INTO organization_types (tenant_id, code, label, sort_order, is_active)
           VALUES (?, ?, ?, (SELECT COALESCE(MAX(sort_order), -1) + 1 FROM organization_types WHERE tenant_id = ?), 1)""",
        (g.tenant_id, code, label, g.tenant_id),
    )
    db.commit()
    row = db.execute(
        "SELECT organization_type_id FROM organization_types WHERE code = ? AND tenant_id = ?", (code, g.tenant_id)
    ).fetchone()
    return row["organization_type_id"]


def _normalize_row(row):
    """CSV headers arrive however the source file happened to capitalize
    them (e.g. 'NAME,TYPE'), and occasionally with doubled internal spaces
    from a sloppy export (e.g. 'Primary  Phone'); the rest of this module
    works with lowercase, single-spaced keys, matching the contacts
    importer's convention."""
    return {re.sub(r"\s+", " ", (k or "").strip().lower()): (v or "").strip() for k, v in row.items()}


# Header spellings this importer recognizes, mapped to the canonical field
# name the rest of this module (and batch_review.html) works with. Lets
# real-world exports through without the person having to rename columns
# first — e.g. a directory built with "Name of Organization" / "Phone(s)" /
# "Notes / What the Organization Does" instead of the plainer "Name" /
# "Phone" / "Notes" this importer originally expected.
ORG_HEADER_ALIASES = {
    "name": ["name", "name of organization", "organization name", "organization", "org name"],
    "type": ["type", "category", "organization type"],
    "address": ["address", "full address", "mailing address"],
    "phone": ["phone", "phone(s)", "phones", "telephone", "tel"],
    "email": ["email", "email(s)", "emails", "e-mail"],
    "website": ["website", "web", "url", "web site"],
    "primary_contact": ["primary contact(s)", "primary contact", "contact", "contacts"],
    "notes": ["notes", "description", "notes / what the organization does", "notes/description", "what the organization does"],
}


def _resolve_org_row(row):
    """Pulls the canonical fields (name/type/address/phone/email/website/
    notes) out of a normalized CSV row by trying every known header
    spelling for each, and folds "Primary Contact(s)" into notes (there's
    no separate contact-person column on organizations). Returns the
    original row plus these canonical keys merged in, so raw_data keeps
    everything for the audit trail while name/type/etc. are always
    reachable the same way regardless of the source file's exact headers."""
    resolved = dict(row)
    for field, aliases in ORG_HEADER_ALIASES.items():
        value = ""
        for alias in aliases:
            if row.get(alias):
                value = row[alias]
                break
        resolved[field] = value

    notes = resolved.get("notes", "")
    contact = resolved.pop("primary_contact", "")
    if contact:
        notes = f"Primary Contact(s): {contact}" + (f"\n\n{notes}" if notes else "")
    resolved["notes"] = notes
    return resolved


@data_exchange_bp.route("/import/organizations", methods=["GET", "POST"])
@login_required
def import_organizations():
    db = get_db()
    if request.method == "POST":
        file = request.files.get("file")
        if not file or not file.filename:
            flash("Please choose a CSV file.", "error")
            return render_template("data_exchange/import_organizations_form.html")

        text = _decode_upload(file)
        reader = csv.DictReader(io.StringIO(text))
        rows = []
        for raw_row in reader:
            resolved = _resolve_org_row(_normalize_row(raw_row))
            # Some source directories run one or more phone numbers straight
            # into the Address column with no field of their own (e.g. "...,
            # Columbus, Ohio 43210 United States of America,  (419) 535-6794
            # (614) 210-0591") — pulled out here and, when there's no Phone
            # column value already, used to fill it in; address.parsed below
            # is built from the phone-stripped text so those digits don't
            # get mistaken for part of the postal address. The raw Address
            # column itself (resolved["address"], -> organizations.
            # full_address) is left exactly as given, phone numbers and all,
            # since it's kept only as a legacy audit trail.
            address_for_parsing, embedded_phones = extract_trailing_phones(resolved.get("address", ""))
            if embedded_phones and not (resolved.get("phone") or "").strip():
                resolved["phone"] = "; ".join(embedded_phones)
            resolved["address_parsed"] = parse_free_text_address(db, address_for_parsing)
            rows.append(resolved)
        if not rows:
            flash("That CSV had no rows.", "error")
            return render_template("data_exchange/import_organizations_form.html")

        db.execute(
            "INSERT INTO import_batches (tenant_id, source_type, target_module, file_name, status, row_count) VALUES (?, 'CSV', 'organizations', ?, 'Staged', ?)",
            (g.tenant_id, file.filename, len(rows)),
        )
        db.commit()
        batch_id = db.execute("SELECT last_insert_rowid() id").fetchone()["id"]

        error_count = 0
        seen_names = set()
        for row in rows:
            name = (row.get("name") or "").strip()
            if not name:
                status, errors = "Invalid", "name is required"
            elif name.lower() in seen_names:
                status, errors = "Invalid", "Duplicate organization name within this file — already staged from an earlier row"
            else:
                status, errors = "Valid", None
                seen_names.add(name.lower())
            if status == "Invalid":
                error_count += 1
            db.execute(
                "INSERT INTO import_staging_rows (tenant_id, batch_id, raw_data, validation_status, validation_errors) VALUES (?, ?, ?, ?, ?)",
                (g.tenant_id, batch_id, json.dumps(row), status, errors),
            )
        db.execute(
            "UPDATE import_batches SET error_count = ?, status = 'Validated' WHERE batch_id = ? AND tenant_id = ?",
            (error_count, batch_id, g.tenant_id),
        )
        db.commit()
        log_action("Import", "import_batch", batch_id, f"Staged {len(rows)} rows from {file.filename} ({error_count} invalid)")
        return redirect(url_for("data_exchange.review_batch", batch_id=batch_id))

    return render_template("data_exchange/import_organizations_form.html")


# ---------------------------------------------------------- import: suppliers

# Header spellings this importer recognizes, mapped to the canonical field
# name the rest of this module (and batch_review.html) works with — same
# alias approach as ORG_HEADER_ALIASES above, tailored to the columns in
# Pakistan_Tourism_Companies_Directory.csv (the seed file this importer was
# built against): Type, Name of Organization, Address, Primary  Phone (note
# the doubled space in that source header — _normalize_row collapses it),
# Phone type, Notes / What the Organization Does, License No., License
# Valid Until. "city"/"province"/"country" and "subtype" cover a second file
# shape (e.g. Car_Rental_Agencies_in_Pakistan-Book1.csv) that gives City and
# Province as their own columns instead of one combined Address string — see
# _resolve_supplier_structured_address below, which is used instead of
# _parse_supplier_address whenever the uploaded file's header row has any of
# these three.
SUPPLIER_HEADER_ALIASES = {
    "type": ["type", "supplier type", "category"],
    "subtype": ["sub-type", "subtype", "sub type"],
    "name": ["name of organization", "name", "supplier name", "organization name"],
    "address": ["address", "full address", "mailing address", "street", "street address"],
    "city": ["city"],
    "province": ["province", "state", "state/province", "province/state"],
    "country": ["country"],
    "phone": ["primary phone", "phone", "phone(s)", "telephone", "tel"],
    "phone_type": ["phone type", "phonetype"],
    "website": ["website", "web", "url", "web site"],
    "fax": ["fax", "fax no.", "fax no", "fax number", "fax(es)"],
    "email": ["email", "email(s)", "emails", "e-mail"],
    "primary_contact": ["primary contact(s)", "primary contact", "contact", "contacts"],
    "notes": ["notes", "notes / what the organization does", "description"],
    "license_no": ["license no.", "license no", "license number", "license #"],
    "license_valid_until": ["license valid until", "license expiry", "license expiration"],
}

# Which normalized header spellings signal "this file gives City/Province/
# Country as their own columns" -- computed once per upload against the
# CSV's own header row (not per-row values, so a Default Country still
# applies uniformly even to rows whose City/Province cells are blank, e.g.
# Avis/Europcar/Hertz placeholder rows with nothing filled in but the name).
_SUPPLIER_STRUCTURED_GEO_HEADERS = set(
    SUPPLIER_HEADER_ALIASES["city"] + SUPPLIER_HEADER_ALIASES["province"] + SUPPLIER_HEADER_ALIASES["country"]
)


def _normalize_website(url):
    url = (url or "").strip()
    if not url:
        return ""
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url
    return url


def _parse_contact_name(raw):
    """"Aziz Boolani (Chief Executive, Serena Hotels)" ->
    ("Aziz Boolani", "Chief Executive, Serena Hotels"); a name with no
    trailing parenthetical just returns as-is with no title."""
    raw = (raw or "").strip()
    if not raw:
        return "", ""
    m = re.match(r"^(.*?)\s*\(([^)]+)\)\s*$", raw)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return raw, ""


def _build_phone_entries(phone_raw, phone_overflow_raw, fax_raw, phone_type_label):
    """Turns the CSV's phone-related columns into a list of
    {"raw", "extension", "type_label"} dicts, one per phone number to
    create under the imported supplier's contact. Two source quirks this
    accounts for, both specific to Pakistan_Tourism__HotelCompanies.csv:

    - Its "Phone(s)" column sometimes has an embedded, un-quoted comma in
      the original text (e.g. a UAN number plus a short extension), which
      spilled that second fragment into the next CSV column over — an
      unnamed column sitting between "Phone(s)" and "Fax" in this file's
      header row. When that overflow text contains "(cell)" it's really a
      second, complete phone number (e.g. "+92-304-111-7008 (cell)") and
      becomes its own Cell-tagged entry; otherwise it's treated as an
      extension on the primary number (e.g. "-7363").
    - A non-blank Fax column becomes its own Fax-tagged entry — Suppliers
      already models fax as one more phone_type rather than a separate
      field (see the Supplier Contact design note), so this is the same
      treatment the first supplier importer already gives a Fax column,
      just generalized to sit alongside a real Phone(s)/overflow/Fax
      combination instead of a single Primary Phone column."""
    entries = []
    phone_raw = (phone_raw or "").strip()
    phone_overflow_raw = (phone_overflow_raw or "").strip()
    fax_raw = (fax_raw or "").strip()

    extension = ""
    if phone_overflow_raw:
        if "(cell)" in phone_overflow_raw.lower():
            cell_number = re.sub(r"\(cell\)", "", phone_overflow_raw, flags=re.IGNORECASE).strip()
            if cell_number:
                entries.append({"raw": cell_number, "extension": "", "type_label": "Cell"})
        else:
            extension = phone_overflow_raw.lstrip("-").strip()

    if phone_raw:
        entries.insert(0, {"raw": phone_raw, "extension": extension, "type_label": phone_type_label or "Office"})
    elif extension:
        # A stray extension fragment with no primary number to attach it to
        # — keep it as its own entry rather than silently dropping it.
        entries.insert(0, {"raw": extension, "extension": "", "type_label": phone_type_label or "Office"})

    if fax_raw:
        entries.append({"raw": fax_raw, "extension": "", "type_label": "Fax"})

    return entries


def _resolve_supplier_row(row):
    """Pulls the canonical fields out of a normalized CSV row by trying
    every known header spelling for each (see SUPPLIER_HEADER_ALIASES), and
    prepends "License No:" / "License Valid Until:" caption lines to notes
    per the request — those two source columns don't have their own field
    on suppliers, so folding them into notes (same technique
    _resolve_org_row already uses for Organizations' "Primary Contact(s)")
    keeps the data from being silently dropped.

    Also resolves the contact-and-phone side of the row: a named
    "Primary Contact(s)" entry is split into contact_name/contact_title
    (see _parse_contact_name); Website is normalized to a real URL; and
    Phone(s)/Fax/the unnamed phone-overflow column (see
    _build_phone_entries) become a phone_entries list ready to insert
    as-is during commit."""
    resolved = dict(row)
    for field, aliases in SUPPLIER_HEADER_ALIASES.items():
        value = ""
        for alias in aliases:
            if row.get(alias):
                value = row[alias]
                break
        resolved[field] = value

    notes = resolved.get("notes", "")
    license_no = resolved.pop("license_no", "")
    license_valid_until = resolved.pop("license_valid_until", "")
    caption_lines = []
    if license_no:
        caption_lines.append(f"License No: {license_no}")
    if license_valid_until:
        caption_lines.append(f"License Valid Until: {license_valid_until}")
    if caption_lines:
        captions = "\n".join(caption_lines)
        notes = f"{captions}\n\n{notes}" if notes else captions
    resolved["notes"] = notes

    resolved["website"] = _normalize_website(resolved.get("website", ""))
    resolved["contact_name"], resolved["contact_title"] = _parse_contact_name(resolved.get("primary_contact", ""))
    # The unnamed phone-overflow column (see _build_phone_entries) has no
    # header text at all, so it isn't reachable via SUPPLIER_HEADER_ALIASES
    # — normalized headers keep it under the empty-string key.
    resolved["phone_entries"] = _build_phone_entries(
        resolved.get("phone", ""), row.get("", ""), resolved.get("fax", ""), resolved.get("phone_type", "")
    )
    return resolved


# Free-text address parsing (Street/City/Province/Country splitting against
# the GLOBAL geography lookups, plus embedded-phone extraction) now lives in
# address_parsing.py (imported at the top of this file), shared between this
# Suppliers importer and the Organizations importer/migration below — see
# that module for the full heuristic. Kept as a same-named alias here so
# every existing call site below still works untouched.
_parse_supplier_address = parse_free_text_address


def _resolve_supplier_structured_address(db, country_text, province_text, city_text, default_country_id=None):
    """Resolves Country/Province/City columns that already arrive split out
    (see _SUPPLIER_STRUCTURED_GEO_HEADERS) against the same geography
    lookups parse_free_text_address uses for a single combined Address
    column — returns the same result shape (street is filled in by the
    caller from the row's own Address/Street column, since there's nothing
    left to split out of it here).

    `default_country_id` — the "Default Country" picked on the import form
    — is used only when the row's own Country column is blank; a row that
    does name its own country always wins over the default.

    City is looked up scoped to the matched Province first; when that
    misses (a typo, or the source directory's Province is simply wrong for
    that City — real-world directories do this, e.g. a car rental listing
    Islamabad under "Punjab" when it's really Islamabad Capital Territory),
    it's tried again across every province of the country, and a hit there
    overrides both city_id AND state_id/state_text with that city's real
    province — self-correcting the mismatch rather than keeping the wrong
    one. Anything that still doesn't match a lookup row falls back to
    state_province_text/city_text/country_text, same as
    parse_free_text_address, so nothing is silently dropped."""
    country_text = (country_text or "").strip()
    province_text = (province_text or "").strip()
    city_text = (city_text or "").strip()
    result = {
        "street": "", "region_id": None,
        "country_id": None, "country_text": country_text,
        "state_id": None, "state_text": province_text,
        "city_id": None, "city_text": city_text,
        "postal_code": "",
    }

    country_row = match_country(db, country_text) if country_text else None
    if country_row:
        result["country_id"] = country_row["country_id"]
        result["region_id"] = country_row["region_id"]
    elif default_country_id:
        result["country_id"] = default_country_id
        default_row = db.execute("SELECT label, region_id FROM countries WHERE country_id = ?", (default_country_id,)).fetchone()
        if default_row:
            result["region_id"] = default_row["region_id"]
            if not result["country_text"]:
                result["country_text"] = default_row["label"]

    state_row = match_state(db, result["country_id"], province_text) if province_text else None
    if state_row:
        result["state_id"] = state_row["state_id"]

    city_row = match_city(db, result["state_id"], city_text) if result["state_id"] and city_text else None
    if not city_row and result["country_id"] and city_text:
        city_row = match_city_anywhere(db, result["country_id"], city_text)
        if city_row:
            result["state_id"] = city_row["state_id"]
            state_label = db.execute("SELECT label FROM states WHERE state_id = ?", (city_row["state_id"],)).fetchone()
            if state_label:
                result["state_text"] = state_label["label"]
    if city_row:
        result["city_id"] = city_row["city_id"]
    return result


def _resolve_supplier_type(db, label):
    """Look up a supplier_types row by label (case-insensitive), creating
    one — with an auto-generated code, same convention as every other
    lookup table — the first time a new label is seen, so an import doesn't
    silently drop a supplier whose Type isn't one of the ones already
    seeded. Table Maintenance > Supplier Types is where it can be relabeled,
    merged, or reordered afterward."""
    label = (label or "").strip()
    if not label:
        return None
    row = db.execute(
        "SELECT supplier_type_id FROM supplier_types WHERE lower(label) = lower(?) AND tenant_id = ?",
        (label, g.tenant_id),
    ).fetchone()
    if row:
        return row["supplier_type_id"]
    from seed_data import _slug

    code = _slug(label)
    db.execute(
        """INSERT INTO supplier_types (tenant_id, code, label, sort_order, is_active)
           VALUES (?, ?, ?, (SELECT COALESCE(MAX(sort_order), -1) + 1 FROM supplier_types WHERE tenant_id = ?), 1)""",
        (g.tenant_id, code, label, g.tenant_id),
    )
    db.commit()
    row = db.execute(
        "SELECT supplier_type_id FROM supplier_types WHERE code = ? AND tenant_id = ?", (code, g.tenant_id)
    ).fetchone()
    return row["supplier_type_id"]


def _resolve_supplier_subtype(db, type_id, value):
    """Look up a supplier_subtypes row under the given Supplier Type by
    either its code (e.g. the seeded "TRANSPORT_PROVIDER_CAR_RENTALS") or
    its label (e.g. "Car Rentals") — case-insensitive either way, so a
    Default Sub-Type picked by code on the import form and a Sub-Type
    column spelled out by label in a CSV both resolve the same row.
    Creates one — auto-generated code prefixed with the parent Type's own
    code, same convention as seed_data._seed_nested — the first time a new
    sub-type is seen for that Type, so an import doesn't silently drop a
    supplier's Sub-Type. Returns None when type_id itself is None (a
    sub-type can't exist without a parent Type) or value is blank."""
    value = (value or "").strip()
    if not value or not type_id:
        return None
    row = db.execute(
        """SELECT supplier_subtype_id FROM supplier_subtypes
           WHERE tenant_id = ? AND supplier_type_id = ? AND (upper(code) = upper(?) OR lower(label) = lower(?))""",
        (g.tenant_id, type_id, value, value),
    ).fetchone()
    if row:
        return row["supplier_subtype_id"]
    # Not found under this specific Type -- also try matching by code across
    # the whole tenant (subtype codes are unique per tenant, not per type),
    # in case `value` is an exact seeded code whose row turns out to belong
    # to a different Type than the one that was resolved for this row.
    row = db.execute(
        "SELECT supplier_subtype_id FROM supplier_subtypes WHERE tenant_id = ? AND upper(code) = upper(?)",
        (g.tenant_id, value),
    ).fetchone()
    if row:
        return row["supplier_subtype_id"]

    from seed_data import _slug

    parent = db.execute(
        "SELECT code FROM supplier_types WHERE supplier_type_id = ? AND tenant_id = ?", (type_id, g.tenant_id)
    ).fetchone()
    parent_code = (parent["code"] if parent and parent["code"] else "TYPE")
    code = f"{parent_code}_{_slug(value)}"[:40]
    db.execute(
        """INSERT INTO supplier_subtypes (tenant_id, supplier_type_id, code, label, sort_order, is_active)
           VALUES (?, ?, ?, ?, (SELECT COALESCE(MAX(sort_order), -1) + 1 FROM supplier_subtypes
                                 WHERE tenant_id = ? AND supplier_type_id = ?), 1)""",
        (g.tenant_id, type_id, code, value, g.tenant_id, type_id),
    )
    db.commit()
    row = db.execute(
        "SELECT supplier_subtype_id FROM supplier_subtypes WHERE tenant_id = ? AND code = ?", (g.tenant_id, code)
    ).fetchone()
    return row["supplier_subtype_id"]


def _lookup_id_by_label(db, table, pk, label):
    if not label:
        return None
    row = db.execute(f"SELECT {pk} FROM {table} WHERE lower(label) = lower(?) AND tenant_id = ?", (label, g.tenant_id)).fetchone()
    return row[pk] if row else None


def _phone_calling_code(db, country_id):
    if not country_id:
        return None
    row = db.execute("SELECT calling_code FROM country_phone_codes WHERE country_id = ? LIMIT 1", (country_id,)).fetchone()
    return row["calling_code"] if row else None


def _split_phone(raw, calling_code=None):
    """'042-35781287' -> ('042', '35781287'); a number with no hyphen is
    left entirely in `number` with no area code. When `calling_code` (e.g.
    '+92') is given and the raw text leads with it — common when a source
    file writes international-format numbers like '+92 42-363-60210'
    instead of the local '042-363-60210' — it's stripped first, so it
    doesn't end up baked into area_code on top of country_code already
    carrying it (see _phone_calling_code). No stricter parsing than that —
    supplier_contact_phones.area_code/number are free-text, same as the
    manual phone form, so this is a best-effort split, not validation."""
    raw = (raw or "").strip()
    if not raw:
        return "", ""
    if calling_code and raw.startswith(calling_code):
        raw = raw[len(calling_code):].lstrip(" -")
    if "-" in raw:
        area, _, number = raw.partition("-")
        return area.strip(), number.strip()
    return "", raw


def _supplier_import_form_context(db):
    """Countries + Supplier Types + Supplier Sub-Types (each sub-type
    labeled with its parent Type, e.g. "Transport Provider — Car Rentals")
    for the three optional Default pickers on the Suppliers import form —
    applied to a row only when that row's own column is blank, never
    overriding a value the file actually gives."""
    countries = db.execute("SELECT * FROM countries WHERE is_active = 1 ORDER BY label").fetchall()
    supplier_types = db.execute(
        "SELECT * FROM supplier_types WHERE is_active = 1 AND tenant_id = ? ORDER BY label COLLATE NOCASE", (g.tenant_id,)
    ).fetchall()
    supplier_subtypes = db.execute(
        """SELECT st.supplier_subtype_id, st.code, st.label, t.label AS type_label
           FROM supplier_subtypes st JOIN supplier_types t ON t.supplier_type_id = st.supplier_type_id
           WHERE st.is_active = 1 AND st.tenant_id = ?
           ORDER BY t.label COLLATE NOCASE, st.label COLLATE NOCASE""",
        (g.tenant_id,),
    ).fetchall()
    return {"countries": countries, "supplier_types": supplier_types, "supplier_subtypes": supplier_subtypes}


@data_exchange_bp.route("/import/suppliers", methods=["GET", "POST"])
@login_required
def import_suppliers():
    db = get_db()
    if request.method == "POST":
        file = request.files.get("file")
        if not file or not file.filename:
            flash("Please choose a CSV file.", "error")
            return render_template("data_exchange/import_suppliers_form.html", **_supplier_import_form_context(db))

        default_country_id_raw = request.form.get("default_country_id") or None
        default_country_id = int(default_country_id_raw) if default_country_id_raw else None
        default_type_id = request.form.get("default_supplier_type_id") or None
        default_subtype_id = request.form.get("default_supplier_subtype_id") or None
        default_type_label = None
        if default_type_id:
            row = db.execute(
                "SELECT label FROM supplier_types WHERE supplier_type_id = ? AND tenant_id = ?", (default_type_id, g.tenant_id)
            ).fetchone()
            default_type_label = row["label"] if row else None
        default_subtype_code = None
        if default_subtype_id:
            row = db.execute(
                "SELECT code FROM supplier_subtypes WHERE supplier_subtype_id = ? AND tenant_id = ?", (default_subtype_id, g.tenant_id)
            ).fetchone()
            default_subtype_code = row["code"] if row else None

        text = _decode_upload(file)
        reader = csv.DictReader(io.StringIO(text))
        normalized_headers = {re.sub(r"\s+", " ", (h or "").strip().lower()) for h in (reader.fieldnames or [])}
        has_structured_geo = bool(normalized_headers & _SUPPLIER_STRUCTURED_GEO_HEADERS)

        rows = []
        for raw_row in reader:
            resolved = _resolve_supplier_row(_normalize_row(raw_row))
            if not resolved.get("type") and default_type_label:
                resolved["type"] = default_type_label
            if not resolved.get("subtype") and default_subtype_code:
                resolved["subtype"] = default_subtype_code
            if has_structured_geo:
                addr = _resolve_supplier_structured_address(
                    db, resolved.get("country"), resolved.get("province"), resolved.get("city"), default_country_id,
                )
                addr["street"] = (resolved.get("address") or "").strip()
                resolved["address_parsed"] = addr
            else:
                resolved["address_parsed"] = _parse_supplier_address(db, resolved.get("address", ""))
            rows.append(resolved)
        if not rows:
            flash("That CSV had no rows.", "error")
            return render_template("data_exchange/import_suppliers_form.html", **_supplier_import_form_context(db))

        db.execute(
            "INSERT INTO import_batches (tenant_id, source_type, target_module, file_name, status, row_count) VALUES (?, 'CSV', 'suppliers', ?, 'Staged', ?)",
            (g.tenant_id, file.filename, len(rows)),
        )
        db.commit()
        batch_id = db.execute("SELECT last_insert_rowid() id").fetchone()["id"]

        error_count = 0
        seen_names = set()
        for row in rows:
            name = (row.get("name") or "").strip()
            if not name:
                status, errors = "Invalid", "name is required"
            elif name.lower() in seen_names:
                status, errors = "Invalid", "Duplicate supplier name within this file — already staged from an earlier row"
            else:
                status, errors = "Valid", None
                seen_names.add(name.lower())
            if status == "Invalid":
                error_count += 1
            db.execute(
                "INSERT INTO import_staging_rows (tenant_id, batch_id, raw_data, validation_status, validation_errors) VALUES (?, ?, ?, ?, ?)",
                (g.tenant_id, batch_id, json.dumps(row), status, errors),
            )
        db.execute(
            "UPDATE import_batches SET error_count = ?, status = 'Validated' WHERE batch_id = ? AND tenant_id = ?",
            (error_count, batch_id, g.tenant_id),
        )
        db.commit()
        log_action("Import", "import_batch", batch_id, f"Staged {len(rows)} rows from {file.filename} ({error_count} invalid)")
        return redirect(url_for("data_exchange.review_batch", batch_id=batch_id))

    return render_template("data_exchange/import_suppliers_form.html", **_supplier_import_form_context(db))


# ------------------------------------------------------ import: points of interest

# Header spellings this importer recognizes, mapped to the canonical field
# name the rest of this module (and batch_review.html) works with — tailored
# to Malaysia_Pakistan_Points_of_Interest.csv's columns (Country, POI Name,
# Type, Address, City, Province/State, Phone, Website, Email, Notes) while
# still accepting the plainer spellings the other importers use.
POI_HEADER_ALIASES = {
    "country": ["country"],
    "name": ["poi name", "name", "name of attraction", "attraction"],
    "type": ["type", "poi type", "category"],
    "address": ["address", "local location", "full address"],
    "city": ["city"],
    "state": ["province/state", "province / state", "state", "province"],
    "phone": ["phone", "phone(s)", "telephone", "tel"],
    "website": ["website", "web", "url", "web site"],
    "email": ["email", "email(s)", "e-mail"],
    "notes": ["notes", "historical significance", "description"],
}


def _resolve_poi_row(row):
    """Pulls the canonical fields out of a normalized CSV row by trying
    every known header spelling for each (see POI_HEADER_ALIASES). The
    source file's "Notes" column reads as historical/architectural
    background on every row (e.g. "Built in 1673 by Mughal Emperor
    Aurangzeb..."), which is exactly what points_of_interest.historical_
    significance is for — so that's where it lands at commit time, not the
    generic notes field (see _commit_poi_rows)."""
    resolved = dict(row)
    for field, aliases in POI_HEADER_ALIASES.items():
        value = ""
        for alias in aliases:
            if row.get(alias):
                value = row[alias]
                break
        resolved[field] = value
    resolved["website"] = _normalize_website(resolved.get("website", ""))
    return resolved


def _resolve_geo_components(db, country_label, state_label, city_label):
    """Resolves a CSV row's separate Country / Province-State / City
    columns against the global geography lookups, reusing the same
    component-based matchers (match_country/match_state/match_city) the
    Organizations/Suppliers free-text address parser uses internally —
    rather than that parser itself, since here the three parts already
    arrive as separate columns instead of one free-text address string.
    A province/city that isn't seeded yet (every country besides Pakistan
    currently has no states/cities seeded at all — see address_parsing.
    country_has_seeded_states's docstring) falls back to the free-text
    state_province_text/city_text columns, same convention used
    everywhere else a linked geography row might not exist. Shared by the
    POI and Contacts importers (both have Country/Province-State/City as
    their own separate CSV columns, unlike Organizations/Suppliers whose
    source files run everything together into one Address column)."""
    country_row = match_country(db, country_label)
    country_id = country_row["country_id"] if country_row else None
    state_row = match_state(db, country_id, state_label) if country_id else None
    state_id = state_row["state_id"] if state_row else None
    city_row = match_city(db, state_id, city_label) if state_id else None
    city_id = city_row["city_id"] if city_row else None
    return {
        "country_id": country_id,
        "state_id": state_id,
        "state_text": None if state_id else ((state_label or "").strip() or None),
        "city_id": city_id,
        "city_text": None if city_id else ((city_label or "").strip() or None),
    }


def _resolve_poi_type(db, label):
    """Look up a poi_types row by label (case-insensitive), creating one —
    with an auto-generated code, same convention as every other lookup
    table (see _resolve_supplier_type above) — the first time a new label
    is seen, so an import doesn't silently drop a POI whose Type isn't one
    of the ones already seeded. Table Maintenance > Points of Interest
    Group > POI Types is where it can be relabeled, merged, or reordered
    afterward."""
    label = (label or "").strip()
    if not label:
        return None
    row = db.execute(
        "SELECT poi_type_id FROM poi_types WHERE lower(label) = lower(?) AND tenant_id = ?",
        (label, g.tenant_id),
    ).fetchone()
    if row:
        return row["poi_type_id"]
    from seed_data import _slug

    code = _slug(label)
    db.execute(
        """INSERT INTO poi_types (tenant_id, code, label, sort_order, is_active)
           VALUES (?, ?, ?, (SELECT COALESCE(MAX(sort_order), -1) + 1 FROM poi_types WHERE tenant_id = ?), 1)""",
        (g.tenant_id, code, label, g.tenant_id),
    )
    db.commit()
    row = db.execute(
        "SELECT poi_type_id FROM poi_types WHERE code = ? AND tenant_id = ?", (code, g.tenant_id)
    ).fetchone()
    return row["poi_type_id"]


@data_exchange_bp.route("/import/points-of-interest", methods=["GET", "POST"])
@login_required
def import_pois():
    db = get_db()
    if request.method == "POST":
        file = request.files.get("file")
        if not file or not file.filename:
            flash("Please choose a CSV file.", "error")
            return render_template("data_exchange/import_pois_form.html")

        text = _decode_upload(file)
        reader = csv.DictReader(io.StringIO(text))
        rows = []
        for raw_row in reader:
            resolved = _resolve_poi_row(_normalize_row(raw_row))
            resolved.update(_resolve_geo_components(db, resolved.get("country", ""), resolved.get("state", ""), resolved.get("city", "")))
            rows.append(resolved)
        if not rows:
            flash("That CSV had no rows.", "error")
            return render_template("data_exchange/import_pois_form.html")

        db.execute(
            "INSERT INTO import_batches (tenant_id, source_type, target_module, file_name, status, row_count) VALUES (?, 'CSV', 'points_of_interest', ?, 'Staged', ?)",
            (g.tenant_id, file.filename, len(rows)),
        )
        db.commit()
        batch_id = db.execute("SELECT last_insert_rowid() id").fetchone()["id"]

        error_count = 0
        seen_names = set()
        for row in rows:
            name = (row.get("name") or "").strip()
            if not name:
                status, errors = "Invalid", "POI Name is required"
            elif name.lower() in seen_names:
                status, errors = "Invalid", "Duplicate POI name within this file — already staged from an earlier row"
            else:
                status, errors = "Valid", None
                seen_names.add(name.lower())
            if status == "Invalid":
                error_count += 1
            db.execute(
                "INSERT INTO import_staging_rows (tenant_id, batch_id, raw_data, validation_status, validation_errors) VALUES (?, ?, ?, ?, ?)",
                (g.tenant_id, batch_id, json.dumps(row), status, errors),
            )
        db.execute(
            "UPDATE import_batches SET error_count = ?, status = 'Validated' WHERE batch_id = ? AND tenant_id = ?",
            (error_count, batch_id, g.tenant_id),
        )
        db.commit()
        log_action("Import", "import_batch", batch_id, f"Staged {len(rows)} rows from {file.filename} ({error_count} invalid)")
        return redirect(url_for("data_exchange.review_batch", batch_id=batch_id))

    return render_template("data_exchange/import_pois_form.html")


# Header spellings this importer recognizes, mapped to the canonical field
# name the rest of this module (and batch_review.html) works with. Built
# against two different Outlook/Exchange-style exports with their own
# quirks: EmployeeBookBatch1a.CSV ("Phonea", a typo evidently meant as a
# plain "Phone"/office number, and "Phone2", a second number with no type
# of its own — same idea as Contacts' "Home Phone 2", see below — sitting
# where "Business Phone" usually would) and EmployeeBookBatch2a.CSV
# ("Phone1" instead of "Phonea", "Country/Region" instead of "Country",
# and — unlike Batch 1's "E-mail 2 Display Name", which is metadata with no
# address in it — an "E-mail 2" column that genuinely does carry a second
# email address on some rows).
EMPLOYEE_HEADER_ALIASES = {
    "title": ["title", "honorific"],
    "first_name": ["first name", "given name"],
    "middle_name": ["middle name"],
    "last_name": ["last name", "surname", "family name"],
    "full_name": ["full_name", "full name", "name"],
    "suffix": ["suffix"],
    "company": ["company", "company name"],
    "department": ["department", "dept"],
    "job_title": ["job title", "job_title", "position"],
    "street": ["street", "address", "street address"],
    "city": ["city"],
    "state": ["state", "province", "province/state"],
    "postal_code": ["postal code", "business postal code", "zip", "zip code"],
    "country": ["country", "country/region"],
    "fax": ["fax", "business fax"],
    "business_phone": ["phonea", "phone1", "phone 1", "phone", "business phone", "work phone", "office phone"],
    "phone_2": ["phone2", "phone 2", "business phone 2"],
    "home_phone": ["home phone"],
    "mobile_phone": ["mobile phone", "cell phone", "cell"],
    "email": ["e-mail address", "email address", "email", "e-mail"],
    "email_2": ["e-mail 2", "email 2"],
    "gender": ["gender", "sex"],
    "date_of_birth": ["date_of_birth", "date of birth", "dob", "birthday"],
    "notes": ["notes", "note"],
}


def _resolve_employee_row(row):
    """Pulls the canonical fields out of a normalized CSV row (see
    EMPLOYEE_HEADER_ALIASES), then derives the ones no single column maps
    to directly — mirrors _resolve_contact_row closely, since this is the
    same kind of Outlook/Exchange-style export, with two differences: a
    Middle Name column (folded straight into full_name, since the person
    form has no separate field for it either) and no dedicated home on the
    employees table for Title/Suffix/Gender/Company (contacts.py has
    title_id/suffix_id columns; employees does not) — those are kept as
    labeled lines in Notes instead, same treatment POI's importer gives an
    Email column that has nowhere else to go.

    - full_name: used as-is when the file has one; otherwise First +
      Middle + Last joined (skipping any that are blank).
    - phone_entries: one entry per non-blank Fax/Phone("Phonea")/Phone 2/
      Home Phone/Mobile Phone column, tagged with the employee_phone_types
      label it maps to. Only four phone types exist for Employees (Fax,
      Home, Mobile, Office) — "Phonea" and "Phone2" both become 'Office'
      entries (Phone 2 stacking as a second Office number), the same way
      Contacts' importer turns "Home Phone 2" into a second 'Home' entry.
    - email_list: the Email column plus a second "E-mail 2" column, when
      the source file has one and it actually carries a second address
      (as opposed to Batch 1's "E-mail 2 Display Name", which is metadata
      with nothing to import — see EMPLOYEE_HEADER_ALIASES).
    - match_keys: same same-person signature as Contacts (normalized
      email/phone) — a person's name is not a safe uniqueness key on its
      own (see _resolve_contact_row's docstring); used for duplicate
      detection instead of full_name alone, both within this file and
      against employees already on file (see _find_matching_employee)."""
    resolved = dict(row)
    for field, aliases in EMPLOYEE_HEADER_ALIASES.items():
        value = ""
        for alias in aliases:
            if row.get(alias):
                value = row[alias]
                break
        resolved[field] = value

    first = resolved.get("first_name", "").strip()
    middle = resolved.get("middle_name", "").strip()
    last = resolved.get("last_name", "").strip()
    full_name = resolved.get("full_name", "").strip()
    if not full_name:
        full_name = " ".join(p for p in (first, middle, last) if p)
    resolved["full_name"] = full_name

    notes_parts = []
    title = resolved.get("title", "").strip()
    if title:
        notes_parts.append(f"Title: {title}")
    suffix = resolved.get("suffix", "").strip()
    if suffix:
        notes_parts.append(f"Suffix: {suffix}")
    gender = resolved.get("gender", "").strip()
    if gender:
        notes_parts.append(f"Gender: {gender}")
    company = resolved.get("company", "").strip()
    if company:
        notes_parts.append(f"Company: {company}")
    source_notes = resolved.get("notes", "").strip()
    if source_notes:
        notes_parts.append(source_notes)
    resolved["notes"] = "\n".join(notes_parts)

    phone_entries = []
    for col, phone_type in (
        ("business_phone", "Office"), ("home_phone", "Home"),
        ("phone_2", "Office"), ("mobile_phone", "Mobile"),
        ("fax", "Fax"),
    ):
        raw = (resolved.get(col) or "").strip()
        for number in [n.strip() for n in raw.split(";") if n.strip()]:
            phone_entries.append({"number": number, "phone_type": phone_type})
    resolved["phone_entries"] = phone_entries

    email_list = []
    for col in ("email", "email_2"):
        raw = (resolved.get(col) or "").strip()
        email_list.extend(e.strip() for e in raw.split(";") if e.strip())
    resolved["email_list"] = email_list

    match_keys = set()
    for e in email_list:
        match_keys.add(f"email:{e.lower()}")
    for entry in phone_entries:
        digits = _normalize_phone_digits(entry["number"])
        if len(digits) >= 7:
            match_keys.add(f"phone:{digits}")
    resolved["match_keys"] = sorted(match_keys)
    return resolved


@data_exchange_bp.route("/import/employees", methods=["GET", "POST"])
@login_required
def import_employees():
    db = get_db()
    if request.method == "POST":
        file = request.files.get("file")
        if not file or not file.filename:
            flash("Please choose a CSV file.", "error")
            return render_template("data_exchange/import_employees_form.html")

        text = _decode_upload(file)
        reader = csv.DictReader(io.StringIO(text))
        rows = []
        for raw_row in reader:
            resolved = _resolve_employee_row(_normalize_row(raw_row))
            resolved["address"] = {
                "street": (resolved.get("street") or "").strip() or None,
                "postal_code": (resolved.get("postal_code") or "").strip() or None,
                **_resolve_geo_components(db, resolved.get("country", ""), resolved.get("state", ""), resolved.get("city", "")),
            }
            rows.append(resolved)
        if not rows:
            flash("That CSV had no rows.", "error")
            return render_template("data_exchange/import_employees_form.html")

        db.execute(
            "INSERT INTO import_batches (tenant_id, source_type, target_module, file_name, status, row_count) VALUES (?, 'CSV', 'employees', ?, 'Staged', ?)",
            (g.tenant_id, file.filename, len(rows)),
        )
        db.commit()
        batch_id = db.execute("SELECT last_insert_rowid() id").fetchone()["id"]

        error_count = 0
        # Same-name-within-file is not enough on its own to call two rows
        # duplicates (see _resolve_employee_row's docstring) — a match also
        # needs an overlapping phone/email, same rule Contacts uses.
        seen_by_name = {}
        for row in rows:
            full_name = (row.get("full_name") or "").strip()
            row_keys = set(row.get("match_keys") or [])
            if not full_name:
                status, errors = "Invalid", "A name is required (Full Name, or First/Last Name)"
            else:
                name_key = full_name.lower()
                prior_key_sets = seen_by_name.get(name_key, [])
                if row_keys and any(row_keys & prior for prior in prior_key_sets):
                    status, errors = "Invalid", "Duplicate employee within this file — same name and a matching phone/email as an earlier row"
                else:
                    status, errors = "Valid", None
                    seen_by_name.setdefault(name_key, []).append(row_keys)
            if status == "Invalid":
                error_count += 1
            db.execute(
                "INSERT INTO import_staging_rows (tenant_id, batch_id, raw_data, validation_status, validation_errors) VALUES (?, ?, ?, ?, ?)",
                (g.tenant_id, batch_id, json.dumps(row), status, errors),
            )
        db.execute(
            "UPDATE import_batches SET error_count = ?, status = 'Validated' WHERE batch_id = ? AND tenant_id = ?",
            (error_count, batch_id, g.tenant_id),
        )
        db.commit()
        log_action("Import", "import_batch", batch_id, f"Staged {len(rows)} rows from {file.filename} ({error_count} invalid)")
        return redirect(url_for("data_exchange.review_batch", batch_id=batch_id))

    return render_template("data_exchange/import_employees_form.html")


# Header spellings this importer recognizes, mapped to the canonical field
# name the rest of this module (and batch_review.html) works with. Built
# against TTMS_Service_Codes_Bible.csv -- the "Service Codes Bible" export
# that documents the Category/Group/Sub-Group taxonomy behind every Service
# Code (see blueprints/service_taxonomy_admin.py and schema.sql's MODULE I)
# in one flat, business-readable sheet: one row per Sub-Group, not per
# tenant Service -- the "Sequence (Last 4 Digits)" column is illustrative
# text ("0001, 0002, 0003...") rather than real data, since actual sequence
# numbers are assigned per-tenant when a Service is created against a
# Sub-Group (see blueprints/services.py's new_service()), not by this file.
SERVICE_TAXONOMY_HEADER_ALIASES = {
    "category_code": ["category code", "category_code", "cat code"],
    "category_name": ["category name", "category_name", "cat name"],
    "service_code": ["service code", "service_code", "code"],
    "description": ["description of code", "description", "desc"],
    "validity": ["validity", "validity status", "status"],
}

# Case-insensitive Validity column spellings this importer accepts, mapped
# to service_subgroups.validity_status's three CHECK-constrained values.
SERVICE_TAXONOMY_VALIDITY_ALIASES = {
    "active": "active",
    "deprecated": "deprecated",
    "retired": "deprecated",
    "under review": "under_review",
    "under_review": "under_review",
    "review": "under_review",
}


def _resolve_service_taxonomy_row(row):
    """Pulls Category Code/Name, the Category-Group-Subgroup-Sequence
    Service Code, the combined Description, and Validity out of a
    normalized CSV row (see SERVICE_TAXONOMY_HEADER_ALIASES), then derives
    what the taxonomy tables actually need but no single column spells out
    directly:

    - group_code / subgroup_code: the 2nd and 3rd '-'-separated segments
      of the Service Code (e.g. 'MN' and 'MC' out of 'TP-MN-MC-0001') --
      the 4th segment (the sequence number) is ignored, see this section's
      note above.
    - group_name / subgroup_name: the Bible's one shared "Description of
      Code" column reads as "<Category label> — <Group label>, <Subgroup
      label>" (e.g. 'Tour Package — Multi-Nation Tour, Multi-City POIs'),
      so the text after the em dash (or a plain hyphen, if that's what the
      file used) is split on its first comma into Group label / Subgroup
      label. This only matters for a code combination that doesn't already
      exist -- see _commit_service_taxonomy_rows below, which never
      renames an existing Category/Group/Sub-Group from this derived text,
      only a brand new one is created with it.
    - validity_status: mapped via SERVICE_TAXONOMY_VALIDITY_ALIASES;
      unrecognized text (anything not Active/Deprecated/Under Review, any
      case) is left as None so the staging step below can flag the row
      Invalid rather than silently default it to Active."""
    resolved = dict(row)
    for field, aliases in SERVICE_TAXONOMY_HEADER_ALIASES.items():
        value = ""
        for alias in aliases:
            if row.get(alias):
                value = row[alias]
                break
        resolved[field] = value

    category_code = resolved.get("category_code", "").strip().upper()
    category_name = resolved.get("category_name", "").strip()
    service_code = resolved.get("service_code", "").strip().upper()
    description = resolved.get("description", "").strip()
    validity_raw = resolved.get("validity", "").strip()

    parts = service_code.split("-")
    group_code = parts[1].strip() if len(parts) >= 3 else ""
    subgroup_code = parts[2].strip() if len(parts) >= 3 else ""

    desc_after_dash = description
    for dash in (" — ", " – ", " - "):
        if dash in description:
            desc_after_dash = description.split(dash, 1)[1]
            break
    if "," in desc_after_dash:
        group_name, subgroup_name = [p.strip() for p in desc_after_dash.split(",", 1)]
    else:
        group_name, subgroup_name = "", desc_after_dash.strip()

    resolved.update({
        "category_code": category_code,
        "category_name": category_name,
        "service_code": service_code,
        "group_code": group_code,
        "subgroup_code": subgroup_code,
        "group_name": group_name,
        "subgroup_name": subgroup_name,
        "description": description,
        "validity_raw": validity_raw,
        "validity_status": SERVICE_TAXONOMY_VALIDITY_ALIASES.get(validity_raw.lower()),
    })
    return resolved


@data_exchange_bp.route("/import/service-codes", methods=["GET", "POST"])
@login_required
def import_service_taxonomy():
    db = get_db()
    if request.method == "POST":
        file = request.files.get("file")
        if not file or not file.filename:
            flash("Please choose a CSV file.", "error")
            return render_template("data_exchange/import_service_taxonomy_form.html")

        text = _decode_upload(file)
        reader = csv.DictReader(io.StringIO(text))
        rows = [_resolve_service_taxonomy_row(_normalize_row(raw_row)) for raw_row in reader]
        if not rows:
            flash("That CSV had no rows.", "error")
            return render_template("data_exchange/import_service_taxonomy_form.html")

        db.execute(
            "INSERT INTO import_batches (tenant_id, source_type, target_module, file_name, status, row_count) VALUES (?, 'CSV', 'service_taxonomy', ?, 'Staged', ?)",
            (g.tenant_id, file.filename, len(rows)),
        )
        db.commit()
        batch_id = db.execute("SELECT last_insert_rowid() id").fetchone()["id"]

        error_count = 0
        seen_codes = set()
        for row in rows:
            errors = []
            if not row["category_code"]:
                errors.append("Category Code is required.")
            if len(row["service_code"].split("-")) < 4:
                errors.append("Service Code must be in Category-Group-Subgroup-Sequence form, e.g. 'TP-MN-MC-0001'.")
            elif row["service_code"].split("-")[0] != row["category_code"]:
                errors.append(f"Service Code's Category segment ('{row['service_code'].split('-')[0]}') doesn't match the Category Code column ('{row['category_code']}').")
            if not row["subgroup_name"]:
                errors.append("Could not derive a Sub-Group name from the Description of Code column.")
            if row["validity_status"] is None:
                errors.append(f"Unrecognized Validity value '{row['validity_raw']}' — expected Active, Under Review, or Deprecated.")

            code_key = f"{row['category_code']}-{row['group_code']}-{row['subgroup_code']}"
            if not errors and code_key in seen_codes:
                errors.append("Duplicate Service Code family within this file — already staged from an earlier row (harmless if re-uploading the same Bible; only the last one wins).")
            if not errors:
                seen_codes.add(code_key)

            status = "Valid" if not errors else "Invalid"
            if status == "Invalid":
                error_count += 1
            db.execute(
                "INSERT INTO import_staging_rows (tenant_id, batch_id, raw_data, validation_status, validation_errors) VALUES (?, ?, ?, ?, ?)",
                (g.tenant_id, batch_id, json.dumps(row), status, "; ".join(errors) or None),
            )
        db.execute(
            "UPDATE import_batches SET error_count = ?, status = 'Validated' WHERE batch_id = ? AND tenant_id = ?",
            (error_count, batch_id, g.tenant_id),
        )
        db.commit()
        log_action("Import", "import_batch", batch_id, f"Staged {len(rows)} rows from {file.filename} ({error_count} invalid)")
        return redirect(url_for("data_exchange.review_batch", batch_id=batch_id))

    return render_template("data_exchange/import_service_taxonomy_form.html")


def _resolve_service_catalog_row(db, row):
    """Same field extraction as _resolve_service_taxonomy_row (Category/
    Group/Sub-Group codes, Description, Validity out of the Bible CSV
    shape), plus a live lookup of the Sub-Group it names -- this importer
    creates rows in the tenant-scoped `services` catalog (Inventory
    Management > Services), which can only ever reference a Sub-Group that
    already exists in the GLOBAL taxonomy (see import_service_taxonomy
    above, the importer for THAT table). A CSV whose taxonomy hasn't been
    imported yet, or that names a retired code, resolves subgroup_id to
    None / a 'deprecated' validity_status here so the staging step below
    can reject it with a clear message instead of the database trigger
    silently doing it with a much less friendly one.

    If this row's own Description of Code column is blank, the Sub-Group's
    own (already-stored) description is used as a fallback -- covers a
    trimmed-down CSV that only lists Category/Group/Subgroup/Service Code/
    Validity with no description column of its own."""
    resolved = _resolve_service_taxonomy_row(row)
    subgroup = db.execute(
        """SELECT sg.subgroup_id, sg.subgroup_name, sg.validity_status, sg.description,
                  g.group_code, g.group_name, c.category_code, c.category_name
           FROM service_subgroups sg
           JOIN service_groups g ON g.group_id = sg.group_id
           JOIN service_categories c ON c.category_id = g.category_id
           WHERE upper(c.category_code) = ? AND upper(g.group_code) = ? AND upper(sg.subgroup_code) = ?""",
        (resolved["category_code"], resolved["group_code"], resolved["subgroup_code"]),
    ).fetchone()
    resolved["subgroup_id"] = subgroup["subgroup_id"] if subgroup else None
    resolved["subgroup_validity_status"] = subgroup["validity_status"] if subgroup else None
    if not (resolved.get("description") or "").strip() and subgroup:
        resolved["description"] = subgroup["description"] or ""
    return resolved


@data_exchange_bp.route("/import/service-catalog", methods=["GET", "POST"])
@login_required
def import_service_catalog():
    db = get_db()
    if request.method == "POST":
        file = request.files.get("file")
        if not file or not file.filename:
            flash("Please choose a CSV file.", "error")
            return render_template("data_exchange/import_service_catalog_form.html")

        text = _decode_upload(file)
        reader = csv.DictReader(io.StringIO(text))
        rows = [_resolve_service_catalog_row(db, _normalize_row(raw_row)) for raw_row in reader]
        if not rows:
            flash("That CSV had no rows.", "error")
            return render_template("data_exchange/import_service_catalog_form.html")

        db.execute(
            "INSERT INTO import_batches (tenant_id, source_type, target_module, file_name, status, row_count) VALUES (?, 'CSV', 'service_catalog', ?, 'Staged', ?)",
            (g.tenant_id, file.filename, len(rows)),
        )
        db.commit()
        batch_id = db.execute("SELECT last_insert_rowid() id").fetchone()["id"]

        error_count = 0
        seen_codes = set()
        for row in rows:
            errors = []
            code_key = f"{row['category_code']}-{row['group_code']}-{row['subgroup_code']}"
            if not row["category_code"] or len(row["service_code"].split("-")) < 4:
                errors.append("Service Code is required, in Category-Group-Subgroup-Sequence form, e.g. 'TP-MN-MC-0001'.")
            elif row["subgroup_id"] is None:
                errors.append(f"No matching Sub-Group '{code_key}' in Service Code Maintenance — import the Service Code taxonomy first, or check the spelling.")
            elif row["subgroup_validity_status"] == "deprecated":
                errors.append(f"'{code_key}' is Deprecated in Service Code Maintenance — a new Service can't be created against a retired code.")
            if not errors and not (row.get("description") or "").strip():
                errors.append("No Description available — fill in the Description of Code column, or add one to this Sub-Group in Service Code Maintenance first.")
            if not errors and code_key in seen_codes:
                errors.append("Duplicate Service Code family within this file — only the first occurrence is used.")
            if not errors:
                seen_codes.add(code_key)

            status = "Valid" if not errors else "Invalid"
            if status == "Invalid":
                error_count += 1
            db.execute(
                "INSERT INTO import_staging_rows (tenant_id, batch_id, raw_data, validation_status, validation_errors) VALUES (?, ?, ?, ?, ?)",
                (g.tenant_id, batch_id, json.dumps(row), status, "; ".join(errors) or None),
            )
        db.execute(
            "UPDATE import_batches SET error_count = ?, status = 'Validated' WHERE batch_id = ? AND tenant_id = ?",
            (error_count, batch_id, g.tenant_id),
        )
        db.commit()
        log_action("Import", "import_batch", batch_id, f"Staged {len(rows)} rows from {file.filename} ({error_count} invalid)")
        return redirect(url_for("data_exchange.review_batch", batch_id=batch_id))

    return render_template("data_exchange/import_service_catalog_form.html")


@data_exchange_bp.route("/import/batches/<int:batch_id>")
@login_required
def review_batch(batch_id):
    db = get_db()
    batch = db.execute(
        "SELECT * FROM import_batches WHERE batch_id = ? AND tenant_id = ?", (batch_id, g.tenant_id)
    ).fetchone()
    if batch is None:
        abort(404)
    rows = db.execute("SELECT * FROM import_staging_rows WHERE batch_id = ? ORDER BY staging_id", (batch_id,)).fetchall()
    parsed = [dict(r, parsed=json.loads(r["raw_data"])) for r in rows]
    return render_template("data_exchange/batch_review.html", batch=batch, rows=parsed)


def _find_matching_contact(db, full_name, match_keys):
    """Same-name-plus-shared-phone/email reuse check for the Contacts
    importer — see _resolve_contact_row's match_keys docstring for why a
    bare name match isn't used here the way Organizations/Suppliers/POI
    use one: a person's name just isn't a reliable identifier the way a
    company or landmark name usually is. Only a name match that ALSO
    shares a normalized phone or email with an existing contact is reused;
    a same-named contact with no overlapping contact info is left alone
    and a new contact is created instead (see _commit_contacts_rows). A
    contact staged/committed with no phone or email at all has no match_
    keys to compare, so it never matches anything, including its own
    earlier import — re-running the import for a phone/email-less row
    will add it again rather than silently skipping it, which is the
    safer default given the alternative is guessing from name alone."""
    if not match_keys:
        return None
    candidates = db.execute(
        "SELECT contact_id FROM contacts WHERE lower(full_name) = lower(?) AND tenant_id = ? AND is_deleted = 0",
        (full_name, g.tenant_id),
    ).fetchall()
    for cand in candidates:
        cand_keys = set()
        for row in db.execute("SELECT email_address FROM contact_emails WHERE contact_id = ?", (cand["contact_id"],)):
            cand_keys.add(f"email:{row['email_address'].strip().lower()}")
        for row in db.execute("SELECT number FROM contact_phones WHERE contact_id = ?", (cand["contact_id"],)):
            digits = _normalize_phone_digits(row["number"])
            if len(digits) >= 7:
                cand_keys.add(f"phone:{digits}")
        if match_keys & cand_keys:
            return cand["contact_id"]
    return None


def _find_matching_employee(db, full_name, match_keys):
    """Same-name-plus-shared-phone/email reuse check for the Employees
    importer, mirroring _find_matching_contact exactly — an employee's name
    is no more reliable a uniqueness key than a contact's (EmployeeBookBatch
    1a.CSV itself has three different Bhattis under three different first
    names, and nothing rules out two employees who happen to share a full
    name). Only a name match that also shares a normalized phone or email
    with an existing, non-deleted employee is reused; an employee with no
    phone/email at all has no match_keys, so it never matches anything —
    re-running the import for that one row adds it again rather than
    guessing from name alone."""
    if not match_keys:
        return None
    candidates = db.execute(
        "SELECT employee_id FROM employees WHERE lower(full_name) = lower(?) AND tenant_id = ? AND is_deleted = 0",
        (full_name, g.tenant_id),
    ).fetchall()
    for cand in candidates:
        cand_keys = set()
        for row in db.execute("SELECT email_address FROM employee_emails WHERE employee_id = ?", (cand["employee_id"],)):
            cand_keys.add(f"email:{row['email_address'].strip().lower()}")
        for row in db.execute("SELECT number FROM employee_phones WHERE employee_id = ?", (cand["employee_id"],)):
            digits = _normalize_phone_digits(row["number"])
            if len(digits) >= 7:
                cand_keys.add(f"phone:{digits}")
        if match_keys & cand_keys:
            return cand["employee_id"]
    return None


def _commit_contacts_rows(db, rows):
    """Commit staged contact rows. A row is reused rather than duplicated
    only when an existing contact shares BOTH its name and a phone/email
    (see _find_matching_contact) — so re-running this import doesn't pile
    up duplicates of what already committed cleanly, without ever
    collapsing two different people who happen to share a name into one
    contact. A brand-new contact gets: Title/Suffix (auto-creating a new
    one on an exact-match miss, same as every other importer-created
    lookup — see _resolve_or_create_lookup), its Organization (found by
    name or created bare per the request — see
    _resolve_contact_organization), one Business-tagged address built
    from Street/City/Province/Country/Postal Code (resolved against the
    geography lookups at staging time — see _resolve_geo_components —
    falling back to free text), every Business/Home/Home 2/Mobile/Fax
    phone number as its own contact_phones row (first one marked
    primary), every Email/E-mail 3 address as its own contact_emails row
    (first one marked primary), and a Category (only the first, since
    contacts has a single contact_category_id — the full original value
    is kept in Notes so a multi-category row's other tags aren't silently
    lost)."""
    committed = 0
    for r in rows:
        data = json.loads(r["raw_data"])
        full_name = (data.get("full_name") or "").strip()
        match_keys = set(data.get("match_keys") or [])

        existing_id = _find_matching_contact(db, full_name, match_keys)
        if existing_id:
            contact_id = existing_id
        else:
            title_id = _resolve_or_create_lookup(db, "contact_titles", "contact_title_id", data.get("title"))
            suffix_id = _resolve_or_create_lookup(db, "contact_suffixes", "contact_suffix_id", data.get("suffix"))
            org_id = _resolve_contact_organization(db, data.get("organization"))

            categories_raw = (data.get("categories") or "").strip()
            first_category = categories_raw.split(";")[0].strip() if categories_raw else ""
            category_id = _resolve_or_create_lookup(db, "contact_categories", "contact_category_id", first_category)

            notes_parts = []
            if categories_raw:
                notes_parts.append(f"Categories: {categories_raw}")
            source_notes = (data.get("notes") or "").strip()
            if source_notes:
                notes_parts.append(source_notes)

            db.execute(
                """INSERT INTO contacts
                   (tenant_id, title_id, full_name, suffix_id, file_as, current_organization_id,
                    current_job_title, contact_category_id, web_page, date_of_birth, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    g.tenant_id, title_id, full_name, suffix_id,
                    (data.get("file_as") or "").strip() or None, org_id,
                    (data.get("job_title") or "").strip() or None, category_id,
                    (data.get("web_page") or "").strip() or None, (data.get("date_of_birth") or "").strip() or None,
                    ("\n\n".join(notes_parts) or None),
                ),
            )
            contact_id = db.execute("SELECT last_insert_rowid() id").fetchone()["id"]

            for i, addr_value in enumerate(data.get("email_list") or []):
                db.execute(
                    "INSERT INTO contact_emails (tenant_id, contact_id, email_address, is_primary) VALUES (?, ?, ?, ?)",
                    (g.tenant_id, contact_id, addr_value, 1 if i == 0 else 0),
                )

            for i, entry in enumerate(data.get("phone_entries") or []):
                db.execute(
                    "INSERT INTO contact_phones (tenant_id, contact_id, phone_type, number, is_primary) VALUES (?, ?, ?, ?, ?)",
                    (g.tenant_id, contact_id, entry["phone_type"], entry["number"], 1 if i == 0 else 0),
                )

            addr = data.get("address") or {}
            if addr.get("street") or addr.get("postal_code") or addr.get("country_id") \
                    or addr.get("state_id") or addr.get("state_text") or addr.get("city_id") or addr.get("city_text"):
                db.execute(
                    """INSERT INTO addresses
                       (tenant_id, owner_type, owner_id, address_type, street, region_id, country_id,
                        state_id, state_province_text, city_id, city_text, postal_code, is_primary)
                       VALUES (?, 'Contact', ?, 'Business', ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
                    (
                        g.tenant_id, contact_id, addr.get("street"), addr.get("region_id"), addr.get("country_id"),
                        addr.get("state_id"), addr.get("state_text"), addr.get("city_id"), addr.get("city_text"),
                        addr.get("postal_code"),
                    ),
                )

        db.execute(
            "UPDATE import_staging_rows SET committed_entity_id = ? WHERE staging_id = ? AND tenant_id = ?",
            (contact_id, r["staging_id"], g.tenant_id),
        )
        committed += 1
    return committed


def _commit_organizations_rows(db, rows):
    """Commit staged organization rows. A name that already exists in the
    live organizations table (case-insensitive) is reused rather than
    duplicated — the staging row is still marked committed, pointing at
    the existing record, so review shows what happened instead of leaving
    it blank. Address/phone/email/website/notes, the structured
    organization_addresses row built from the parsed Address column (tagged
    "Mailing Address"), and structured organization_emails/
    organization_phones rows (one per ";"-separated value in the Email/
    Phone columns, phones tagged "Office") are only written on the INSERT
    path (a brand-new organization); an existing organization that's
    matched by name is left as-is rather than overwritten by the import,
    same as the name-matching behavior this always had."""
    committed = 0
    mailing_address_type_id = _lookup_id_by_label(db, "organization_address_types", "address_type_id", "Mailing Address")
    office_phone_type_id = _lookup_id_by_label(db, "organization_phone_types", "phone_type_id", "Office")
    for r in rows:
        data = json.loads(r["raw_data"])
        name = (data.get("name") or "").strip()

        existing = db.execute(
            "SELECT organization_id FROM organizations WHERE lower(organization_name) = lower(?) AND tenant_id = ?",
            (name, g.tenant_id),
        ).fetchone()
        if existing:
            org_id = existing["organization_id"]
        else:
            type_id = _resolve_organization_type(db, data.get("type"))
            db.execute(
                """INSERT INTO organizations
                   (tenant_id, organization_name, full_address, phone, email, website, organization_type_id, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    g.tenant_id, name,
                    (data.get("address") or "").strip() or None,
                    (data.get("phone") or "").strip() or None,
                    (data.get("email") or "").strip() or None,
                    (data.get("website") or "").strip() or None,
                    type_id,
                    (data.get("notes") or "").strip() or None,
                ),
            )
            org_id = db.execute("SELECT last_insert_rowid() id").fetchone()["id"]

            addr = data.get("address_parsed") or {}
            if addr.get("street") or addr.get("country_id") or addr.get("country_text") \
                    or addr.get("state_id") or addr.get("state_text") or addr.get("city_id") or addr.get("city_text"):
                db.execute(
                    """INSERT INTO organization_addresses
                       (tenant_id, organization_id, address_type_id, street, region_id, country_id,
                        state_id, state_province_text, city_id, city_text, postal_code, is_primary)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
                    (
                        g.tenant_id, org_id, mailing_address_type_id, addr.get("street") or None,
                        addr.get("region_id"), addr.get("country_id"),
                        addr.get("state_id"), (None if addr.get("state_id") else (addr.get("state_text") or None)),
                        addr.get("city_id"), (None if addr.get("city_id") else (addr.get("city_text") or None)),
                        addr.get("postal_code") or None,
                    ),
                )

            # Structured organization_emails/organization_phones rows, split
            # on ";" -- a source Email/Phone column can carry more than one
            # value (see _commit_contacts_rows above for the same pattern).
            # organizations.email/.phone (set above) keep the raw, un-split
            # text as the legacy audit trail; these are the new,
            # queryable/editable rows the Emails/Phones cards read from.
            email = (data.get("email") or "").strip()
            if email:
                for addr_value in [e.strip() for e in email.split(";") if e.strip()]:
                    db.execute(
                        "INSERT INTO organization_emails (tenant_id, organization_id, email_address, is_primary) VALUES (?, ?, ?, 1)",
                        (g.tenant_id, org_id, addr_value),
                    )

            phone = (data.get("phone") or "").strip()
            if phone:
                for num in [p.strip() for p in phone.split(";") if p.strip()]:
                    db.execute(
                        "INSERT INTO organization_phones (tenant_id, organization_id, phone_type_id, number, is_primary) VALUES (?, ?, ?, ?, 1)",
                        (g.tenant_id, org_id, office_phone_type_id, num),
                    )

        db.execute(
            "UPDATE import_staging_rows SET committed_entity_id = ? WHERE staging_id = ? AND tenant_id = ?",
            (org_id, r["staging_id"], g.tenant_id),
        )
        committed += 1
    return committed


def _commit_suppliers_rows(db, rows):
    """Commit staged supplier rows. A name that already exists in the live
    suppliers table (case-insensitive) is reused rather than duplicated —
    same behavior as organizations — and in that case no address/contact/
    phone is added, so re-running an import doesn't pile up duplicate
    addresses on a supplier that's already there. A brand-new supplier gets:
    the supplier row itself (Type and Sub-Type — auto-creating either on an
    exact-match miss, same as always, a Default from the import form
    filling in for a row that didn't name its own — plus Website and
    Notes); one supplier_addresses row (address_type "Main Office") built
    from the Address column (either a single free-text column split
    automatically, or already-separate City/Province/Country columns —
    see _resolve_supplier_structured_address), when there was any address
    data at all; and, when there's a named
    Primary Contact, an email, or any phone/fax number to attach, one
    supplier_contacts row (named after that Primary Contact when given,
    else a placeholder "Main Office" contact — some source rows only have
    a company phone line, no named person) holding every phone_entries
    item (see _build_phone_entries: the primary number, a second Cell
    number, and/or a Fax) as its own supplier_contact_phones row."""
    committed = 0
    main_office_address_type_id = _lookup_id_by_label(db, "supplier_address_types", "address_type_id", "Main Office")
    office_phone_type_id = _lookup_id_by_label(db, "phone_types", "phone_type_id", "Office")

    for r in rows:
        data = json.loads(r["raw_data"])
        name = (data.get("name") or "").strip()

        existing = db.execute(
            "SELECT supplier_id FROM suppliers WHERE lower(supplier_name) = lower(?) AND tenant_id = ? AND is_deleted = 0",
            (name, g.tenant_id),
        ).fetchone()
        if existing:
            supplier_id = existing["supplier_id"]
        else:
            type_id = _resolve_supplier_type(db, data.get("type"))
            subtype_id = _resolve_supplier_subtype(db, type_id, data.get("subtype"))
            db.execute(
                """INSERT INTO suppliers (tenant_id, supplier_name, supplier_type_id, supplier_subtype_id, web_page, notes)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    g.tenant_id, name, type_id, subtype_id,
                    (data.get("website") or "").strip() or None, (data.get("notes") or "").strip() or None,
                ),
            )
            supplier_id = db.execute("SELECT last_insert_rowid() id").fetchone()["id"]

            addr = data.get("address_parsed") or {}
            address_id = None
            has_address_data = (
                (data.get("address") or "").strip() or addr.get("city_id") or addr.get("city_text")
                or addr.get("state_id") or addr.get("state_text") or addr.get("country_id")
            )
            if has_address_data:
                db.execute(
                    """INSERT INTO supplier_addresses
                       (tenant_id, supplier_id, address_type_id, street, region_id, country_id,
                        state_id, state_province_text, city_id, city_text, postal_code, is_primary)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
                    (
                        g.tenant_id, supplier_id, main_office_address_type_id, addr.get("street") or None,
                        addr.get("region_id"), addr.get("country_id"),
                        addr.get("state_id"), (None if addr.get("state_id") else (addr.get("state_text") or None)),
                        addr.get("city_id"), (None if addr.get("city_id") else (addr.get("city_text") or None)),
                        addr.get("postal_code") or None,
                    ),
                )
                address_id = db.execute("SELECT last_insert_rowid() id").fetchone()["id"]

            contact_name = (data.get("contact_name") or "").strip()
            contact_title = (data.get("contact_title") or "").strip()
            email = (data.get("email") or "").strip()
            phone_entries = data.get("phone_entries") or []

            if contact_name or email or phone_entries:
                is_placeholder = not contact_name
                db.execute(
                    """INSERT INTO supplier_contacts (tenant_id, supplier_id, name, title, supplier_address_id, email, notes)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        g.tenant_id, supplier_id, contact_name or "Main Office", contact_title or None, address_id,
                        email or None,
                        (
                            "Auto-created during CSV import to hold the supplier's phone/fax/email — "
                            "rename to an actual named contact once known."
                        ) if is_placeholder else None,
                    ),
                )
                contact_id = db.execute("SELECT last_insert_rowid() id").fetchone()["id"]

                calling_code = _phone_calling_code(db, addr.get("country_id"))
                for i, entry in enumerate(phone_entries):
                    area_code, number = _split_phone(entry.get("raw", ""), calling_code)
                    if not number:
                        continue
                    phone_type_id = _lookup_id_by_label(db, "phone_types", "phone_type_id", entry.get("type_label", "")) or office_phone_type_id
                    db.execute(
                        """INSERT INTO supplier_contact_phones
                           (tenant_id, supplier_contact_id, phone_type_id, country_code, area_code, number, extension, is_primary)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            g.tenant_id, contact_id, phone_type_id, calling_code,
                            area_code or None, number, entry.get("extension") or None, 1 if i == 0 else 0,
                        ),
                    )

        db.execute(
            "UPDATE import_staging_rows SET committed_entity_id = ? WHERE staging_id = ? AND tenant_id = ?",
            (supplier_id, r["staging_id"], g.tenant_id),
        )
        committed += 1
    return committed


def _commit_poi_rows(db, rows):
    """Commit staged POI rows. A name that already exists in the live
    points_of_interest table (case-insensitive, tenant-scoped, not deleted)
    is reused rather than duplicated — same "match by name, skip
    re-import" behavior as Organizations/Suppliers — so re-running this
    import after fixing a few rows doesn't pile up duplicates of what
    already committed cleanly. A brand-new POI gets: the POI Type
    (auto-creating a new POI Type on an exact-match miss — see
    _resolve_poi_type), Country/Province-State/City as resolved at
    staging time by _resolve_geo_components (a linked row where the geography
    lookups have one, free text otherwise), the Address column mapped to
    local_location (there's no separate street-level address table on
    POI), Phone/Website as given, the CSV's descriptive Notes column
    mapped to historical_significance (see _resolve_poi_row), and — since
    points_of_interest has no dedicated email column — an Email captured
    into notes as "Email: ...", the one field left over for it."""
    committed = 0
    for r in rows:
        data = json.loads(r["raw_data"])
        name = (data.get("name") or "").strip()

        existing = db.execute(
            "SELECT poi_id FROM points_of_interest WHERE lower(name) = lower(?) AND tenant_id = ? AND is_deleted = 0",
            (name, g.tenant_id),
        ).fetchone()
        if existing:
            poi_id = existing["poi_id"]
        else:
            type_id = _resolve_poi_type(db, data.get("type"))
            email = (data.get("email") or "").strip()
            db.execute(
                """INSERT INTO points_of_interest
                   (tenant_id, name, poi_type_id, country_id, state_id, state_province_text,
                    city_id, city_text, local_location, phone, website, historical_significance, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    g.tenant_id, name, type_id,
                    data.get("country_id"), data.get("state_id"), data.get("state_text"),
                    data.get("city_id"), data.get("city_text"),
                    (data.get("address") or "").strip() or None,
                    (data.get("phone") or "").strip() or None,
                    (data.get("website") or "").strip() or None,
                    (data.get("notes") or "").strip() or None,
                    (f"Email: {email}" if email else None),
                ),
            )
            poi_id = db.execute("SELECT last_insert_rowid() id").fetchone()["id"]

        db.execute(
            "UPDATE import_staging_rows SET committed_entity_id = ? WHERE staging_id = ? AND tenant_id = ?",
            (poi_id, r["staging_id"], g.tenant_id),
        )
        committed += 1
    return committed


def _commit_employee_rows(db, rows):
    """Commit staged employee rows. An employee is reused rather than
    duplicated only when an existing, non-deleted employee shares BOTH its
    name and a phone/email (see _find_matching_employee) — same rule
    Contacts uses, for the same reason (a name alone isn't a safe match).
    A brand-new employee gets: Department/Job Title (auto-creating a new
    one on an exact-match miss, same convention as every other importer-
    created lookup — see _resolve_or_create_lookup), Title/Suffix/Gender/
    Company folded into Notes (employees has no columns of its own for
    any of those — see _resolve_employee_row), one Home-tagged address
    built from Street/City/Province/Country/Postal Code (resolved against
    the geography lookups at staging time, falling back to free text —
    same as the Address field on the employee's own address form), every
    Fax/Phone("Phonea")/Phone 2/Home/Mobile number as its own
    employee_phones row tagged with its employee_phone_types id
    (auto-created on a miss, though the four this importer ever produces —
    Fax/Home/Mobile/Office — are seeded by default), and the Email column
    as one employee_emails row."""
    committed = 0
    for r in rows:
        data = json.loads(r["raw_data"])
        full_name = (data.get("full_name") or "").strip()
        match_keys = set(data.get("match_keys") or [])

        existing_id = _find_matching_employee(db, full_name, match_keys)
        if existing_id:
            employee_id = existing_id
        else:
            department_id = _resolve_or_create_lookup(db, "departments", "department_id", data.get("department"))
            job_title_id = _resolve_or_create_lookup(db, "job_titles", "job_title_id", data.get("job_title"))

            db.execute(
                """INSERT INTO employees (tenant_id, full_name, department_id, job_title_id, notes)
                   VALUES (?, ?, ?, ?, ?)""",
                (g.tenant_id, full_name, department_id, job_title_id, (data.get("notes") or "").strip() or None),
            )
            employee_id = db.execute("SELECT last_insert_rowid() id").fetchone()["id"]

            for i, addr_value in enumerate(data.get("email_list") or []):
                db.execute(
                    "INSERT INTO employee_emails (tenant_id, employee_id, email_address, is_primary) VALUES (?, ?, ?, ?)",
                    (g.tenant_id, employee_id, addr_value, 1 if i == 0 else 0),
                )

            for i, entry in enumerate(data.get("phone_entries") or []):
                phone_type_id = _resolve_or_create_lookup(db, "employee_phone_types", "phone_type_id", entry["phone_type"])
                db.execute(
                    "INSERT INTO employee_phones (tenant_id, employee_id, phone_type_id, number, is_primary) VALUES (?, ?, ?, ?, ?)",
                    (g.tenant_id, employee_id, phone_type_id, entry["number"], 1 if i == 0 else 0),
                )

            addr = data.get("address") or {}
            if addr.get("street") or addr.get("postal_code") or addr.get("country_id") \
                    or addr.get("state_id") or addr.get("state_text") or addr.get("city_id") or addr.get("city_text"):
                db.execute(
                    """INSERT INTO addresses
                       (tenant_id, owner_type, owner_id, address_type, street, region_id, country_id,
                        state_id, state_province_text, city_id, city_text, postal_code, is_primary)
                       VALUES (?, 'Employee', ?, 'Home', ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
                    (
                        g.tenant_id, employee_id, addr.get("street"), addr.get("region_id"), addr.get("country_id"),
                        addr.get("state_id"), addr.get("state_text"), addr.get("city_id"), addr.get("city_text"),
                        addr.get("postal_code"),
                    ),
                )

        db.execute(
            "UPDATE import_staging_rows SET committed_entity_id = ? WHERE staging_id = ? AND tenant_id = ?",
            (employee_id, r["staging_id"], g.tenant_id),
        )
        committed += 1
    return committed


def _find_or_create_service_category(db, code, name):
    """Find-or-create by Category Code (case-insensitive) in the GLOBAL
    service_categories table -- no tenant_id filter, unlike every other
    find-or-create helper in this module, since this table has no
    tenant_id column at all (schema.sql's MODULE I design note: shared
    taxonomy, not each tenant's private list). A code that already exists
    is reused as-is -- its Name is never overwritten from the CSV, so a
    rename made through Service Code Maintenance survives a re-import of
    the same Bible file."""
    row = db.execute("SELECT category_id FROM service_categories WHERE upper(category_code) = ?", (code,)).fetchone()
    if row:
        return row["category_id"]
    db.execute(
        "INSERT INTO service_categories (category_code, category_name, is_active) VALUES (?, ?, 1)",
        (code, name or code),
    )
    return db.execute("SELECT last_insert_rowid() id").fetchone()["id"]


def _find_or_create_service_group(db, category_id, code, name):
    """Find-or-create by (Category, Group Code) in the GLOBAL
    service_groups table -- see _find_or_create_service_category's
    docstring for why there's no tenant_id involved, and why an existing
    row's Name is left untouched."""
    row = db.execute(
        "SELECT group_id FROM service_groups WHERE category_id = ? AND upper(group_code) = ?", (category_id, code)
    ).fetchone()
    if row:
        return row["group_id"]
    db.execute(
        "INSERT INTO service_groups (category_id, group_code, group_name, is_active) VALUES (?, ?, ?, 1)",
        (category_id, code, name or code),
    )
    return db.execute("SELECT last_insert_rowid() id").fetchone()["id"]


def _commit_service_taxonomy_rows(db, rows):
    """Commit staged Service Codes Bible rows into the GLOBAL
    service_categories/service_groups/service_subgroups tables (see
    schema.sql's MODULE I and blueprints/service_taxonomy_admin.py).

    Each row centers on one Sub-Group. A Category/Group/Sub-Group Code
    that already exists (matched case-insensitively) is reused, never
    duplicated -- re-uploading the same Bible file after it was already
    committed is a safe no-op for anything that already matches. For an
    EXISTING Sub-Group, only Description and Validity are refreshed from
    the file (the whole point of a re-import is keeping those in sync with
    the Bible) -- Name is deliberately left alone, matching Service Code
    Maintenance's own "Code is immutable, renames go through the Edit
    screen, not a bulk reimport" rule (see service_taxonomy_admin.py's
    module docstring). A brand-new Sub-Group (and any brand-new Category/
    Group it needs) is created outright, with Group/Sub-Group Name and
    Description as derived by _resolve_service_taxonomy_row.

    committed_entity_id is set to the Sub-Group's id -- the one row each
    staged record actually centers on -- so Review shows a link straight
    to it in Service Code Maintenance."""
    committed = 0
    for r in rows:
        data = json.loads(r["raw_data"])
        category_id = _find_or_create_service_category(db, data["category_code"], data["category_name"])
        group_id = _find_or_create_service_group(db, category_id, data["group_code"], data["group_name"])

        existing = db.execute(
            "SELECT subgroup_id FROM service_subgroups WHERE group_id = ? AND upper(subgroup_code) = ?",
            (group_id, data["subgroup_code"]),
        ).fetchone()
        if existing:
            subgroup_id = existing["subgroup_id"]
            db.execute(
                "UPDATE service_subgroups SET description = ?, validity_status = ? WHERE subgroup_id = ?",
                (data["description"] or None, data["validity_status"], subgroup_id),
            )
        else:
            db.execute(
                """INSERT INTO service_subgroups (group_id, subgroup_code, subgroup_name, description, validity_status, is_active)
                   VALUES (?, ?, ?, ?, ?, 1)""",
                (group_id, data["subgroup_code"], data["subgroup_name"], data["description"] or None, data["validity_status"]),
            )
            subgroup_id = db.execute("SELECT last_insert_rowid() id").fetchone()["id"]

        db.execute(
            "UPDATE import_staging_rows SET committed_entity_id = ? WHERE staging_id = ? AND tenant_id = ?",
            (subgroup_id, r["staging_id"], g.tenant_id),
        )
        committed += 1
    return committed


def _commit_service_catalog_rows(db, rows):
    """Commit staged rows into the tenant-scoped `services` catalog
    (Inventory Management > Services) -- the counterpart to
    _commit_service_taxonomy_rows above, which populates the GLOBAL
    Category/Group/Sub-Group tables this one reads from but never writes
    to. Distinguishing these two as separate importers (rather than one
    that does both) came directly out of confusion the first version
    caused: uploading the Bible CSV through the taxonomy importer alone
    populates Service Code Maintenance, not the Services list -- a person
    expecting the latter needs this second importer instead, run against
    the very same file.

    A tenant that already has a Service against a given Sub-Group (any
    status, oldest sequence number first) has that Sub-Group's row reused,
    not duplicated -- same "match first, only create on a genuine miss"
    rule every importer in this module follows, and it means re-running
    this import after a person has already started curating their catalog
    never clutters it with a second generic entry alongside their own. A
    Sub-Group with no existing Service gets exactly one created: sequence
    number 1 (or the tenant's next free one, mirroring new_service()'s own
    numbering), Description from the row (falling back to the Sub-Group's
    own description -- see _resolve_service_catalog_row), and
    status='draft' -- these are bulk-generated starting points, not
    reviewed/priced entries, so they're deliberately not born 'active'."""
    committed = 0
    for r in rows:
        data = json.loads(r["raw_data"])
        subgroup_id = data["subgroup_id"]

        existing = db.execute(
            "SELECT service_id FROM services WHERE tenant_id = ? AND subgroup_id = ? ORDER BY sequence_number LIMIT 1",
            (g.tenant_id, subgroup_id),
        ).fetchone()
        if existing:
            service_id = existing["service_id"]
        else:
            next_seq = db.execute(
                "SELECT COALESCE(MAX(sequence_number), 0) + 1 AS n FROM services WHERE tenant_id = ? AND subgroup_id = ?",
                (g.tenant_id, subgroup_id),
            ).fetchone()["n"]
            service_code = f"{data['category_code']}-{data['group_code']}-{data['subgroup_code']}-{next_seq:04d}"
            db.execute(
                """INSERT INTO services (tenant_id, subgroup_id, sequence_number, service_code, description, status)
                   VALUES (?, ?, ?, ?, ?, 'draft')""",
                (g.tenant_id, subgroup_id, next_seq, service_code, (data.get("description") or "").strip()),
            )
            service_id = db.execute("SELECT last_insert_rowid() id").fetchone()["id"]

        db.execute(
            "UPDATE import_staging_rows SET committed_entity_id = ? WHERE staging_id = ? AND tenant_id = ?",
            (service_id, r["staging_id"], g.tenant_id),
        )
        committed += 1
    return committed


COMMIT_HANDLERS = {
    "contacts": (_commit_contacts_rows, "contact(s)"),
    "organizations": (_commit_organizations_rows, "organization(s)"),
    "suppliers": (_commit_suppliers_rows, "supplier(s)"),
    "points_of_interest": (_commit_poi_rows, "point(s) of interest"),
    "employees": (_commit_employee_rows, "employee(s)"),
    "service_taxonomy": (_commit_service_taxonomy_rows, "taxonomy entrie(s)"),
    "service_catalog": (_commit_service_catalog_rows, "service(s)"),
}


@data_exchange_bp.route("/import/batches/<int:batch_id>/commit", methods=["POST"])
@login_required
def commit_batch(batch_id):
    db = get_db()
    batch = db.execute(
        "SELECT * FROM import_batches WHERE batch_id = ? AND tenant_id = ?", (batch_id, g.tenant_id)
    ).fetchone()
    if batch is None:
        abort(404)
    if batch["status"] == "Committed":
        flash("This batch was already committed.", "error")
        return redirect(url_for("data_exchange.review_batch", batch_id=batch_id))

    handler, noun = COMMIT_HANDLERS.get(batch["target_module"], (None, None))
    if handler is None:
        abort(400)

    rows = db.execute(
        "SELECT * FROM import_staging_rows WHERE batch_id = ? AND validation_status = 'Valid' AND committed_entity_id IS NULL",
        (batch_id,),
    ).fetchall()

    committed = handler(db, rows)

    db.execute(
        "UPDATE import_batches SET status = 'Committed' WHERE batch_id = ? AND tenant_id = ?", (batch_id, g.tenant_id)
    )
    db.commit()
    log_action("Import", "import_batch", batch_id, f"Committed {committed} rows into {batch['target_module']}")
    flash(f"Committed {committed} {noun}.", "success")
    return redirect(url_for("data_exchange.review_batch", batch_id=batch_id))


@data_exchange_bp.route("/import/batches/<int:batch_id>/reject", methods=["POST"])
@login_required
def reject_batch(batch_id):
    db = get_db()
    db.execute(
        "UPDATE import_batches SET status = 'Rejected' WHERE batch_id = ? AND tenant_id = ?", (batch_id, g.tenant_id)
    )
    db.commit()
    log_action("Import", "import_batch", batch_id, "Batch rejected")
    flash("Batch rejected — nothing was imported.", "success")
    return redirect(url_for("data_exchange.index"))
