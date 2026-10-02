"""
Catalog Sync -- keeps each tenant's own POIs and Suppliers in step with the
platform catalogs (platform_pois, platform_accommodation,
platform_restaurants).

How a catalog record reaches a tenant
-------------------------------------
* added   -- the tenant had nothing like it, so a copy was created: a Point
             of Interest, or a Supplier under the locked Hotel / Resort /
             Restaurant type.
* linked  -- the tenant already had the record under its own spelling
             ("Badshahi Masjid" for the catalog's "Badshahi Mosque"); the two
             are tied together instead of creating a second copy.
* skipped -- the SystemAdmin chose not to bring it into this tenant.

One tenant_catalog_links row per (tenant, catalog record) records which it
was, plus three small JSON maps used to keep the copy in sync:

* baseline  -- per field, the value last taken from the platform. While the
               tenant's value still equals it, the tenant hasn't touched that
               field, so a platform change is applied automatically.
* pending   -- per field, a platform value that differs from a value the
               tenant changed. Shown to the tenant as "Platform has a
               different value" with Accept / Keep mine.
* dismissed -- per field, a platform value the tenant chose to keep theirs
               over (or that differed when an existing record was linked);
               not offered again unless the platform value changes.

Only the fields the tenant's own screens have are copied (see the adapters
below). Everything else in the catalog -- entry fee, opening hours, star
rating, rates, cuisine, price range, images, catalog notes -- is shown live
from the platform on the tenant's view page, so it is never out of date.

Sync runs
---------
* The SystemAdmin's Catalog Sync screen (blueprints/catalog_sync.py) does a
  tenant's first catch-up with a preview: Add / Link / Review.
* After that, every platform change is pushed automatically (hooks in the
  catalog screens, the import wizard and merge): edits update untouched
  fields, new records are added (or linked when the tenant has the same
  name in the same city), and possible matches wait on the Catalog Sync
  screen for a decision.
* A new tenant gets the whole catalog when it is created.
"""
import json

from fuzzy import compare, normalize, split_alt_names
from platform_catalog import CATALOGS
from utils import normalize_map_coordinates

SYNC_ENTITIES = ("pois", "accommodation", "restaurants")

LINKS_DDL = """
CREATE TABLE IF NOT EXISTS tenant_catalog_links (
    link_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    entity          TEXT NOT NULL CHECK (entity IN ('pois','accommodation','restaurants')),
    catalog_id      INTEGER NOT NULL,       -- platform_pois / platform_accommodation / platform_restaurants id
    local_table     TEXT NOT NULL,          -- 'points_of_interest' or 'suppliers'
    local_id        INTEGER,                -- the tenant's record; NULL when how = 'skipped'
    how             TEXT NOT NULL CHECK (how IN ('added','linked','skipped')),
    baseline        TEXT,                   -- JSON {field: value last taken from the platform}
    pending         TEXT,                   -- JSON {field: platform value awaiting the tenant's Accept / Keep mine}
    dismissed       TEXT,                   -- JSON {field: platform value the tenant kept theirs over}
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    synced_at       TEXT,
    UNIQUE (tenant_id, entity, catalog_id)
);
CREATE INDEX IF NOT EXISTS idx_tenant_catalog_links_local ON tenant_catalog_links(tenant_id, local_table, local_id);
CREATE INDEX IF NOT EXISTS idx_tenant_catalog_links_catalog ON tenant_catalog_links(entity, catalog_id);
"""

_RANK = {"same": 3, "sounds": 2, "similar": 1}

# Platform amenity column -> tenant Hotel amenity option code.
AMENITY_CODES = {"amen_dining": "FOOD_AND_DRINK_RESTAURANT", "amen_pool": "WELLNESS_POOL", "amen_gym": "WELLNESS_GYM",
                 "amen_room_service": "FOOD_AND_DRINK_ROOM_SERVICE", "amen_parking": "CONVENIENCE_PARKING",
                 "amen_internet": "IN_ROOM_WI_FI", "amen_business_center": "BUSINESS_BUSINESS_CENTER"}


def _clean(v):
    """Comparable form of a field value."""
    if v is None:
        return None
    if isinstance(v, str):
        v = v.strip()
        return v or None
    if isinstance(v, (list, tuple)):
        items = [_clean(x) for x in v]
        return None if all(x is None for x in items) else items
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def _same(a, b):
    return _clean(a) == _clean(b)


def _empty(v):
    return _clean(v) is None


def _loads(text):
    return json.loads(text) if text else {}


# ---- geography labels (for showing values) -------------------------------------------

def _place_label(db, region_id, country_id, state_id, state_text, city_id, city_text):
    parts = []
    if city_id:
        r = db.execute("SELECT label FROM cities WHERE city_id = ?", (city_id,)).fetchone()
        parts.append(r[0] if r else None)
    else:
        parts.append(city_text)
    if state_id:
        r = db.execute("SELECT label FROM states WHERE state_id = ?", (state_id,)).fetchone()
        parts.append(r[0] if r else None)
    else:
        parts.append(state_text)
    if country_id:
        r = db.execute("SELECT label FROM countries WHERE country_id = ?", (country_id,)).fetchone()
        parts.append(r[0] if r else None)
    return ", ".join(p for p in parts if p)


def _city_label(db, city_id, city_text):
    if city_id:
        r = db.execute("SELECT label FROM cities WHERE city_id = ?", (city_id,)).fetchone()
        return r[0] if r else None
    return city_text


# ---- adapters: how a catalog record maps onto the tenant's own tables ----------------------

class _Adapter:
    entity = None
    local_table = None
    local_pk = None
    fields = []          # [(field, label)]

    def __init__(self, db, tenant_id):
        self.db, self.tenant_id = db, tenant_id

    def label(self, field):
        return dict(self.fields).get(field, field)

    def catalog_rows(self, ids=None):
        spec = CATALOGS[self.entity]
        sql = f"SELECT *, {spec['pk']} AS id FROM {spec['table']} WHERE is_active = 1"
        params = []
        if ids is not None:
            ids = list(ids)
            if not ids:
                return []
            sql += f" AND {spec['pk']} IN ({','.join('?' * len(ids))})"
            params = ids
        return self.db.execute(sql + " ORDER BY name", params).fetchall()

    def catalog_row(self, catalog_id):
        spec = CATALOGS[self.entity]
        return self.db.execute(f"SELECT *, {spec['pk']} AS id FROM {spec['table']} WHERE {spec['pk']} = ?",
                               (catalog_id,)).fetchone()

    def alive(self, local_id):
        return self.db.execute(
            f"SELECT 1 FROM {self.local_table} WHERE {self.local_pk} = ? AND tenant_id = ? AND is_deleted = 0",
            (local_id, self.tenant_id)).fetchone() is not None

    def local_name(self, local_id):
        col = "name" if self.local_table == "points_of_interest" else "supplier_name"
        r = self.db.execute(f"SELECT {col} FROM {self.local_table} WHERE {self.local_pk} = ? AND tenant_id = ?",
                            (local_id, self.tenant_id)).fetchone()
        return r[0] if r else None

    # subclasses: platform_values(P), local_values(local_id), write(local_id, changes), create(values),
    # match_pool(), display(field, value)


class PoiAdapter(_Adapter):
    entity = "pois"
    local_table = "points_of_interest"
    local_pk = "poi_id"
    fields = [("name", "Name"), ("poi_type_id", "POI Type"), ("year_established", "Year Established"),
              ("location", "Location"), ("local_location", "Local Location / Address"), ("phone", "Phone"),
              ("website", "Website"), ("historical_significance", "Historical Significance"),
              ("map_coordinates", "Map Coordinates")]
    LOCATION_COLS = ["region_id", "country_id", "state_id", "state_province_text", "city_id", "city_text"]

    def __init__(self, db, tenant_id):
        super().__init__(db, tenant_id)
        self._types = {r["code"]: r["poi_type_id"] for r in db.execute(
            "SELECT code, poi_type_id FROM poi_types WHERE tenant_id = ? AND code IS NOT NULL", (tenant_id,))}
        self._platform_type_codes = {r[0]: r[1] for r in db.execute(
            "SELECT pt.poi_type_id, pt.code FROM poi_types pt JOIN tenants t ON t.tenant_id = pt.tenant_id "
            "WHERE t.is_platform = 1")}

    def platform_values(self, p):
        coords = None
        if p["latitude"] is not None and p["longitude"] is not None:
            coords = normalize_map_coordinates(f"{p['latitude']}, {p['longitude']}") or None
        return {
            "name": p["name"],
            "poi_type_id": self._types.get(self._platform_type_codes.get(p["poi_type_id"])),
            "year_established": p["year_founded"],
            "location": [p[c] for c in self.LOCATION_COLS],
            "local_location": p["address"],
            "phone": p["phone"],
            "website": p["website"],
            "historical_significance": p["description"] or p["significance"],
            "map_coordinates": coords,
        }

    def local_values(self, local_id):
        r = self.db.execute("SELECT * FROM points_of_interest WHERE poi_id = ? AND tenant_id = ?",
                            (local_id, self.tenant_id)).fetchone()
        out = {f: r[f] for f, _ in self.fields if f != "location"}
        out["location"] = [r[c] for c in self.LOCATION_COLS]
        return out

    def _columns(self, changes):
        cols = {}
        for f, v in changes.items():
            if f == "location":
                cols.update(dict(zip(self.LOCATION_COLS, v or [None] * 6)))
            else:
                cols[f] = v
        return cols

    def write(self, local_id, changes):
        cols = self._columns(changes)
        if cols:
            self.db.execute(
                f"UPDATE points_of_interest SET {', '.join(f'{c} = ?' for c in cols)}, updated_at = datetime('now') "
                "WHERE poi_id = ? AND tenant_id = ?", tuple(cols.values()) + (local_id, self.tenant_id))

    def create(self, values):
        cols = dict(self._columns(values), tenant_id=self.tenant_id)
        cur = self.db.execute(f"INSERT INTO points_of_interest ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                              tuple(cols.values()))
        return cur.lastrowid

    def match_pool(self):
        return [dict(r) for r in self.db.execute(
            """SELECT p.poi_id AS id, p.name, p.city_id, p.city_text, COALESCE(c.label, p.city_text) AS city_label
               FROM points_of_interest p LEFT JOIN cities c ON c.city_id = p.city_id
               WHERE p.tenant_id = ? AND p.is_deleted = 0""", (self.tenant_id,))]

    def display(self, field, value):
        if _empty(value):
            return ""
        if field == "location":
            return _place_label(self.db, *value)
        if field == "poi_type_id":
            r = self.db.execute("SELECT label FROM poi_types WHERE poi_type_id = ?", (value,)).fetchone()
            return r[0] if r else ""
        return str(value)


class SupplierAdapter(_Adapter):
    local_table = "suppliers"
    local_pk = "supplier_id"
    ADDRESS_COLS = ["street", "region_id", "country_id", "state_id", "state_province_text", "city_id", "city_text"]
    type_codes = ()

    fields = [("supplier_name", "Name"), ("supplier_type_id", "Supplier Type"), ("supplier_subtype_id", "Sub-Type"),
              ("web_page", "Web Page"), ("address", "Address"), ("phone", "Phone"), ("email", "Email"),
              ("amenities", "Amenities")]

    def __init__(self, db, tenant_id):
        super().__init__(db, tenant_id)
        self._types = {r["code"]: r for r in db.execute(
            "SELECT supplier_type_id, code, template_key FROM supplier_types WHERE tenant_id = ? AND code IS NOT NULL",
            (tenant_id,))}
        self._subtypes = {r["code"]: r["supplier_subtype_id"] for r in db.execute(
            "SELECT supplier_subtype_id, code FROM supplier_subtypes WHERE tenant_id = ? AND code IS NOT NULL",
            (tenant_id,))}
        self._amenity_ids = {r["code"]: r["amenity_option_id"] for r in db.execute(
            "SELECT amenity_option_id, code FROM hotel_amenity_options WHERE tenant_id = ? AND code IS NOT NULL",
            (tenant_id,))}

    def type_code(self, p):
        raise NotImplementedError

    def _type_id(self, code):
        row = self._types.get(code)
        return row["supplier_type_id"] if row else None

    def platform_values(self, p):
        code = self.type_code(p)
        out = {
            "supplier_name": p["name"],
            "supplier_type_id": self._type_id(code),
            "supplier_subtype_id": None,
            "web_page": p["website"],
            "address": [p["address"]] + [p[c] for c in self.ADDRESS_COLS[1:]],
            "phone": p["phone"],
            "email": p["email"],
        }
        stars = p["star_rating"] if "star_rating" in p.keys() else None
        if stars in (4, 5):
            out["supplier_subtype_id"] = self._subtypes.get(f"{code}_{'FIVE_STAR' if stars == 5 else '4_STAR'}")
        if code in self._types and self._types[code]["template_key"] == "hotel" and "amen_pool" in p.keys():
            out["amenities"] = sorted(AMENITY_CODES[c] for c in AMENITY_CODES if p[c] == 1 and AMENITY_CODES[c] in self._amenity_ids)
        return out

    def _address_row(self, local_id):
        return self.db.execute(
            "SELECT * FROM supplier_addresses WHERE supplier_id = ? AND tenant_id = ? "
            "ORDER BY is_primary DESC, supplier_address_id LIMIT 1", (local_id, self.tenant_id)).fetchone()

    def _phone_row(self, local_id):
        return self.db.execute(
            "SELECT * FROM supplier_phones WHERE supplier_id = ? AND tenant_id = ? "
            "ORDER BY is_primary DESC, supplier_phone_id LIMIT 1", (local_id, self.tenant_id)).fetchone()

    def _email_row(self, local_id):
        return self.db.execute(
            "SELECT * FROM supplier_emails WHERE supplier_id = ? AND tenant_id = ? "
            "ORDER BY is_primary DESC, supplier_email_id LIMIT 1", (local_id, self.tenant_id)).fetchone()

    def local_values(self, local_id):
        s = self.db.execute("SELECT * FROM suppliers WHERE supplier_id = ? AND tenant_id = ?",
                            (local_id, self.tenant_id)).fetchone()
        a, ph, em = self._address_row(local_id), self._phone_row(local_id), self._email_row(local_id)
        phone = None
        if ph:
            phone = ph["number"] if not (ph["country_code"] or ph["area_code"] or ph["extension"]) else \
                "-".join(x for x in (ph["country_code"], ph["area_code"], ph["number"]) if x) + \
                (f" x{ph['extension']}" if ph["extension"] else "")
        codes = {v: k for k, v in self._amenity_ids.items()}
        mapped = set(AMENITY_CODES.values())
        amenities = sorted(codes[r[0]] for r in self.db.execute(
            "SELECT amenity_option_id FROM supplier_amenities WHERE supplier_id = ? AND tenant_id = ?",
            (local_id, self.tenant_id)) if codes.get(r[0]) in mapped)
        return {
            "supplier_name": s["supplier_name"], "supplier_type_id": s["supplier_type_id"],
            "supplier_subtype_id": s["supplier_subtype_id"], "web_page": s["web_page"],
            "address": [a[c] for c in self.ADDRESS_COLS] if a else [None] * 7,
            "phone": phone, "email": em["email_address"] if em else None, "amenities": amenities,
        }

    def write(self, local_id, changes):
        db, tid = self.db, self.tenant_id
        main = {f: v for f, v in changes.items() if f in ("supplier_name", "supplier_type_id", "supplier_subtype_id", "web_page")}
        if main:
            db.execute(f"UPDATE suppliers SET {', '.join(f'{c} = ?' for c in main)}, updated_at = datetime('now') "
                       "WHERE supplier_id = ? AND tenant_id = ?", tuple(main.values()) + (local_id, tid))
        if "address" in changes:
            vals = dict(zip(self.ADDRESS_COLS, changes["address"] or [None] * 7))
            row = self._address_row(local_id)
            if row:
                db.execute(f"UPDATE supplier_addresses SET {', '.join(f'{c} = ?' for c in vals)}, updated_at = datetime('now') "
                           "WHERE supplier_address_id = ?", tuple(vals.values()) + (row["supplier_address_id"],))
            elif any(v is not None for v in vals.values()):
                at = db.execute("SELECT address_type_id FROM supplier_address_types WHERE tenant_id = ? "
                                "ORDER BY code = 'MAIN_OFFICE' DESC, sort_order LIMIT 1", (tid,)).fetchone()
                cols = dict(vals, tenant_id=tid, supplier_id=local_id, address_type_id=at[0] if at else None, is_primary=1)
                db.execute(f"INSERT INTO supplier_addresses ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                           tuple(cols.values()))
        if "phone" in changes:
            row, v = self._phone_row(local_id), _clean(changes["phone"])
            if row and v:
                db.execute("UPDATE supplier_phones SET country_code = NULL, area_code = NULL, extension = NULL, number = ?, "
                           "updated_at = datetime('now') WHERE supplier_phone_id = ?", (v, row["supplier_phone_id"]))
            elif row:
                db.execute("DELETE FROM supplier_phones WHERE supplier_phone_id = ?", (row["supplier_phone_id"],))
            elif v:
                pt = db.execute("SELECT phone_type_id FROM phone_types WHERE tenant_id = ? "
                                "ORDER BY code = 'OFFICE' DESC, sort_order LIMIT 1", (tid,)).fetchone()
                db.execute("INSERT INTO supplier_phones (tenant_id, supplier_id, phone_type_id, number, is_primary) "
                           "VALUES (?, ?, ?, ?, 1)", (tid, local_id, pt[0] if pt else None, v))
        if "email" in changes:
            row, v = self._email_row(local_id), _clean(changes["email"])
            if row and v:
                db.execute("UPDATE supplier_emails SET email_address = ?, updated_at = datetime('now') "
                           "WHERE supplier_email_id = ?", (v, row["supplier_email_id"]))
            elif row:
                db.execute("DELETE FROM supplier_emails WHERE supplier_email_id = ?", (row["supplier_email_id"],))
            elif v:
                db.execute("INSERT INTO supplier_emails (tenant_id, supplier_id, email_address, is_primary) "
                           "VALUES (?, ?, ?, 1)", (tid, local_id, v))
        if "amenities" in changes:
            want = set(changes["amenities"] or [])
            for code in AMENITY_CODES.values():
                opt = self._amenity_ids.get(code)
                if opt is None:
                    continue
                if code in want:
                    db.execute("INSERT OR IGNORE INTO supplier_amenities (tenant_id, supplier_id, amenity_option_id) "
                               "VALUES (?, ?, ?)", (tid, local_id, opt))
                else:
                    db.execute("DELETE FROM supplier_amenities WHERE tenant_id = ? AND supplier_id = ? AND amenity_option_id = ?",
                               (tid, local_id, opt))

    def create(self, values):
        cur = self.db.execute("INSERT INTO suppliers (tenant_id, supplier_name) VALUES (?, ?)",
                              (self.tenant_id, values["supplier_name"]))
        local_id = cur.lastrowid
        self.write(local_id, values)
        return local_id

    def match_pool(self):
        type_ids = [self._type_id(c) for c in self.type_codes if self._type_id(c)]
        if not type_ids:
            return []
        return [dict(r) for r in self.db.execute(
            f"""SELECT s.supplier_id AS id, s.supplier_name AS name, a.city_id, a.city_text,
                       COALESCE(c.label, a.city_text) AS city_label
                FROM suppliers s
                LEFT JOIN supplier_addresses a ON a.supplier_address_id = (
                    SELECT x.supplier_address_id FROM supplier_addresses x WHERE x.supplier_id = s.supplier_id
                    ORDER BY x.is_primary DESC, x.supplier_address_id LIMIT 1)
                LEFT JOIN cities c ON c.city_id = a.city_id
                WHERE s.tenant_id = ? AND s.is_deleted = 0 AND s.is_external_resource = 0
                  AND s.supplier_type_id IN ({','.join('?' * len(type_ids))})""", [self.tenant_id] + type_ids)]

    def display(self, field, value):
        if _empty(value):
            return ""
        if field == "address":
            place = _place_label(self.db, *value[1:])
            return " — ".join(x for x in (value[0], place) if x)
        if field == "supplier_type_id":
            r = self.db.execute("SELECT label FROM supplier_types WHERE supplier_type_id = ?", (value,)).fetchone()
            return r[0] if r else ""
        if field == "supplier_subtype_id":
            r = self.db.execute("SELECT label FROM supplier_subtypes WHERE supplier_subtype_id = ?", (value,)).fetchone()
            return r[0] if r else ""
        if field == "amenities":
            labels = {r[0]: r[1] for r in self.db.execute(
                "SELECT code, label FROM hotel_amenity_options WHERE tenant_id = ?", (self.tenant_id,))}
            return ", ".join(labels.get(c, c) for c in value)
        return str(value)


class AccommodationAdapter(SupplierAdapter):
    entity = "accommodation"
    type_codes = ("HOTEL", "RESORT")

    def type_code(self, p):
        return "RESORT" if p["property_type"] == "Resort" else "HOTEL"


class RestaurantAdapter(SupplierAdapter):
    entity = "restaurants"
    type_codes = ("RESTAURANT",)

    def type_code(self, p):
        return "RESTAURANT"


ADAPTERS = {"pois": PoiAdapter, "accommodation": AccommodationAdapter, "restaurants": RestaurantAdapter}
LOCAL_TABLE = {"pois": "points_of_interest", "accommodation": "suppliers", "restaurants": "suppliers"}


# ---- matching ------------------------------------------------------------------------------

def _city_key(city_id, city_text):
    if city_id:
        return ("id", city_id)
    return ("text", normalize(city_text or "")) if city_text else None


def _best_local(p, pool, city_label):
    """Strongest tenant record this catalog record may be: (local, how).
    Names are compared within the same city (the city's own name ignored);
    tenant records with no city at all can at most be a 'similar' hint."""
    key = _city_key(p["city_id"], p["city_text"])
    best, best_how = None, None
    alt = split_alt_names(p["alt_names"])
    for loc in pool:
        lkey = _city_key(loc["city_id"], loc["city_text"])
        if lkey is not None and lkey != key:
            continue
        how = compare(loc["name"], p["name"], alt, ignore=[x for x in (city_label, loc["city_label"]) if x])
        if how and lkey is None:
            how = "similar"
        if how and (best_how is None or _RANK[how] > _RANK[best_how]):
            best, best_how = loc, how
            if how == "same" and lkey is not None:
                break
    return best, best_how


def links_for(db, tenant_id, entity):
    return {r["catalog_id"]: r for r in db.execute(
        "SELECT * FROM tenant_catalog_links WHERE tenant_id = ? AND entity = ?", (tenant_id, entity))}


def plan(db, tenant_id, entity, catalog_ids=None):
    """What a sync would do with the catalog records this tenant doesn't have
    yet: [{catalog, city, action ('add' / 'link' / 'review'), match, how,
    default}], plus the already-linked records that would change."""
    a = ADAPTERS[entity](db, tenant_id)
    linked = links_for(db, tenant_id, entity)
    taken = {r["local_id"] for r in linked.values() if r["local_id"]}
    pool = [loc for loc in a.match_pool() if loc["id"] not in taken]
    items = []
    for p in a.catalog_rows(catalog_ids):
        if p["id"] in linked:
            continue
        city = _city_label(db, p["city_id"], p["city_text"])
        loc, how = _best_local(p, pool, city)
        items.append({"catalog": p, "city": city, "match": loc, "how": how})
    # A tenant record can only be linked to one catalog record: the strongest claim wins.
    claimed = set()
    for it in sorted(items, key=lambda x: -_RANK.get(x["how"], 0)):
        if it["match"] is None:
            it["action"], it["default"] = "add", "add"
        elif it["match"]["id"] in claimed:
            it["action"], it["default"], it["note"] = "review", "add", "Already matched to another catalog record"
        elif it["how"] == "same":
            it["action"], it["default"] = "link", "link"
            claimed.add(it["match"]["id"])
        else:
            it["action"] = "review"
            it["default"] = "link" if it["how"] == "sounds" else "add"
            if it["how"] == "sounds":
                claimed.add(it["match"]["id"])
    items.sort(key=lambda x: ({"review": 0, "link": 1, "add": 2}[x["action"]], x["city"] or "", x["catalog"]["name"]))
    return items


# ---- syncing one linked record -------------------------------------------------------------------

def sync_link(db, adapter, link, p=None, dry_run=False, first=False):
    """Bring one linked tenant record up to date with its catalog record.
    Returns [(field, kind)] where kind is 'updated' or 'pending'.
    first=True (a record just linked): empty tenant fields are filled; where
    the tenant already has a different value, theirs is kept quietly."""
    if not link["local_id"] or not adapter.alive(link["local_id"]):
        return []
    p = p or adapter.catalog_row(link["catalog_id"])
    if p is None:
        return []
    new_vals, cur_vals = adapter.platform_values(p), adapter.local_values(link["local_id"])
    baseline, pending, dismissed = _loads(link["baseline"]), _loads(link["pending"]), _loads(link["dismissed"])
    changes, result = {}, []
    for field, _label in adapter.fields:
        if field not in new_vals:
            continue
        new, cur = new_vals[field], cur_vals.get(field)
        if _same(new, cur):
            baseline[field] = _clean(new)
            pending.pop(field, None)
            continue
        untouched = field in baseline and _same(cur, baseline[field])
        if untouched or _empty(cur):
            if _empty(new) and not untouched:
                continue
            changes[field] = new
            baseline[field] = _clean(new)
            pending.pop(field, None)
            result.append((field, "updated"))
        elif _empty(new):
            pending.pop(field, None)  # the platform has nothing to offer here
        elif first:
            dismissed[field] = _clean(new)
        elif field in dismissed and _same(dismissed[field], new):
            pending.pop(field, None)
        elif not (field in pending and _same(pending[field], new)):
            pending[field] = _clean(new)
            result.append((field, "pending"))
    if dry_run:
        return result
    if changes:
        adapter.write(link["local_id"], changes)
    db.execute("UPDATE tenant_catalog_links SET baseline = ?, pending = ?, dismissed = ?, synced_at = datetime('now') "
               "WHERE link_id = ?", (json.dumps(baseline), json.dumps(pending) if pending else None,
                                     json.dumps(dismissed) if dismissed else None, link["link_id"]))
    return result


def _insert_link(db, tenant_id, entity, catalog_id, local_id, how):
    cur = db.execute("INSERT INTO tenant_catalog_links (tenant_id, entity, catalog_id, local_table, local_id, how) "
                     "VALUES (?, ?, ?, ?, ?, ?)", (tenant_id, entity, catalog_id, LOCAL_TABLE[entity], local_id, how))
    return db.execute("SELECT * FROM tenant_catalog_links WHERE link_id = ?", (cur.lastrowid,)).fetchone()


# ---- a whole sync run ------------------------------------------------------------------------

def apply(db, tenant_id, decisions=None, auto=False, entities=SYNC_ENTITIES, catalog_ids=None):
    """Run a sync for one tenant.

    decisions: {"<entity>:<catalog_id>": "add" | "link" | "skip"} from the
    preview screen (anything not listed takes the plan's default).
    auto=True (pushes after a platform change, new tenants): exact matches
    are linked, records with no match are added, possible matches are left
    for the SystemAdmin.
    catalog_ids: {entity: [ids]} to limit the run to some records.
    Returns {entity: {added, linked, skipped, updated, pending}}."""
    decisions = decisions or {}
    counts = {}
    for entity in entities:
        ids = (catalog_ids or {}).get(entity) if catalog_ids is not None else None
        if catalog_ids is not None and entity not in catalog_ids:
            continue
        a = ADAPTERS[entity](db, tenant_id)
        c = {"added": 0, "linked": 0, "skipped": 0, "updated": 0, "pending": 0}
        for it in plan(db, tenant_id, entity, ids):
            p = it["catalog"]
            if auto:
                choice = {"add": "add", "link": "link"}.get(it["action"])
            else:
                choice = decisions.get(f"{entity}:{p['id']}", it["default"])
                if choice == "link" and not it["match"]:
                    choice = "add"
            if choice == "add":
                values = a.platform_values(p)
                local_id = a.create(values)
                link = _insert_link(db, tenant_id, entity, p["id"], local_id, "added")
                db.execute("UPDATE tenant_catalog_links SET baseline = ?, synced_at = datetime('now') WHERE link_id = ?",
                           (json.dumps({f: _clean(v) for f, v in values.items()}), link["link_id"]))
                c["added"] += 1
            elif choice == "link":
                link = _insert_link(db, tenant_id, entity, p["id"], it["match"]["id"], "linked")
                sync_link(db, a, link, p, first=True)
                c["linked"] += 1
            elif choice == "skip":
                _insert_link(db, tenant_id, entity, p["id"], None, "skipped")
                c["skipped"] += 1
        # Existing links: push platform changes.
        wanted = None if ids is None else set(ids)
        for link in db.execute("SELECT * FROM tenant_catalog_links WHERE tenant_id = ? AND entity = ? AND how != 'skipped'",
                               (tenant_id, entity)).fetchall():
            if wanted is not None and link["catalog_id"] not in wanted:
                continue
            res = sync_link(db, a, link)
            if any(k == "updated" for _, k in res):
                c["updated"] += 1
            c["pending"] += sum(1 for _, k in res if k == "pending")
        counts[entity] = c
    db.execute("UPDATE tenants SET catalog_synced_at = datetime('now') WHERE tenant_id = ?", (tenant_id,))
    db.commit()
    return counts


def preview_updates(db, tenant_id, entity):
    """For the preview screen: how many linked records a sync would change."""
    a = ADAPTERS[entity](db, tenant_id)
    updated = pending = 0
    for link in db.execute("SELECT * FROM tenant_catalog_links WHERE tenant_id = ? AND entity = ? AND how != 'skipped'",
                           (tenant_id, entity)).fetchall():
        res = sync_link(db, a, link, dry_run=True)
        updated += any(k == "updated" for _, k in res)
        pending += sum(1 for _, k in res if k == "pending")
    return updated, pending


def synced_tenant_ids(db):
    return [r[0] for r in db.execute(
        "SELECT tenant_id FROM tenants WHERE is_platform = 0 AND catalog_synced_at IS NOT NULL")]


def push(db, entity, catalog_ids=None):
    """After a platform change: sync every tenant that has had its first
    catalog sync. catalog_ids=None means the whole catalog (after an import)."""
    if entity not in SYNC_ENTITIES:
        return {}
    out = {}
    for tid in synced_tenant_ids(db):
        out[tid] = apply(db, tid, auto=True, entities=(entity,),
                         catalog_ids=None if catalog_ids is None else {entity: list(catalog_ids)})
    return out


def forget_catalog_record(db, entity, catalog_id):
    """The platform record was deleted: tenants keep their copies, which
    simply stop syncing."""
    db.execute("DELETE FROM tenant_catalog_links WHERE entity = ? AND catalog_id = ?", (entity, catalog_id))


# ---- tenant side -----------------------------------------------------------------------------------

def link_for_local(db, tenant_id, local_table, local_id):
    return db.execute("SELECT * FROM tenant_catalog_links WHERE tenant_id = ? AND local_table = ? AND local_id = ?",
                      (tenant_id, local_table, local_id)).fetchone()


def differences(db, tenant_id, link):
    """[{field, label, mine, platform}] for the link's pending fields."""
    a = ADAPTERS[link["entity"]](db, tenant_id)
    pending = _loads(link["pending"])
    if not pending or not a.alive(link["local_id"]):
        return []
    cur = a.local_values(link["local_id"])
    return [{"field": f, "label": a.label(f), "mine": a.display(f, cur.get(f)) or "(blank)",
             "platform": a.display(f, v)} for f, v in pending.items()]


def resolve(db, tenant_id, link_id, field, accept):
    """Tenant's answer to one difference: accept=True takes the platform
    value; False keeps theirs (and stops offering that platform value)."""
    link = db.execute("SELECT * FROM tenant_catalog_links WHERE link_id = ? AND tenant_id = ?",
                      (link_id, tenant_id)).fetchone()
    if link is None:
        return False
    pending = _loads(link["pending"])
    if field not in pending:
        return False
    value = pending.pop(field)
    baseline, dismissed = _loads(link["baseline"]), _loads(link["dismissed"])
    if accept:
        ADAPTERS[link["entity"]](db, tenant_id).write(link["local_id"], {field: value})
        baseline[field] = value
        dismissed.pop(field, None)
    else:
        dismissed[field] = value
    db.execute("UPDATE tenant_catalog_links SET baseline = ?, pending = ?, dismissed = ? WHERE link_id = ?",
               (json.dumps(baseline), json.dumps(pending) if pending else None,
                json.dumps(dismissed) if dismissed else None, link_id))
    db.commit()
    return True


def pending_links(db, tenant_id, local_table=None):
    sql = "SELECT * FROM tenant_catalog_links WHERE tenant_id = ? AND pending IS NOT NULL"
    params = [tenant_id]
    if local_table:
        sql += " AND local_table = ?"
        params.append(local_table)
    return db.execute(sql, params).fetchall()


def pending_count(db, tenant_id, local_table=None):
    return sum(len(_loads(r["pending"])) for r in pending_links(db, tenant_id, local_table))


def view_context(db, tenant_id, local_table, local_id):
    """What the tenant's POI / Supplier view page shows about the catalog
    record behind it (None when the record didn't come from the catalog)."""
    link = link_for_local(db, tenant_id, local_table, local_id)
    if link is None:
        return None
    row, details = catalog_details(db, link["entity"], link["catalog_id"])
    return {"link": link, "how": link["how"], "details": details, "row": row,
            "diffs": differences(db, tenant_id, link),
            "checked_on": row["checked_on"] if row is not None else None,
            "source": row["source"] if row is not None else None}


def linked_local_ids(db, tenant_id, local_table):
    return {r[0] for r in db.execute(
        "SELECT local_id FROM tenant_catalog_links WHERE tenant_id = ? AND local_table = ? AND local_id IS NOT NULL",
        (tenant_id, local_table))}


def catalog_details(db, entity, catalog_id):
    """The catalog record's own details for the tenant's view page:
    [(label, value)] of every filled field the tenant's record doesn't hold."""
    spec = CATALOGS[entity]
    row = db.execute(
        f"""SELECT r.*, pt.label AS poi_type_label FROM {spec['table']} r
            LEFT JOIN poi_types pt ON pt.poi_type_id = {'r.poi_type_id' if entity == 'pois' else 'NULL'}
            WHERE r.{spec['pk']} = ?""", (catalog_id,)).fetchone()
    if row is None:
        return None, []
    skip = {"name", "poi_type_id", "year_founded", "address", "phone", "website", "property_type",
            "latitude", "longitude", "source", "checked_on"}
    if entity == "pois":
        skip.add("description" if row["description"] else "significance")
    else:
        skip.add("email")
    out = []
    amen = []
    for col, label, kind, opts in spec["fields"] + [("alt_names", "Other Names", "textarea", {}),
                                                    ("notes", "Notes", "textarea", {}),
                                                    ("email", "Email", "email", {})]:
        if col in skip or col in [x[0] for x in out]:
            continue
        v = row[col]
        if v is None or v == "":
            continue
        if kind == "yn":
            if opts.get("group") == "Amenities":
                if v == 1:
                    amen.append(label)
                continue
            v = "Yes" if v == 1 else "No"
        elif kind == "money":
            cur = row["currency"] if "currency" in row.keys() and row["currency"] else ""
            v = f"{cur} {v:,.0f}".strip()
        elif col == "alt_names":
            v = ", ".join(split_alt_names(v))
        if col == "currency":
            continue
        out.append((col, label, v))
    if amen:
        out.append(("amenities", "Amenities (catalog)", ", ".join(amen)))
    return row, [(label, v, col in ("description", "notes", "significance")) for col, label, v in out]
