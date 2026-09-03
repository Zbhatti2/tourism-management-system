"""
Migration: adds organizations.notes (TEXT, nullable) to an existing
database. Safe to re-run — checks PRAGMA table_info first and does nothing
if the column is already there.

New installs don't need this: schema.sql already includes the column, so
`flask --app app init-db` on a fresh database already has it. This script
is only for a database that was created BEFORE this change — e.g. the one
that shipped in the Phase 1.1 skeleton zip, already seeded with the
Heritage Tours tenant and Zeb's login. Run it once against that database
to add the column without losing anything already in it:

    flask --app app migrate-add-organization-notes

or directly:

    python migrate_add_organization_notes.py
"""
import sqlite3

from config import Config


def migrate():
    db = sqlite3.connect(Config.DATABASE_PATH)
    try:
        columns = [row[1] for row in db.execute("PRAGMA table_info(organizations)").fetchall()]
        if "notes" in columns:
            print("organizations.notes already exists — nothing to do.")
            return
        db.execute("ALTER TABLE organizations ADD COLUMN notes TEXT")
        db.commit()
        print("Added organizations.notes.")
    finally:
        db.close()


if __name__ == "__main__":
    migrate()
