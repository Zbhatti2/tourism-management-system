"""
Migration: structures the Organizations module's free-text "Address" field
into Street/City/Province/Country, and lets an organization have more than
one address — safe to re-run, and does NOT touch any existing data it has
already migrated (it only ever adds rows/tables, never drops or overwrites).

Organizations keeps its own, separate address-type lookup and address table
from Suppliers at every level, per the standing rule that the two modules
share no type/sub-type/address/contact/phone data — see schema.sql's
"MODULE A" and "MODULE C" comments.

What this does, in order:

  1. Renames the address_types table (Suppliers' address-type lookup) to
     supplier_address_types, if it's still present under its old name —
     freeing up "address_types" as a name and making room for Organizations'
     own, separate organization_address_types below. A database already
     migrated past this point (address_types already renamed, e.g. by a
     fresh install off the current schema.sql) is left alone. This uses
     SQLite's ALTER TABLE RENAME, which updates every dependent foreign key
     reference automatically (e.g. supplier_addresses.address_type_id) —
     no data is touched, only the table's name.

  2. Creates the organization_address_types lookup + index, and the
     organization_addresses table + indexes, if they don't already exist —
     matching schema.sql exactly.

  3. For every existing tenant: seeds Organization Address Types (Mailing
     Address, Physical Address) via seed_data._seed_simple(). Idempotent —
     INSERT OR IGNORE keyed on (tenant_id, code).

  4. Backfills one organization_addresses row (address_type "Mailing
     Address", is_primary) for every existing organization that has a
     non-blank full_address AND doesn't already have an organization_
     addresses row — so this step is itself idempotent and safe to re-run,
     including against a database this migration has already been run
     against once (nothing double-inserts). The free text is parsed via
     address_parsing.parse_free_text_address, matching City/Province/
     Country against the existing geography lookups exactly like the
     Organizations CSV importer does; any phone number(s) embedded in the
     text (e.g. "..., Columbus, Ohio 43210 United States of America,
     (419) 535-6794 (614) 210-0591" — a real shape found in existing data)
     are extracted via address_parsing.extract_trailing_phones and used to
     fill in organizations.phone, but ONLY when that column is currently
     blank — an organization that already has a phone on file is left
     alone. organizations.full_address itself is never modified or
     cleared by this step; it stays in place as a legacy fallback/audit
     trail alongside the new structured row.

     NOTE on geography matching: the states/cities lookup tables are
     currently seeded with Pakistan data only (plus a full country list).
     An address whose Country matches (most will, including "USA"/"United
     States of America" style spellings — see address_parsing.
     COUNTRY_NAME_ALIASES) but whose Province/City don't have a matching
     seeded row (true of most non-Pakistan addresses, e.g. the Sikh
     Organizations Directory's largely US-based entries) still gets fully
     split into Street/City/Province/Country — City and Province just fall
     back to their free-text columns (city_text/state_province_text)
     instead of a linked city_id/state_id, exactly like the manual address
     form's "not on file" fallback. That's a by-design limitation of the
     current geography data, not a bug in this migration.

New installs don't need this: schema.sql already has the new shape (no
organizations have any full_address to backfill from yet on a fresh
install, so step 4 is a no-op there). Run this once against a database
created before this change:

    flask --app app migrate-organization-addresses

or directly:

    python migrate_organization_addresses.py
"""
import sqlite3

from address_parsing import extract_trailing_phones, parse_free_text_address
from config import Config


def _table_exists(db, name):
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def migrate():
    db = sqlite3.connect(Config.DATABASE_PATH)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA foreign_keys = OFF")  # table rename below, re-enabled at the end

        # 1. Rename address_types -> supplier_address_types, if still under
        # its old name (see module docstring, step 1).
        if _table_exists(db, "supplier_address_types"):
            print("supplier_address_types table already exists — nothing to rename.")
        elif _table_exists(db, "address_types"):
            db.execute("ALTER TABLE address_types RENAME TO supplier_address_types")
            db.commit()
            print("Renamed address_types -> supplier_address_types (Suppliers' address-type lookup); "
                  "supplier_addresses.address_type_id now points at it under its new name automatically.")
        else:
            print("Neither address_types nor supplier_address_types exists yet — this database predates "
                  "the Suppliers module; run `flask --app app migrate-add-suppliers` first.")

        # 2. New Organizations address tables.
        if not _table_exists(db, "organization_address_types"):
            db.execute("""
                CREATE TABLE organization_address_types (
                    address_type_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                    code            TEXT,
                    label           TEXT NOT NULL,
                    description     TEXT,
                    sort_order      INTEGER DEFAULT 0,
                    is_active       INTEGER NOT NULL DEFAULT 1,
                    UNIQUE (tenant_id, code)
                )
            """)
            db.execute("CREATE INDEX idx_organization_address_types_tenant ON organization_address_types(tenant_id)")
            print("Created organization_address_types table.")
        else:
            print("organization_address_types table already exists.")

        if not _table_exists(db, "organization_addresses"):
            db.execute("""
                CREATE TABLE organization_addresses (
                    organization_address_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                    organization_id INTEGER NOT NULL REFERENCES organizations(organization_id),
                    address_type_id INTEGER REFERENCES organization_address_types(address_type_id),
                    street          TEXT,
                    unit            TEXT,
                    region_id       INTEGER REFERENCES regions(region_id),
                    country_id      INTEGER REFERENCES countries(country_id),
                    state_id        INTEGER REFERENCES states(state_id),
                    state_province_text TEXT,
                    city_id         INTEGER REFERENCES cities(city_id),
                    city_text       TEXT,
                    postal_code     TEXT,
                    is_primary      INTEGER NOT NULL DEFAULT 0,
                    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """)
            db.execute("CREATE INDEX idx_organization_addresses_tenant ON organization_addresses(tenant_id)")
            db.execute("CREATE INDEX idx_organization_addresses_org ON organization_addresses(organization_id)")
            db.execute("CREATE INDEX idx_organization_addresses_type ON organization_addresses(address_type_id)")
            print("Created organization_addresses table.")
        else:
            print("organization_addresses table already exists.")

        db.commit()
        db.execute("PRAGMA foreign_keys = ON")
        db.commit()

        # 3. Per-tenant: seed Organization Address Types.
        from seed_data import _seed_simple, ORGANIZATION_ADDRESS_TYPES

        tenants = db.execute("SELECT tenant_id, tenant_name FROM tenants").fetchall()
        for t in tenants:
            _seed_simple(db, "organization_address_types", t["tenant_id"], ORGANIZATION_ADDRESS_TYPES)
            db.commit()
            print(f"Seeded Organization Address Types for '{t['tenant_name']}'.")

        # 4. Backfill organization_addresses from full_address, per tenant,
        # for organizations that don't already have an address row.
        total_backfilled = 0
        total_phones_recovered = 0
        for t in tenants:
            tenant_id = t["tenant_id"]
            mailing_type_id = db.execute(
                "SELECT address_type_id FROM organization_address_types WHERE tenant_id = ? AND lower(label) = 'mailing address'",
                (tenant_id,),
            ).fetchone()
            mailing_type_id = mailing_type_id["address_type_id"] if mailing_type_id else None

            orgs = db.execute(
                """SELECT organization_id, organization_name, full_address, phone FROM organizations
                   WHERE tenant_id = ? AND full_address IS NOT NULL AND trim(full_address) != ''
                   AND organization_id NOT IN (
                       SELECT organization_id FROM organization_addresses WHERE tenant_id = ?
                   )""",
                (tenant_id, tenant_id),
            ).fetchall()

            for org in orgs:
                cleaned_address, embedded_phones = extract_trailing_phones(org["full_address"])
                addr = parse_free_text_address(db, cleaned_address)

                if not (addr.get("street") or addr.get("country_id") or addr.get("country_text")
                        or addr.get("state_id") or addr.get("state_text") or addr.get("city_id") or addr.get("city_text")):
                    continue  # nothing parseable at all (e.g. full_address was just whitespace/punctuation)

                db.execute(
                    """INSERT INTO organization_addresses
                       (tenant_id, organization_id, address_type_id, street, region_id, country_id,
                        state_id, state_province_text, city_id, city_text, postal_code, is_primary)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
                    (
                        tenant_id, org["organization_id"], mailing_type_id, addr.get("street") or None,
                        addr.get("region_id"), addr.get("country_id"),
                        addr.get("state_id"), (None if addr.get("state_id") else (addr.get("state_text") or None)),
                        addr.get("city_id"), (None if addr.get("city_id") else (addr.get("city_text") or None)),
                        addr.get("postal_code") or None,
                    ),
                )
                total_backfilled += 1

                if embedded_phones and not (org["phone"] or "").strip():
                    db.execute(
                        "UPDATE organizations SET phone = ?, updated_at = datetime('now') WHERE organization_id = ? AND tenant_id = ?",
                        ("; ".join(embedded_phones), org["organization_id"], tenant_id),
                    )
                    total_phones_recovered += 1

            db.commit()

        print(f"Backfilled {total_backfilled} organization_addresses row(s) from existing full_address data "
              f"(recovering {total_phones_recovered} phone number(s) that had been embedded in the address text "
              f"into organizations.phone). organizations.full_address itself was left untouched.")

    finally:
        db.close()


if __name__ == "__main__":
    migrate()
