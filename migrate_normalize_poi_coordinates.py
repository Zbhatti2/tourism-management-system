"""
One-time data cleanup: rewrites every existing points_of_interest.
map_coordinates value into the canonical DMS display format (e.g.
'31°35′17″N 74°18′34″E'), for the request:

    "Please make the Map coordinates field Data in this format
    '31°35′17″N 74°18′34″E' and linkable via hot link to Google Maps so
    the use can click on the link and see the POI on Google Maps."

Going forward, every new/edited POI is normalized automatically at save
time (blueprints/poi.py's _form_fields, via utils.normalize_map_
coordinates) — this script exists only to bring any *pre-existing* rows
(entered before that normalization shipped, e.g. as plain decimal degrees)
in line with it too, and to normalize whatever a future CSV/bulk import
puts in this column.

Only rewrites a row whose value both (a) parses as real coordinates and
(b) doesn't already match the canonical format exactly — a row that isn't
recognizable as coordinates (a plain text description, or something too
garbled to parse) is left completely untouched, per the "never destroy
data" rule; run again after adding a note to Table Maintenance rather than
guessing at fixing it here.

Safe to re-run — idempotent (a second run touches 0 rows).

    flask --app app migrate-normalize-poi-coordinates
or directly:
    python migrate_normalize_poi_coordinates.py
"""
import sqlite3

from config import Config
from utils import normalize_map_coordinates, parse_coordinates


def migrate():
    db = sqlite3.connect(Config.DATABASE_PATH)
    db.row_factory = sqlite3.Row
    try:
        rows = db.execute(
            "SELECT poi_id, map_coordinates FROM points_of_interest WHERE map_coordinates IS NOT NULL AND map_coordinates != ''"
        ).fetchall()
        updated = skipped_unparseable = 0
        for row in rows:
            raw = row["map_coordinates"]
            if parse_coordinates(raw) is None:
                skipped_unparseable += 1
                continue
            normalized = normalize_map_coordinates(raw)
            if normalized != raw:
                db.execute(
                    "UPDATE points_of_interest SET map_coordinates = ? WHERE poi_id = ?", (normalized, row["poi_id"])
                )
                updated += 1
        db.commit()
        print(
            f"Normalized {updated} map_coordinates value(s) to the canonical DMS format. "
            f"{skipped_unparseable} row(s) left untouched (not recognizable as coordinates). "
            f"{len(rows) - updated - skipped_unparseable} already matched."
        )
    finally:
        db.close()


if __name__ == "__main__":
    migrate()
