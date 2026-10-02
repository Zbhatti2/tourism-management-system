"""
Module — Currency & Exchange Rates (Sept 2026). GLOBAL, TenantAdmin-level
maintenance screen — same access level as Geography Maintenance — for the
`currencies`, `exchange_rates` and `exchange_rate_history` tables. See
schema.sql's "MODULE E -- Currencies & Exchange Rates" comment for the
full design story (Zeb: "I need a currency rates and exchange rate table
with US dollar as the base rate").

"Foundation only" for this phase (Zeb's own scoping decision): this screen
lets a TenantAdmin/SystemAdmin maintain the currency list and manually
enter/track exchange rates. It deliberately does NOT yet wire dual-currency
display into the Package/Tour costing screens — that's a follow-on phase.

Two things worth calling out that differ from geography_admin.py, the
closest sibling screen:

1. `currencies.code` (e.g. 'USD') is the join key other tables use
   (countries.currency_code, tenants.host_currency_code,
   exchange_rates.currency_code) — unlike geography_admin's lookup tables,
   which are always joined by surrogate integer id and so can have their
   `code`/`label` freely relabeled. Renaming a currency's code here would
   silently orphan every FK pointing at the old value (SQLite has no
   cascade-rename), so the edit form leaves `code` read-only after
   creation; only label/symbol/sort_order/is_active can change.

2. There's no delete route. A currency in use (by a country, a tenant's
   Host Currency, or an exchange rate history entry) is exactly the kind
   of thing that would need the same usage-check/reassign machinery
   geography_admin.py has — out of scope for "foundation only". Deactivate
   (is_active = 0, same as everywhere else) is enough for now; it hides
   the currency from pickers without breaking any existing reference.

Manual rate entry (new_rate) always does two things atomically: upserts
the current-value cache row in `exchange_rates`, and appends a row to
`exchange_rate_history` — the identical "current cache + append-only
history" split `supplier_rooms`/`supplier_room_price_history` already
uses ("Zeb: Pricing will change over time so I need to track history").
USD itself is exempt — it's the fixed 1.0 base rate seeded once by
seed_data.seed_global_lookups(), never manually re-entered.

Since Oct 2026 this screen is SystemAdmin-only ("Currencies" in the
Platform Admin sidebar): currencies and exchange rates are shared platform
data (Group A of the platform master-data plan). Tenants still choose
their own Host Currency on System Management.
"""
import sqlite3
from datetime import date

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from auth.decorators import system_admin_required
from db import get_db, log_action

currency_admin_bp = Blueprint("currency_admin", __name__)


def _safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _get_currency(db, code):
    row = db.execute("SELECT * FROM currencies WHERE code = ?", (code,)).fetchone()
    if row is None:
        abort(404)
    return row


@currency_admin_bp.route("/")
@system_admin_required
def index():
    db = get_db()
    q = request.args.get("q", "").strip()
    where = ""
    params = []
    if q:
        where = " WHERE c.label LIKE ? OR c.code LIKE ?"
        params = [f"%{q}%", f"%{q}%"]
    rows = db.execute(
        "SELECT c.*, r.rate_to_usd, r.rate_as_of "
        "FROM currencies c LEFT JOIN exchange_rates r ON r.currency_code = c.code"
        f"{where} ORDER BY c.sort_order, c.label COLLATE NOCASE",
        params,
    ).fetchall()
    countries_in_use = {
        row["currency_code"]
        for row in db.execute("SELECT DISTINCT currency_code FROM countries WHERE currency_code IS NOT NULL").fetchall()
    }
    return render_template("currency_admin/index.html", currencies=rows, q=q, countries_in_use=countries_in_use)


@currency_admin_bp.route("/new", methods=["GET", "POST"])
@system_admin_required
def new_currency():
    db = get_db()
    if request.method == "POST":
        form = request.form
        code = form.get("code", "").strip().upper()
        label = form.get("label", "").strip()
        symbol = form.get("symbol", "").strip() or None
        sort_order = _safe_int(form.get("sort_order"), 0)
        is_active = 1 if form.get("is_active") else 0
        if not code or len(code) != 3 or not code.isalpha():
            flash("Currency code is required and must be exactly 3 letters (ISO 4217, e.g. 'USD').", "error")
            return render_template("currency_admin/form.html", currency=None)
        if not label:
            flash("Label is required.", "error")
            return render_template("currency_admin/form.html", currency=None)
        try:
            db.execute(
                "INSERT INTO currencies (code, label, symbol, sort_order, is_active) VALUES (?, ?, ?, ?, ?)",
                (code, label, symbol, sort_order, is_active),
            )
            db.commit()
        except sqlite3.IntegrityError:
            flash(f"Currency code '{code}' already exists.", "error")
            return render_template("currency_admin/form.html", currency=None)
        log_action("Create", "currencies", None, f"Added currency '{code}' ({label}) (global table)")
        flash(f"'{code} — {label}' added.", "success")
        return redirect(url_for("currency_admin.index"))
    return render_template("currency_admin/form.html", currency=None)


@currency_admin_bp.route("/<code>/edit", methods=["GET", "POST"])
@system_admin_required
def edit_currency(code):
    db = get_db()
    currency = _get_currency(db, code)
    if request.method == "POST":
        form = request.form
        label = form.get("label", "").strip()
        symbol = form.get("symbol", "").strip() or None
        sort_order = _safe_int(form.get("sort_order"), 0)
        is_active = 1 if form.get("is_active") else 0
        if not label:
            flash("Label is required.", "error")
            return render_template("currency_admin/form.html", currency=currency)
        db.execute(
            "UPDATE currencies SET label = ?, symbol = ?, sort_order = ?, is_active = ? WHERE code = ?",
            (label, symbol, sort_order, is_active, code),
        )
        db.commit()
        log_action("Update", "currencies", None, f"Updated currency '{code}' -> '{label}' (global table)")
        flash(f"'{code} — {label}' updated.", "success")
        return redirect(url_for("currency_admin.index"))
    return render_template("currency_admin/form.html", currency=currency)


@currency_admin_bp.route("/<code>/toggle-active", methods=["POST"])
@system_admin_required
def toggle_active(code):
    db = get_db()
    currency = _get_currency(db, code)
    new_state = 0 if currency["is_active"] else 1
    db.execute("UPDATE currencies SET is_active = ? WHERE code = ?", (new_state, code))
    db.commit()
    log_action("Update", "currencies", None, f"{'Activated' if new_state else 'Deactivated'} currency '{code}' (global table)")
    flash(f"'{code}' {'activated' if new_state else 'deactivated'}.", "success")
    return redirect(url_for("currency_admin.index"))


@currency_admin_bp.route("/<code>/rate", methods=["GET", "POST"])
@system_admin_required
def new_rate(code):
    db = get_db()
    currency = _get_currency(db, code)
    if code == "USD":
        flash("USD is the fixed base currency (1.0) and can't be re-entered.", "error")
        return redirect(url_for("currency_admin.index"))
    current = db.execute("SELECT * FROM exchange_rates WHERE currency_code = ?", (code,)).fetchone()
    if request.method == "POST":
        form = request.form
        rate_to_usd = _safe_float(form.get("rate_to_usd"))
        rate_as_of = form.get("rate_as_of", "").strip() or date.today().isoformat()
        if rate_to_usd is None:
            flash("Rate must be a positive number (units of this currency per 1 USD).", "error")
            return render_template("currency_admin/rate_form.html", currency=currency, current=current)
        db.execute(
            "INSERT INTO exchange_rate_history (currency_code, rate_to_usd, rate_as_of) VALUES (?, ?, ?)",
            (code, rate_to_usd, rate_as_of),
        )
        if current is None:
            db.execute(
                "INSERT INTO exchange_rates (currency_code, rate_to_usd, rate_as_of) VALUES (?, ?, ?)",
                (code, rate_to_usd, rate_as_of),
            )
        else:
            db.execute(
                "UPDATE exchange_rates SET rate_to_usd = ?, rate_as_of = ?, updated_at = datetime('now') WHERE currency_code = ?",
                (rate_to_usd, rate_as_of, code),
            )
        db.commit()
        log_action("Update", "exchange_rates", None, f"Entered {code} exchange rate: {rate_to_usd} per 1 USD, as of {rate_as_of}")
        flash(f"{code} rate updated to {rate_to_usd} per 1 USD (as of {rate_as_of}).", "success")
        return redirect(url_for("currency_admin.index"))
    return render_template(
        "currency_admin/rate_form.html", currency=currency, current=current,
        today=date.today().isoformat(),
    )


@currency_admin_bp.route("/<code>/history")
@system_admin_required
def rate_history(code):
    db = get_db()
    currency = _get_currency(db, code)
    history = db.execute(
        "SELECT * FROM exchange_rate_history WHERE currency_code = ? ORDER BY rate_as_of DESC, exchange_rate_history_id DESC",
        (code,),
    ).fetchall()
    return render_template("currency_admin/history.html", currency=currency, history=history)
