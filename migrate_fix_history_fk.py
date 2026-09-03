"""
Migration: drops the (misguided) FOREIGN KEY constraint on
contact_phones_history.phone_id, contact_emails_history.email_id, and
addresses_history.address_id — three "Deleted" audit-trail tables that are
supposed to keep working after the row they describe is gone.

Bug this fixes: contacts.py's delete_phone()/delete_email()/delete_address()
all follow the same two-step sequence — INSERT a "Deleted" snapshot row into
the *_history table (which records which phone_id/email_id/address_id it
was), THEN DELETE the live row. With PRAGMA foreign_keys = ON (db.py turns
this on for every request), that DELETE fails with
"sqlite3.IntegrityError: FOREIGN KEY constraint failed" — the history row
just inserted still points at the phone_id/email_id/address_id, and a hard
FK requires the referenced row to keep existing for as long as anything
points at it. In other words, the schema as originally written made it
*impossible* to ever finish deleting a contact phone, email, or address —
the history row you need to insert (to record what you're about to delete)
is exactly what blocks you from then deleting it.

The fix: phone_id/email_id/address_id on these three history tables no
longer need to be a live, enforced foreign key — previous_value already
holds a full snapshot of what was deleted, so the id is only ever used
after the fact as a "this was row #N" label, not as a live pointer that
needs to resolve to a current row. Dropping the REFERENCES clause (SQLite
can't ALTER a column's constraint in place, so this rebuilds each table —
new table, copy every existing row across unchanged, drop, rename, same
technique as migrate_add_suppliers.py's addresses.owner_type CHECK
narrowing) lets the exact same insert-then-delete sequence in contacts.py
succeed, with no code changes needed there. Every existing history row
(including ones already describing deleted phones/emails/addresses from
before this fix, if any got in a half-deleted state) is preserved exactly
as-is.

Safe to re-run — each of the three rebuilds is skipped individually if that
table's FK is already gone (e.g. this migration already ran, or the
database was created after this fix shipped in schema.sql).

New installs don't need this: schema.sql no longer declares these three FKs
at all, so `flask --app app init-db` on a fresh database already has the
fixed shape. Run this once against a database created before this fix:

    flask --app app migrate-fix-history-fk

or directly:

    python migrate_fix_history_fk.py
"""
import sqlite3

from config import Config


def _has_fk_on(db, table, column):
    if db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone() is None:
        return False
    return any(row[3] == column for row in db.execute(f"PRAGMA foreign_key_list({table})").fetchall())


def _rebuild(db, table, create_sql, indexes):
    cols = [r["name"] for r in db.execute(f"PRAGMA table_info({table})").fetchall()]
    col_list = ", ".join(cols)
    db.execute(create_sql.replace(f"CREATE TABLE {table}", f"CREATE TABLE {table}__new", 1))
    db.execute(f"INSERT INTO {table}__new ({col_list}) SELECT {col_list} FROM {table}")
    db.execute(f"DROP TABLE {table}")
    db.execute(f"ALTER TABLE {table}__new RENAME TO {table}")
    for idx_sql in indexes:
        db.execute(idx_sql)


def migrate():
    db = sqlite3.connect(Config.DATABASE_PATH)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA foreign_keys = OFF")  # table rebuilds below

        if _has_fk_on(db, "contact_phones_history", "phone_id"):
            _rebuild(
                db, "contact_phones_history",
                """
                CREATE TABLE contact_phones_history (
                    phone_history_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                    phone_id        INTEGER NOT NULL,
                    contact_id      INTEGER NOT NULL,
                    previous_value  TEXT,
                    superseded_at   TEXT NOT NULL DEFAULT (datetime('now')),
                    superseded_reason TEXT
                )
                """,
                ["CREATE INDEX idx_contact_phones_history_tenant ON contact_phones_history(tenant_id)"],
            )
            db.commit()
            print("Dropped the FK on contact_phones_history.phone_id — deleting a contact phone no longer errors.")
        else:
            print("contact_phones_history.phone_id already has no FK (or table not present) — nothing to do.")

        if _has_fk_on(db, "contact_emails_history", "email_id"):
            _rebuild(
                db, "contact_emails_history",
                """
                CREATE TABLE contact_emails_history (
                    email_history_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                    email_id        INTEGER NOT NULL,
                    contact_id      INTEGER NOT NULL,
                    previous_value  TEXT,
                    superseded_at   TEXT NOT NULL DEFAULT (datetime('now')),
                    superseded_reason TEXT
                )
                """,
                ["CREATE INDEX idx_contact_emails_history_tenant ON contact_emails_history(tenant_id)"],
            )
            db.commit()
            print("Dropped the FK on contact_emails_history.email_id — deleting a contact email no longer errors.")
        else:
            print("contact_emails_history.email_id already has no FK (or table not present) — nothing to do.")

        if _has_fk_on(db, "addresses_history", "address_id"):
            _rebuild(
                db, "addresses_history",
                """
                CREATE TABLE addresses_history (
                    address_history_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                    address_id      INTEGER NOT NULL,
                    owner_type      TEXT NOT NULL,
                    owner_id        INTEGER NOT NULL,
                    field_name      TEXT,
                    previous_value  TEXT,
                    superseded_at   TEXT NOT NULL DEFAULT (datetime('now')),
                    superseded_reason TEXT
                )
                """,
                [
                    "CREATE INDEX idx_addresses_history_tenant ON addresses_history(tenant_id)",
                    "CREATE INDEX idx_addresses_history_address ON addresses_history(address_id)",
                ],
            )
            db.commit()
            print("Dropped the FK on addresses_history.address_id — deleting a contact address no longer errors.")
        else:
            print("addresses_history.address_id already has no FK (or table not present) — nothing to do.")

    finally:
        db.close()


if __name__ == "__main__":
    migrate()
