"""
AI usage metering -- every Claude API call TMS makes is recorded against the
tenant it ran for, so tenant-level agents can be billed (Zeb, Oct 2026:
"The Tenants will pay for these Agents"). Platform-level agents are logged
with tenant_id NULL (a platform cost).

record() is given the API response's `usage` block and works out the cost
from PRICES. A tenant can have a monthly spending limit
(tenants.ai_monthly_limit_usd, set by the SystemAdmin on the AI Usage
screen); check_limit() is called before a tenant's agent starts, so a run is
refused once the month's spend reaches it.

Prices are list prices in US dollars (Claude API pricing page, Oct 2026).
They are kept here, not hardcoded at each call, so a price change is one
edit. An unknown model is still logged (tokens and searches), with cost
left blank rather than guessed.
"""
from datetime import date

# model id prefix -> (input $/million tokens, output $/million tokens)
PRICES = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-haiku-3-5": (0.8, 4.0),
}
WEB_SEARCH_PER_REQUEST = 10.0 / 1000      # $10 per 1,000 searches; web fetch has no extra charge
CACHE_WRITE_FACTOR, CACHE_READ_FACTOR = 1.25, 0.1

DDL = """
CREATE TABLE IF NOT EXISTS ai_usage_log (
    usage_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER REFERENCES tenants(tenant_id),   -- NULL = platform-level agent (platform cost)
    user_id         INTEGER REFERENCES users(user_id),
    feature         TEXT NOT NULL,          -- e.g. 'Image Collector', 'Agent Run'
    ref_type        TEXT,                   -- what the call was for, e.g. 'image_agent_runs'
    ref_id          INTEGER,
    model           TEXT,
    input_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
    web_searches    INTEGER NOT NULL DEFAULT 0,
    web_fetches     INTEGER NOT NULL DEFAULT 0,
    cost_usd        REAL,                   -- NULL when the model's price isn't known
    note            TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ai_usage_log_tenant ON ai_usage_log(tenant_id, created_at);
"""


def _price(model):
    model = model or ""
    best = None
    for prefix, p in PRICES.items():
        if model.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, p)
    return best[1] if best else None


def _get(obj, name, default=0):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default) or default
    return getattr(obj, name, default) or default


def cost(model, input_tokens=0, output_tokens=0, cache_write=0, cache_read=0, searches=0):
    p = _price(model)
    search_cost = searches * WEB_SEARCH_PER_REQUEST
    if p is None:
        return None
    return round((input_tokens * p[0] + output_tokens * p[1] + cache_write * p[0] * CACHE_WRITE_FACTOR
                  + cache_read * p[0] * CACHE_READ_FACTOR) / 1_000_000 + search_cost, 6)


def record(db, tenant_id, feature, model, usage, user_id=None, ref_type=None, ref_id=None, note=None):
    """Log one API call from its response.usage. Never raises: metering must
    not break the work it measures."""
    try:
        inp, out = _get(usage, "input_tokens"), _get(usage, "output_tokens")
        cw, cr = _get(usage, "cache_creation_input_tokens"), _get(usage, "cache_read_input_tokens")
        stu = _get(usage, "server_tool_use", None)
        searches, fetches = _get(stu, "web_search_requests"), _get(stu, "web_fetch_requests")
        db.execute("""INSERT INTO ai_usage_log (tenant_id, user_id, feature, ref_type, ref_id, model, input_tokens,
                          output_tokens, cache_write_tokens, cache_read_tokens, web_searches, web_fetches, cost_usd, note)
                      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                   (tenant_id, user_id, feature, ref_type, ref_id, model, inp, out, cw, cr, searches, fetches,
                    cost(model, inp, out, cw, cr, searches), note))
        db.commit()
    except Exception:
        pass


def month_bounds(month=None):
    """('YYYY-MM-01', first day of next month) for 'YYYY-MM' (default: this month)."""
    y, m = (int(x) for x in (month or date.today().strftime("%Y-%m")).split("-"))
    nxt = (y + (m == 12), 1 if m == 12 else m + 1)
    return f"{y:04d}-{m:02d}-01", f"{nxt[0]:04d}-{nxt[1]:02d}-01"


def month_spend(db, tenant_id, month=None):
    start, end = month_bounds(month)
    return db.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM ai_usage_log WHERE tenant_id IS ? "
                      "AND created_at >= ? AND created_at < ?", (tenant_id, start, end)).fetchone()[0]


def monthly_limit(db, tenant_id):
    r = db.execute("SELECT ai_monthly_limit_usd FROM tenants WHERE tenant_id = ?", (tenant_id,)).fetchone()
    return r[0] if r else None


def check_limit(db, tenant_id):
    """None when the tenant may run an agent; otherwise a message saying why not."""
    limit = monthly_limit(db, tenant_id)
    if limit is None:
        return None
    spent = month_spend(db, tenant_id)
    if spent >= limit:
        return (f"This month's AI allowance (${limit:,.2f}) has been used (${spent:,.2f} so far). "
                "Ask the TMS administrator to raise it.")
    return None


def summary(db, tenant_id=None, month=None, all_tenants=False):
    """Totals for a month: per feature (one tenant) or per tenant (all)."""
    start, end = month_bounds(month)
    if all_tenants:
        return db.execute(
            """SELECT u.tenant_id, COALESCE(t.tenant_name, 'Platform') AS tenant_name, t.ai_monthly_limit_usd,
                      COUNT(*) AS calls, SUM(u.input_tokens) AS input_tokens, SUM(u.output_tokens) AS output_tokens,
                      SUM(u.web_searches) AS web_searches, COALESCE(SUM(u.cost_usd), 0) AS cost_usd
               FROM ai_usage_log u LEFT JOIN tenants t ON t.tenant_id = u.tenant_id
               WHERE u.created_at >= ? AND u.created_at < ?
               GROUP BY u.tenant_id ORDER BY cost_usd DESC""", (start, end)).fetchall()
    return db.execute(
        """SELECT feature, COUNT(*) AS calls, SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens,
                  SUM(web_searches) AS web_searches, COALESCE(SUM(cost_usd), 0) AS cost_usd
           FROM ai_usage_log WHERE tenant_id IS ? AND created_at >= ? AND created_at < ?
           GROUP BY feature ORDER BY cost_usd DESC""", (tenant_id, start, end)).fetchall()


def recent(db, tenant_id, month=None, limit=200):
    start, end = month_bounds(month)
    return db.execute(
        """SELECT u.*, us.display_name FROM ai_usage_log u LEFT JOIN users us ON us.user_id = u.user_id
           WHERE u.tenant_id IS ? AND u.created_at >= ? AND u.created_at < ?
           ORDER BY u.usage_id DESC LIMIT ?""", (tenant_id, start, end, limit)).fetchall()


def months(db, tenant_id=None, all_tenants=False):
    sql = "SELECT DISTINCT substr(created_at, 1, 7) FROM ai_usage_log"
    params = ()
    if not all_tenants:
        sql += " WHERE tenant_id IS ?"
        params = (tenant_id,)
    found = [r[0] for r in db.execute(sql + " ORDER BY 1 DESC", params)]
    this = date.today().strftime("%Y-%m")
    return ([this] if this not in found else []) + found
