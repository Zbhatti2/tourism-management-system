"""
Supplier Groups (Zeb, Oct 2026): every Supplier belongs to one of a fixed set
of groups, so the Suppliers list, the menu and system behaviour can work a
group at a time:

    Accommodation       Hotels, Resorts, guest houses
    F&B                 Restaurants, fast food, cafés, catering
    Transport           Airlines, car rental, bus services, trains
    External Resources  Agents, helpers, consultants, guides
    All Others          everything else

supplier_groups is GLOBAL (the same groups for every tenant). Each tenant's
Supplier Type belongs to a group (supplier_types.supplier_group_id, set in
Table Maintenance), and each Supplier carries its group
(suppliers.supplier_group_id). Triggers keep the supplier's group in step
with its Type -- whichever screen, import or sync changed it -- and an
External Resource is always in External Resources.
"""
import re

GROUPS = [
    # code, label, short description, icon, sort
    ("ACCOMMODATION", "Accommodation", "Hotels, Resorts, guest houses", "building", 1),
    ("FOOD_BEVERAGE", "F&B", "Restaurants, fast food, cafés, catering", "cup-hot", 2),
    ("TRANSPORT", "Transport", "Airlines, car rental, bus services, trains", "bus-front", 3),
    ("EXTERNAL_RESOURCES", "External Resources", "Agents, helpers, consultants, guides", "person-badge", 4),
    ("OTHER", "All Others", "Every other supplier", "three-dots", 5),
]
CODES = [g[0] for g in GROUPS]
URL_KEYS = {"ACCOMMODATION": "accommodation", "FOOD_BEVERAGE": "fnb", "TRANSPORT": "transport",
            "EXTERNAL_RESOURCES": "external", "OTHER": "other"}
BY_URL_KEY = {v: k for k, v in URL_KEYS.items()}

DDL = """
CREATE TABLE IF NOT EXISTS supplier_groups (
    supplier_group_id INTEGER PRIMARY KEY AUTOINCREMENT,
    code            TEXT NOT NULL UNIQUE,
    label           TEXT NOT NULL,
    description     TEXT,
    icon            TEXT,
    sort_order      INTEGER NOT NULL DEFAULT 0
);
"""

# A new Type without a group gets one from keywords in its code / label
# (a simpler form of guess() below, in SQL, so every way of adding a Type --
# Table Maintenance, Platform Lookups, seeding a new tenant -- gets one).
_KEYWORDS = [
    ("ACCOMMODATION", ["hotel", "resort", "motel", "guest house", "guesthouse", "lodge", "hostel", "accommodation",
                       "homestay", "villa"]),
    ("FOOD_BEVERAGE", ["restaurant", "food", "cafe", "café", "coffee", "catering", "bakery", "dhaba", "eatery",
                       "beverage"]),
    ("TRANSPORT", ["airline", "aviation", "transport", "car rental", "bus", "coach", "train", "rail", "taxi", "ferry",
                   "boat", "jeep", "rental", "limousine", "vehicle", "cruise"]),
    ("EXTERNAL_RESOURCES", ["guide", "agent", "consultant", "helper", "porter", "interpreter", "translator",
                            "photographer", "freelanc", "escort", "driver"]),
]
_TEXT = "lower(replace(COALESCE(NEW.code, ''), '_', ' ') || ' ' || COALESCE(NEW.label, ''))"
_GUESS_SQL = "(CASE " + " ".join(
    "WHEN " + " OR ".join(f"{_TEXT} LIKE '%{k}%'" for k in kws) + f" THEN '{group}'" for group, kws in _KEYWORDS
) + " ELSE 'OTHER' END)"

# Keep suppliers.supplier_group_id in step with the supplier's Type.
_GROUP_FOR_NEW = """(CASE WHEN NEW.is_external_resource = 1
        THEN (SELECT supplier_group_id FROM supplier_groups WHERE code = 'EXTERNAL_RESOURCES')
        ELSE COALESCE((SELECT supplier_group_id FROM supplier_types WHERE supplier_type_id = NEW.supplier_type_id),
                      (SELECT supplier_group_id FROM supplier_groups WHERE code = 'OTHER')) END)"""
TRIGGERS = f"""
DROP TRIGGER IF EXISTS trg_suppliers_group_insert;
CREATE TRIGGER trg_suppliers_group_insert AFTER INSERT ON suppliers
BEGIN
    UPDATE suppliers SET supplier_group_id = {_GROUP_FOR_NEW} WHERE supplier_id = NEW.supplier_id;
END;
DROP TRIGGER IF EXISTS trg_suppliers_group_update;
CREATE TRIGGER trg_suppliers_group_update AFTER UPDATE OF supplier_type_id, is_external_resource ON suppliers
BEGIN
    UPDATE suppliers SET supplier_group_id = {_GROUP_FOR_NEW} WHERE supplier_id = NEW.supplier_id;
END;
DROP TRIGGER IF EXISTS trg_supplier_types_group_insert;
CREATE TRIGGER trg_supplier_types_group_insert AFTER INSERT ON supplier_types
WHEN NEW.supplier_group_id IS NULL
BEGIN
    UPDATE supplier_types SET supplier_group_id = (SELECT supplier_group_id FROM supplier_groups WHERE code = {_GUESS_SQL})
    WHERE supplier_type_id = NEW.supplier_type_id;
END;
DROP TRIGGER IF EXISTS trg_supplier_types_group_update;
CREATE TRIGGER trg_supplier_types_group_update AFTER UPDATE OF supplier_group_id ON supplier_types
BEGIN
    UPDATE suppliers SET supplier_group_id = NEW.supplier_group_id
    WHERE supplier_type_id = NEW.supplier_type_id AND is_external_resource = 0;
END;
"""

# A first guess for a Type's group, from its code or label (the tenant can
# change it in Table Maintenance).
_GUESS = [
    ("ACCOMMODATION", r"hotel|resort|motel|guest ?house|lodge|hostel|\binn\b|\bcamp|accommodation|b&b|bed and breakfast|homestay|serviced apartment|villa"),
    ("FOOD_BEVERAGE", r"restaurant|food|\bcafe|café|coffee|catering|bakery|dhaba|\bbar\b|eatery|f&b|beverage|\btea\b"),
    ("TRANSPORT", r"airline|\bair\b|aviation|transport|\bcars?\b|car rental|\bbus|\bcoach|\btrain|\brail|taxi|\bcab\b|\bvans?\b|ferry|\bboat|jeep|rental|limousine|vehicle|cruise"),
    ("EXTERNAL_RESOURCES", r"guide|\bagent|consultant|helper|porter|interpreter|translator|photographer|freelanc|escort|\bcook\b|driver"),
]


def guess(code, label):
    text = f"{(code or '').replace('_', ' ')} {label or ''}".lower()
    for group, pattern in _GUESS:
        if re.search(pattern, text):
            return group
    return "OTHER"


def seed(db):
    for code, label, desc, icon, sort in GROUPS:
        db.execute("INSERT OR IGNORE INTO supplier_groups (code, label, description, icon, sort_order) VALUES (?, ?, ?, ?, ?)",
                   (code, label, desc, icon, sort))


def group_id(db, code):
    r = db.execute("SELECT supplier_group_id FROM supplier_groups WHERE code = ?", (code,)).fetchone()
    return r[0] if r else None


def assign_types(db, only_missing=True):
    """Give Supplier Types without a group (or all of them) a group from
    their code / label."""
    ids = {code: group_id(db, code) for code in CODES}
    sql = "SELECT supplier_type_id, code, label FROM supplier_types"
    if only_missing:
        sql += " WHERE supplier_group_id IS NULL"
    for r in db.execute(sql).fetchall():
        db.execute("UPDATE supplier_types SET supplier_group_id = ? WHERE supplier_type_id = ?",
                   (ids[guess(r["code"], r["label"])], r["supplier_type_id"]))


def refresh_suppliers(db):
    """Set every supplier's group from its Type (used once by the migration)."""
    ext, other = group_id(db, "EXTERNAL_RESOURCES"), group_id(db, "OTHER")
    db.execute("""UPDATE suppliers SET supplier_group_id = CASE WHEN is_external_resource = 1 THEN ?
                      ELSE COALESCE((SELECT t.supplier_group_id FROM supplier_types t
                                     WHERE t.supplier_type_id = suppliers.supplier_type_id), ?) END""", (ext, other))


def all_groups(db):
    return db.execute("SELECT * FROM supplier_groups ORDER BY sort_order").fetchall()


def by_url_key(db, key):
    code = BY_URL_KEY.get(key or "")
    if not code:
        return None
    return db.execute("SELECT * FROM supplier_groups WHERE code = ?", (code,)).fetchone()


def counts(db, tenant_id):
    """{group code: number of suppliers} for one tenant."""
    return {r[0]: r[1] for r in db.execute(
        """SELECT g.code, COUNT(s.supplier_id) FROM supplier_groups g
           LEFT JOIN suppliers s ON s.supplier_group_id = g.supplier_group_id AND s.tenant_id = ? AND s.is_deleted = 0
           GROUP BY g.code""", (tenant_id,))}


def menu():
    """Sidebar children for the Suppliers menu."""
    items = [{"key": "suppliers", "label": "All Suppliers", "icon": "list-ul", "endpoint": "suppliers.list_suppliers",
              "args": {"group": "all"}, "group": "all"}]
    for code, label, _d, icon, _s in GROUPS:
        items.append({"key": "suppliers", "label": label, "icon": icon, "endpoint": "suppliers.list_suppliers",
                      "args": {"group": URL_KEYS[code]}, "group": URL_KEYS[code]})
    return items
