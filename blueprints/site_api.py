"""
The Website API: what a tenant's own website (its own repository and
Coolify app, like GSS's tenant websites -- e.g. Zbhatti2/ma-vie-tours-website
on www.mavietours.com) reads from and sends to TMS.

    /api/site/v1/<site key>/...

The site key is public (it sits in the website's js/config.js, like the GSS
lead-intake token); it only says which tenant's published tours to show.
Website -> Settings & Groups in TMS shows it, with a button to replace it.

Who may call it (browsers enforce this through CORS):
* the live site -- https://<domain> and https://www.<domain> -- once
  "Website is live" is on;
* the test addresses listed in Settings (e.g. http://localhost:8080 or a
  Coolify preview URL), live or not.
Images are plain <img> requests (no Origin) and are always served -- they
are only ever images of published tours.

Reads (GET):  site, tours (groups + tour cards), tours/<slug>, entity/<kind>/<id>,
              img/<kind>/<owner>/<image id>/<thumb|full>, chat/poll
Writes (POST, form-encoded, no cookies): enquire, book, chat/start,
              chat/send, chat/person
Answers are JSON; errors are {"error": "..."} with a 4xx status. Image
addresses in the answers are paths on TMS ("/api/site/v1/..."): the website
puts its TMS address in front of them.

Only published tours and what they show are ever read; costs, margins,
supplier contacts and internal notes never leave TMS.
"""
from flask import Blueprint, Response, abort, g, jsonify, request, url_for

import website
from db import get_db

site_api_bp = Blueprint("site_api", __name__)

HONEYPOT = "website_url"   # a hidden field people never fill in
MAX_PER_HOUR = 8           # enquiries / bookings per visitor address per hour
CHATS_PER_HOUR = 10


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


@site_api_bp.errorhandler(ApiError)
def _api_error(e):
    return jsonify({"error": str(e)}), e.status


@site_api_bp.url_value_preprocessor
def _pull_key(endpoint, values):
    g.site_key = (values or {}).pop("key", None)


@site_api_bp.url_defaults
def _add_key(endpoint, values):
    if "key" not in values and g.get("site_key"):
        values["key"] = g.site_key


def _request_origin():
    o = request.headers.get("Origin")
    return o.rstrip("/").lower() if o else None


@site_api_bp.before_request
def _load_site():
    found = website.site_for_key(get_db(), g.site_key)
    if found is None:
        raise ApiError("Unknown website key.", 404)
    g.site_tenant_id, g.site = found
    live, tests = website.live_origins(g.site), website.test_origins(g.site)
    allowed = tests | (live if g.site["enabled"] else set())
    origin = _request_origin()
    g.cors_origin = origin if origin in allowed else None
    if request.method == "OPTIONS":
        return Response(status=204)
    if request.endpoint == "site_api.image":
        return None  # <img> requests: published images only, any page may show them
    if origin and g.cors_origin is None:
        raise ApiError("This web address may not use this website's data.", 403)
    if not origin and not g.site["enabled"]:
        raise ApiError("The website is not live yet.", 403)


@site_api_bp.after_request
def _cors(resp):
    if g.get("cors_origin"):
        resp.headers["Access-Control-Allow-Origin"] = g.cors_origin
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Access-Control-Max-Age"] = "3600"
    resp.headers["Vary"] = "Origin"
    return resp


def img(kind, owner, image_id=0, size="full"):
    return url_for("site_api.image", kind=kind, owner=owner, image_id=image_id, size=size)


def _client_ip():
    return (request.headers.get("X-Forwarded-For") or request.remote_addr or "").split(",")[0].strip()[:60]


def _num(v):
    return float(v) if v not in (None, "") else None


# ---- reads ----------------------------------------------------------------------------------

PUBLIC_SETTINGS = ["site_title", "banner_text", "about_text", "contact_email", "contact_phone", "whatsapp", "address",
                   "color_primary", "color_accent", "payment_methods", "payment_terms", "deposit_policy", "booking_terms",
                   "cancellation_policy", "refund_policy", "privacy_policy", "deposit_pct", "deadline_days"]


@site_api_bp.route("/site")
def site():
    s = g.site
    out = {k: s.get(k) for k in PUBLIC_SETTINGS}
    out["live"] = bool(s["enabled"])
    out["chat_enabled"] = bool(s.get("chat_enabled"))
    out["logo"] = img("logo", 0) if s["has_logo"] else None
    out["hero"] = img("hero", 0) if s["has_hero"] else None
    return jsonify(out)


def _departure_json(d):
    return {"id": d["departure_id"], "start": d["start_date"], "end": d["end_date"], "price": _num(d["price"]),
            "deadline": d["deadline"], "seats_left": d["seats_left"], "bookable": d["bookable"], "label": d["label"]}


def _tour_card(t):
    return {"slug": t["slug"], "title": t["title"], "tagline": t["tagline"], "group": t["package_group_id"],
            "days": t["duration_days"], "nights": t["duration_nights"], "min": t["group_min"], "max": t["group_max"],
            "price": _num(t["price_usd"]),
            "images": [{"src": img(i["kind"], i["owner"], i["id"]), "thumb": img(i["kind"], i["owner"], i["id"], "thumb"),
                        "caption": i["caption"]} for i in t["images"]],
            "departures": [_departure_json(d) for d in t["departures"]]}


@site_api_bp.route("/tours")
def tours():
    db = get_db()
    groups = website.public_groups(db, g.site_tenant_id)
    return jsonify({"groups": [{"id": gr["id"], "slug": gr["slug"], "caption": gr["caption"], "blurb": gr["blurb"],
                                "tours": gr["tours"], "image": img("group", gr["id"]) if gr["has_image"] else None}
                               for gr in groups],
                    "tours": [_tour_card(t) for t in website.public_tours(db, g.site_tenant_id)]})


@site_api_bp.route("/tours/<slug>")
def tour(slug):
    t = website.tour_detail(get_db(), g.site_tenant_id, slug)
    if t is None:
        raise ApiError("No such tour.", 404)
    out = _tour_card(t)
    out["departures"] = [_departure_json(d) for d in t["all_departures"]]
    for k in ("overview", "who_for", "requirements", "difficulty", "max_altitude_m", "lodging", "payment_terms",
              "cancellation_policy", "deposit", "deadline_days"):
        out[k] = t[k]
    out["single_supplement"] = _num(t["single_supplement_usd"])
    out["highlights"] = t["highlight_list"]
    out["packing"] = t["packing"]
    out["included"] = t["included_list"]
    out["excluded"] = t["excluded_list"]
    out["route"] = [{"city": s["city"], "country": s["country"], "nights": s["nights"]} for s in t["route"]]
    out["itinerary"] = [{"day": d["day_number"], "title": d["title"], "city": d["city"], "description": d["description"],
                    "pois": [{"id": p["poi_id"], "name": p["name"], "type": p["type"]} for p in d["pois"]],
                    "stays": [{"id": s["supplier_id"], "name": s["name"], "grade": s["grade"]} for s in d["stays"]],
                    "meals": [{"id": s["supplier_id"], "name": s["name"]} for s in d["meals"]]} for d in t["days"]]
    out["group_info"] = ({"id": t["group"]["package_group_id"], "caption": t["group"]["caption"], "slug": t["group"]["slug"]}
                         if t["group"] else None)
    return jsonify(out)


@site_api_bp.route("/entity/<kind>/<int:owner>")
def entity(kind, owner):
    """A Point of Interest, hotel or restaurant on a published tour: for the pop-up."""
    if kind not in ("poi", "supplier"):
        raise ApiError("Unknown kind.", 404)
    e = website.entity(get_db(), g.site_tenant_id, kind, owner)
    if e is None:
        raise ApiError("Not found.", 404)
    e["images"] = [{"src": img(kind, owner, i["id"]), "thumb": img(kind, owner, i["id"], "thumb"), "caption": i["caption"]}
                   for i in e["images"]]
    return jsonify(e)


@site_api_bp.route("/img/<kind>/<int:owner>/<int:image_id>/<size>")
def image(kind, owner, image_id, size):
    if kind not in ("tour", "poi", "supplier", "group", "logo", "hero") or size not in ("thumb", "full"):
        abort(404)
    data, mime = website.image_bytes(get_db(), g.site_tenant_id, kind, owner, image_id, size)
    if not data:
        abort(404)
    return Response(data, mimetype=mime or "image/jpeg", headers={"Cache-Control": "public, max-age=3600"})


# ---- enquiries and booking requests -----------------------------------------------------------

def _intake(kind):
    db = get_db()
    form = request.form
    if form.get(HONEYPOT):  # a bot filled the hidden field: pretend all is well
        return jsonify({"ok": True})
    ip = _client_ip()
    if website.recent_from_ip(db, g.site_tenant_id, ip) >= MAX_PER_HOUR:
        raise ApiError("We've received several messages from you in the last hour. Please email us directly, "
                       "or try again later.", 429)
    try:
        eid = website.record_enquiry(db, g.site_tenant_id, kind, form, ip=ip)
    except ValueError as e:
        raise ApiError(str(e)) from None
    website.notify(db, g.site, eid)
    return jsonify({"ok": True})


@site_api_bp.route("/enquire", methods=["POST", "OPTIONS"])
def enquire():
    return _intake("enquiry")


@site_api_bp.route("/book", methods=["POST", "OPTIONS"])
def book():
    return _intake("booking")


# ---- the chat window (website_chat.py) --------------------------------------------------------

def _chat_json(db, chat, after=0):
    import website_chat
    chat = db.execute("SELECT * FROM web_chats WHERE chat_id = ?", (chat["chat_id"],)).fetchone()
    return jsonify({"token": chat["token"], "status": chat["status"], "messages": website_chat.messages(db, chat["chat_id"], after)})


def _chat_on():
    if not g.site.get("chat_enabled"):
        raise ApiError("The chat is switched off.", 404)


def _chat_or_404(db):
    import website_chat
    _chat_on()
    chat = website_chat.by_token(db, g.site_tenant_id, request.values.get("token"))
    if chat is None:
        raise ApiError("This chat has ended. Please start a new one.", 404)
    return chat


@site_api_bp.route("/chat/start", methods=["POST", "OPTIONS"])
def chat_start():
    import website_chat
    _chat_on()
    db = get_db()
    chat = website_chat.by_token(db, g.site_tenant_id, request.form.get("token"))
    if chat is None:
        if db.execute("SELECT COUNT(*) FROM web_chats WHERE tenant_id = ? AND ip = ? AND created_at >= datetime('now', '-60 minutes')",
                      (g.site_tenant_id, _client_ip())).fetchone()[0] >= CHATS_PER_HOUR:
            raise ApiError("Too many chats from your connection. Please use the enquiry form.", 429)
        chat_id, _token = website_chat.start(db, g.site_tenant_id, (request.form.get("page") or "")[:200], _client_ip())
        chat = db.execute("SELECT * FROM web_chats WHERE chat_id = ?", (chat_id,)).fetchone()
        website_chat.add(db, chat, "ai", g.site.get("chat_welcome") or
                         f"Hello! I'm the {g.site['site_title']} assistant. Ask me anything about our tours: dates, "
                         "prices, the itinerary, hotels, what to bring or how to book. You can ask for a person at any time.")
    return _chat_json(db, chat)


@site_api_bp.route("/chat/send", methods=["POST", "OPTIONS"])
def chat_send():
    import website_chat
    db = get_db()
    chat = _chat_or_404(db)
    try:
        website_chat.visitor_says(db, g.site_tenant_id, chat, request.form.get("text"))
    except website_chat.ChatError as e:
        raise ApiError(str(e)) from None
    return _chat_json(db, chat, request.form.get("after", type=int) or 0)


@site_api_bp.route("/chat/poll")
def chat_poll():
    db = get_db()
    chat = _chat_or_404(db)
    return _chat_json(db, chat, request.args.get("after", type=int) or 0)


@site_api_bp.route("/chat/person", methods=["POST", "OPTIONS"])
def chat_person():
    import website_chat
    db = get_db()
    chat = _chat_or_404(db)
    website_chat.ask_for_person(db, chat, (request.form.get("name") or "")[:150], (request.form.get("email") or "")[:200])
    return _chat_json(db, chat, request.form.get("after", type=int) or 0)
