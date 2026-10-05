"""
The tenant's public website (website.py). Served at /site/<tenant code>/ (a
preview on the TMS address) and, through site_router.py, on the tenant's own
domain (www.mavietours.com/ ...).

Pages
-----
* /              Home: the Group boxes; a Group's tours in a slider; a tour's
                 photos in a slider, with a bottom bar (departures, duration,
                 group size, price, Contact us) and "Tour details".
* /tours/<slug>  The tour page: overview, highlights, who it's for, photos,
                 dates and prices, itinerary with hotels, restaurants and
                 Points of Interest (each opens its photos and description),
                 what's included, requirements, packing list, payments and
                 policies, the Booking form and an enquiry form.
* /contact, /about, /policies, /thanks
* /enquire, /book  (POST) -- into the Website Inbox in TMS.

Only published tours and what they show are ever read.
"""
import json

from flask import Blueprint, Response, abort, g, redirect, render_template, request, session, url_for

import website
from db import get_db

site_bp = Blueprint("site", __name__, template_folder="../templates")

HONEYPOT = "website_url"   # a hidden field people never fill in
MAX_PER_HOUR = 8


@site_bp.url_value_preprocessor
def _pull_code(endpoint, values):
    g.site_code = (values or {}).pop("code", None)


@site_bp.url_defaults
def _add_code(endpoint, values):
    if "code" not in values and g.get("site_code"):
        values["code"] = g.site_code


@site_bp.before_request
def _load_site():
    db = get_db()
    found = website.site_for_code(db, g.site_code)
    if found is None:
        abort(404)
    g.site_tenant_id, g.site = found
    on_domain = bool(request.environ.get("tms.site_code"))
    # Not live yet: only the tenant's own signed-in users (and the SystemAdmin) can preview it.
    if not g.site["enabled"]:
        if on_domain or not (session.get("tenant_id") == g.site_tenant_id or session.get("role") == "SystemAdmin"):
            abort(404)
        g.site_preview = True


def surl(endpoint, **kw):
    """A site link: /site/<code>/... on the TMS address, plain /... on the
    tenant's own domain."""
    u = url_for("site." + endpoint, **kw)
    if request.environ.get("tms.site_code"):
        prefix = f"/site/{g.site_code}"
        if u.startswith(prefix):
            u = u[len(prefix):] or "/"
    return u


def img(kind, owner, image_id=0, size="full"):
    return surl("image", kind=kind, owner=owner, image_id=image_id, size=size)


@site_bp.context_processor
def _ctx():  # noqa: D103
    return {"surl": surl, "img": img, "site": g.get("site"), "site_preview": g.get("site_preview"),
            "money": lambda v: f"US${float(v):,.0f}" if v not in (None, "") else ""}


def _tour_json(t):
    """What the home page's sliders and bottom bar need for one tour."""
    return {"slug": t["slug"], "title": t["title"], "tagline": t["tagline"], "group": t["package_group_id"],
            "days": t["duration_days"], "nights": t["duration_nights"], "min": t["group_min"], "max": t["group_max"],
            "price": float(t["price_usd"]) if t["price_usd"] else None,
            "url": surl("tour", slug=t["slug"]),
            "cover": img(t["images"][0]["kind"], t["images"][0]["owner"], t["images"][0]["id"], "full") if t["images"] else None,
            "images": [{"src": img(i["kind"], i["owner"], i["id"], "full"), "thumb": img(i["kind"], i["owner"], i["id"], "thumb"),
                        "caption": i["caption"]} for i in t["images"]],
            "departures": [{"id": d["departure_id"], "start": d["start_date"], "end": d["end_date"],
                            "price": float(d["price"]) if d["price"] else None, "label": d["label"],
                            "bookable": d["bookable"], "seats_left": d["seats_left"]} for d in t["departures"]]}


@site_bp.route("/")
def home():
    db = get_db()
    groups = website.public_groups(db, g.site_tenant_id)
    tours = website.public_tours(db, g.site_tenant_id)
    data = {"groups": [{"id": gr["id"], "slug": gr["slug"], "caption": gr["caption"], "blurb": gr["blurb"],
                        "image": img("group", gr["id"]) if gr["has_image"] else None} for gr in groups],
            "tours": [_tour_json(t) for t in tours]}
    pick_group = request.args.get("group")
    pick_tour = request.args.get("tour")
    return render_template("site/home.html", groups=groups, data=data, pick_group=pick_group, pick_tour=pick_tour)


@site_bp.route("/tours/<slug>")
def tour(slug):
    db = get_db()
    t = website.tour_detail(db, g.site_tenant_id, slug)
    if t is None:
        abort(404)
    return render_template("site/tour.html", t=t, tj=_tour_json(t), form=None, error=None,
                           book_departure=request.args.get("departure", type=int))


@site_bp.route("/contact")
def contact():
    db = get_db()
    tours = website.public_tours(db, g.site_tenant_id)
    return render_template("site/contact.html", tours=tours, form=None, error=None, pick=request.args.get("tour"))


@site_bp.route("/about")
def about():
    return render_template("site/about.html")


@site_bp.route("/policies")
def policies():
    return render_template("site/policies.html")


@site_bp.route("/thanks")
def thanks():
    return render_template("site/thanks.html", kind=request.args.get("kind", "enquiry"))


def _client_ip():
    return (request.headers.get("X-Forwarded-For") or request.remote_addr or "").split(",")[0].strip()[:60]


def _intake(kind):
    db = get_db()
    form = request.form
    back_tour = form.get("tour") or None
    if form.get(HONEYPOT):  # a bot filled the hidden field: pretend all is well
        return redirect(surl("thanks", kind=kind))
    ip = _client_ip()
    if website.recent_from_ip(db, g.site_tenant_id, ip) >= MAX_PER_HOUR:
        error = "We've received several messages from you in the last hour. Please email us directly, or try again later."
    else:
        try:
            eid = website.record_enquiry(db, g.site_tenant_id, kind, form, ip=ip)
        except ValueError as e:
            error = str(e)
        else:
            _notify(db, eid)
            return redirect(surl("thanks", kind=kind))
    # back to the form with what they typed
    if back_tour:
        t = website.tour_detail(db, g.site_tenant_id, back_tour)
        if t is not None:
            return render_template("site/tour.html", t=t, tj=_tour_json(t), form=form, error=error,
                                   error_kind=kind, book_departure=form.get("departure_id", type=int)), 400
    tours = website.public_tours(db, g.site_tenant_id)
    return render_template("site/contact.html", tours=tours, form=form, error=error, pick=back_tour), 400


@site_bp.route("/enquire", methods=["POST"])
def enquire():
    return _intake("enquiry")


@site_bp.route("/book", methods=["POST"])
def book():
    return _intake("booking")


def _notify(db, enquiry_id):
    import mailer
    e = db.execute("""SELECT e.*, COALESCE(w.title, p.package_name) AS tour, d.start_date FROM web_enquiries e
                      LEFT JOIN packages p ON p.package_id = e.package_id LEFT JOIN package_web w ON w.package_id = e.package_id
                      LEFT JOIN package_departures d ON d.departure_id = e.departure_id WHERE e.enquiry_id = ?""",
                   (enquiry_id,)).fetchone()
    s = g.site
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


@site_bp.route("/entity/<kind>/<int:owner>.json")
def entity(kind, owner):
    """A Point of Interest, hotel or restaurant on a published tour: for the pop-up."""
    if kind not in ("poi", "supplier"):
        abort(404)
    e = website.entity(get_db(), g.site_tenant_id, kind, owner)
    if e is None:
        abort(404)
    e["images"] = [{"src": img(kind, owner, i["id"], "full"), "thumb": img(kind, owner, i["id"], "thumb"),
                    "caption": i["caption"]} for i in e["images"]]
    return Response(json.dumps(e), mimetype="application/json")


@site_bp.route("/img/<kind>/<int:owner>/<int:image_id>/<size>")
def image(kind, owner, image_id, size):
    if kind not in ("tour", "poi", "supplier", "group", "logo", "hero") or size not in ("thumb", "full"):
        abort(404)
    data, mime = website.image_bytes(get_db(), g.site_tenant_id, kind, owner, image_id, size)
    if not data:
        abort(404)
    return Response(data, mimetype=mime or "image/jpeg", headers={"Cache-Control": "public, max-age=3600"})


# ---- the chat window (website_chat.py) --------------------------------------------------------

def _chat_json(db, chat, after=0):
    import website_chat
    chat = db.execute("SELECT * FROM web_chats WHERE chat_id = ?", (chat["chat_id"],)).fetchone()
    return {"token": chat["token"], "status": chat["status"], "messages": website_chat.messages(db, chat["chat_id"], after)}


def _chat_or_404(db):
    import website_chat
    if not g.site.get("chat_enabled"):
        abort(404)
    chat = website_chat.by_token(db, g.site_tenant_id, request.values.get("token"))
    if chat is None:
        abort(404)
    return chat


@site_bp.route("/chat/start", methods=["POST"])
def chat_start():
    import website_chat
    if not g.site.get("chat_enabled"):
        abort(404)
    db = get_db()
    chat = website_chat.by_token(db, g.site_tenant_id, request.form.get("token"))
    if chat is None:
        if db.execute("SELECT COUNT(*) FROM web_chats WHERE tenant_id = ? AND ip = ? AND created_at >= datetime('now', '-60 minutes')",
                      (g.site_tenant_id, _client_ip())).fetchone()[0] >= 10:
            return {"error": "Too many chats from your connection. Please use the enquiry form."}, 429
        chat_id, _token = website_chat.start(db, g.site_tenant_id, request.form.get("page"), _client_ip())
        chat = db.execute("SELECT * FROM web_chats WHERE chat_id = ?", (chat_id,)).fetchone()
        website_chat.add(db, chat, "ai", g.site.get("chat_welcome") or
                         f"Hello! I'm the {g.site['site_title']} assistant. Ask me anything about our tours: dates, "
                         "prices, the itinerary, hotels, what to bring or how to book. You can ask for a person at any time.")
    return _chat_json(db, chat)


@site_bp.route("/chat/send", methods=["POST"])
def chat_send():
    import website_chat
    db = get_db()
    chat = _chat_or_404(db)
    after = request.form.get("after", type=int) or 0
    try:
        website_chat.visitor_says(db, g.site_tenant_id, chat, request.form.get("text"))
    except website_chat.ChatError as e:
        return {"error": str(e)}, 400
    return _chat_json(db, chat, after)


@site_bp.route("/chat/poll")
def chat_poll():
    db = get_db()
    chat = _chat_or_404(db)
    return _chat_json(db, chat, request.args.get("after", type=int) or 0)


@site_bp.route("/chat/person", methods=["POST"])
def chat_person():
    import website_chat
    db = get_db()
    chat = _chat_or_404(db)
    website_chat.ask_for_person(db, chat, request.form.get("name"), request.form.get("email"))
    return _chat_json(db, chat, request.form.get("after", type=int) or 0)
