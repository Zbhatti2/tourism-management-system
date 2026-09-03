"""
Migration: adds Gender to the Employee form, as a proper lookup table
(genders) rather than a free-text field, and removes the freeform "Job
role" field from the Employee form (hr.py, employee_form.html,
employee_view.html) since it's redundant with the Primary/Secondary Role
pickers added earlier (see migrate_add_employee_role_manager.py) — Job
Roles are now addressed entirely through the HR Roles lookup table.

What this does, in order:

  1. Creates the `genders` table (if it doesn't already exist) — same
     shared shape as every other Module E lookup (id, code, label,
     description, sort_order, is_active), tenant-scoped, managed from
     Table Maintenance -> Human Resources Group -> Genders.
  2. Adds employees.gender_id (nullable FK into genders) if missing, plus
     its index.
  3. Seeds the starter Genders list (Male / Female / Other) for every
     existing tenant — same idempotent INSERT OR IGNORE keyed on
     (tenant_id, code) every other starter lookup uses, so re-running this
     never duplicates or clobbers a tenant's own edits.

employees.job_role itself is NOT dropped or touched by this migration —
per this project's rule that legacy columns are kept forever, it stays in
the schema with whatever value it already had. It's simply no longer read
or written by the Employee form's create/edit routes (see hr.py) or shown
on the Employee view page, so nothing new can be entered there and nothing
old is silently erased on the next edit.

Safe to re-run — the table, column, index, and per-tenant seed rows are
all added only if not already present.

New installs don't need this: schema.sql and seed_data.py already have
this shape, and `flask --app app seed-tenant` seeds Genders as part of
creating a tenant. Run this once against a database created before this
change:

    flask --app app migrate-add-employee-gender

or directly:

    python migrate_add_employee_gender.py
"""
import sqlite3

from config import Config
from seed_data import GENDERS, _seed_simple


def migrate():
    db = sqlite3.connect(Config.DATABASE_PATH)
    db.row_factory = sqlite3.Row
    try:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "genders" not in tables:
            db.execute(
                """CREATE TABLE genders (
                    gender_id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
                    code            TEXT,
                    label           TEXT NOT NULL,
                    description     TEXT,
                    sort_order      INTEGER DEFAULT 0,
                    is_active       INTEGER NOT NULL DEFAULT 1,
                    UNIQUE (tenant_id, code)
                )"""
            )
            db.execute("CREATE INDEX idx_genders_tenant ON genders(tenant_id)")
            db.commit()
            print("Created genders.")
        else:
            print("genders already exists — nothing to do there.")

        columns = [row[1] for row in db.execute("PRAGMA table_info(employees)").fetchall()]
        if "gender_id" not in columns:
            db.execute("ALTER TABLE employees ADD COLUMN gender_id INTEGER REFERENCES genders(gender_id)")
            db.commit()
            print("Added employees.gender_id.")
        else:
            print("employees.gender_id already exists — nothing to do there.")

        existing_indexes = {row[1] for row in db.execute("PRAGMA index_list(employees)").fetchall()}
        if "idx_employees_gender" not in existing_indexes:
            db.execute("CREATE INDEX idx_employees_gender ON employees(gender_id)")
            db.commit()

        for tenant in db.execute("SELECT tenant_id FROM tenants").fetchall():
            _seed_simple(db, "genders", tenant["tenant_id"], GENDERS)
        db.commit()
        print("Seeded Genders (Male / Female / Other) for every existing tenant that didn't already have its own.")
    finally:
        db.close()


if __name__ == "__main__":
    migrate()
