"""
Migration: adds external_resources.organization_id to a database created
before the External Resource form grew an Organization field.

organization_id links a resource (a non-salaried/contract worker) to the
company/firm it works through -- e.g. a contractor supplied by an agency,
or a freelancer who bills through their own company -- picked from the
same shared `organizations` table Contacts already links to via
current_organization_id (see organizations.py's REFERENCES list, which
this migration's companion code change adds an "external_resources" entry
to, so an Organization still in use by a resource can't be deleted
outright, same protection Contacts already gets).

Nullable, like every other lookup FK on this table -- a resource with no
organization on file (an independent freelancer, or simply not recorded
yet) is left alone rather than guessed at.

Safe to re-run -- the column and its index are each added only if not
already present.

New installs don't need this: schema.sql already includes this column and
its index, so `flask --app app init-db` on a fresh database already has
it. Run this once against a database created before this change:

    flask --app app migrate-add-resource-organization

or directly:

    python migrate_add_resource_organization.py
"""
import sqlite3

from config import Config


def migrate():
    db = sqlite3.connect(Config.DATABASE_PATH)
    try:
        columns = [row[1] for row in db.execute("PRAGMA table_info(external_resources)").fetchall()]
        if "organization_id" not in columns:
            db.execute("ALTER TABLE external_resources ADD COLUMN organization_id INTEGER REFERENCES organizations(organization_id)")
            db.commit()
            added = True
        else:
            added = False

        existing_indexes = {row[1] for row in db.execute("PRAGMA index_list(external_resources)").fetchall()}
        if "idx_external_resources_organization" not in existing_indexes:
            db.execute("CREATE INDEX idx_external_resources_organization ON external_resources(organization_id)")
            db.commit()

        if added:
            print("Added external_resources.organization_id (plus its index).")
        else:
            print("external_resources.organization_id already exists — nothing to do.")
    finally:
        db.close()


if __name__ == "__main__":
    migrate()
