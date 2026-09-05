"""
Migration: adds package_route_stops.region_id, .state_id, and
.state_province_text, bringing the Route Stop form's geography picker up
to the same Region -> Country -> Province/State -> City shape already
used everywhere else geography is captured (organization_addresses,
supplier_addresses, employee/resource addresses, host addresses, and
points_of_interest -- see templates/_geography_fields.html and
static/js/geography_picker.js).

Per Zeb's report: the Route Stop form only ever offered Country -> City,
skipping the Province/State level entirely. Cities are seeded under
States (154 cities, all under Pakistan's 7 provinces), so a country with
states-but-no-cities-yet, like the United States (51 states, 0 cities),
showed an empty City dropdown with no way to tell whether that was
"nothing on file for this state" or a broken filter -- and no visible
way to type the city by hand either. Switching to the shared
Region/Country/State/City picker fixes both: the cascade now stops
exactly where the data does (Country -> States populate normally, City
correctly shows "None on file" and reveals its free-text fallback), and
Boston, MA can be typed into state_province_text/city_text same as any
other not-yet-seeded place.

What this does, in order:

  1. Adds package_route_stops.region_id (nullable INTEGER FK) if missing.
  2. Adds package_route_stops.state_id (nullable INTEGER FK) if missing.
  3. Adds package_route_stops.state_province_text (nullable TEXT) if
     missing.

Existing rows keep their country_id/city_id/city_text untouched --
nothing is reinterpreted, the new columns are simply blank until a stop
is edited and re-saved through the new form.

Same idempotent "PRAGMA table_info + ALTER TABLE ADD COLUMN" pattern as
migrate_add_route_stop_layover.py. Safe to re-run.

New installs don't need this: schema.sql already has this shape. Run
this once against a database created before this change:

    flask --app app migrate-add-route-stop-geography

or directly:

    python migrate_add_route_stop_geography.py
"""
import sqlite3

from config import Config


def migrate():
    db = sqlite3.connect(Config.DATABASE_PATH)
    db.row_factory = sqlite3.Row
    try:
        columns = [row[1] for row in db.execute("PRAGMA table_info(package_route_stops)").fetchall()]
        if "region_id" not in columns:
            db.execute("ALTER TABLE package_route_stops ADD COLUMN region_id INTEGER REFERENCES regions(region_id)")
            db.commit()
            print("Added package_route_stops.region_id.")
        else:
            print("package_route_stops.region_id already exists — nothing to do there.")

        columns = [row[1] for row in db.execute("PRAGMA table_info(package_route_stops)").fetchall()]
        if "state_id" not in columns:
            db.execute("ALTER TABLE package_route_stops ADD COLUMN state_id INTEGER REFERENCES states(state_id)")
            db.commit()
            print("Added package_route_stops.state_id.")
        else:
            print("package_route_stops.state_id already exists — nothing to do there.")

        columns = [row[1] for row in db.execute("PRAGMA table_info(package_route_stops)").fetchall()]
        if "state_province_text" not in columns:
            db.execute("ALTER TABLE package_route_stops ADD COLUMN state_province_text TEXT")
            db.commit()
            print("Added package_route_stops.state_province_text.")
        else:
            print("package_route_stops.state_province_text already exists — nothing to do there.")
    finally:
        db.close()


if __name__ == "__main__":
    migrate()
