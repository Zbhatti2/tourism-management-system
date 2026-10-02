"""
Platform catalogs -- one definition per shared (platform-level) entity.

Each entry in CATALOGS describes a table the SystemAdmin maintains for every
tenant: its columns, how they appear on screen, which columns the list
shows, and what other tables point at it (so a merge can move those links).
The generic screens (blueprints/platform_catalog.py), the duplicate finder
and merge (platform_merge.py) and the import wizard (platform_import.py)
all read these definitions, so a column is declared once.

Field kinds
-----------
text, textarea, url, email, date (YYYY-MM-DD text), int, real, money (real),
yn (1 / 0 / NULL shown as Yes / No / blank), select (fixed options),
poi_type (FK to the platform's locked POI Types), country (FK countries).
Every catalog also has the shared location block (region / country /
province / city with text fallbacks, address, latitude, longitude) and the
shared tail (alternate names, notes, source, checked on, active).
"""

# Columns every catalog table has, in addition to its own fields.
COMMON_DDL = """
    name            TEXT NOT NULL,
    alt_names       TEXT,                   -- other spellings / former names, one per line (matched on import)
    region_id       INTEGER REFERENCES regions(region_id),
    country_id      INTEGER REFERENCES countries(country_id),
    state_id        INTEGER REFERENCES states(state_id),
    state_province_text TEXT,
    city_id         INTEGER REFERENCES cities(city_id),
    city_text       TEXT,
    address         TEXT,
    latitude        REAL,
    longitude       REAL,
    phone           TEXT,
    email           TEXT,
    website         TEXT,
    notes           TEXT,                   -- appended to, never overwritten, by imports
    source          TEXT,
    checked_on      TEXT,
    is_active       INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))"""

AMENITIES = [("amen_dining", "Dining"), ("amen_pool", "Pool"), ("amen_gym", "Gym"),
             ("amen_room_service", "Room Service"), ("amen_parking", "Parking"), ("amen_internet", "Internet"),
             ("amen_business_center", "Business Center"), ("amen_pets", "Pets Allowed")]

PROPERTY_TYPES = ["Hotel", "Resort", "Guest House", "Motel", "Pilgrim Lodging", "Apartment Suite"]
MISSION_TYPES = ["Embassy", "High Commission", "Consulate General", "Consulate", "Honorary Consulate",
                 "Other Mission"]

CATALOGS = {
    "pois": {
        "table": "platform_pois", "pk": "poi_id", "label": "Points of Interest", "singular": "Point of Interest",
        "icon": "geo-alt", "type_field": "poi_type_id",
        "fields": [
            ("poi_type_id", "POI Type", "poi_type", {"required": True}),
            ("significance", "Significance / Category", "text", {}),
            ("year_founded", "Year Founded / Era", "text", {}),
            ("description", "Description", "textarea", {}),
            ("entry_fee", "Entry Fee", "text", {}),
            ("opening_hours", "Days / Hours Open", "text", {}),
            ("image_url", "Image URL", "url", {}),
            ("video_url", "Video URL", "url", {}),
        ],
        "list": [("poi_type_label", "Type"), ("city_label", "City"), ("country_label", "Country")],
        "references": [],
    },
    "accommodation": {
        "table": "platform_accommodation", "pk": "accommodation_id", "label": "Accommodation",
        "singular": "Property", "icon": "building", "type_field": "property_type",
        "fields": [
            ("property_type", "Property Type", "select", {"required": True, "options": PROPERTY_TYPES}),
            ("star_rating", "Star Rating", "int", {"min": 1, "max": 5}),
            ("rating_note", "Rating (as published)", "text", {}),
            ("rooms", "No. of Rooms", "int", {"min": 0}),
            ("currency", "Currency", "text", {"maxlength": 3}),
            ("rate_single", "Avg Rate Single / Queen", "money", {}),
            ("rate_double", "Avg Rate Double / King", "money", {}),
            ("rate_year", "Rate Year", "int", {}),
        ] + [(col, label, "yn", {"group": "Amenities"}) for col, label in AMENITIES],
        "list": [("property_type", "Type"), ("star_rating", "Stars"), ("city_label", "City"),
                 ("country_label", "Country")],
        "references": [],
    },
    "restaurants": {
        "table": "platform_restaurants", "pk": "restaurant_id", "label": "Restaurants", "singular": "Restaurant",
        "icon": "cup-hot", "type_field": None,
        "fields": [
            ("rating", "Rating (out of 5)", "real", {"min": 0, "max": 5}),
            ("class", "Class", "text", {}),
            ("cuisine", "Cuisine / Specialty", "text", {}),
            ("currency", "Currency", "text", {"maxlength": 3}),
            ("price_from", "Price per Person — From", "money", {}),
            ("price_to", "Price per Person — To", "money", {}),
            ("group_suitable", "Group Suitable", "yn", {}),
        ],
        "list": [("cuisine", "Cuisine"), ("city_label", "City"), ("country_label", "Country")],
        "references": [],
    },
    "embassies": {
        "table": "embassies", "pk": "embassy_id", "label": "Embassies & Consulates", "singular": "Mission",
        "icon": "flag", "type_field": "mission_type",
        "fields": [
            ("represented_country_id", "Represented Country", "country", {"required": True}),
            ("mission_type", "Mission Type", "select", {"required": True, "options": MISSION_TYPES}),
            ("landmark", "Nearby Landmark", "text", {}),
            ("visa_phone", "Visa / Consular Phone", "text", {}),
            ("fax", "Fax", "text", {}),
            ("other_source", "Other Source", "text", {}),
        ],
        "list": [("represented_label", "Represents"), ("mission_type", "Type"), ("city_label", "City"),
                 ("country_label", "Country")],
        "references": [],
    },
}

# Transport Hubs keep their own screens (blueprints/transport_hubs.py) but use
# the same duplicate finder and merge, so they're described here too.
HUBS = {
    "table": "transport_hubs", "pk": "hub_id", "label": "Transport Hubs", "singular": "Hub", "icon": "airplane",
    "type_field": "hub_type_id",
    "fields": [
        ("hub_type_id", "Hub Type", "int", {"required": True}),
        ("code", "Code", "text", {}),
        ("icao_code", "ICAO Code", "text", {}),
        ("operator", "Operator", "text", {}),
        ("scope", "Scope", "select", {"options": ["International", "Domestic", "Regional"]}),
        ("is_major", "Major", "yn", {}),
    ],
    "list": [], "has_phone_email": False, "same_type_only": True,  # a station never duplicates an airport
    # (table, column[, extra WHERE]) -- moved to the kept hub on merge.
    "references": [
        ("package_route_stops", "arrival_hub_id"), ("package_route_stops", "departure_hub_id"),
        ("package_components", "from_hub_id"), ("package_components", "to_hub_id"),
        ("knowledge_graph_edges", "subject_id", "subject_type = 'TransportHub'"),
        ("knowledge_graph_edges", "object_id", "object_type = 'TransportHub'"),
    ],
}

MERGEABLE = dict(CATALOGS, hubs=HUBS)

# Base columns shown on view/edit, in order, after the catalog's own fields.
LOCATION_FIELDS = [("address", "Address / Landmark", "text", {}), ("latitude", "Latitude", "real", {}),
                   ("longitude", "Longitude", "real", {})]
CONTACT_FIELDS = [("phone", "Phone", "text", {}), ("email", "Email", "email", {}), ("website", "Website", "url", {})]
TAIL_FIELDS = [("alt_names", "Alternate Names", "textarea", {"help": "Other spellings or former names, one per line."}),
               ("notes", "Notes", "textarea", {}), ("source", "Source", "text", {}),
               ("checked_on", "Checked On", "date", {})]


def all_fields(spec):
    """Every editable column for a catalog, in display order (location fields
    other than the geography picker included)."""
    base = [("name", "Name", "text", {"required": True})] + spec["fields"] + LOCATION_FIELDS
    if spec.get("has_phone_email", True):
        base += CONTACT_FIELDS
    else:
        base += [f for f in CONTACT_FIELDS if f[0] in ("phone", "website")]
    return base + TAIL_FIELDS


def ddl(spec):
    """CREATE TABLE statement for one catalog (not used for hubs)."""
    own = []
    for col, _label, kind, _opts in spec["fields"]:
        sqltype = {"int": "INTEGER", "real": "REAL", "money": "REAL", "yn": "INTEGER", "poi_type": "INTEGER",
                   "country": "INTEGER"}.get(kind, "TEXT")
        ref = {"poi_type": " REFERENCES poi_types(poi_type_id)",
               "country": " REFERENCES countries(country_id)"}.get(kind, "")
        own.append(f"    {col:<16} {sqltype}{ref}")
    t, pk = spec["table"], spec["pk"]
    return (f"CREATE TABLE IF NOT EXISTS {t} (\n    {pk:<16} INTEGER PRIMARY KEY AUTOINCREMENT,"
            f"{COMMON_DDL},\n" + ",\n".join(own) + "\n);\n"
            f"CREATE INDEX IF NOT EXISTS idx_{t}_city ON {t}(city_id);\n"
            f"CREATE INDEX IF NOT EXISTS idx_{t}_country ON {t}(country_id);\n")


SUPPORT_DDL = """
CREATE TABLE IF NOT EXISTS platform_merge_log (
    merge_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    entity          TEXT NOT NULL,          -- key in platform_catalog.MERGEABLE, or 'cities'
    kept_id         INTEGER NOT NULL,
    removed_id      INTEGER NOT NULL,
    kept_before     TEXT NOT NULL,          -- JSON snapshot of the kept row before the merge
    removed_row     TEXT NOT NULL,          -- JSON snapshot of the merged-away row
    repointed       TEXT,                   -- JSON list of [table, column, rowid] links moved to the kept row
    merged_by       INTEGER REFERENCES users(user_id),
    merged_at       TEXT NOT NULL DEFAULT (datetime('now')),
    undone_at       TEXT,
    undone_by       INTEGER REFERENCES users(user_id)
);
CREATE TABLE IF NOT EXISTS platform_import_runs (
    run_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    entity          TEXT NOT NULL,
    mode            TEXT NOT NULL DEFAULT 'normal' CHECK (mode IN ('normal','notes')),
    file_name       TEXT,
    options         TEXT,                   -- JSON
    status          TEXT NOT NULL DEFAULT 'previewed' CHECK (status IN ('previewed','imported','discarded')),
    summary         TEXT,                   -- JSON counts after import
    created_by      INTEGER REFERENCES users(user_id),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    imported_at     TEXT
);
CREATE TABLE IF NOT EXISTS platform_import_rows (
    run_id          INTEGER NOT NULL REFERENCES platform_import_runs(run_id),
    row_num         INTEGER NOT NULL,       -- spreadsheet row number
    data            TEXT NOT NULL,          -- JSON: cleaned values keyed by template column
    status          TEXT NOT NULL,          -- new / update / duplicate / problem
    match_id        INTEGER,                -- existing record (or earlier row, see match_row) it matches
    match_row       INTEGER,                -- earlier row in the same file it duplicates
    match_label     TEXT,
    message         TEXT,
    decision        TEXT,                   -- for duplicates: merge / new / skip
    result          TEXT,                   -- after import: created / updated / skipped / failed
    PRIMARY KEY (run_id, row_num)
);
"""

# The 15 POI Types proposed from the Karachi-Kashgar research; added to the
# platform's locked list (Platform Lookups) by the catalog migration.
NEW_POI_TYPES = ["Archaeological Site", "Mausoleum / Shrine", "Monument / Memorial", "Viewpoint", "Bazaar / Market",
                 "Park / Garden", "Natural Landmark", "Heritage Village / Old City", "Mountain / Glacier",
                 "National Park", "Valley", "Beach / Waterfront", "Temple", "Church", "Cultural Venue"]
