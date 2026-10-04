"""
TMS Agents: Accommodation and Restaurants from a PDF (Zeb, Oct 2026: "Pdf
Agents (Content and Images) ... This only exists for POIs, but Accommodation
and Restaurants have to be added").

The same reading as pdf_poi_agent.py -- the same upload, parts, photo
extraction and page images, the same matching of names (spelling, sounds-like,
other spellings, city, coordinates), the same "Several per PDF" / "One PDF per
property" modes and the same review queue -- for the platform Accommodation
and Restaurants catalogs:

* a known hotel / restaurant: each value the PDF states that the catalog
  lacks or has differently becomes a proposal; what else the PDF says
  (history, facilities, dining, policies) is offered as text to add to its
  Notes -- only the sentences it doesn't already have (text_dedup.py);
* a new one: its values are proposed as a new record, created when its
  values are approved;
* its photos go into one Master Image Catalog upload for curation, each
  assigned to the property whose pages it is on.

Approved changes reach tenants through Catalog Sync (with their images).
"""
import base64
import json
import re

import agent_runs
import pdf_poi_agent as base
import platform_agents as pa
from platform_catalog import PROPERTY_TYPES

PdfError = base.PdfError

SPECS = {
    "pdf_accommodation": {
        "entity": "platform_accommodation", "new_entity": "platform_accommodation_new",
        "table": "platform_accommodation", "pk": "accommodation_id", "sync": "accommodation",
        "image_kind": "platform_accommodation", "singular": "property", "plural": "properties",
        "label": "Accommodation", "agent_name": "TMS Agent: Accommodation from PDF",
        "what": "hotels, resorts, guest houses, motels, lodges, rest houses and other places to stay",
        "modes": {"multi": "Several properties per PDF", "per_poi": "One PDF per property"},
    },
    "pdf_restaurants": {
        "entity": "platform_restaurants", "new_entity": "platform_restaurants_new",
        "table": "platform_restaurants", "pk": "restaurant_id", "sync": "restaurants",
        "image_kind": "platform_restaurant", "singular": "restaurant", "plural": "restaurants",
        "label": "Restaurants", "agent_name": "TMS Agent: Restaurants from PDF",
        "what": "restaurants, cafés, food streets, eateries and other places to eat",
        "modes": {"multi": "Several restaurants per PDF", "per_poi": "One PDF per restaurant"},
    },
}
BY_NEW_ENTITY = {s["new_entity"]: s for s in SPECS.values()}
BY_ENTITY = {s["entity"]: s for s in SPECS.values()}

FIELD_HINTS = {
    "star_rating": "official star rating 1-5, only when the document states one",
    "rating_note": "the rating or class as the document words it",
    "rooms": "number of rooms",
    "ref_room_rate": "a DOUBLE room per night converted to US dollars, a number; give the original price and "
                     "currency in rate_note",
    "rating": "a traveller score out of 5",
    "class": "e.g. Upscale, Casual, Fast food, Hotel dining",
    "cuisine": "comma-separated",
    "currency": "3-letter code of the prices, e.g. PKR",
    "price_from": "typical price per person, low end, a number",
    "price_to": "typical price per person, high end, a number",
    "group_suitable": "Yes if it can seat a tour group of 15-30",
}


def spec_of(agent_key):
    return SPECS[agent_key]


def fields_of(spec):
    return pa.FIELDS[spec["entity"]]


def _tool(spec):
    fields = fields_of(spec)
    props = {f: {"type": "string", "description": (FIELD_HINTS.get(f) or label) + (
        " (Yes or No, only when the document says)" if kind == "yesno" else "")}
             for f, (label, kind) in fields.items() if f not in ("latitude", "longitude")}
    props.update({
        "name": {"type": "string"}, "alt_names": {"type": "array", "items": {"type": "string"}},
        "city": {"type": "string"}, "country": {"type": "string"},
        "notes": {"type": "string",
                  "description": "Everything else the document says about the place -- history, setting, facilities, "
                                 "rooms, dining, prices, policies, who runs it -- as the document gives it"},
        "coordinates": {"type": "string", "description": "Coordinates exactly as the document gives them"},
        "photos": {"type": "array", "items": {"type": "string"},
                   "description": "The ids (e.g. P9-3) of the photos that show this place"},
        "main_pages": {"type": "array", "items": {"type": "integer"},
                       "description": "Page numbers (as printed in the page list) where this place is described"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    })
    if spec["entity"] == "platform_accommodation":
        props["property_type"] = {"type": "string", "enum": PROPERTY_TYPES}
        props["rate_note"] = {"type": "string", "description": "The room price as the document gives it, with its "
                                                               "currency, room type and season"}
    return {"name": "report_places", "description": f"Report the {spec['plural']} described in this part of the PDF.",
            "input_schema": {"type": "object", "properties": {"places": {"type": "array", "items": {
                "type": "object", "properties": props, "required": ["name", "main_pages"]}}}, "required": ["places"]}}


def read_part(client, spec, part_pdf, first, last, file_name, meter, photos=(), one_place=None):
    """[place dicts] for one part of the PDF (page numbers are the
    original PDF's)."""
    scope = (f"The whole document is about ONE {spec['singular']}{': ' + one_place if one_place else ''}. Report "
             "exactly one place, gathering everything the document says about it.\n\n") if one_place is not None else (
        f"List every {spec['singular']} that these pages DESCRIBE (not ones only mentioned in passing).\n\n")
    prompt = (
        f"This is pages {first}-{last} of the PDF \"{file_name}\". It is being used to build a tourism platform's "
        f"catalog of {spec['label']} ({spec['what']}).\n\n" + scope +
        "For each place give only what the document states:\n"
        "- name: as the document spells it (without any list number); other spellings or names in alt_names;\n"
        "- city or nearest town, and country;\n"
        "- the catalog fields (see the tool), each only when stated;\n"
        "- address or landmark, phone, email, website;\n"
        "- coordinates exactly as written;\n"
        "- notes: everything else the document says about the place, in its own words as far as possible, "
        "keeping every fact, without repeating the fields above;\n"
        f"- main_pages: the page numbers ({first}-{last}, counting the first page of this part as {first});\n"
        "- confidence (0-1) that the details are stated clearly.\n"
        "Leave out anything the document doesn't state.")
    content = [{"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                               "data": base64.b64encode(part_pdf).decode("ascii")}}]
    if photos:
        prompt += ("\n\nThe photos in these pages follow, each with its id, page and position. In each place's "
                   "photos, list the ids of the photos that show that place. Leave out photos that show no listed place.")
    content.append({"type": "text", "text": prompt})
    for ph in photos:
        content.append({"type": "text", "text": f"Photo {ph['id']}: page {ph['page']}, {base.where_on_page(ph['box'])}"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                    "data": base.thumbnail(ph["raw"])}})
    content.append({"type": "text", "text": "Call report_places once."})
    resp = agent_runs.create_message(client, model=pa.model(), max_tokens=16000, tools=[_tool(spec)],
                                     tool_choice={"type": "tool", "name": "report_places"},
                                     messages=[{"role": "user", "content": content}])
    meter(getattr(resp, "usage", None))
    places = []
    for b in getattr(resp, "content", []) or []:
        if getattr(b, "type", None) == "tool_use" and getattr(b, "name", None) == "report_places":
            places = (b.input or {}).get("places") or []
    ids = {ph["id"] for ph in photos}
    out = []
    for p in places:
        name = (p.get("name") or "").strip()
        if not name:
            continue
        pages = sorted({int(x) for x in (p.get("main_pages") or []) if str(x).lstrip("-").isdigit()
                        and first <= int(x) <= last})
        p = dict(p, name=name[:200], main_pages=pages, photos=[x for x in (p.get("photos") or []) if x in ids])
        p["history"] = p.pop("notes", None)  # merge_places joins 'history' without repeating sentences
        out.append(p)
    if one_place is not None and len(out) > 1:
        out = [base.merge_places(out, force=True)[0]]
    return out


def catalog(db, spec):
    from fuzzy import split_alt_names
    return [{"id": r[spec["pk"]], "name": r["name"], "alt": split_alt_names(r["alt_names"]),
             "city": (r["city_label"] or r["city_text"] or ""), "row": r}
            for r in db.execute(f"""SELECT p.*, ci.label AS city_label FROM {spec['table']} p
                                    LEFT JOIN cities ci ON ci.city_id = p.city_id""")]


# ---- the run -------------------------------------------------------------------------------------------------------

def start(db, agent_key, files, params, user_id):
    spec = spec_of(agent_key)
    params = dict(params, mode=params.get("mode") if params.get("mode") in spec["modes"] else "multi")
    return base.start(db, files, params, user_id, agent_key=agent_key, modes=spec["modes"])


def run(db, run_id, agent_key, client, log, meter, cancelled):
    """Called by platform_agents.run. Returns the number of proposals."""
    import image_catalog as ic
    spec = spec_of(agent_key)
    ups = base.uploads(db, run_id)
    if not ups:
        raise PdfError("The uploaded PDF is missing.")
    params = json.loads(db.execute("SELECT params FROM platform_agent_runs WHERE run_id = ?", (run_id,)).fetchone()[0] or "{}")
    mode = params.get("mode") or "multi"
    known = catalog(db, spec)
    owners = [{"id": p["id"], "name": p["name"], "city": p["city"]} for p in known]
    places, all_photos = [], {}
    for n, up in enumerate(ups):
        db.execute("UPDATE platform_agent_runs SET items_done = ?, heartbeat_at = datetime('now') WHERE run_id = ?", (n, run_id))
        db.commit()
        if cancelled():
            return 0
        data, file_name = up["file_data"], up["file_name"]
        n_pages = min(up["page_count"] or base.inspect(data), base.MAX_PAGES)
        photos = base.extract_images(data, lambda t: log(f"{file_name}: {t}"))
        for ph in photos:
            ph["upload_id"] = up["upload_id"]
            ph["key"] = f"{up['upload_id']}:{ph['id']}"
            all_photos[ph["key"]] = ph
        per_page = {}
        for ph in photos:
            per_page[ph["page"]] = per_page.get(ph["page"], 0) + 1
        chunks = base.plan_parts(n_pages, per_page)
        one, file_rec = None, None
        if mode == "per_poi":
            one = base.name_from_file(file_name)
            oid, how, note, _rest = ic.match_file(one, owners, spec["label"])
            if oid and how == "exact":
                file_rec = next(p for p in known if p["id"] == oid)
                log(f"{file_name}: file name matches “{file_rec['name']}”")
            elif oid:
                log(f"{file_name}: {note}")
        log(f"{file_name}: {up['page_count']} page(s), {len(photos)} photo(s), read in {len(chunks)} part(s)"
            + (f" (only the first {base.MAX_PAGES} pages)" if (up["page_count"] or 0) > base.MAX_PAGES else ""))
        found_here = []
        for first, last in chunks:
            if cancelled():
                return 0
            db.execute("UPDATE platform_agent_runs SET heartbeat_at = datetime('now') WHERE run_id = ?", (run_id,))
            db.commit()
            part_photos = [ph for ph in photos if first <= ph["page"] <= last]
            log(f"   pages {first}-{last}: reading…")
            try:
                found = read_part(client, spec, base.sub_pdf(data, first, last), first, last, file_name, meter,
                                  part_photos, one_place=one)
            except Exception as e:
                fatal = pa.explain_api_error(e)
                if fatal:
                    raise pa.AgentError(fatal) from e
                log(f"   failed: {e}")
                continue
            log(f"   {len(found)} place(s): " + "; ".join(p["name"] for p in found[:12]) + ("…" if len(found) > 12 else ""))
            for p in found:
                p["photos"] = [f"{up['upload_id']}:{x}" for x in p.get("photos") or []]
                p["upload_id"], p["file_name"] = up["upload_id"], file_name
            found_here += found
        if mode == "per_poi" and found_here:
            p = base.merge_places(found_here, force=True)[0]
            p["photos"] = [ph["key"] for ph in photos]
            if file_rec:
                p["id_hint"] = file_rec["id"]
            found_here = [p]
        places += found_here
    places = base.merge_places(places)
    total = propose(db, run_id, spec, places, params, client=client, meter=meter, log=log)
    log(f"{total} value(s) proposed for {len(places)} place(s)")
    db.execute("UPDATE platform_agent_runs SET items_done = ?, proposals = ?, heartbeat_at = datetime('now') WHERE run_id = ?",
               (len(ups), total, run_id))
    db.commit()
    if cancelled():
        return total
    stage_images(db, run_id, spec, ups, places, all_photos, log)
    return total


def _existing_text(db, spec, rec):
    parts = [rec["row"]["notes"] or ""]
    parts += [r[0] for r in db.execute(
        "SELECT proposed_value FROM platform_agent_proposals WHERE entity = ? AND record_key = ? "
        "AND field = 'notes' AND status = 'pending'", (spec["entity"], str(rec["id"])))]
    return "\n".join(x for x in parts if x)


def _shown(kind, value):
    """A stored value as the agent would state it (amenities: Yes / No)."""
    if kind == "yesno" and value in (0, 1):
        return "Yes" if value else "No"
    return value


def propose(db, run_id, spec, places, params, client=None, meter=lambda u: None, log=lambda t: None):
    """Write the proposals: changes to known records, and new ones."""
    import text_dedup
    known_all = catalog(db, spec)
    by_id = {p["id"]: p for p in known_all}
    fields = fields_of(spec)
    total, new_n, dup_n = 0, 0, 0
    checks = []
    for i, p in enumerate(places):
        rec = by_id.get(p.get("id_hint")) if p.get("id_hint") else None
        how = "file name" if rec else None
        if rec is None:
            rec, how = base.match(p, known_all)
        p["_known"], p["_how"] = rec, how
        if rec and p.get("history"):
            existing = _existing_text(db, spec, rec)
            if existing.strip():
                left, _share = text_dedup.wording_check(existing, p["history"])
                if not left:
                    p["_dup"] = True
                    continue
                checks.append({"key": str(i), "name": rec["name"], "existing": existing, "new": left})
    verdicts = {}
    if checks and client is not None:
        try:
            verdicts = text_dedup.meaning_check(client, pa.model(), checks, meter,
                                                create=lambda **kw: agent_runs.create_message(client, **kw))
        except Exception as e:
            log(f"   duplicate-text check failed ({e}); new text is proposed unchecked")
    for i, p in enumerate(places):
        rec, how = p.pop("_known", None), p.pop("_how", None)
        conf = p.get("confidence")
        conf = float(conf) if isinstance(conf, (int, float)) else None
        src = base.source_ref(run_id, p["main_pages"], p.get("upload_id"))
        pages_note = (f"{p.get('file_name') or 'PDF'}, page{'s' if len(p['main_pages']) != 1 else ''} "
                      f"{', '.join(map(str, p['main_pages'][:8]))}") if p["main_pages"] else (p.get("file_name") or None)
        lat, lon = base.coords_of(p)
        rate_note = (p.get("rate_note") or "").strip() or None
        rows = []  # (field, current, value, note)
        if rec:
            p["rec_id"] = rec["id"]
            key, label, entity = str(rec["id"]), rec["name"], spec["entity"]
            if how in ("sounds", "similar"):
                label += f"  (matched “{p['name']}” by a similar name — check)"
            for f, (_l, kind) in fields.items():
                if f in ("latitude", "longitude"):
                    continue
                value, err = pa.validate(kind, p.get(f))
                if err:
                    continue
                cur = _shown(kind, rec["row"][f])
                if cur not in (None, "") and str(cur).strip().lower() == str(value).strip().lower():
                    continue
                rows.append((f, None if cur in (None, "") else str(cur), value, rate_note if f == "ref_room_rate" else None))
            if lat is not None and rec["row"]["latitude"] is None:
                rows += [("latitude", None, lat, None), ("longitude", None, lon, None)]
            if p.pop("_dup", False):
                dup_n += 1
            elif p.get("history"):
                text, note = p["history"], None
                v = verdicts.get(str(i))
                if v is not None:
                    if v["verdict"] == "duplicate" or not v["new_text"]:
                        text = None
                        dup_n += 1
                    else:
                        text = v["new_text"]
                        note = ("Conflicts with what's stored: " + "; ".join(v["conflicts"])) if v["conflicts"] else \
                            "New facts only; the rest is already in its Notes."
                if text:
                    cur_notes = rec["row"]["notes"]
                    rows.append(("notes", f"{len(cur_notes)} characters already" if cur_notes else None,
                                 f"{text.strip()}\n{base._credit(p)}", note))
        else:
            new_n += 1
            key, entity = f"new:{run_id}:{new_n}", spec["new_entity"]
            city = (p.get("city") or "").strip()
            label = f"{p['name']} (new{', ' + city if city else ''})"
            p["new_key"] = key
            rows.append(("name", None, p["name"], None))
            if city:
                rows.append(("city", None, city + (f", {p['country']}" if p.get("country") else ""), None))
            if spec["entity"] == "platform_accommodation":
                ptype = p.get("property_type") if p.get("property_type") in PROPERTY_TYPES else params.get("property_type")
                if ptype:
                    rows.append(("property_type", None, ptype, None))
            for f, (_l, kind) in fields.items():
                if f in ("latitude", "longitude"):
                    continue
                value, err = pa.validate(kind, p.get(f))
                if not err:
                    rows.append((f, None, value, rate_note if f == "ref_room_rate" else None))
            if lat is not None:
                rows += [("latitude", None, lat, None), ("longitude", None, lon, None)]
            if p.get("history"):
                rows.append(("notes", None, f"{p['history'].strip()}\n{base._credit(p)}", None))
        for f, cur, value, note in rows:
            db.execute("""INSERT INTO platform_agent_proposals (run_id, entity, record_key, record_label, field,
                              current_value, proposed_value, confidence, source_url, note)
                          VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                       (run_id, entity, key, label, f, cur, str(value), conf, src,
                        " · ".join(x for x in (note, pages_note) if x) or None))
            total += 1
    if dup_n:
        log(f"   {dup_n} place(s): their text is already in the catalog -- nothing proposed for Notes")
    db.commit()
    return total


def stage_images(db, run_id, spec, ups, places, all_photos, log):
    """The PDF's photos in one Master Image Catalog upload for curation,
    each with the place it shows."""
    import image_catalog as ic
    from platform_lookups import platform_tenant_id
    if not all_photos:
        return None
    owner_of, by_page = {}, {}
    for p in places:
        for k in p.get("photos") or []:
            owner_of.setdefault(k, p)
        for pg in p.get("main_pages") or []:
            by_page.setdefault((p.get("upload_id"), pg), []).append(p)
    for key, ph in all_photos.items():
        if key not in owner_of and len(by_page.get((ph["upload_id"], ph["page"]), [])) == 1:
            owner_of[key] = by_page[(ph["upload_id"], ph["page"])][0]
    names = {u["upload_id"]: u["file_name"] for u in ups}
    files, meta = [], {}
    many = len(ups) > 1
    for key, ph in all_photos.items():
        fname = (f"{re.sub(r'[^A-Za-z0-9]+', '-', names[ph['upload_id']])[:40]}-" if many else "") + ph["name"]
        m = {"source_url": base.source_ref(run_id, [ph["page"]], ph["upload_id"] if many else None),
             "licence": f"From “{names[ph['upload_id']]}”, page {ph['page']}"}
        p = owner_of.get(key)
        if p:
            m["title"] = f"{p['name']} — page {ph['page']}"
            if p.get("rec_id"):
                m["owner_id"] = p["rec_id"]
            elif p.get("new_key"):
                m["new_key"], m["new_name"] = p["new_key"], p["name"]
        files.append((fname, ph["raw"]))
        meta[fname] = m
    pid = platform_tenant_id(db)
    label = names[ups[0]["upload_id"]] if not many else f"{len(ups)} PDFs"
    batch_id = ic.create_batch(db, pid, [], source="AI Agent", contributor=spec["agent_name"], kind=spec["image_kind"],
                               upload_name=f"{label} (photos)")
    ic.add_items(db, pid, batch_id, files, meta=meta)
    for name, m in meta.items():
        if m.get("new_key"):
            db.execute("UPDATE image_import_items SET match_note = ? WHERE batch_id = ? AND file_name = ?",
                       (f"For the new {spec['singular']} “{m['new_name']}”: approve it in the TMS Agents review queue "
                        f"and this photo is assigned to it. [{m['new_key']}]", batch_id, name))
    db.execute("UPDATE platform_agent_runs SET batch_id = ? WHERE run_id = ?", (batch_id, run_id))
    db.commit()
    matched = sum(1 for m in meta.values() if m.get("owner_id") or m.get("new_key"))
    log(f"{len(files)} photo(s) staged for curation ({matched} assigned to a place; the rest: choose while curating)")
    return batch_id


# ---- approving a new record -----------------------------------------------------------------------------------------

NEW_FIELDS = ("name", "city", "property_type")


def create_new_record(db, record_key, new_entity, user_id=None):
    """Create the platform record behind a 'new:' proposal group (on the
    first approval of any of its values). Returns its id."""
    from datetime import date
    from geo_resolve import Resolver
    spec = BY_NEW_ENTITY[new_entity]
    rows = {r["field"]: r for r in db.execute(
        "SELECT * FROM platform_agent_proposals WHERE record_key = ? AND entity = ?", (record_key, new_entity))}
    name = rows.get("name")
    if name is None or name["status"] == "rejected":
        raise pa.AgentError(f"approve its Name first (a new {spec['singular']} needs one)")
    city_id = country_id = state_id = region_id = None
    city_text = None
    if rows.get("city") and rows["city"]["status"] != "rejected":
        city_name, _, country_name = rows["city"]["proposed_value"].partition(",")
        r = Resolver(db)
        country = r.country(country_name.strip())[0] if country_name.strip() else None
        if country is None:
            country = next((c for c in r.countries if c["label"] == "Pakistan"), None)
        if country is not None:
            row, how = r.city(country["country_id"], None, city_name.strip())
            country_id, region_id = country["country_id"], r.region_for(country["country_id"])
            if row and how in ("same", "sounds"):
                city_id, state_id = row["city_id"], row["state_id"]
        if city_id is None:
            city_text = city_name.strip() or None
    run_id = int(record_key.split(":")[1])
    run = db.execute("SELECT r.params, r.batch_id, u.file_name FROM platform_agent_runs r LEFT JOIN platform_agent_uploads u "
                     "ON u.run_id = r.run_id WHERE r.run_id = ?", (run_id,)).fetchone()
    params = json.loads((run["params"] if run else None) or "{}")
    cols = {"name": name["proposed_value"], "region_id": region_id, "country_id": country_id, "state_id": state_id,
            "city_id": city_id, "city_text": city_text, "source": f"PDF: {run['file_name'] if run else 'upload'}",
            "checked_on": date.today().isoformat(), "is_active": 1}
    if spec["entity"] == "platform_accommodation":
        pt = rows.get("property_type")
        cols["property_type"] = (pt["proposed_value"] if pt and pt["status"] != "rejected" else None) \
            or params.get("property_type") or "Hotel"
    cur = db.execute(f"INSERT INTO {spec['table']} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                     tuple(cols.values()))
    rid = cur.lastrowid
    db.execute("UPDATE platform_agent_proposals SET record_key = ? WHERE record_key = ? AND entity = ?",
               (str(rid), record_key, new_entity))
    if run and run["batch_id"]:
        db.execute("""UPDATE image_import_items SET owner_id = ?, match_how = 'entity', include = 1, match_note = NULL
                      WHERE batch_id = ? AND status = 'pending' AND match_note LIKE ?""",
                   (rid, run["batch_id"], f"%[{record_key}]%"))
    for f in NEW_FIELDS:
        if f in rows and rows[f]["status"] == "pending":
            db.execute("UPDATE platform_agent_proposals SET status = 'approved', decided_by = ?, decided_at = datetime('now') "
                       "WHERE proposal_id = ?", (user_id, rows[f]["proposal_id"]))
    return rid
