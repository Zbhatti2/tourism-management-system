"""
Website (tenant): what the tenant's public website shows (website.py).

* Settings & Groups -- the site on/off, its title, banner, logo, hero
  picture, colours, contact details and policies; each Group's box (image,
  caption, short text).
* Tours on the website -- every package with its website page: publish it,
  its web address, overview, highlights, requirements, price, deposit,
  booking deadline, departures and its own photos.
* Website Inbox -- enquiries and booking requests from the site.
"""
from flask import Blueprint, Response, abort, current_app, flash, g, redirect, render_template, request, url_for

import website
from auth.decorators import login_required
from db import get_db, log_action

website_admin_bp = Blueprint("website_admin", __name__)

MAX_IMAGE = 8 * 1024 * 1024


def _tenant():
    if not g.get("tenant_id"):
        abort(403)


def _upload(field):
    """(bytes, mime) of an uploaded image, None when nothing was chosen;
    raises ValueError for a non-image or a too-large file."""
    f = request.files.get(field)
    if not f or not f.filename:
        return None
    data = f.read()
    if not data:
        return None
    if len(data) > MAX_IMAGE:
        raise ValueError("That image is larger than 8 MB.")
    mime = f.mimetype or ""
    if not mime.startswith("image/"):
        raise ValueError("Choose an image file (JPG, PNG or WebP).")
    return data, mime


def _public_base():
    """This TMS's own address as visitors reach it. Behind Coolify's proxy
    Flask sees http, but the public address is https."""
    host = request.host
    scheme = (request.headers.get("X-Forwarded-Proto") or request.scheme).split(",")[0].strip()
    if scheme == "http" and not host.split(":")[0] in ("localhost", "127.0.0.1"):
        scheme = "https"
    return f"{scheme}://{host}"


def site_url(s):
    """Where the tenant's own website is (its domain, else a test address)."""
    return website.site_address(s)


# ---- settings and groups ---------------------------------------------------------------------

@website_admin_bp.route("/", methods=["GET", "POST"])
@login_required
def settings():
    _tenant()
    db = get_db()
    g.nav_section = "settings"
    if request.method == "POST":
        f = request.form
        values = {k: (f.get(k) or "").strip() or None for k in website.SETTING_FIELDS}
        values["enabled"] = 1 if f.get("enabled") else 0
        values["chat_enabled"] = 1 if f.get("chat_enabled") else 0
        for k in ("default_deposit_pct", "default_deadline_days"):
            raw = (f.get(k) or "").strip()
            values[k] = int(raw) if raw.isdigit() else None
        try:
            logo, hero = _upload("logo"), _upload("hero")
        except ValueError as e:
            flash(str(e), "error")
            return redirect(url_for("website_admin.settings"))
        db.execute("UPDATE tenants SET website_domain = ? WHERE tenant_id = ?", (website.clean_domain(f.get("website_domain")), g.tenant_id))
        website.save_settings(db, g.tenant_id, values, logo=logo, hero=hero)
        log_action("Update", "tenant_websites", g.tenant_id, "Updated the website settings")
        flash("Website settings saved.", "success")
        section = f.get("section")
        return redirect(url_for("website_admin.settings") + (f"#{section}" if section in ("site", "api", "contact", "policies", "chat") else ""))
    key = website.ensure_site_key(db, g.tenant_id)
    s = website.settings(db, g.tenant_id)
    return render_template("website_admin/settings.html", s=s, groups=website.groups_admin(db, g.tenant_id),
                           site_key=key, tms_base=_public_base(),
                           preview=site_url(s), help_id="website_admin/overview")


@website_admin_bp.route("/new-key", methods=["POST"])
@login_required
def new_key():
    _tenant()
    if g.get("role") not in ("TenantAdmin", "SystemAdmin"):
        abort(403)
    website.new_site_key(get_db(), g.tenant_id)
    log_action("Update", "tenant_websites", g.tenant_id, "Replaced the Website API key")
    flash("New website key made. Put it in the website's js/config.js and redeploy the website: the old key no longer works.", "success")
    return redirect(url_for("website_admin.settings") + "#api")


@website_admin_bp.route("/groups/<int:group_id>", methods=["POST"])
@login_required
def save_group(group_id):
    _tenant()
    db = get_db()
    try:
        website.save_group(db, g.tenant_id, group_id, bool(request.form.get("web_show")),
                           (request.form.get("web_caption") or "").strip(), (request.form.get("web_blurb") or "").strip(),
                           image=_upload("image"))
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("website_admin.settings") + "#groups")
    log_action("Update", "package_groups", group_id, "Updated the group's website box")
    flash("Group box saved.", "success")
    return redirect(url_for("website_admin.settings") + "#groups")


@website_admin_bp.route("/image/<kind>/<int:owner_id>")
@website_admin_bp.route("/image/<kind>/<int:owner_id>/<int:image_id>")
@login_required
def image(kind, owner_id, image_id=0):
    """The admin's own preview of a site image (logo, hero, group, tour photo)."""
    _tenant()
    db = get_db()
    if kind in ("logo", "hero", "group"):
        data, mime = website.image_bytes(db, g.tenant_id, kind, owner_id, image_id)
    elif kind == "tour":
        r = db.execute("SELECT thumb_data, file_data, mime_type FROM package_web_images WHERE image_id = ? AND tenant_id = ?",
                       (image_id, g.tenant_id)).fetchone()
        data, mime = ((r[0], "image/jpeg") if r and r[0] else (r[1], r[2]) if r else (None, None))
    else:
        abort(404)
    if not data:
        abort(404)
    return Response(data, mimetype=mime or "image/jpeg", headers={"Cache-Control": "private, max-age=300"})


# ---- tours ---------------------------------------------------------------------------------

@website_admin_bp.route("/tours")
@login_required
def tours():
    _tenant()
    db = get_db()
    g.nav_section = "tours"
    rows = db.execute("""SELECT p.package_id, p.package_code, p.package_name, p.status, p.duration_days, pg.label AS group_label,
                                w.published, w.slug, w.price_usd, COALESCE(w.title, p.package_name) AS web_title,
                                (SELECT COUNT(*) FROM package_departures d WHERE d.package_id = p.package_id
                                 AND d.start_date >= date('now') AND d.status != 'cancelled') AS upcoming,
                                (SELECT COUNT(*) FROM package_web_images i WHERE i.package_id = p.package_id) AS photos
                         FROM packages p LEFT JOIN package_web w ON w.package_id = p.package_id
                         LEFT JOIN package_groups pg ON pg.package_group_id = p.package_group_id
                         WHERE p.tenant_id = ? AND p.status != 'archived'
                         ORDER BY COALESCE(w.published, 0) DESC, pg.label, p.package_name""", (g.tenant_id,)).fetchall()
    s = website.settings(db, g.tenant_id)
    return render_template("website_admin/tours.html", rows=rows, s=s, preview=site_url(s),
                           help_id="website_admin/overview")


def _package(db, package_id):
    p = db.execute("""SELECT p.*, pg.label AS group_label FROM packages p
                      LEFT JOIN package_groups pg ON pg.package_group_id = p.package_group_id
                      WHERE p.package_id = ? AND p.tenant_id = ?""", (package_id, g.tenant_id)).fetchone()
    if p is None:
        abort(404)
    return p


@website_admin_bp.route("/tours/<int:package_id>", methods=["GET", "POST"])
@login_required
def tour(package_id):
    _tenant()
    db = get_db()
    g.nav_section = "tours"
    p = _package(db, package_id)
    if request.method == "POST":
        try:
            slug = website.save_package_web(db, g.tenant_id, package_id, request.form)
        except ValueError as e:
            flash(str(e), "error")
            return _tour_page(db, p, form=request.form)
        log_action("Update", "package_web", package_id, f"Website page of {p['package_code']} saved"
                   + (" (published)" if request.form.get("published") else ""))
        flash("Saved. " + ("The tour is on the website at /tours/" + slug + "." if request.form.get("published")
                           else "The tour is not published yet."), "success")
        return redirect(url_for("website_admin.tour", package_id=package_id))
    return _tour_page(db, p)


def _tour_page(db, p, form=None):
    w = website.package_web(db, g.tenant_id, p["package_id"]) or {}
    s = website.settings(db, g.tenant_id)
    images = db.execute("SELECT image_id, caption, sort_order FROM package_web_images WHERE package_id = ? AND tenant_id = ? "
                        "ORDER BY sort_order, image_id", (p["package_id"], g.tenant_id)).fetchall()
    auto = [i for i in website.tour_images(db, g.tenant_id, p["package_id"]) if i["kind"] != "tour"]
    deps = [website.departure_view(d, w, s, p["duration_days"]) for d in website.departures(db, g.tenant_id, p["package_id"])]
    return render_template("website_admin/tour.html", p=p, w=w, form=form, s=s, images=images, auto_images=auto,
                           departures=deps, statuses=website.DEPARTURE_STATUSES, preview=site_url(s),
                           help_id="website_admin/overview")


@website_admin_bp.route("/tours/<int:package_id>/departures", methods=["POST"])
@website_admin_bp.route("/tours/<int:package_id>/departures/<int:departure_id>", methods=["POST"])
@login_required
def departure(package_id, departure_id=None):
    _tenant()
    db = get_db()
    _package(db, package_id)
    if request.form.get("action") == "delete" and departure_id:
        kept = website.delete_departure(db, g.tenant_id, package_id, departure_id)
        flash("Departure cancelled (it has enquiries or bookings, so it is kept)." if kept else "Departure deleted.", "success")
    else:
        try:
            website.save_departure(db, g.tenant_id, package_id, request.form, departure_id)
            flash("Departure saved.", "success")
        except ValueError as e:
            flash(str(e), "error")
    return redirect(url_for("website_admin.tour", package_id=package_id) + "#departures")


@website_admin_bp.route("/tours/<int:package_id>/images", methods=["POST"])
@login_required
def tour_images(package_id):
    _tenant()
    db = get_db()
    _package(db, package_id)
    action = request.form.get("action")
    if action == "delete":
        db.execute("DELETE FROM package_web_images WHERE image_id = ? AND package_id = ? AND tenant_id = ?",
                   (request.form.get("image_id", type=int), package_id, g.tenant_id))
        db.commit()
        flash("Photo removed.", "success")
    elif action == "caption":
        for key, val in request.form.items():
            if key.startswith("caption_") and key[8:].isdigit():
                db.execute("UPDATE package_web_images SET caption = ? WHERE image_id = ? AND package_id = ? AND tenant_id = ?",
                           (val.strip() or None, int(key[8:]), package_id, g.tenant_id))
            if key.startswith("sort_") and key[5:].isdigit() and val.strip().lstrip("-").isdigit():
                db.execute("UPDATE package_web_images SET sort_order = ? WHERE image_id = ? AND package_id = ? AND tenant_id = ?",
                           (int(val), int(key[5:]), package_id, g.tenant_id))
        db.commit()
        flash("Captions and order saved.", "success")
    else:
        n = 0
        for f in request.files.getlist("photos"):
            if not f or not f.filename:
                continue
            data = f.read()
            if not data or len(data) > MAX_IMAGE or not (f.mimetype or "").startswith("image/"):
                flash(f"{f.filename}: not an image, or larger than 8 MB.", "error")
                continue
            caption = f.filename.rsplit(".", 1)[0].replace("_", " ").replace("-", " ").strip()
            website.add_package_image(db, g.tenant_id, package_id, data, f.mimetype, caption)
            n += 1
        if n:
            flash(f"{n} photo{'s' if n != 1 else ''} added.", "success")
    return redirect(url_for("website_admin.tour", package_id=package_id) + "#photos")


# ---- inbox ---------------------------------------------------------------------------------

@website_admin_bp.route("/inbox")
@login_required
def inbox():
    _tenant()
    db = get_db()
    g.nav_section = "inbox"
    status = request.args.get("status", "open")
    kind = request.args.get("kind") or None
    q = (request.args.get("q") or "").strip()
    rows = website.inbox(db, g.tenant_id, status=status, kind=kind, q=q)
    return render_template("website_admin/inbox.html", rows=rows, status=status, kind=kind, q=q,
                           statuses=website.STATUSES, help_id="website_admin/overview")


@website_admin_bp.route("/inbox/<int:enquiry_id>", methods=["GET", "POST"])
@login_required
def enquiry(enquiry_id):
    _tenant()
    db = get_db()
    g.nav_section = "inbox"
    e = db.execute("""SELECT e.*, COALESCE(w.title, p.package_name) AS tour, p.package_code, d.start_date, d.end_date,
                             w.slug, u.display_name AS handler
                      FROM web_enquiries e LEFT JOIN packages p ON p.package_id = e.package_id
                      LEFT JOIN package_web w ON w.package_id = e.package_id
                      LEFT JOIN package_departures d ON d.departure_id = e.departure_id
                      LEFT JOIN users u ON u.user_id = e.handled_by
                      WHERE e.enquiry_id = ? AND e.tenant_id = ?""", (enquiry_id, g.tenant_id)).fetchone()
    if e is None:
        abort(404)
    if request.method == "POST":
        status = request.form.get("status")
        if status not in website.STATUSES:
            status = e["status"]
        db.execute("UPDATE web_enquiries SET status = ?, staff_note = ?, handled_by = ?, handled_at = datetime('now') "
                   "WHERE enquiry_id = ? AND tenant_id = ?",
                   (status, (request.form.get("staff_note") or "").strip() or None, g.user_id, enquiry_id, g.tenant_id))
        if status == "confirmed" and e["status"] != "confirmed" and e["kind"] == "booking" and e["departure_id"]:
            db.execute("UPDATE package_departures SET seats_taken = seats_taken + ? WHERE departure_id = ? AND tenant_id = ?",
                       (e["travellers"] or 1, e["departure_id"], g.tenant_id))
        db.commit()
        log_action("Update", "web_enquiries", enquiry_id, f"Website {e['kind']} marked {website.STATUSES[status]}")
        flash("Saved.", "success")
        return redirect(url_for("website_admin.enquiry", enquiry_id=enquiry_id))
    import json
    return render_template("website_admin/enquiry.html", e=e, details=json.loads(e["details"]) if e["details"] else {},
                           statuses=website.STATUSES, help_id="website_admin/overview")


# ---- website chats (website_chat.py) ---------------------------------------------------------

def _chat(db, chat_id):
    c = db.execute("SELECT * FROM web_chats WHERE chat_id = ? AND tenant_id = ?", (chat_id, g.tenant_id)).fetchone()
    if c is None:
        abort(404)
    return c


@website_admin_bp.route("/chats")
@login_required
def chats():
    import website_chat
    _tenant()
    db = get_db()
    g.nav_section = "chats"
    show = request.args.get("show", "open")
    return render_template("website_admin/chats.html", rows=website_chat.chats(db, g.tenant_id, show), show=show,
                           statuses=website_chat.STATUSES, help_id="website_admin/overview")


@website_admin_bp.route("/chats/<int:chat_id>")
@login_required
def chat(chat_id):
    import website_chat
    _tenant()
    db = get_db()
    g.nav_section = "chats"
    c = _chat(db, chat_id)
    website_chat.mark_seen(db, chat_id)
    return render_template("website_admin/chat.html", c=c, msgs=website_chat.messages(db, chat_id),
                           statuses=website_chat.STATUSES, help_id="website_admin/overview")


@website_admin_bp.route("/chats/<int:chat_id>/poll")
@login_required
def chat_poll(chat_id):
    import website_chat
    _tenant()
    db = get_db()
    c = _chat(db, chat_id)
    website_chat.mark_seen(db, chat_id)
    return {"status": c["status"], "status_label": website_chat.STATUSES[c["status"]],
            "messages": website_chat.messages(db, chat_id, request.args.get("after", type=int) or 0)}


@website_admin_bp.route("/chats/<int:chat_id>", methods=["POST"])
@login_required
def chat_action(chat_id):
    import website_chat
    _tenant()
    db = get_db()
    c = _chat(db, chat_id)
    action = request.form.get("action")
    try:
        if action == "reply":
            website_chat.staff_reply(db, c, request.form.get("text"), g.user_id)
        elif action == "take":
            website_chat.set_status(db, c, "human", user_id=g.user_id,
                                    note=f"{g.get('display_name') or 'A member of our team'} has joined the chat.")
        elif action == "to_ai":
            website_chat.set_status(db, c, "ai", note="Our assistant will answer from here. Ask for a person at any time.")
        elif action == "close":
            website_chat.set_status(db, c, "closed", note="This chat has been closed. Write again any time.")
        else:
            abort(400)
    except website_chat.ChatError as e:
        if request.form.get("ajax"):
            return {"error": str(e)}, 400
        flash(str(e), "error")
    if request.form.get("ajax"):
        return {"ok": True}
    return redirect(url_for("website_admin.chat", chat_id=chat_id))
