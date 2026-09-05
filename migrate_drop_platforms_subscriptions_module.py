"""
Migration: permanently drops the archived "Platforms & Subscriptions"
tables — per Zeb's request to remove all traces of that PIMS-carried
module now that it's confirmed not needed for a Tourism/Travel Management
System.

Background: migrate_poi_remove_platforms_accounts.py (an earlier
migration) already disconnected this module from the app -- no blueprint,
no route, no menu item has touched it since -- but played it safe by
archive-RENAMING its tables (prefixing each with "_archived_") rather than
dropping them, in case the data was ever needed again. It's now confirmed
not needed: every one of these tables is either completely empty or holds
only unused lookup/picklist seed values with zero real records referencing
them (no actual platform, subscription, or software license was ever
entered). So this migration finishes the job and drops them for real.

This does NOT touch the separate "Accounts" module's archived tables
(_archived_personal_accounts, _archived_personal_accounts_history,
_archived_personal_account_types) -- only Platforms & Subscriptions was
in scope for this request.

What this does, in order:

  1. DROPs each of the 16 _archived_* tables that belonged to Platforms &
     Subscriptions, if present:
       _archived_cloud_platforms,
       _archived_cloud_platform_phones(+history),
       _archived_cloud_platform_emails(+history),
       _archived_cloud_service_subscriptions(+history),
       _archived_desktop_software_licenses(+history),
       _archived_desktop_software_license_phones(+history),
       _archived_desktop_software_license_emails(+history),
       _archived_desktop_software_license_reference_links,
       _archived_service_types, _archived_software_types.
     A table that's already gone (or never existed on this database --
     e.g. a database created after this migration already shipped, or one
     that never ran migrate_poi_remove_platforms_accounts.py in the first
     place) is skipped, so this is safe to re-run.

New installs don't need this: schema.sql never defined these tables in
the first place. Run this once against a database that still has the
archived tables (i.e. one that ran migrate_poi_remove_platforms_accounts.py
at some point):

    flask --app app migrate-drop-platforms-subscriptions

or directly:

    python migrate_drop_platforms_subscriptions_module.py
"""
import sqlite3

from config import Config

# Every archived table that belonged to Platforms & Subscriptions
# specifically (not Accounts -- that module's _archived_personal_accounts*
# tables are deliberately left alone).
DROP_TABLES = [
    "_archived_cloud_platforms",
    "_archived_cloud_platform_phones",
    "_archived_cloud_platform_phones_history",
    "_archived_cloud_platform_emails",
    "_archived_cloud_platform_emails_history",
    "_archived_cloud_service_subscriptions",
    "_archived_cloud_service_subscriptions_history",
    "_archived_desktop_software_licenses",
    "_archived_desktop_software_licenses_history",
    "_archived_desktop_software_license_phones",
    "_archived_desktop_software_license_phones_history",
    "_archived_desktop_software_license_emails",
    "_archived_desktop_software_license_emails_history",
    "_archived_desktop_software_license_reference_links",
    "_archived_service_types",
    "_archived_software_types",
]


def _table_exists(db, name):
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def migrate():
    db = sqlite3.connect(Config.DATABASE_PATH)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA foreign_keys = OFF")  # drops below, re-enabled at the end

        dropped, skipped = 0, 0
        for table in DROP_TABLES:
            if not _table_exists(db, table):
                skipped += 1
                continue
            db.execute(f"DROP TABLE {table}")
            dropped += 1
        db.commit()

        db.execute("PRAGMA foreign_keys = ON")
        db.commit()

        print(f"Dropped {dropped} archived Platforms & Subscriptions table(s) "
              f"({skipped} already gone or never present, left alone).")
        print("The separate Accounts module's archived tables "
              "(_archived_personal_accounts, _archived_personal_accounts_history, "
              "_archived_personal_account_types) were left untouched -- out of "
              "scope for this request.")
    finally:
        db.close()


if __name__ == "__main__":
    migrate()
