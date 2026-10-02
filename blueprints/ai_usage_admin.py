"""
AI Usage screens (metering in ai_usage.py).

* /ai-usage/            Tenant Admin: this organisation's AI spend by month and
                        feature, the call log, and its monthly allowance.
* /platform/ai-usage/   SystemAdmin: every tenant's spend for a month, and each
                        tenant's monthly allowance (blank = no limit).
"""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

import ai_usage
from auth.decorators import system_admin_required, tenant_admin_required
from db import get_db, log_action

ai_usage_bp = Blueprint("ai_usage", __name__)
platform_ai_usage_bp = Blueprint("platform_ai_usage", __name__)


def _month(choices):
    m = request.args.get("month")
    return m if m in choices else choices[0]


@ai_usage_bp.route("/")
@tenant_admin_required
def tenant_usage():
    if g.get("tenant_id") is None:
        abort(404)
    db = get_db()
    months = ai_usage.months(db, g.tenant_id)
    month = _month(months)
    rows = ai_usage.summary(db, g.tenant_id, month)
    return render_template("ai_usage/tenant.html", months=months, month=month, rows=rows,
                           total=sum(r["cost_usd"] for r in rows), calls=ai_usage.recent(db, g.tenant_id, month),
                           limit=ai_usage.monthly_limit(db, g.tenant_id),
                           spent=ai_usage.month_spend(db, g.tenant_id))


@platform_ai_usage_bp.route("/")
@system_admin_required
def platform_usage():
    db = get_db()
    months = ai_usage.months(db, all_tenants=True)
    month = _month(months)
    used = {r["tenant_id"]: r for r in ai_usage.summary(db, month=month, all_tenants=True)}
    tenants = []
    for t in db.execute("SELECT tenant_id, tenant_name, account_number, ai_monthly_limit_usd FROM tenants "
                        "WHERE is_platform = 0 ORDER BY tenant_name COLLATE NOCASE").fetchall():
        tenants.append({"t": t, "u": used.get(t["tenant_id"])})
    return render_template("ai_usage/platform.html", months=months, month=month, tenants=tenants,
                           platform=used.get(None), total=sum(r["cost_usd"] for r in used.values()))


@platform_ai_usage_bp.route("/<int:tenant_id>/limit", methods=["POST"])
@system_admin_required
def set_limit(tenant_id):
    db = get_db()
    t = db.execute("SELECT tenant_name FROM tenants WHERE tenant_id = ? AND is_platform = 0", (tenant_id,)).fetchone()
    if t is None:
        abort(404)
    raw = (request.form.get("limit") or "").strip().replace("$", "").replace(",", "")
    try:
        value = round(float(raw), 2) if raw else None
        if value is not None and value < 0:
            raise ValueError
    except ValueError:
        flash("Enter the allowance in dollars, e.g. 25, or leave it blank for no limit.", "error")
        return redirect(url_for("platform_ai_usage.platform_usage", month=request.form.get("month")))
    db.execute("UPDATE tenants SET ai_monthly_limit_usd = ? WHERE tenant_id = ?", (value, tenant_id))
    db.commit()
    log_action("Update", "tenants", tenant_id, f"AI monthly allowance for {t['tenant_name']} set to "
               f"{'no limit' if value is None else f'${value:,.2f}'}", tenant_id=tenant_id)
    flash(f"{t['tenant_name']}: monthly AI allowance {'removed (no limit)' if value is None else f'set to ${value:,.2f}'}.", "success")
    return redirect(url_for("platform_ai_usage.platform_usage", month=request.form.get("month")))
