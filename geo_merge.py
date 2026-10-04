"""
Duplicate provinces and cities in the global geography (Zeb, Oct 2026:
"Duplicates in the Provinces is confusing the addition of Cities into
Entity Records" -- "Khyber Pakhtunkhwa" and "Kyber Pakhtun Khawa").

* duplicates(): pairs of provinces (in the same country) or cities (in the
  same province) whose names are the same, sound alike or are near
  spellings -- shown on the Geography maintenance list with a Merge button.
* merge(): moves EVERYTHING that points at one entry to another -- every
  table with a foreign key to it, found from the schema (addresses of every
  kind, Points of Interest, the platform catalogs, packages, hubs,
  embassies...) -- then deletes the old entry and keeps its name as an
  alternate spelling, so imports that use it find the right one.
  Merging provinces also merges their cities: a city that exists in both
  (Peshawar under each spelling of the province) becomes one city.
* similar_existing(): the names a new entry looks like, so adding a
  duplicate needs a second look.
"""
from fuzzy import compare, normalize, similarity, sound_key, split_alt_names

GEO = {"regions": ("regions", "region_id", None), "countries": ("countries", "country_id", "region_id"),
       "states": ("states", "state_id", "country_id"), "cities": ("cities", "city_id", "state_id")}


def _joined_key(name):
    return sound_key(name).replace(" ", "")


def same_place(a, b):
    """'same', 'sounds', 'similar' or None for two place names (spaces don't
    count: "Kyber Pakhtun Khawa" / "Khyber Pakhtunkhwa"). Place names are
    short, so this is stricter than names of hotels: a short name (under 6
    letters) must be the same, a sounds-alike needs spellings 82% alike and
    a near spelling 90% (Sahiwal is not Shahwali, Dina is not Dinga)."""
    na, nb = normalize(a).replace(" ", ""), normalize(b).replace(" ", "")
    if not na or not nb:
        return None
    if na == nb:
        return "same"
    if min(len(na), len(nb)) < 6:
        return None
    sim = similarity(a, b)
    how = compare(a, b)
    if how == "same":
        return "same"
    if (how == "sounds" or (_joined_key(a) and _joined_key(a) == _joined_key(b))) and sim >= 0.82:
        return "sounds"
    return "similar" if sim >= 0.9 and min(len(na), len(nb)) >= 7 else None


def references(db, table, configured=()):
    """[(table, column)] of every column pointing at `table`."""
    out = [(r["table"], r["fk"]) for r in configured]
    for (t,) in db.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'").fetchall():
        for fk in db.execute(f"PRAGMA foreign_key_list('{t}')").fetchall():
            if fk[2] == table and (t, fk[3]) not in out:
                out.append((t, fk[3]))
    return out


def _has_col(db, table, col):
    return any(r[1] == col for r in db.execute(f"PRAGMA table_info('{table}')"))


def _add_alt(db, table, pk, rid, name):
    if not _has_col(db, table, "alt_names"):
        return
    row = db.execute(f"SELECT label, alt_names FROM {table} WHERE {pk} = ?", (rid,)).fetchone()
    if row is None or normalize(name) == normalize(row[0]):
        return
    alts = split_alt_names(row[1])
    if any(normalize(a) == normalize(name) for a in alts):
        return
    db.execute(f"UPDATE {table} SET alt_names = ? WHERE {pk} = ?", ("\n".join(alts + [name]), rid))


def _move(db, table, col, old, new):
    """Re-point table.col from old to new; rows that would clash with a
    unique key are left (and counted)."""
    cur = db.execute(f"UPDATE OR IGNORE {table} SET {col} = ? WHERE {col} = ?", (new, old))
    return cur.rowcount


def _merge_distances(db, old, new):
    for r in db.execute("SELECT * FROM city_distances WHERE city_a_id = ? OR city_b_id = ?", (old, old)).fetchall():
        other = r["city_b_id"] if r["city_a_id"] == old else r["city_a_id"]
        if other == new:
            db.execute("DELETE FROM city_distances WHERE distance_id = ?", (r["distance_id"],))
            continue
        a, b = sorted((new, other))
        twin = db.execute("SELECT * FROM city_distances WHERE city_a_id = ? AND city_b_id = ?", (a, b)).fetchone()
        if twin:  # keep the existing pair, filling its gaps
            for col in ("road_km", "drive_minutes", "drive_minutes_max", "route_name", "rail_available", "source", "notes"):
                if col in r.keys() and twin[col] in (None, "") and r[col] not in (None, ""):
                    db.execute(f"UPDATE city_distances SET {col} = ? WHERE distance_id = ?", (r[col], twin["distance_id"]))
            db.execute("DELETE FROM city_distances WHERE distance_id = ?", (r["distance_id"],))
        else:
            db.execute("UPDATE city_distances SET city_a_id = ?, city_b_id = ? WHERE distance_id = ?", (a, b, r["distance_id"]))


def merge_city(db, old, new, also_delete=True):
    """Everything pointing at city `old` now points at `new`."""
    if old == new:
        return 0
    o = db.execute("SELECT * FROM cities WHERE city_id = ?", (old,)).fetchone()
    moved = 0
    for t, col in references(db, "cities"):
        if t == "city_distances":
            continue
        moved += _move(db, t, col, old, new)
    _merge_distances(db, old, new)
    if o is not None:
        n = db.execute("SELECT * FROM cities WHERE city_id = ?", (new,)).fetchone()
        for col in ("latitude", "longitude", "timezone", "altitude_m"):
            if col in o.keys() and n[col] is None and o[col] is not None:
                db.execute(f"UPDATE cities SET {col} = ? WHERE city_id = ?", (o[col], new))
        if "is_checkpoint" in o.keys() and o["is_checkpoint"]:
            db.execute("UPDATE cities SET is_checkpoint = 1 WHERE city_id = ?", (new,))
        for alt in [o["label"]] + split_alt_names(o["alt_names"] if "alt_names" in o.keys() else None):
            _add_alt(db, "cities", "city_id", new, alt)
    if also_delete:
        db.execute("DELETE FROM cities WHERE city_id = ?", (old,))
    return moved


def merge_state(db, old, new, also_delete=True):
    """Cities of province `old` move to `new` (a city both have becomes
    one), and everything else pointing at `old` follows."""
    if old == new:
        return 0
    moved = 0
    target_cities = db.execute("SELECT city_id, label, alt_names FROM cities WHERE state_id = ?", (new,)).fetchall()
    for c in db.execute("SELECT city_id, label FROM cities WHERE state_id = ?", (old,)).fetchall():
        twin = next((t for t in target_cities
                     if same_place(c["label"], t["label"]) in ("same", "sounds")
                     or any(normalize(a) == normalize(c["label"]) for a in split_alt_names(t["alt_names"]))), None)
        if twin:
            moved += merge_city(db, c["city_id"], twin["city_id"]) + 1
        else:
            db.execute("UPDATE cities SET state_id = ? WHERE city_id = ?", (new, c["city_id"]))
            moved += 1
    for t, col in references(db, "states"):
        if t == "cities":
            continue
        moved += _move(db, t, col, old, new)
    o = db.execute("SELECT label, alt_names FROM states WHERE state_id = ?", (old,)).fetchone() \
        if _has_col(db, "states", "alt_names") else db.execute("SELECT label FROM states WHERE state_id = ?", (old,)).fetchone()
    if o is not None:
        for alt in [o["label"]] + (split_alt_names(o["alt_names"]) if "alt_names" in o.keys() else []):
            _add_alt(db, "states", "state_id", new, alt)
    if also_delete:
        db.execute("DELETE FROM states WHERE state_id = ?", (old,))
    return moved


def merge(db, table_key, old, new, also_delete=True):
    if table_key == "cities":
        return merge_city(db, old, new, also_delete)
    if table_key == "states":
        return merge_state(db, old, new, also_delete)
    table, pk, _parent = GEO[table_key]
    moved = sum(_move(db, t, col, old, new) for t, col in references(db, table))
    if also_delete:
        db.execute(f"DELETE FROM {table} WHERE {pk} = ?", (old,))
    return moved


def duplicates(db, table_key, parent_filter=None, limit=60):
    """[{a, b, how}] -- pairs of rows under the same parent that are likely
    the same place; a is the one with less use (the one to merge away)."""
    if table_key not in ("states", "cities"):
        return []
    table, pk, parent = GEO[table_key]
    sql = f"SELECT {pk} AS id, label, {parent} AS parent_id" + (", alt_names" if _has_col(db, table, "alt_names") else "") \
        + f" FROM {table}" + (f" WHERE {parent} = ?" if parent_filter else "")
    rows = db.execute(sql, (parent_filter,) if parent_filter else ()).fetchall()
    groups = {}
    for r in rows:
        groups.setdefault(r["parent_id"], []).append(r)
    use = _usage(db, table, pk)
    out = []
    for items in groups.values():
        keyed = [(r, _joined_key(r["label"]), normalize(r["label"]).replace(" ", "")) for r in items]
        for i, (a, ka, na) in enumerate(keyed):
            for b, kb, nb in keyed[i + 1:]:
                if not ka or not kb or ka[0] != kb[0] or min(len(na), len(nb)) < 0.6 * max(len(na), len(nb)):
                    continue
                how = same_place(a["label"], b["label"])
                if not how:
                    continue
                x, y = (a, b) if use.get(a["id"], 0) <= use.get(b["id"], 0) else (b, a)
                out.append({"a": dict(x, uses=use.get(x["id"], 0)), "b": dict(y, uses=use.get(y["id"], 0)), "how": how})
                if len(out) >= limit:
                    return out
    return out


def _usage(db, table, pk):
    counts = {}
    for t, col in references(db, table):
        for rid, n in db.execute(f"SELECT {col}, COUNT(*) FROM {t} WHERE {col} IS NOT NULL GROUP BY {col}"):
            counts[rid] = counts.get(rid, 0) + n
    return counts


def similar_existing(db, table_key, label, parent_id=None):
    """Names in the table (under the same parent) that look like `label`."""
    if table_key not in GEO or not label:
        return []
    table, pk, parent = GEO[table_key]
    sql = f"SELECT label FROM {table}" + (f" WHERE {parent} = ?" if parent and parent_id else "")
    rows = db.execute(sql, (parent_id,) if parent and parent_id else ()).fetchall()
    return [r[0] for r in rows if same_place(label, r[0])][:5]
