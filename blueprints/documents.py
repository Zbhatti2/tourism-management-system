"""
Module D — Documents Management & Personal Knowledge Base.

Two things this module demonstrates that A-C didn't need: a many-to-many
lookup relationship (content_knowledge_domains — spec §3.5 decision, since
content can span more than one domain) via checkboxes, and free-text
tag-style child tables (content_keywords, content_hashtags) edited as a
comma-separated field and normalized into rows on save.
"""
import os

from flask import Blueprint, abort, flash, g, jsonify, redirect, render_template, request, url_for

from auth.decorators import login_required
from db import get_db, log_action
from utils import open_local_path, pick_file_dialog

documents_bp = Blueprint("documents", __name__)

DESCRIPTION_MAX_LENGTH = 200


def _split_terms(raw: str):
    return sorted({t.strip() for t in raw.split(",") if t.strip()})


@documents_bp.route("/")
@login_required
def index():
    db = get_db()
    q = request.args.get("q", "").strip()
    sql = """
        SELECT c.*, ct.label AS content_type_label
        FROM content c
        LEFT JOIN content_types ct ON ct.content_type_id = c.content_type_id
        WHERE c.is_deleted = 0 AND c.tenant_id = ?
    """
    params = [g.tenant_id]
    if q:
        sql += " AND (c.content_name LIKE ? OR c.description LIKE ? OR c.authors LIKE ?)"
        like = f"%{q}%"
        params += [like, like, like]
    sql += " ORDER BY c.content_name"
    items = db.execute(sql, params).fetchall()
    return render_template("documents/list.html", items=items, q=q)


def _lookups(db):
    tenant_id = g.tenant_id
    return {
        "content_types": db.execute(
            "SELECT * FROM content_types WHERE is_active = 1 AND tenant_id = ? ORDER BY label COLLATE NOCASE", (tenant_id,)
        ).fetchall(),
        "knowledge_domains": db.execute(
            "SELECT * FROM knowledge_domains WHERE is_active = 1 AND tenant_id = ? ORDER BY label COLLATE NOCASE", (tenant_id,)
        ).fetchall(),
    }


@documents_bp.route("/new", methods=["GET", "POST"])
@login_required
def new_content():
    db = get_db()
    if request.method == "POST":
        form = request.form
        description = form.get("description", "").strip()
        if len(description) > DESCRIPTION_MAX_LENGTH:
            flash(f"Description must be {DESCRIPTION_MAX_LENGTH} characters or fewer (currently {len(description)}).", "error")
            selected_domains = {int(v) for v in form.getlist("knowledge_domain_ids") if v.isdigit()}
            return render_template(
                "documents/form.html", item=form, content_id=None, selected_domains=selected_domains,
                keywords=form.get("keywords", ""), hashtags=form.get("hashtags", ""), **_lookups(db),
            )
        db.execute(
            "INSERT INTO content (tenant_id, content_name, content_type_id, authors, description, notes) VALUES (?, ?, ?, ?, ?, ?)",
            (g.tenant_id, form["content_name"].strip(), form.get("content_type_id") or None,
             form.get("authors", "").strip() or None, description or None,
             form.get("notes", "").strip() or None),
        )
        db.commit()
        content_id = db.execute("SELECT last_insert_rowid() id").fetchone()["id"]

        for domain_id in form.getlist("knowledge_domain_ids"):
            db.execute(
                "INSERT OR IGNORE INTO content_knowledge_domains (tenant_id, content_id, knowledge_domain_id) VALUES (?, ?, ?)",
                (g.tenant_id, content_id, domain_id),
            )
        for term in _split_terms(form.get("keywords", "")):
            db.execute(
                "INSERT OR IGNORE INTO content_keywords (tenant_id, content_id, term) VALUES (?, ?, ?)",
                (g.tenant_id, content_id, term),
            )
        for term in _split_terms(form.get("hashtags", "")):
            db.execute(
                "INSERT OR IGNORE INTO content_hashtags (tenant_id, content_id, term) VALUES (?, ?, ?)",
                (g.tenant_id, content_id, term),
            )
        db.commit()

        log_action("Create", "content", content_id, f"Created content {form['content_name']}")
        flash("Content created.", "success")
        return redirect(url_for("documents.view_content", content_id=content_id))
    return render_template("documents/form.html", item=None, content_id=None, selected_domains=set(), keywords="", hashtags="", **_lookups(db))


@documents_bp.route("/<int:content_id>")
@login_required
def view_content(content_id):
    db = get_db()
    item = db.execute(
        """SELECT c.*, ct.label AS content_type_label FROM content c
           LEFT JOIN content_types ct ON ct.content_type_id = c.content_type_id
           WHERE c.content_id = ? AND c.is_deleted = 0 AND c.tenant_id = ?""", (content_id, g.tenant_id),
    ).fetchone()
    if item is None:
        abort(404)
    locations = db.execute("SELECT * FROM content_locations WHERE content_id = ? ORDER BY location_id", (content_id,)).fetchall()
    domains = db.execute(
        """SELECT kd.label FROM content_knowledge_domains ckd
           JOIN knowledge_domains kd ON kd.knowledge_domain_id = ckd.knowledge_domain_id
           WHERE ckd.content_id = ? ORDER BY kd.sort_order""", (content_id,),
    ).fetchall()
    keywords = db.execute("SELECT term FROM content_keywords WHERE content_id = ? ORDER BY term", (content_id,)).fetchall()
    hashtags = db.execute("SELECT term FROM content_hashtags WHERE content_id = ? ORDER BY term", (content_id,)).fetchall()
    contact_links = db.execute(
        """SELECT ccl.*, c.full_name, c.file_as, clt.label AS link_type_label
           FROM content_contact_links ccl
           LEFT JOIN contacts c ON c.contact_id = ccl.contact_id AND c.is_deleted = 0
           LEFT JOIN content_link_types clt ON clt.content_link_type_id = ccl.link_type_id
           WHERE ccl.content_id = ?
           ORDER BY ccl.content_contact_link_id""",
        (content_id,),
    ).fetchall()
    return render_template(
        "documents/view.html", item=item, locations=locations, domains=domains,
        keywords=keywords, hashtags=hashtags, contact_links=contact_links,
    )


@documents_bp.route("/<int:content_id>/edit", methods=["GET", "POST"])
@login_required
def edit_content(content_id):
    db = get_db()
    item = db.execute(
        "SELECT * FROM content WHERE content_id = ? AND is_deleted = 0 AND tenant_id = ?", (content_id, g.tenant_id)
    ).fetchone()
    if item is None:
        abort(404)

    if request.method == "POST":
        form = request.form
        description = form.get("description", "").strip()
        if len(description) > DESCRIPTION_MAX_LENGTH:
            flash(f"Description must be {DESCRIPTION_MAX_LENGTH} characters or fewer (currently {len(description)}).", "error")
            selected_domains = {int(v) for v in form.getlist("knowledge_domain_ids") if v.isdigit()}
            return render_template(
                "documents/form.html", item=form, content_id=content_id, selected_domains=selected_domains,
                keywords=form.get("keywords", ""), hashtags=form.get("hashtags", ""), **_lookups(db),
            )
        db.execute(
            "UPDATE content SET content_name=?, content_type_id=?, authors=?, description=?, notes=?, updated_at=datetime('now') WHERE content_id=? AND tenant_id=?",
            (form["content_name"].strip(), form.get("content_type_id") or None,
             form.get("authors", "").strip() or None, description or None,
             form.get("notes", "").strip() or None, content_id, g.tenant_id),
        )
        db.execute("DELETE FROM content_knowledge_domains WHERE content_id = ? AND tenant_id = ?", (content_id, g.tenant_id))
        for domain_id in form.getlist("knowledge_domain_ids"):
            db.execute(
                "INSERT OR IGNORE INTO content_knowledge_domains (tenant_id, content_id, knowledge_domain_id) VALUES (?, ?, ?)",
                (g.tenant_id, content_id, domain_id),
            )
        db.execute("DELETE FROM content_keywords WHERE content_id = ? AND tenant_id = ?", (content_id, g.tenant_id))
        for term in _split_terms(form.get("keywords", "")):
            db.execute(
                "INSERT OR IGNORE INTO content_keywords (tenant_id, content_id, term) VALUES (?, ?, ?)",
                (g.tenant_id, content_id, term),
            )
        db.execute("DELETE FROM content_hashtags WHERE content_id = ? AND tenant_id = ?", (content_id, g.tenant_id))
        for term in _split_terms(form.get("hashtags", "")):
            db.execute(
                "INSERT OR IGNORE INTO content_hashtags (tenant_id, content_id, term) VALUES (?, ?, ?)",
                (g.tenant_id, content_id, term),
            )
        db.commit()

        log_action("Update", "content", content_id, "Updated content")
        flash("Content updated.", "success")
        return redirect(url_for("documents.view_content", content_id=content_id))

    selected_domains = {
        r["knowledge_domain_id"] for r in db.execute(
            "SELECT knowledge_domain_id FROM content_knowledge_domains WHERE content_id = ?", (content_id,)
        ).fetchall()
    }
    keywords = ", ".join(r["term"] for r in db.execute("SELECT term FROM content_keywords WHERE content_id = ? ORDER BY term", (content_id,)).fetchall())
    hashtags = ", ".join(r["term"] for r in db.execute("SELECT term FROM content_hashtags WHERE content_id = ? ORDER BY term", (content_id,)).fetchall())
    return render_template("documents/form.html", item=item, content_id=content_id, selected_domains=selected_domains, keywords=keywords, hashtags=hashtags, **_lookups(db))


@documents_bp.route("/<int:content_id>/delete", methods=["POST"])
@login_required
def delete_content(content_id):
    db = get_db()
    item = db.execute(
        "SELECT * FROM content WHERE content_id = ? AND is_deleted = 0 AND tenant_id = ?", (content_id, g.tenant_id)
    ).fetchone()
    if item is None:
        abort(404)
    db.execute(
        "UPDATE content SET is_deleted = 1, updated_at = datetime('now') WHERE content_id = ? AND tenant_id = ?",
        (content_id, g.tenant_id),
    )
    db.commit()
    log_action("Delete", "content", content_id, f"Deleted content {item['content_name']}")
    flash("Content deleted.", "success")
    return redirect(url_for("documents.index"))


@documents_bp.route("/browse-file")
@login_required
def browse_file():
    path, error = pick_file_dialog()
    return jsonify(path=path, error=error)


# --------------------------------------------------------------- contacts link
# "Contacts Link" — lets a content item reference an existing Contact, a
# plain URL, or both (at least one is required), optionally tagged with a
# Link Type (e.g. "Author", "Reviewed by"). Same list/add/remove pattern as
# contacts' "Reference Links", pointed the other way (from Content).

@documents_bp.route("/<int:content_id>/contact-links/new", methods=["GET", "POST"])
@login_required
def new_contact_link(content_id):
    db = get_db()
    item = db.execute(
        "SELECT * FROM content WHERE content_id = ? AND is_deleted = 0 AND tenant_id = ?", (content_id, g.tenant_id)
    ).fetchone()
    if item is None:
        abort(404)
    if request.method == "POST":
        contact_id = request.form.get("contact_id", type=int) or None
        url = request.form.get("url", "").strip() or None
        if not contact_id and not url:
            flash("Pick a contact, enter a URL, or both.", "error")
            return redirect(url_for("documents.new_contact_link", content_id=content_id))
        db.execute(
            "INSERT INTO content_contact_links (tenant_id, content_id, contact_id, url, link_type_id, description, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                g.tenant_id, content_id, contact_id, url,
                request.form.get("link_type_id") or None,
                request.form.get("description", "").strip() or None,
                request.form.get("notes", "").strip() or None,
            ),
        )
        db.commit()
        log_action("Create", "content_contact_link", content_id, "Added contacts link")
        return redirect(url_for("documents.view_content", content_id=content_id))
    contacts = db.execute(
        "SELECT contact_id, full_name, file_as FROM contacts WHERE is_deleted = 0 AND tenant_id = ? ORDER BY file_as, full_name",
        (g.tenant_id,),
    ).fetchall()
    link_types = db.execute(
        "SELECT * FROM content_link_types WHERE is_active = 1 AND tenant_id = ? ORDER BY label COLLATE NOCASE", (g.tenant_id,)
    ).fetchall()
    return render_template("documents/content_link_form.html", item=item, contacts=contacts, link_types=link_types)


@documents_bp.route("/<int:content_id>/contact-links/<int:link_id>/delete", methods=["POST"])
@login_required
def delete_contact_link(content_id, link_id):
    db = get_db()
    db.execute(
        "DELETE FROM content_contact_links WHERE content_contact_link_id = ? AND content_id = ? AND tenant_id = ?",
        (link_id, content_id, g.tenant_id),
    )
    db.commit()
    log_action("Delete", "content_contact_link", content_id, f"Deleted contacts link #{link_id}")
    return redirect(url_for("documents.view_content", content_id=content_id))


@documents_bp.route("/<int:content_id>/locations/<int:location_id>/open")
@login_required
def open_location(content_id, location_id):
    db = get_db()
    loc = db.execute(
        "SELECT * FROM content_locations WHERE location_id = ? AND content_id = ? AND tenant_id = ?",
        (location_id, content_id, g.tenant_id),
    ).fetchone()
    if loc is None:
        return jsonify(ok=False, error="Location not found."), 404
    if loc["location_type"] != "Local Drive Path":
        return jsonify(ok=True)
    path = loc["path_or_url"]
    if not os.path.exists(path):
        return jsonify(ok=False, error=f"File not found on disk: {path}")
    try:
        open_local_path(path)
    except Exception as e:
        return jsonify(ok=False, error=f"Couldn't open the file: {e}")
    log_action("Open", "content_location", content_id, f"Opened {path}")
    return jsonify(ok=True)


@documents_bp.route("/<int:content_id>/locations/new", methods=["GET", "POST"])
@login_required
def new_location(content_id):
    db = get_db()
    item = db.execute(
        "SELECT * FROM content WHERE content_id = ? AND tenant_id = ?", (content_id, g.tenant_id)
    ).fetchone()
    if item is None:
        abort(404)
    if request.method == "POST":
        form = request.form
        db.execute(
            "INSERT INTO content_locations (tenant_id, content_id, location_type, path_or_url) VALUES (?, ?, ?, ?)",
            (g.tenant_id, content_id, form["location_type"], form["path_or_url"].strip()),
        )
        db.commit()
        log_action("Create", "content_location", content_id, "Added location")
        return redirect(url_for("documents.view_content", content_id=content_id))
    return render_template("documents/location_form.html", item=item)


@documents_bp.route("/<int:content_id>/locations/<int:location_id>/delete", methods=["POST"])
@login_required
def delete_location(content_id, location_id):
    db = get_db()
    db.execute(
        "DELETE FROM content_locations WHERE location_id = ? AND content_id = ? AND tenant_id = ?",
        (location_id, content_id, g.tenant_id),
    )
    db.commit()
    log_action("Delete", "content_location", content_id, f"Deleted location #{location_id}")
    return redirect(url_for("documents.view_content", content_id=content_id))
