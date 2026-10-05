"""
Tenant websites (Zeb, Oct 2026): "a website ... on the tenant's own domain
(www.mavietours.com) ... display ALL Groups, all Tours within that
Group, the Pricing of the Tour with a schedule, requirements, booking
deadline, payment terms / methods, deposits, Booking Form, and other relevant
info such as cancellation, refunds ... an Email and inquiry intake form
besides a booking form ... a Chat Window".

TMS is the tenant's back end. Each tenant's website is its own small site in
its own repository (like GSS's tenant websites: e.g. Zbhatti2/
ma-vie-tours-website), deployed as its own Coolify app; it reads and writes
through the Website API (blueprints/site_api.py), which only ever gives out
what the tenant chose to publish:

* tenant_websites -- one row per tenant: on/off, title, banner, logo and
  hero image, colours, contact details, and the policies shown on the site
  (payment methods and terms, deposits, booking terms, cancellation, refunds).
* package_groups (Table Maintenance) get a website image, caption and blurb:
  each Group is a box on the home page.
* package_web -- a package's website page: published or not, web address
  (slug), overview, highlights, who it is for, requirements, packing list,
  difficulty, altitude, lodging, the "from" price in US$, deposit, booking
  deadline and its own terms (falling back to the tenant's).
* package_web_images -- the tour's own photos; the itinerary's Points of
  Interest and hotels add theirs after them.
* package_departures -- the schedule: dates, price, seats, booking deadline.
* web_enquiries -- what visitors send: enquiries and booking requests, the
  Website Inbox in TMS.

Never published: costs, margins, supplier contacts or internal notes.
"""
import json
import re
from datetime import date, timedelta

DDL = """
CREATE TABLE IF NOT EXISTS tenant_websites (
    tenant_id       INTEGER PRIMARY KEY REFERENCES tenants(tenant_id),
    enabled         INTEGER NOT NULL DEFAULT 0,
    site_title      TEXT,
    banner_text     TEXT,                   -- the strip under the header, e.g. "Take a look at our newest tours..."
    about_text      TEXT,
    contact_email   TEXT,
    contact_phone   TEXT,
    whatsapp        TEXT,                   -- number for the WhatsApp button, digits with country code
    address         TEXT,
    color_primary   TEXT,                   -- dark band colour, e.g. #3b5556
    color_accent    TEXT,                   -- buttons and headings, e.g. #e3b45b
    logo_data       BLOB,
    logo_mime       TEXT,
    hero_data       BLOB,
    hero_mime       TEXT,
    payment_methods TEXT,
    payment_terms   TEXT,
    deposit_policy  TEXT,
    booking_terms   TEXT,
    cancellation_policy TEXT,
    refund_policy   TEXT,
    privacy_policy  TEXT,
    default_deposit_pct INTEGER,            -- e.g. 25 (% of the tour price)
    default_deadline_days INTEGER,          -- book at least N days before departure
    notify_email    TEXT,                   -- who is told about new enquiries and bookings
    chat_enabled    INTEGER NOT NULL DEFAULT 1,
    chat_welcome    TEXT,
    site_key        TEXT,                   -- public key the tenant's website uses to call the Website API
    test_origins    TEXT,                   -- extra web addresses allowed to call it (one per line), e.g. a test site
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS package_web (
    package_id      INTEGER PRIMARY KEY REFERENCES packages(package_id),
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    published       INTEGER NOT NULL DEFAULT 0,
    slug            TEXT,
    title           TEXT,                   -- the name on the website (defaults to the package name)
    tagline         TEXT,                   -- caption under the tour's photo, e.g. "10 Days - 15 Gurdwaras"
    overview        TEXT,
    highlights      TEXT,                   -- one per line: "TREKKING: We embark on..."
    who_for         TEXT,
    requirements    TEXT,                   -- fitness, passports, visas, vaccinations...
    packing_list    TEXT,
    difficulty      TEXT,
    max_altitude_m  INTEGER,
    lodging         TEXT,
    group_size_min  INTEGER,
    group_size_max  INTEGER,
    price_usd       NUMERIC,                -- "from" price per person
    single_supplement_usd NUMERIC,
    deposit_usd     NUMERIC,                -- per person; or deposit_pct
    deposit_pct     INTEGER,
    deadline_days   INTEGER,                -- book at least N days before departure (else the site default)
    included        TEXT,                   -- defaults to the package's inclusions
    excluded        TEXT,
    payment_terms   TEXT,                   -- this tour's own, else the site's
    cancellation_policy TEXT,
    sort_order      INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (tenant_id, slug)
);
CREATE TABLE IF NOT EXISTS package_web_images (
    image_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    package_id      INTEGER NOT NULL REFERENCES packages(package_id),
    caption         TEXT,
    file_data       BLOB NOT NULL,
    mime_type       TEXT,
    thumb_data      BLOB,
    sort_order      INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_package_web_images_pkg ON package_web_images(package_id);
CREATE TABLE IF NOT EXISTS package_departures (
    departure_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    package_id      INTEGER NOT NULL REFERENCES packages(package_id),
    start_date      TEXT NOT NULL,
    end_date        TEXT,
    price_usd       NUMERIC,                -- per person; else the tour's price
    seats           INTEGER,
    seats_taken     INTEGER NOT NULL DEFAULT 0,
    booking_deadline TEXT,                  -- else start_date - deadline days
    status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','full','closed','cancelled')),
    notes           TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_package_departures_pkg ON package_departures(package_id, start_date);
CREATE TABLE IF NOT EXISTS web_enquiries (
    enquiry_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    kind            TEXT NOT NULL CHECK (kind IN ('enquiry','booking')),
    package_id      INTEGER REFERENCES packages(package_id),
    departure_id    INTEGER REFERENCES package_departures(departure_id),
    name            TEXT NOT NULL,
    email           TEXT NOT NULL,
    phone           TEXT,
    country         TEXT,
    travellers      INTEGER,
    doubles         INTEGER,
    singles         INTEGER,
    message         TEXT,
    details         TEXT,                   -- JSON: traveller names, dietary / medical notes, how they heard of us
    status          TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new','in_progress','confirmed','closed','spam')),
    staff_note      TEXT,
    handled_by      INTEGER REFERENCES users(user_id),
    handled_at      TEXT,
    ip              TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_web_enquiries_tenant ON web_enquiries(tenant_id, status, created_at);
"""

GROUP_COLUMNS = [("web_show", "INTEGER NOT NULL DEFAULT 1"), ("web_caption", "TEXT"), ("web_blurb", "TEXT"),
                 ("web_image_data", "BLOB"), ("web_image_mime", "TEXT"), ("web_slug", "TEXT")]

DEFAULTS = {
    "color_primary": "#3b5556", "color_accent": "#e3b45b",
    "payment_methods": "Bank transfer (wire), credit or debit card through a secure payment link, and PayPal.",
    "payment_terms": "A deposit secures your place. The balance is due 60 days before departure. Bookings made "
                     "within 60 days of departure are paid in full.",
    "deposit_policy": "25% of the tour price per person, payable when we confirm your booking.",
    "booking_terms": "Your booking is confirmed when we receive your deposit and send you a written confirmation. "
                     "Travel insurance covering medical evacuation is required.",
    "cancellation_policy": "More than 90 days before departure: deposit refunded less a US$100 fee. 60-90 days: "
                           "deposit forfeited. Less than 60 days: no refund. If we cancel a departure, you receive a "
                           "full refund or a place on another date.",
    "refund_policy": "Refunds are made to the original payment method within 14 days. Unused services during the "
                     "tour are not refunded.",
}
DEFAULT_DEPOSIT_PCT = 25
DEFAULT_DEADLINE_DAYS = 45
STATUSES = {"new": "New", "in_progress": "In progress", "confirmed": "Confirmed", "closed": "Closed", "spam": "Spam"}
DEPARTURE_STATUSES = {"open": "Open", "full": "Full", "closed": "Booking closed", "cancelled": "Cancelled"}


SITE_COLUMNS = [("site_key", "TEXT"), ("test_origins", "TEXT")]


def migrate(db, column_exists):
    db.executescript(DDL)
    for col, decl in SITE_COLUMNS:  # databases that ran an early copy of this migration
        if not column_exists(db, "tenant_websites", col):
            db.execute(f"ALTER TABLE tenant_websites ADD COLUMN {col} {decl}")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_tenant_websites_key ON tenant_websites(site_key)")
    for col, decl in GROUP_COLUMNS:
        if not column_exists(db, "package_groups", col):
            db.execute(f"ALTER TABLE package_groups ADD COLUMN {col} {decl}")
    db.commit()


def slugify(text):
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:80] or "tour"


# ---- tenant settings -----------------------------------------------------------------------

def settings(db, tenant_id):
    """The tenant's website settings as a dict, with defaults filled in."""
    row = db.execute("SELECT * FROM tenant_websites WHERE tenant_id = ?", (tenant_id,)).fetchone()
    s = dict(row) if row else {"tenant_id": tenant_id, "enabled": 0, "chat_enabled": 1}
    for k, v in DEFAULTS.items():
        if not s.get(k):
            s[k] = v
    s["deposit_pct"] = s.get("default_deposit_pct") or DEFAULT_DEPOSIT_PCT
    s["deadline_days"] = s.get("default_deadline_days") or DEFAULT_DEADLINE_DAYS
    t = db.execute("SELECT tenant_name, tenant_code, website_domain FROM tenants WHERE tenant_id = ?", (tenant_id,)).fetchone()
    s["tenant_name"] = t["tenant_name"] if t else ""
    s["tenant_code"] = t["tenant_code"] if t else ""
    s["website_domain"] = t["website_domain"] if t else None
    s["site_title"] = s.get("site_title") or s["tenant_name"]
    s["has_logo"] = bool(row and row["logo_data"])
    s["has_hero"] = bool(row and row["hero_data"])
    return s


SETTING_FIELDS = ["test_origins", "site_title", "banner_text", "about_text", "contact_email", "contact_phone", "whatsapp", "address",
                  "color_primary", "color_accent", "payment_methods", "payment_terms", "deposit_policy", "booking_terms",
                  "cancellation_policy", "refund_policy", "privacy_policy", "notify_email", "chat_welcome"]


def save_settings(db, tenant_id, values, logo=None, hero=None):
    if not db.execute("SELECT 1 FROM tenant_websites WHERE tenant_id = ?", (tenant_id,)).fetchone():
        db.execute("INSERT INTO tenant_websites (tenant_id) VALUES (?)", (tenant_id,))
    cols = {k: values.get(k) for k in SETTING_FIELDS + ["enabled", "chat_enabled", "default_deposit_pct",
                                                        "default_deadline_days"] if k in values}
    if logo:
        cols["logo_data"], cols["logo_mime"] = logo
    if hero:
        cols["hero_data"], cols["hero_mime"] = hero
    if cols:
        db.execute(f"UPDATE tenant_websites SET {', '.join(f'{c} = ?' for c in cols)}, updated_at = datetime('now') "
                   "WHERE tenant_id = ?", tuple(cols.values()) + (tenant_id,))
    db.commit()


def ensure_site_key(db, tenant_id):
    """The tenant's Website API key, made the first time it is needed."""
    import secrets
    if not db.execute("SELECT 1 FROM tenant_websites WHERE tenant_id = ?", (tenant_id,)).fetchone():
        db.execute("INSERT INTO tenant_websites (tenant_id) VALUES (?)", (tenant_id,))
    row = db.execute("SELECT site_key FROM tenant_websites WHERE tenant_id = ?", (tenant_id,)).fetchone()
    if row[0]:
        return row[0]
    key = "site_" + secrets.token_urlsafe(18)
    db.execute("UPDATE tenant_websites SET site_key = ? WHERE tenant_id = ?", (key, tenant_id))
    db.commit()
    return key


def new_site_key(db, tenant_id):
    """Replace the key (the old one stops working at once)."""
    ensure_site_key(db, tenant_id)
    db.execute("UPDATE tenant_websites SET site_key = NULL WHERE tenant_id = ?", (tenant_id,))
    return ensure_site_key(db, tenant_id)


def site_for_key(db, key):
    """(tenant_id, settings) for a Website API key, or None."""
    if not key or not key.startswith("site_"):
        return None
    t = db.execute("""SELECT w.tenant_id FROM tenant_websites w JOIN tenants t ON t.tenant_id = w.tenant_id
                      WHERE w.site_key = ? AND COALESCE(t.is_platform, 0) = 0""", (key,)).fetchone()
    if t is None:
        return None
    return t[0], settings(db, t[0])


def clean_domain(raw):
    return (raw or "").strip().lower().replace("https://", "").replace("http://", "").split("/")[0] or None


def _origin(raw):
    raw = (raw or "").strip().rstrip("/")
    if not raw:
        return None
    if "://" not in raw:  # a bare name: https, except for this PC
        raw = ("http://" if raw.startswith(("localhost", "127.0.0.1")) else "https://") + raw
    scheme, _, rest = raw.partition("://")
    return f"{scheme.lower()}://{rest.split('/')[0].lower()}"


def live_origins(s):
    """The web addresses of the live site: the domain with and without www."""
    host = clean_domain(s.get("website_domain"))
    if not host:
        return set()
    bare = host[4:] if host.startswith("www.") else host
    return {f"https://{bare}", f"https://www.{bare}"}


def test_origins(s):
    return {o for o in (_origin(line) for line in (s.get("test_origins") or "").splitlines()) if o}


def site_address(s):
    """Where to look at the site: the domain, else the first test address."""
    host = clean_domain(s.get("website_domain"))
    if host:
        return f"https://{host}/"
    tests = [_origin(line) for line in (s.get("test_origins") or "").splitlines() if _origin(line)]
    return tests[0] + "/" if tests else None


# ---- groups --------------------------------------------------------------------------------

def save_group(db, tenant_id, group_id, show, caption, blurb, image=None):
    cols = {"web_show": 1 if show else 0, "web_caption": caption or None, "web_blurb": blurb or None}
    label = db.execute("SELECT label FROM package_groups WHERE package_group_id = ? AND tenant_id = ?",
                       (group_id, tenant_id)).fetchone()
    if label is None:
        raise ValueError("Unknown group")
    cols["web_slug"] = slugify(caption or label[0])
    if image:
        cols["web_image_data"], cols["web_image_mime"] = image
    db.execute(f"UPDATE package_groups SET {', '.join(f'{c} = ?' for c in cols)} WHERE package_group_id = ? AND tenant_id = ?",
               tuple(cols.values()) + (group_id, tenant_id))
    db.commit()


def groups_admin(db, tenant_id):
    return db.execute("""SELECT g.package_group_id, g.label, g.is_active, g.web_show, g.web_caption, g.web_blurb,
                                g.web_image_data IS NOT NULL AS has_image,
                                (SELECT COUNT(*) FROM packages p JOIN package_web w ON w.package_id = p.package_id
                                 WHERE p.package_group_id = g.package_group_id AND w.published = 1
                                 AND p.status != 'archived') AS published
                         FROM package_groups g WHERE g.tenant_id = ? ORDER BY g.sort_order, g.label COLLATE NOCASE""",
                      (tenant_id,)).fetchall()


# ---- a package's website page --------------------------------------------------------------

WEB_FIELDS = ["title", "tagline", "overview", "highlights", "who_for", "requirements", "packing_list", "difficulty",
              "lodging", "included", "excluded", "payment_terms", "cancellation_policy"]
WEB_NUMBERS = ["max_altitude_m", "group_size_min", "group_size_max", "price_usd", "single_supplement_usd",
               "deposit_usd", "deposit_pct", "deadline_days", "sort_order"]


def package_web(db, tenant_id, package_id):
    row = db.execute("SELECT * FROM package_web WHERE package_id = ? AND tenant_id = ?", (package_id, tenant_id)).fetchone()
    return dict(row) if row else None


def unique_slug(db, tenant_id, wanted, package_id):
    base = slugify(wanted)
    slug, n = base, 2
    while db.execute("SELECT 1 FROM package_web WHERE tenant_id = ? AND slug = ? AND package_id != ?",
                     (tenant_id, slug, package_id)).fetchone():
        slug, n = f"{base}-{n}", n + 1
    return slug


def save_package_web(db, tenant_id, package_id, values):
    pkg = db.execute("SELECT package_name FROM packages WHERE package_id = ? AND tenant_id = ?",
                     (package_id, tenant_id)).fetchone()
    if pkg is None:
        raise ValueError("Unknown package")
    cols = {k: (values.get(k) or "").strip() or None for k in WEB_FIELDS}
    for k in WEB_NUMBERS:
        raw = str(values.get(k) or "").replace(",", "").replace("$", "").strip()
        try:
            cols[k] = (float(raw) if "." in raw else int(raw)) if raw else None
        except ValueError:
            raise ValueError(f"{k.replace('_', ' ').capitalize()} must be a number.")
    cols["sort_order"] = cols["sort_order"] or 0
    cols["published"] = 1 if values.get("published") else 0
    cols["slug"] = unique_slug(db, tenant_id, values.get("slug") or cols["title"] or pkg[0], package_id)
    if cols["published"] and not cols["price_usd"]:
        raise ValueError("Give the tour a price (US$ per person) before publishing it.")
    if not db.execute("SELECT 1 FROM package_web WHERE package_id = ?", (package_id,)).fetchone():
        db.execute("INSERT INTO package_web (package_id, tenant_id) VALUES (?, ?)", (package_id, tenant_id))
    db.execute(f"UPDATE package_web SET {', '.join(f'{c} = ?' for c in cols)}, updated_at = datetime('now') "
               "WHERE package_id = ? AND tenant_id = ?", tuple(cols.values()) + (package_id, tenant_id))
    db.commit()
    return cols["slug"]


def departures(db, tenant_id, package_id, upcoming_only=False):
    sql = "SELECT * FROM package_departures WHERE tenant_id = ? AND package_id = ?"
    args = [tenant_id, package_id]
    if upcoming_only:
        sql += " AND start_date >= ? AND status != 'cancelled'"
        args.append(date.today().isoformat())
    return [dict(r) for r in db.execute(sql + " ORDER BY start_date", args)]


def save_departure(db, tenant_id, package_id, values, departure_id=None):
    start = (values.get("start_date") or "").strip()
    try:
        date.fromisoformat(start)
    except ValueError:
        raise ValueError("Give the departure a start date.")
    end = (values.get("end_date") or "").strip() or None
    if end:
        try:
            if date.fromisoformat(end) < date.fromisoformat(start):
                raise ValueError("The end date is before the start date.")
        except ValueError as e:
            raise ValueError(str(e) if "before" in str(e) else "The end date isn't a date.")
    deadline = (values.get("booking_deadline") or "").strip() or None

    def num(k, cast=int):
        raw = str(values.get(k) or "").replace(",", "").replace("$", "").strip()
        try:
            return cast(raw) if raw else None
        except ValueError:
            raise ValueError(f"{k.replace('_', ' ').capitalize()} must be a number.")
    status = values.get("status") if values.get("status") in DEPARTURE_STATUSES else "open"
    cols = {"start_date": start, "end_date": end, "price_usd": num("price_usd", float), "seats": num("seats"),
            "seats_taken": num("seats_taken") or 0, "booking_deadline": deadline, "status": status,
            "notes": (values.get("notes") or "").strip() or None}
    if departure_id:
        db.execute(f"UPDATE package_departures SET {', '.join(f'{c} = ?' for c in cols)} "
                   "WHERE departure_id = ? AND tenant_id = ? AND package_id = ?",
                   tuple(cols.values()) + (departure_id, tenant_id, package_id))
    else:
        db.execute(f"INSERT INTO package_departures (tenant_id, package_id, {', '.join(cols)}) "
                   f"VALUES (?, ?, {', '.join('?' * len(cols))})", (tenant_id, package_id) + tuple(cols.values()))
    db.commit()


def delete_departure(db, tenant_id, package_id, departure_id):
    used = db.execute("SELECT COUNT(*) FROM web_enquiries WHERE departure_id = ?", (departure_id,)).fetchone()[0]
    if used:
        db.execute("UPDATE package_departures SET status = 'cancelled' WHERE departure_id = ? AND tenant_id = ?",
                   (departure_id, tenant_id))
    else:
        db.execute("DELETE FROM package_departures WHERE departure_id = ? AND tenant_id = ? AND package_id = ?",
                   (departure_id, tenant_id, package_id))
    db.commit()
    return bool(used)


def add_package_image(db, tenant_id, package_id, data, mime, caption=None):
    from image_catalog import make_thumb
    nxt = db.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 FROM package_web_images WHERE package_id = ?",
                     (package_id,)).fetchone()[0]
    db.execute("INSERT INTO package_web_images (tenant_id, package_id, caption, file_data, mime_type, thumb_data, sort_order) "
               "VALUES (?, ?, ?, ?, ?, ?, ?)", (tenant_id, package_id, caption, data, mime, make_thumb(data), nxt))
    db.commit()


# ---- what the public site reads ------------------------------------------------------------

def deadline_for(dep, web, site):
    if dep.get("booking_deadline"):
        return dep["booking_deadline"]
    days = (web or {}).get("deadline_days") or site["deadline_days"]
    try:
        return (date.fromisoformat(dep["start_date"]) - timedelta(days=int(days))).isoformat()
    except (TypeError, ValueError):
        return None


def departure_view(dep, web, site, duration_days=None):
    d = dict(dep)
    d["price"] = dep.get("price_usd") or (web or {}).get("price_usd")
    d["deadline"] = deadline_for(dep, web, site)
    if not d.get("end_date") and duration_days:
        try:
            d["end_date"] = (date.fromisoformat(dep["start_date"]) + timedelta(days=int(duration_days) - 1)).isoformat()
        except ValueError:
            pass
    d["seats_left"] = (dep["seats"] - (dep["seats_taken"] or 0)) if dep.get("seats") else None
    past_deadline = bool(d["deadline"] and d["deadline"] < date.today().isoformat())
    d["bookable"] = dep["status"] == "open" and not past_deadline and (d["seats_left"] is None or d["seats_left"] > 0)
    d["label"] = (DEPARTURE_STATUSES.get(dep["status"]) if dep["status"] != "open"
                  else "Booking closed" if past_deadline else "Full" if d["seats_left"] == 0 else "Open")
    return d


def deposit_text(web, site, price):
    if web.get("deposit_usd"):
        return f"US${web['deposit_usd']:,.0f} per person"
    pct = web.get("deposit_pct") or site["deposit_pct"]
    if price:
        return f"{pct}% (US${float(price) * pct / 100:,.0f} per person)"
    return f"{pct}% of the tour price"


def public_groups(db, tenant_id):
    """Groups with at least one published tour, in order."""
    return [dict(r) for r in db.execute(
        """SELECT g.package_group_id AS id, g.label, COALESCE(g.web_caption, g.label) AS caption, g.web_blurb AS blurb,
                  COALESCE(g.web_slug, lower(replace(g.label, ' ', '-'))) AS slug, g.web_image_data IS NOT NULL AS has_image,
                  COUNT(p.package_id) AS tours
           FROM package_groups g
           JOIN packages p ON p.package_group_id = g.package_group_id AND p.status != 'archived'
           JOIN package_web w ON w.package_id = p.package_id AND w.published = 1
           WHERE g.tenant_id = ? AND g.is_active = 1 AND COALESCE(g.web_show, 1) = 1
           GROUP BY g.package_group_id ORDER BY g.sort_order, g.label COLLATE NOCASE""", (tenant_id,))]


def public_tours(db, tenant_id, group_id=None, slug=None):
    """Published tours (cards): what the home page and the bottom bar need."""
    sql = """SELECT p.package_id, p.package_code, p.package_name, p.duration_days, p.duration_nights, p.min_pax, p.max_pax,
                    p.package_group_id, p.difficulty_rating, w.*
             FROM packages p JOIN package_web w ON w.package_id = p.package_id
             WHERE p.tenant_id = ? AND w.published = 1 AND p.status != 'archived'"""
    args = [tenant_id]
    if group_id:
        sql += " AND p.package_group_id = ?"
        args.append(group_id)
    if slug:
        sql += " AND w.slug = ?"
        args.append(slug)
    site = settings(db, tenant_id)
    out = []
    for r in db.execute(sql + " ORDER BY w.sort_order, COALESCE(w.title, p.package_name) COLLATE NOCASE", args):
        t = dict(r)
        t["title"] = t["title"] or t["package_name"]
        t["group_min"] = t["group_size_min"] or t["min_pax"]
        t["group_max"] = t["group_size_max"] or t["max_pax"]
        t["difficulty"] = t["difficulty"] or t["difficulty_rating"]
        t["departures"] = [departure_view(d, t, site, t["duration_days"])
                           for d in departures(db, tenant_id, t["package_id"], upcoming_only=True)]
        t["images"] = tour_images(db, tenant_id, t["package_id"])
        out.append(t)
    return out


def _poi_ids(db, package_id):
    return [r[0] for r in db.execute("""SELECT DISTINCT dp.poi_id FROM package_day_pois dp
                                        JOIN package_days d ON d.day_id = dp.day_id
                                        WHERE d.package_id = ? ORDER BY d.day_number, dp.sequence_number""", (package_id,))]


def _supplier_ids(db, package_id):
    return [r[0] for r in db.execute("""SELECT DISTINCT c.supplier_id FROM package_components c
                                        LEFT JOIN package_days d ON d.day_id = c.day_id
                                        WHERE c.package_id = ? AND c.supplier_id IS NOT NULL
                                        ORDER BY COALESCE(d.day_number, 999)""", (package_id,))]


def tour_images(db, tenant_id, package_id, limit=24):
    """[{kind, owner, id, caption}] -- the tour's own photos first, then its
    Points of Interest's and hotels'."""
    out = [{"kind": "tour", "owner": package_id, "id": r["image_id"], "caption": r["caption"] or ""}
           for r in db.execute("SELECT image_id, caption FROM package_web_images WHERE package_id = ? AND tenant_id = ? "
                               "ORDER BY sort_order, image_id", (package_id, tenant_id))]
    for pid in _poi_ids(db, package_id):
        for r in db.execute("""SELECT i.poi_image_id, COALESCE(i.image_name, p.name) AS caption FROM poi_images i
                               JOIN points_of_interest p ON p.poi_id = i.poi_id
                               WHERE i.poi_id = ? AND i.tenant_id = ? AND COALESCE(i.is_deleted, 0) = 0
                               AND (i.platform_image_id IS NOT NULL OR (i.location_type = 'Stored in Database' AND i.file_data IS NOT NULL))
                               ORDER BY i.sort_order, i.poi_image_id LIMIT 2""", (pid, tenant_id)):
            out.append({"kind": "poi", "owner": pid, "id": r[0], "caption": r[1]})
        if len(out) >= limit:
            return out[:limit]
    from image_catalog import image_type_id
    tid = image_type_id(db, tenant_id)
    for sid in _supplier_ids(db, package_id):
        for r in db.execute("""SELECT d.supplier_document_id, COALESCE(d.document_name, s.supplier_name) FROM supplier_documents d
                               JOIN suppliers s ON s.supplier_id = d.supplier_id
                               WHERE d.supplier_id = ? AND d.tenant_id = ? AND d.is_deleted = 0 AND d.document_type_id = ?
                               ORDER BY d.sort_order, d.supplier_document_id LIMIT 1""", (sid, tenant_id, tid)):
            out.append({"kind": "supplier", "owner": sid, "id": r[0], "caption": r[1]})
        if len(out) >= limit:
            break
    return out[:limit]


def published_entity(db, tenant_id, kind, owner_id):
    """True when a POI / supplier appears in a published tour (only those
    are shown on the site)."""
    if kind == "poi":
        sql = """SELECT 1 FROM package_day_pois dp JOIN package_days d ON d.day_id = dp.day_id
                 JOIN package_web w ON w.package_id = d.package_id AND w.published = 1
                 WHERE dp.poi_id = ? AND dp.tenant_id = ? LIMIT 1"""
    elif kind == "supplier":
        sql = """SELECT 1 FROM package_components c JOIN package_web w ON w.package_id = c.package_id AND w.published = 1
                 WHERE c.supplier_id = ? AND c.tenant_id = ? LIMIT 1"""
    else:
        return False
    return db.execute(sql, (owner_id, tenant_id)).fetchone() is not None


def image_bytes(db, tenant_id, kind, owner_id, image_id, size="full"):
    """(bytes, mime) of a public image, or (None, None)."""
    from image_catalog import make_thumb, document_thumb
    if kind == "tour":
        r = db.execute("""SELECT i.file_data, i.mime_type, i.thumb_data FROM package_web_images i
                          JOIN package_web w ON w.package_id = i.package_id AND w.published = 1
                          WHERE i.image_id = ? AND i.package_id = ? AND i.tenant_id = ?""",
                       (image_id, owner_id, tenant_id)).fetchone()
        if r is None:
            return None, None
        return (r[2], "image/jpeg") if size == "thumb" and r[2] else (r[0], r[1])
    if kind == "group":
        r = db.execute("SELECT web_image_data, web_image_mime FROM package_groups WHERE package_group_id = ? AND tenant_id = ?",
                       (owner_id, tenant_id)).fetchone()
        return (r[0], r[1]) if r and r[0] else (None, None)
    if kind in ("logo", "hero"):
        r = db.execute(f"SELECT {kind}_data, {kind}_mime FROM tenant_websites WHERE tenant_id = ?", (tenant_id,)).fetchone()
        return (r[0], r[1]) if r and r[0] else (None, None)
    if not published_entity(db, tenant_id, kind, owner_id):
        return None, None
    if kind == "poi":
        from image_owners import platform_image
        r = db.execute("SELECT * FROM poi_images WHERE poi_image_id = ? AND poi_id = ? AND tenant_id = ? "
                       "AND COALESCE(is_deleted, 0) = 0", (image_id, owner_id, tenant_id)).fetchone()
        if r is None:
            return None, None
        if r["platform_image_id"]:
            return platform_image(db, r["platform_image_id"], size)
        if r["file_data"]:
            if size == "thumb":
                thumb = r["thumb_data"] or make_thumb(r["file_data"])
                if thumb:
                    return thumb, "image/jpeg"
            return r["file_data"], r["mime_type"] or "image/jpeg"
        return None, None
    if kind == "supplier":
        if not db.execute("SELECT 1 FROM supplier_documents WHERE supplier_document_id = ? AND supplier_id = ? "
                          "AND tenant_id = ? AND is_deleted = 0", (image_id, owner_id, tenant_id)).fetchone():
            return None, None
        if size == "thumb":
            data, mime = document_thumb(db, tenant_id, image_id)
            if data:
                return data, mime
        loc = db.execute("SELECT file_data, mime_type FROM supplier_document_locations WHERE supplier_document_id = ? "
                         "AND tenant_id = ? AND location_type = 'Stored in Database' ORDER BY location_id LIMIT 1",
                         (image_id, tenant_id)).fetchone()
        return (loc[0], loc[1] or "image/jpeg") if loc and loc[0] else (None, None)
    return None, None


def tour_detail(db, tenant_id, slug):
    """Everything the tour page shows: the card, then the route, the days
    with their Points of Interest, hotels and restaurants."""
    tours = public_tours(db, tenant_id, slug=slug)
    if not tours:
        return None
    t = tours[0]
    pid = t["package_id"]
    site = settings(db, tenant_id)
    t["all_departures"] = [departure_view(d, t, site, t["duration_days"]) for d in departures(db, tenant_id, pid, True)]
    t["deposit"] = deposit_text(t, site, t["price_usd"])
    t["deadline_days"] = t["deadline_days"] or site["deadline_days"]
    pkg = db.execute("SELECT * FROM packages WHERE package_id = ?", (pid,)).fetchone()
    t["included"] = t["included"] or pkg["inclusions"]
    t["excluded"] = t["excluded"] or pkg["exclusions"]
    t["overview"] = t["overview"] or pkg["description"]
    t["route"] = [dict(r) for r in db.execute(
        """SELECT s.sequence_number, COALESCE(c.label, s.city_text) AS city, s.nights, s.is_layover, s.is_checkpoint,
                  co.label AS country
           FROM package_route_stops s LEFT JOIN cities c ON c.city_id = s.city_id
           LEFT JOIN countries co ON co.country_id = s.country_id
           WHERE s.package_id = ? ORDER BY s.sequence_number""", (pid,))]
    days = []
    for d in db.execute("""SELECT d.*, COALESCE(c.label, s.city_text) AS city FROM package_days d
                           LEFT JOIN package_route_stops s ON s.stop_id = d.route_stop_id
                           LEFT JOIN cities c ON c.city_id = s.city_id
                           WHERE d.package_id = ? ORDER BY d.day_number""", (pid,)):
        day = dict(d)
        # the planner's internal notes (tour_design.py writes them as "Team notes: ...") stay in TMS
        day["description"] = "\n\n".join(p for p in (d["description"] or "").split("\n\n")
                                         if not p.strip().lower().startswith("team notes")).strip() or None
        day["pois"] = [dict(r) for r in db.execute(
            """SELECT p.poi_id, p.name, pt.label AS type, dp.visit_notes FROM package_day_pois dp
               JOIN points_of_interest p ON p.poi_id = dp.poi_id
               LEFT JOIN poi_types pt ON pt.poi_type_id = p.poi_type_id
               WHERE dp.day_id = ? AND p.is_deleted = 0 ORDER BY dp.sequence_number""", (d["day_id"],))]
        day["stays"], day["meals"] = [], []
        for r in db.execute(
                """SELECT DISTINCT s.supplier_id, s.supplier_name, st.label AS grade, g.code AS group_code, c.is_accommodation
                   FROM package_components c JOIN suppliers s ON s.supplier_id = c.supplier_id AND s.is_deleted = 0
                   LEFT JOIN supplier_subtypes st ON st.supplier_subtype_id = s.supplier_subtype_id
                   LEFT JOIN supplier_groups g ON g.supplier_group_id = s.supplier_group_id
                   WHERE c.day_id = ? ORDER BY c.sequence_number""", (d["day_id"],)):
            item = {"supplier_id": r["supplier_id"], "name": r["supplier_name"], "grade": r["grade"]}
            if r["is_accommodation"] or r["group_code"] == "ACCOMMODATION":
                day["stays"].append(item)
            elif r["group_code"] == "FOOD_BEVERAGE":
                day["meals"].append(item)
        days.append(day)
    t["days"] = days
    t["highlight_list"] = _lines(t["highlights"])
    t["packing"] = _lines(t["packing_list"])
    t["included_list"] = _lines(t["included"])
    t["excluded_list"] = _lines(t["excluded"])
    t["group"] = db.execute("SELECT package_group_id, label, COALESCE(web_caption, label) AS caption, "
                            "COALESCE(web_slug, lower(replace(label, ' ', '-'))) AS slug FROM package_groups "
                            "WHERE package_group_id = ?", (t["package_group_id"],)).fetchone() if t["package_group_id"] else None
    return t


def _lines(text):
    out = []
    for line in (text or "").splitlines():
        line = line.strip().lstrip("-•*").strip()
        if not line:
            continue
        head, sep, rest = line.partition(":")
        out.append({"head": head.strip(), "text": rest.strip()} if sep and len(head) <= 40 and rest.strip()
                   else {"head": None, "text": line})
    return out


def entity(db, tenant_id, kind, owner_id):
    """A Point of Interest or a hotel / restaurant for the site's pop-up:
    name, type, place, description, website, photos. Only if it is in a
    published tour."""
    if not published_entity(db, tenant_id, kind, owner_id):
        return None
    if kind == "poi":
        p = db.execute("""SELECT p.*, pt.label AS type, COALESCE(c.label, p.city_text) AS city
                          FROM points_of_interest p LEFT JOIN poi_types pt ON pt.poi_type_id = p.poi_type_id
                          LEFT JOIN cities c ON c.city_id = p.city_id WHERE p.poi_id = ? AND p.tenant_id = ?""",
                       (owner_id, tenant_id)).fetchone()
        if p is None:
            return None
        imgs = [{"id": r[0], "caption": r[1] or p["name"]} for r in db.execute(
            """SELECT poi_image_id, image_name FROM poi_images WHERE poi_id = ? AND tenant_id = ? AND COALESCE(is_deleted, 0) = 0
               AND (platform_image_id IS NOT NULL OR (location_type = 'Stored in Database' AND file_data IS NOT NULL))
               ORDER BY sort_order, poi_image_id LIMIT 12""", (owner_id, tenant_id))]
        return {"kind": "poi", "id": owner_id, "name": p["name"], "type": p["type"], "place": p["city"],
                "description": p["historical_significance"], "since": p["year_established"],
                "website": p["website"] if (p["website"] or "").startswith("http") else None, "images": imgs}
    s = db.execute("""SELECT s.supplier_id, s.supplier_name, s.web_page, t.label AS type, st.label AS grade,
                             (SELECT COALESCE(c.label, a.city_text) FROM supplier_addresses a LEFT JOIN cities c ON c.city_id = a.city_id
                              WHERE a.supplier_id = s.supplier_id ORDER BY a.is_primary DESC LIMIT 1) AS city
                      FROM suppliers s LEFT JOIN supplier_types t ON t.supplier_type_id = s.supplier_type_id
                      LEFT JOIN supplier_subtypes st ON st.supplier_subtype_id = s.supplier_subtype_id
                      WHERE s.supplier_id = ? AND s.tenant_id = ?""", (owner_id, tenant_id)).fetchone()
    if s is None:
        return None
    from image_catalog import image_type_id
    imgs = [{"id": r[0], "caption": r[1] or s["supplier_name"]} for r in db.execute(
        """SELECT supplier_document_id, document_name FROM supplier_documents WHERE supplier_id = ? AND tenant_id = ?
           AND is_deleted = 0 AND document_type_id = ? ORDER BY sort_order, supplier_document_id LIMIT 12""",
        (owner_id, tenant_id, image_type_id(db, tenant_id)))]
    # A description from the platform catalog this supplier came from, if any.
    desc = None
    link = db.execute("SELECT entity, catalog_id FROM tenant_catalog_links WHERE tenant_id = ? AND local_table = 'suppliers' "
                      "AND local_id = ? AND how != 'skipped'", (tenant_id, owner_id)).fetchone()
    if link and link["entity"] in ("accommodation", "restaurants"):
        table, pk = ("platform_accommodation", "accommodation_id") if link["entity"] == "accommodation" \
            else ("platform_restaurants", "restaurant_id")
        c = db.execute(f"SELECT * FROM {table} WHERE {pk} = ?", (link["catalog_id"],)).fetchone()
        if c is not None:
            bits = []
            if link["entity"] == "accommodation":
                if c["star_rating"]:
                    bits.append(f"{c['star_rating']}-star {(c['property_type'] or 'hotel').lower()}")
                if c["rooms"]:
                    bits.append(f"{c['rooms']} rooms")
            else:
                if c["cuisine"]:
                    bits.append(c["cuisine"])
                if c["rating"]:
                    bits.append(f"rated {c['rating']}/5")
            if c["address"]:
                bits.append(c["address"])
            desc = " · ".join(bits) or None
    return {"kind": "supplier", "id": owner_id, "name": s["supplier_name"], "type": s["grade"] or s["type"],
            "place": s["city"], "description": desc,
            "website": s["web_page"] if (s["web_page"] or "").startswith("http") else None, "images": imgs}


# ---- enquiries and booking requests --------------------------------------------------------

def recent_from_ip(db, tenant_id, ip, minutes=60):
    return db.execute("SELECT COUNT(*) FROM web_enquiries WHERE tenant_id = ? AND ip = ? AND created_at >= datetime('now', ?)",
                      (tenant_id, ip, f"-{minutes} minutes")).fetchone()[0]


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def record_enquiry(db, tenant_id, kind, form, ip=None):
    """Validate and store an enquiry or booking request. Returns its id;
    raises ValueError with what's missing."""
    name, email = (form.get("name") or "").strip()[:150], (form.get("email") or "").strip()[:200]
    errors = []
    if not name:
        errors.append("your name")
    if not EMAIL_RE.match(email):
        errors.append("a valid email address")
    package_id = departure_id = None
    if form.get("tour"):
        row = db.execute("SELECT w.package_id FROM package_web w WHERE w.tenant_id = ? AND w.slug = ? AND w.published = 1",
                         (tenant_id, form.get("tour"))).fetchone()
        package_id = row[0] if row else None
    if kind == "booking":
        if package_id is None:
            errors.append("the tour")
        raw = str(form.get("departure_id") or "").strip()
        dep = db.execute("SELECT departure_id FROM package_departures WHERE departure_id = ? AND package_id = ? AND tenant_id = ? "
                         "AND status = 'open'", (int(raw), package_id, tenant_id)).fetchone() \
            if package_id and raw.isdigit() else None
        if dep is None:
            errors.append("a departure date")
        else:
            departure_id = dep[0]
        if not form.get("accept_terms"):
            errors.append("your acceptance of the booking terms")
    elif not (form.get("message") or "").strip():
        errors.append("your question or message")
    if errors:
        raise ValueError("Please add " + ", ".join(errors) + ".")

    def num(k):
        try:
            v = int(str(form.get(k) or "").strip())
            return v if 0 <= v <= 500 else None
        except ValueError:
            return None
    details = {k: (form.get(k) or "").strip()[:2000] for k in ("traveller_names", "dietary", "medical", "heard_from",
                                                               "preferred_contact", "travel_month")
               if (form.get(k) or "").strip()}
    cur = db.execute("""INSERT INTO web_enquiries (tenant_id, kind, package_id, departure_id, name, email, phone, country,
                            travellers, doubles, singles, message, details, ip)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                     (tenant_id, kind, package_id, departure_id, name, email, (form.get("phone") or "").strip()[:60] or None,
                      (form.get("country") or "").strip()[:80] or None, num("travellers"), num("doubles"), num("singles"),
                      (form.get("message") or "").strip()[:5000] or None, json.dumps(details) if details else None, ip))
    db.commit()
    return cur.lastrowid


def inbox(db, tenant_id, status=None, kind=None, q=None):
    sql = """SELECT e.*, COALESCE(w.title, p.package_name) AS tour, d.start_date FROM web_enquiries e
             LEFT JOIN packages p ON p.package_id = e.package_id LEFT JOIN package_web w ON w.package_id = e.package_id
             LEFT JOIN package_departures d ON d.departure_id = e.departure_id WHERE e.tenant_id = ?"""
    args = [tenant_id]
    if status == "open":
        sql += " AND e.status IN ('new', 'in_progress')"
    elif status in STATUSES:
        sql += " AND e.status = ?"
        args.append(status)
    if kind in ("enquiry", "booking"):
        sql += " AND e.kind = ?"
        args.append(kind)
    if q:
        sql += " AND (e.name LIKE ? OR e.email LIKE ? OR e.message LIKE ?)"
        args += [f"%{q}%"] * 3
    return db.execute(sql + " ORDER BY e.created_at DESC, e.enquiry_id DESC LIMIT 500", args).fetchall()


def new_count(db, tenant_id):
    try:
        return db.execute("SELECT COUNT(*) FROM web_enquiries WHERE tenant_id = ? AND status = 'new'", (tenant_id,)).fetchone()[0]
    except Exception:
        return 0


def notify(db, site, enquiry_id):
    """Email the tenant about a new enquiry / booking request and thank the
    visitor (mailer.py; nothing is sent when email isn't set up)."""
    import mailer
    e = db.execute("""SELECT e.*, COALESCE(w.title, p.package_name) AS tour, d.start_date FROM web_enquiries e
                      LEFT JOIN packages p ON p.package_id = e.package_id LEFT JOIN package_web w ON w.package_id = e.package_id
                      LEFT JOIN package_departures d ON d.departure_id = e.departure_id WHERE e.enquiry_id = ?""",
                   (enquiry_id,)).fetchone()
    s = site
    what = "booking request" if e["kind"] == "booking" else "enquiry"
    lines = [f"New website {what} from {e['name']} <{e['email']}>",
             f"Tour: {e['tour']}" if e["tour"] else None, f"Departure: {e['start_date']}" if e["start_date"] else None,
             f"Travellers: {e['travellers']}" if e["travellers"] else None, f"Phone: {e['phone']}" if e["phone"] else None,
             f"Country: {e['country']}" if e["country"] else None, "", e["message"] or "", "",
             "Open the Website Inbox in TMS to follow it up."]
    msgs = [(s.get("notify_email") or s.get("contact_email"), f"[{s['site_title']}] New {what}: {e['name']}",
             "\n".join(x for x in lines if x is not None), e["email"])]
    ack = (f"Dear {e['name']},\n\nThank you for your {what}" + (f" for {e['tour']}" if e["tour"] else "") +
           ". We have received it and will reply within one business day.\n\n" +
           ("Your place is not confirmed until we write to you with the deposit details.\n\n" if e["kind"] == "booking" else "") +
           f"With best wishes,\n{s['site_title']}" + (f"\n{s['contact_phone']}" if s.get("contact_phone") else ""))
    msgs.append((e["email"], f"{s['site_title']}: we received your {what}", ack, s.get("contact_email")))
    mailer.send(msgs)
