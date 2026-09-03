"""
Migration: adds employees.primary_role_id, employees.secondary_role_id, and
employees.manager_id to a database created before the Employee form grew
three new fields:

  - Primary Role  — required at the application layer (see hr.py's
    new_employee()/edit_employee()), picked from the same shared "Roles"
    lookup (hr_roles) the Roles checklist on the Employee view page already
    uses. Each hr_roles row already has a `description` column (added back
    when the HR module itself was created) — that column doubles as the
    role's "description of duties": the Employee form shows it read-only
    next to the Primary/Secondary Role pickers, auto-filled from whichever
    role is selected, no new column needed on hr_roles.
  - Secondary Role — optional, same lookup, same auto-filled duties display.
  - Reports To ("manager_id") — an optional self-reference to another row
    in employees (nullable, so whoever sits at the top of the org chart
    simply has none). hr.py stops an employee from being set as their own
    manager; deeper cycles (A -> B -> A) aren't checked, same level of
    effort as the rest of this app's lookup pickers.

None of the three columns are declared NOT NULL at the database level, even
though Primary Role is described as required — this migration runs against
a database that may already have employees with no role recorded at all,
and guessing a role for them would be fabricating data. Requiring one is
instead enforced in the web form (like "Full name" already is), so every
employee touched going forward is required to have one, without silently
assigning a fictitious role to existing records this migration knows
nothing about.

Safe to re-run — each column and each index is added only if not already
present.

New installs don't need this: schema.sql already includes these columns and
their indexes, so `flask --app app init-db` on a fresh database already has
them. Run this once against a database created before this change:

    flask --app app migrate-add-employee-role-manager

or directly:

    python migrate_add_employee_role_manager.py
"""
import sqlite3

from config import Config


def migrate():
    db = sqlite3.connect(Config.DATABASE_PATH)
    try:
        columns = [row[1] for row in db.execute("PRAGMA table_info(employees)").fetchall()]
        added = []

        if "primary_role_id" not in columns:
            db.execute("ALTER TABLE employees ADD COLUMN primary_role_id INTEGER REFERENCES hr_roles(role_id)")
            added.append("primary_role_id")
        if "secondary_role_id" not in columns:
            db.execute("ALTER TABLE employees ADD COLUMN secondary_role_id INTEGER REFERENCES hr_roles(role_id)")
            added.append("secondary_role_id")
        if "manager_id" not in columns:
            db.execute("ALTER TABLE employees ADD COLUMN manager_id INTEGER REFERENCES employees(employee_id)")
            added.append("manager_id")
        db.commit()

        existing_indexes = {row[1] for row in db.execute("PRAGMA index_list(employees)").fetchall()}
        index_ddl = {
            "idx_employees_primary_role": "CREATE INDEX idx_employees_primary_role ON employees(primary_role_id)",
            "idx_employees_secondary_role": "CREATE INDEX idx_employees_secondary_role ON employees(secondary_role_id)",
            "idx_employees_manager": "CREATE INDEX idx_employees_manager ON employees(manager_id)",
        }
        for name, ddl in index_ddl.items():
            if name not in existing_indexes:
                db.execute(ddl)
        db.commit()

        if added:
            print(f"Added employees columns: {', '.join(added)} (plus their indexes).")
        else:
            print("employees.primary_role_id / secondary_role_id / manager_id already exist — nothing to do.")
    finally:
        db.close()


if __name__ == "__main__":
    migrate()
