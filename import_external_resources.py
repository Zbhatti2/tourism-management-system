"""
Bulk import: creates External Resources (suppliers.is_external_resource=1)
from a "Non-Employee Resources Contact" CSV export — an address-book style
directory of individual people (Outlook/Exchange contact export shape:
Title, First Name, Last Name, Company, Job Title, Street, City, State,
Postal Code, Country, Fax, Phone 1, Mobile Phone, E-mail 1, E-mail 2, Web
Page), not the vendor-directory shape blueprints/data_exchange.py's
`import_suppliers()` staging workflow was built for (Type/Name/Address/
Phone columns, one phone/fax per row). Built for the request:

    "Please import this Seed data in attached file
    'Non-Employee-Resources-Contact-Batch1a.CSV' to the 'External
    Resources' Table. Some Contacts have multiple Phones and eMails."

— run directly against the redesigned External Resource model
(migrate_add_supplier_resource_contact_info.py): one suppliers row per
person (Title+First+Last -> supplier_name, Company -> company_name, Job
Title folded into notes, same "Caption: value" convention
_resolve_supplier_row already uses for License No./Valid Until), one
billing address (Street/City/State/Postal Code/Country -- geography
resolved against the GLOBAL countries/states/cities lookups the same way
blueprints/data_exchange.py's structured-geo Suppliers importer does, via
address_parsing.match_country/match_state/match_city/match_city_anywhere;
free-text fallback columns are used whenever nothing matches, so nothing
from the source file is silently dropped), and one supplier_phones/
supplier_emails row per non-blank Fax/Phone 1/Mobile Phone and E-mail 1/
E-mail 2 column (a contact can have up to three phones and two emails,
per the request), tagged Fax/Office/Cell using the tenant's existing
phone_types lookup (already seeded — see seed_data.SUPPLIER_PHONE_TYPES).
A non-blank Web Page becomes a supplier_reference_links row (there's no
web_page field on the dedicated External Resource form/view — see
migrate_add_supplier_resource_contact_info.py — so, per that redesign,
this goes in Reference Links instead, same as a manually-added one would).

Safe to re-run against the same file (or a later batch that repeats a
row): a row is skipped, not re-inserted, when an External Resource with
the same supplier_name + company_name (case-insensitive) already exists
for the tenant — see _already_imported() below. Exact duplicate rows
*within* one file (this source file has one — "Munir Hussain", 55 Webro
Road — appearing twice back to back) are likewise only imported once.

Usage:
    flask --app app import-external-resources HERITAGE path/to/file.csv
or directly:
    python import_external_resources.py HERITAGE path/to/file.csv
"""
import csv
import re
import sqlite3
import sys

from address_parsing import match_city, match_city_anywhere, match_country, match_state
from config import Config

# CSV column positions (0-based) — this source file's header row is
# ['', 'First Name', 'Last Name', 'Company', 'Job Title', 'Street', 'City',
#  'State', 'Postal Code', 'Country', 'Fax', 'Phone 1', 'Mobile Phone',
#  'E-mail  1', 'E-mail 2  ', 'Web Page', ''] — read positionally rather
# than by header name because the first and last columns both have a blank
# header (an Outlook "Title" column and a trailing empty column), which
# would collide under the same '' key with csv.DictReader.
COL_TITLE, COL_FIRST, COL_LAST, COL_COMPANY, COL_JOB_TITLE = 0, 1, 2, 3, 4
COL_STREET, COL_CITY, COL_STATE, COL_POSTAL, COL_COUNTRY = 5, 6, 7, 8, 9
COL_FAX, COL_PHONE1, COL_MOBILE, COL_EMAIL1, COL_EMAIL2, COL_WEB = 10, 11, 12, 13, 14, 15


def _clean(value):
    value = (value or "").strip()
    # A handful of source rows have a stray trailing comma baked into a
    # quoted field (e.g. Company "Ahmed GmbH,", "High Style Fashion
    # GmbH,") — an artifact of the original export, not part of the name.
    if value.endswith(","):
        value = value[:-1].strip()
    return value


def _normalize_website(url):
    url = (url or "").strip()
    if not url:
        return ""
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url
    return url


def _resolve_address(db, country_text, state_text, city_text):
    """Same structured Country/Province/City resolution
    blueprints/data_exchange.py's _resolve_supplier_structured_address uses
    for a Suppliers CSV that gives City/Province/Country as their own
    columns (rather than one free-text Address blob) — matched against the
    GLOBAL countries/states/cities lookups, self-correcting a City whose
    given Province doesn't actually match it, and falling back to the raw
    text on anything that doesn't resolve so nothing is silently dropped.
    Reimplemented here (rather than imported from blueprints.data_exchange)
    to keep this standalone import script's only dependency on the app's
    web layer at zero."""
    country_text = (country_text or "").strip()
    state_text = (state_text or "").strip()
    city_text = (city_text or "").strip()
    result = {
        "region_id": None, "country_id": None, "country_text": country_text,
        "state_id": None, "state_text": state_text,
        "city_id": None, "city_text": city_text,
    }
    country_row = match_country(db, country_text) if country_text else None
    if country_row:
        result["country_id"] = country_row["country_id"]
        result["region_id"] = country_row["region_id"]

    state_row = match_state(db, result["country_id"], state_text) if state_text else None
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


def _phone_type_id(db, tenant_id, label):
    row = db.execute(
        "SELECT phone_type_id FROM phone_types WHERE tenant_id = ? AND lower(label) = lower(?)", (tenant_id, label)
    ).fetchone()
    return row["phone_type_id"] if row else None


def _already_imported(db, tenant_id, supplier_name, company_name):
    row = db.execute(
        """SELECT supplier_id FROM suppliers
           WHERE tenant_id = ? AND is_deleted = 0 AND is_external_resource = 1
                 AND lower(supplier_name) = lower(?) AND lower(COALESCE(company_name, '')) = lower(COALESCE(?, ''))""",
        (tenant_id, supplier_name, company_name),
    ).fetchone()
    return row is not None


def import_csv(csv_path, tenant_code):
    db = sqlite3.connect(Config.DATABASE_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    try:
        tenant = db.execute("SELECT tenant_id FROM tenants WHERE tenant_code = ?", (tenant_code,)).fetchone()
        if tenant is None:
            raise SystemExit(f"No tenant found with tenant_code={tenant_code!r}.")
        tenant_id = tenant["tenant_id"]

        with open(csv_path, newline="", encoding="utf-8-sig", errors="replace") as f:
            rows = list(csv.reader(f))
        if not rows:
            print("Empty file — nothing to import.")
            return
        data_rows = rows[1:]  # rows[0] is the header

        created = skipped_blank = skipped_dupe_in_file = skipped_existing = 0
        seen_in_file = set()

        for raw in data_rows:
            # Pad short rows (a trailing blank column can be dropped by
            # some CSV writers) so positional access below never IndexErrors.
            raw = raw + [""] * (max(COL_WEB, COL_COUNTRY) + 1 - len(raw))
            if not any(c.strip() for c in raw):
                continue

            key = tuple(c.strip() for c in raw)
            if key in seen_in_file:
                skipped_dupe_in_file += 1
                continue
            seen_in_file.add(key)

            title = _clean(raw[COL_TITLE])
            first = _clean(raw[COL_FIRST])
            last = _clean(raw[COL_LAST])
            company = _clean(raw[COL_COMPANY]) or None
            job_title = _clean(raw[COL_JOB_TITLE])
            street = _clean(raw[COL_STREET])
            city = _clean(raw[COL_CITY])
            state = _clean(raw[COL_STATE])
            postal = _clean(raw[COL_POSTAL])
            country = _clean(raw[COL_COUNTRY])
            fax = _clean(raw[COL_FAX])
            phone1 = _clean(raw[COL_PHONE1])
            mobile = _clean(raw[COL_MOBILE])
            email1 = _clean(raw[COL_EMAIL1])
            email2 = _clean(raw[COL_EMAIL2])
            webpage = _clean(raw[COL_WEB])

            supplier_name = re.sub(r"\s+", " ", " ".join(p for p in (title, first, last) if p)).strip()
            if not supplier_name:
                skipped_blank += 1
                continue

            if _already_imported(db, tenant_id, supplier_name, company):
                skipped_existing += 1
                continue

            notes = f"Job Title: {job_title}" if job_title else None

            db.execute(
                """INSERT INTO suppliers (tenant_id, supplier_name, is_external_resource, company_name, notes)
                   VALUES (?, ?, 1, ?, ?)""",
                (tenant_id, supplier_name, company, notes),
            )
            supplier_id = db.execute("SELECT last_insert_rowid() id").fetchone()["id"]

            if street or city or state or postal or country:
                addr = _resolve_address(db, country, state, city)
                db.execute(
                    """INSERT INTO supplier_addresses
                       (tenant_id, supplier_id, street, region_id, country_id, state_id, state_province_text,
                        city_id, city_text, postal_code, is_primary)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
                    (
                        tenant_id, supplier_id, street or None, addr["region_id"], addr["country_id"],
                        addr["state_id"], addr["state_text"] or None, addr["city_id"], addr["city_text"] or None,
                        postal or None,
                    ),
                )

            phone_entries = [
                (phone1, "Office"),
                (mobile, "Cell"),
                (fax, "Fax"),
            ]
            first_phone = True
            for number, type_label in phone_entries:
                if not number:
                    continue
                db.execute(
                    """INSERT INTO supplier_phones (tenant_id, supplier_id, phone_type_id, number, is_primary)
                       VALUES (?, ?, ?, ?, ?)""",
                    (tenant_id, supplier_id, _phone_type_id(db, tenant_id, type_label), number, 1 if first_phone else 0),
                )
                first_phone = False

            first_email = True
            for email in (email1, email2):
                if not email:
                    continue
                db.execute(
                    "INSERT INTO supplier_emails (tenant_id, supplier_id, email_address, is_primary) VALUES (?, ?, ?, ?)",
                    (tenant_id, supplier_id, email, 1 if first_email else 0),
                )
                first_email = False

            if webpage:
                db.execute(
                    "INSERT INTO supplier_reference_links (tenant_id, supplier_id, url, description) VALUES (?, ?, ?, ?)",
                    (tenant_id, supplier_id, _normalize_website(webpage), "Web Page"),
                )

            db.commit()
            created += 1

        print(
            f"Imported {created} External Resource(s) from {csv_path}. "
            f"Skipped: {skipped_existing} already imported, {skipped_dupe_in_file} duplicate row(s) within the file, "
            f"{skipped_blank} blank row(s)."
        )
    finally:
        db.close()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("Usage: python import_external_resources.py <TENANT_CODE> <path/to/file.csv>")
    import_csv(sys.argv[2], sys.argv[1])
