"""
Lists remember how you left them (Zeb, Oct 2026: "This should be standard
throughout the System ... when a type is selected the list shows many
entries. So if the user does not like their pick they can return to the
same list to pick another").

Every list page in LIST_ENDPOINTS remembers its filters and search (its
query string, kept in the session per list URL). When you come back to
the list without them -- Back from a record, Cancel, or the redirect after
Save or Delete -- from a page inside that list (the list's own URL is a
prefix of the page you were on), you get the same filtered list again.

Starting fresh:
  - the list's own Clear button (a link from the list to itself without
    filters) forgets them;
  - coming from another module (the sidebar) shows the list unfiltered;
  - ?fresh=1 on any list URL forgets them.

No blueprint has to do anything: it's one before_request hook (init_app).
Suppliers and Points of Interest also pass their filters explicitly and
highlight the record just saved; this hook covers everything else.
"""
from urllib.parse import urlencode, urlparse

from flask import redirect, request, session

# Pages that show a filterable list of records.
LIST_ENDPOINTS = {
    "contacts.list_contacts", "organizations.list_organizations", "suppliers.list_suppliers", "poi.list_pois",
    "hr.list_employees", "products.list_products", "services.list_services", "packages.list_packages",
    "transport_hubs.list_hubs", "documents.index", "intelligence.index", "currency_admin.index",
    "ai_agents.list_review", "system_mgmt.audit_log", "geography_admin.manage", "platform_catalog.list_records",
    "users.list_users", "tenants_admin.list_tenants", "tour_planner.index", "suppliers.image_catalog",
}
IGNORE = {"hl", "fresh"}
KEEP_AT_MOST = 40


def _filters():
    return {k: [v for v in vs if v != ""] for k, vs in request.args.to_dict(flat=False).items()
            if k not in IGNORE and any(v != "" for v in vs)}


def _remember():
    if request.method != "GET" or request.endpoint not in LIST_ENDPOINTS:
        return None
    key = request.path
    saved = dict(session.get("list_filters") or {})
    filters = _filters()
    if filters:
        if saved.get(key) != filters:
            saved.pop(key, None)
            saved[key] = filters
            while len(saved) > KEEP_AT_MOST:
                saved.pop(next(iter(saved)))
            session["list_filters"] = saved
        return None
    if request.args.get("fresh"):
        if key in saved:
            saved.pop(key)
            session["list_filters"] = saved
        return None
    ref = urlparse(request.referrer or "")
    if ref.netloc and ref.netloc != request.host:
        return None
    if ref.path == key:  # Clear on the list itself
        if key in saved:
            saved.pop(key)
            session["list_filters"] = saved
        return None
    if key in saved and ref.path.startswith(key.rstrip("/") + "/"):
        query = dict(saved[key])
        if request.args.get("hl"):
            query["hl"] = [request.args["hl"]]
        return redirect(f"{key}?{urlencode(query, doseq=True)}")
    return None


def init_app(app):
    app.before_request(_remember)
