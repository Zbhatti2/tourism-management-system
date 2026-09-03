"""
Module G — Organization Intelligence.

A running, append-only journal of freeform operational knowledge that
staff of the orchestrating organization (this tenant) enter over time —
things picked up in the field that don't belong on any one Organization/
Supplier/POI record: two spellings of the same place meaning the same
thing, a hotel's pet or childcare policy, a route's drive time in bad
weather, and so on.

Unlike Contacts, there is no history/versioning table here. Superseding
outdated knowledge isn't done by editing or deleting a prior entry — it's
done by simply adding a newer-dated one about the same topic. entry_date
and entered_by_name together establish an entry's currency, and the list
below sorts newest entry_date first (ties broken by whichever was entered
last), so the most current information on a topic naturally surfaces at
the top without any entry needing to reference or replace another.
"""
from datetime import date

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from auth.decorators import login_required
from db import get_db, log_action

intelligence_bp = Blueprint("intelligence", __name__)


def _get_entry(db, entry_id):
    entry = db.execute(
        "SELECT * FROM organization_intelligence WHERE intelligence_id = ? AND tenant_id = ?",
        (entry_id, g.tenant_id),
    ).fetchone()
    if entry is None:
        abort(404)
    return entry


@intelligence_bp.route("/")
@login_required
def index():
    db = get_db()
    q = request.args.get("q", "").strip()
    sql = "SELECT * FROM organization_intelligence WHERE tenant_id = ?"
    params = [g.tenant_id]
    if q:
        sql += " AND (note LIKE ? OR entered_by_name LIKE ?)"
        like = f"%{q}%"
        params += [like, like]
    # Newest entry_date first (currency), ties broken by whichever was
    # entered last -- see the module docstring on why there's no other
    # notion of "supersedes" here.
    sql += " ORDER BY entry_date DESC, intelligence_id DESC"
    entries = db.execute(sql, params).fetchall()
    return render_template("intelligence/index.html", entries=entries, q=q, today=date.today().isoformat())


@intelligence_bp.route("/add", methods=["POST"])
@login_required
def add_entry():
    db = get_db()
    form = request.form
    note = form.get("note", "").strip()
    entry_date = form.get("entry_date", "").strip()
    q = form.get("q", "").strip()

    if not note:
        flash("Enter some text for this intelligence entry.", "error")
    elif not entry_date:
        flash("Choose a date for this entry.", "error")
    else:
        db.execute(
            """INSERT INTO organization_intelligence
               (tenant_id, entry_date, entered_by_user_id, entered_by_name, note)
               VALUES (?, ?, ?, ?, ?)""",
            (g.tenant_id, entry_date, g.user_id, g.display_name, note),
        )
        db.commit()
        entry_id = db.execute("SELECT last_insert_rowid() id").fetchone()["id"]
        log_action("Create", "organization_intelligence", entry_id, f"Added intelligence entry dated {entry_date}")
        flash("Intelligence entry added.", "success")

    return redirect(url_for("intelligence.index", q=q) if q else url_for("intelligence.index"))


@intelligence_bp.route("/<int:entry_id>/edit", methods=["GET", "POST"])
@login_required
def edit_entry(entry_id):
    db = get_db()
    entry = _get_entry(db, entry_id)
    if request.method == "POST":
        form = request.form
        note = form.get("note", "").strip()
        entry_date = form.get("entry_date", "").strip()
        if not note:
            flash("Enter some text for this intelligence entry.", "error")
            return render_template("intelligence/form.html", entry=entry)
        if not entry_date:
            flash("Choose a date for this entry.", "error")
            return render_template("intelligence/form.html", entry=entry)
        db.execute(
            "UPDATE organization_intelligence SET note = ?, entry_date = ?, updated_at = datetime('now') "
            "WHERE intelligence_id = ? AND tenant_id = ?",
            (note, entry_date, entry_id, g.tenant_id),
        )
        db.commit()
        log_action("Update", "organization_intelligence", entry_id, "Edited intelligence entry")
        flash("Intelligence entry updated.", "success")
        return redirect(url_for("intelligence.index"))
    return render_template("intelligence/form.html", entry=entry)


@intelligence_bp.route("/<int:entry_id>/delete", methods=["POST"])
@login_required
def delete_entry(entry_id):
    db = get_db()
    _get_entry(db, entry_id)
    db.execute(
        "DELETE FROM organization_intelligence WHERE intelligence_id = ? AND tenant_id = ?",
        (entry_id, g.tenant_id),
    )
    db.commit()
    log_action("Delete", "organization_intelligence", entry_id, "Deleted intelligence entry")
    flash("Intelligence entry deleted.", "success")
    return redirect(url_for("intelligence.index"))
