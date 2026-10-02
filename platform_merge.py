"""
Duplicate finding, merging and un-merging for the platform catalogs
(platform_catalog.MERGEABLE: POIs, Accommodation, Restaurants, Embassies,
Transport Hubs).

* candidates()       -- existing records an incoming name may duplicate
                        (import wizard).
* find_duplicates()  -- likely duplicate pairs already in a catalog.
* merge()            -- folds one record into another: the caller picks, per
                        field, which side's value to keep (an empty kept
                        value is always filled from the other side); notes
                        are combined; the merged-away name becomes an
                        alternate name; every link to the removed record is
                        moved to the kept one. Logged in platform_merge_log.
* undo_merge()       -- puts both records and every moved link back.
"""
import json
from collections import defaultdict
from datetime import date

from fuzzy import NEARBY_KM, compare, normalize, split_alt_names
from platform_catalog import MERGEABLE, all_fields
from utils import haversine_km

REASONS = {"same": "Same name", "sounds": "Sounds alike", "similar": "Similar name", "near": "Same place (within 150 m)"}
_RANK = {"same": 4, "sounds": 3, "similar": 2, "near": 1}


def _rows(db, spec, where="", params=()):
    type_col = f", t.{spec['type_field']} AS type_value" if spec.get("type_field") else ", NULL AS type_value"
    return db.execute(
        f"""SELECT t.{spec['pk']} AS id, t.name, t.alt_names, t.city_id, t.city_text, t.latitude, t.longitude
                   {type_col}, COALESCE(c.label, t.city_text) AS city_label
            FROM {spec['table']} t LEFT JOIN cities c ON c.city_id = t.city_id
            WHERE t.is_active = 1 {where}""", params).fetchall()


def _city_key(row):
    return ("id", row["city_id"]) if row["city_id"] else ("text", normalize(row["city_text"] or ""))


def relation(a, b, same_type_only=False):
    """How two catalog rows relate: 'same' / 'sounds' / 'similar' (names, same
    city -- the city's own name is left out of the comparison) or 'near'
    (same type within 150 m), else None. same_type_only: names only match
    within the same type (Transport Hubs: a station is never an airport)."""
    if same_type_only and a["type_value"] is not None and b["type_value"] is not None and a["type_value"] != b["type_value"]:
        return None
    if _city_key(a) == _city_key(b) and _city_key(a)[1]:
        city_words = [x for x in (a["city_label"], b["city_label"]) if x]
        how = compare(a["name"], b["name"], split_alt_names(b["alt_names"]), ignore=city_words)
        if how:
            return how
    if (a["type_value"] == b["type_value"] and None not in (a["latitude"], a["longitude"], b["latitude"], b["longitude"])
            and haversine_km(a["latitude"], a["longitude"], b["latitude"], b["longitude"]) <= NEARBY_KM):
        return "near"
    return None


def candidates(db, entity, name, city_id=None, city_text=None, lat=None, lon=None, type_value=None, limit=5,
               city_label=None):
    """Existing records this incoming record may be, strongest first:
    [(row, how), ...]."""
    spec = MERGEABLE[entity]
    probe = {"name": name, "alt_names": None, "city_id": city_id, "city_text": city_text, "latitude": lat,
             "longitude": lon, "type_value": type_value, "city_label": city_label}
    where, params = [], []
    if city_id:
        where.append("t.city_id = ?")
        params.append(city_id)
    elif city_text:
        where.append("lower(t.city_text) = lower(?)")
        params.append(city_text)
    if lat is not None and lon is not None:
        d = 0.01  # ~1 km box; relation() applies the exact 150 m test
        where.append("(t.latitude BETWEEN ? AND ? AND t.longitude BETWEEN ? AND ?)")
        params += [lat - d, lat + d, lon - d, lon + d]
    if not where:
        return []
    found = []
    if city_label is None and city_id:
        r = db.execute("SELECT label FROM cities WHERE city_id = ?", (city_id,)).fetchone()
        probe["city_label"] = r[0] if r else None
    for row in _rows(db, spec, "AND (" + " OR ".join(where) + ")", params):
        how = relation(probe, row, spec.get("same_type_only", False))
        if how:
            found.append((row, how))
    found.sort(key=lambda x: -_RANK[x[1]])
    return found[:limit]


def find_duplicates(db, entity):
    """Likely duplicate pairs in a catalog: [(row_a, row_b, how), ...],
    strongest first. Compares within each city, plus same-type records
    within 150 m."""
    spec = MERGEABLE[entity]
    rows = _rows(db, spec)
    pairs = {}
    by_city = defaultdict(list)
    for r in rows:
        if _city_key(r)[1]:
            by_city[_city_key(r)].append(r)
    for group in by_city.values():
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                how = relation(a, b, spec.get("same_type_only", False)) or relation(b, a, spec.get("same_type_only", False))
                if how:
                    pairs[(a["id"], b["id"])] = (a, b, how)
    grid = defaultdict(list)  # ~1 km cells for the proximity check
    for r in rows:
        if r["latitude"] is not None and r["longitude"] is not None:
            grid[(round(r["latitude"], 2), round(r["longitude"], 2))].append(r)
    for (gy, gx), cell in grid.items():
        neighbours = [r for dy in (-0.01, 0, 0.01) for dx in (-0.01, 0, 0.01)
                      for r in grid.get((round(gy + dy, 2), round(gx + dx, 2)), [])]
        for a in cell:
            for b in neighbours:
                if a["id"] < b["id"] and (a["id"], b["id"]) not in pairs and relation(a, b) == "near":
                    pairs[(a["id"], b["id"])] = (a, b, "near")
    return sorted(pairs.values(), key=lambda p: (-_RANK[p[2]], (p[0]["city_label"] or ""), p[0]["name"]))


# ---- merge / undo ------------------------------------------------------------

def merge_fields(spec):
    """Columns a merge chooses between (everything editable except notes and
    alternate names, which are always combined)."""
    cols = [f[0] for f in all_fields(spec) if f[0] not in ("notes", "alt_names")]
    return cols + ["region_id", "country_id", "state_id", "state_province_text", "city_id", "city_text", "is_active"]


def combine_notes(kept_notes, removed_notes, removed_name):
    if not removed_notes:
        return kept_notes
    head = f"— merged from \"{removed_name}\", {date.today().isoformat()} —"
    return f"{kept_notes}\n\n{head}\n{removed_notes}" if kept_notes else removed_notes


def combine_alt_names(kept, removed):
    names = split_alt_names(kept["alt_names"]) + [removed["name"]] + split_alt_names(removed["alt_names"])
    seen, out = {normalize(kept["name"])}, []
    for n in names:
        if normalize(n) not in seen:
            seen.add(normalize(n))
            out.append(n)
    return "\n".join(out) or None


def merge(db, entity, keep_id, remove_id, take_from_removed=(), user_id=None):
    """Merge record remove_id into keep_id. take_from_removed: columns whose
    value should come from the removed record. Returns the merge_id."""
    spec = MERGEABLE[entity]
    t, pk = spec["table"], spec["pk"]
    kept = db.execute(f"SELECT * FROM {t} WHERE {pk} = ?", (keep_id,)).fetchone()
    removed = db.execute(f"SELECT * FROM {t} WHERE {pk} = ?", (remove_id,)).fetchone()
    if kept is None or removed is None or keep_id == remove_id:
        raise ValueError("Both records must exist and be different.")
    take = set(take_from_removed)
    updates = {}
    for col in merge_fields(spec):
        if col not in kept.keys():
            continue
        if col in take or (kept[col] in (None, "") and removed[col] not in (None, "")):
            updates[col] = removed[col]
    if "city_id" in take:  # location moves as a block
        for col in ("region_id", "country_id", "state_id", "state_province_text", "city_text"):
            updates[col] = removed[col]
    updates["notes"] = combine_notes(kept["notes"], removed["notes"], removed["name"])
    updates["alt_names"] = combine_alt_names(kept, removed)

    moved = []
    for ref in spec.get("references", []):
        table, col = ref[0], ref[1]
        extra = f" AND {ref[2]}" if len(ref) > 2 else ""
        for r in db.execute(f"SELECT rowid AS rid, * FROM {table} WHERE {col} = ?{extra}", (remove_id,)).fetchall():
            cur = db.execute(f"UPDATE OR IGNORE {table} SET {col} = ? WHERE rowid = ?", (keep_id, r["rid"]))
            if cur.rowcount:
                moved.append([table, col, r["rid"]])
            else:  # the kept record already has this exact link -- drop the copy, keep it for undo
                row = {k: r[k] for k in r.keys() if k != "rid"}
                db.execute(f"DELETE FROM {table} WHERE rowid = ?", (r["rid"],))
                moved.append([table, col, None, row])

    db.execute(f"UPDATE {t} SET {', '.join(f'{c} = ?' for c in updates)}, updated_at = datetime('now') "
               f"WHERE {pk} = ?", tuple(updates.values()) + (keep_id,))
    db.execute(f"DELETE FROM {t} WHERE {pk} = ?", (remove_id,))
    cur = db.execute(
        "INSERT INTO platform_merge_log (entity, kept_id, removed_id, kept_before, removed_row, repointed, merged_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (entity, keep_id, remove_id, json.dumps(dict(kept)), json.dumps(dict(removed)), json.dumps(moved), user_id))
    db.commit()
    return cur.lastrowid


def undo_merge(db, merge_id, user_id=None):
    """Reverse a merge: restore the kept record as it was, re-create the
    merged-away record with its original id, and move its links back."""
    log = db.execute("SELECT * FROM platform_merge_log WHERE merge_id = ?", (merge_id,)).fetchone()
    if log is None or log["undone_at"]:
        raise ValueError("That merge doesn't exist or was already undone.")
    later = db.execute(
        "SELECT 1 FROM platform_merge_log WHERE entity = ? AND merge_id > ? AND undone_at IS NULL "
        "AND (kept_id IN (?, ?) OR removed_id IN (?, ?))",
        (log["entity"], merge_id, log["kept_id"], log["removed_id"], log["kept_id"], log["removed_id"])).fetchone()
    if later:
        raise ValueError("A later merge involves one of these records. Undo that one first.")
    spec = MERGEABLE[log["entity"]]
    t, pk = spec["table"], spec["pk"]
    if db.execute(f"SELECT 1 FROM {t} WHERE {pk} = ?", (log["removed_id"],)).fetchone():
        raise ValueError("The merged-away record's id is in use again; it can't be restored automatically.")
    # Snapshots only restore columns the table still has (a column may have
    # been dropped since, e.g. the Accommodation rate columns in Oct 2026).
    live = {r[1] for r in db.execute(f"PRAGMA table_info({t})")}
    kept_before = {k: v for k, v in json.loads(log["kept_before"]).items() if k in live}
    removed_row = {k: v for k, v in json.loads(log["removed_row"]).items() if k in live}
    cols = [c for c in kept_before if c != pk]
    db.execute(f"UPDATE {t} SET {', '.join(f'{c} = ?' for c in cols)} WHERE {pk} = ?",
               tuple(kept_before[c] for c in cols) + (log["kept_id"],))
    db.execute(f"INSERT INTO {t} ({', '.join(removed_row)}) VALUES ({', '.join('?' * len(removed_row))})",
               tuple(removed_row.values()))
    for entry in json.loads(log["repointed"] or "[]"):
        table, col, rid = entry[0], entry[1], entry[2]
        if rid is not None:
            db.execute(f"UPDATE {table} SET {col} = ? WHERE rowid = ?", (log["removed_id"], rid))
        else:
            row = entry[3]
            db.execute(f"INSERT OR IGNORE INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                       tuple(row.values()))
    db.execute("UPDATE platform_merge_log SET undone_at = datetime('now'), undone_by = ? WHERE merge_id = ?",
               (user_id, merge_id))
    db.commit()
