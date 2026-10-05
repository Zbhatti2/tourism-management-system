"""
The website's chat window (Zeb, Oct 2026: "a Chat Window to answer
questions ... an AI-Agent to answer all questions on the Tour, and
everything on the Website (and a human when requested by the client)").

* A visitor opens the chat on any page of the tenant's site. Their messages
  go to the AI agent, which answers only from what the site publishes: the
  tours (itinerary, dates, prices, hotels, inclusions, requirements), the
  policies (payments, deposits, deadlines, cancellation, refunds) and the
  contact details. It never invents prices or dates and never sees costs.
* "Talk to a person" -- or the agent noticing that the visitor wants one --
  hands the chat to the tenant's team: it waits in TMS (Website -> Website
  Chats), where staff reply; while a person handles it the AI stays quiet.
  Staff can hand it back to the AI or close it.
* Each AI answer is metered to the tenant's AI usage ("Website Chat"); over
  the tenant's monthly allowance, or with no API key, chats go to the team.
  A chat may send at most MAX_PER_HOUR messages an hour.
"""
import json
import secrets

import agent_runs
import ai_usage
import website

DDL = """
CREATE TABLE IF NOT EXISTS web_chats (
    chat_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    token           TEXT NOT NULL UNIQUE,       -- the visitor's key to their chat (kept in their browser)
    status          TEXT NOT NULL DEFAULT 'ai' CHECK (status IN ('ai','waiting','human','closed')),
    visitor_name    TEXT,
    visitor_email   TEXT,
    page            TEXT,                       -- where the chat started
    ip              TEXT,
    staff_user_id   INTEGER REFERENCES users(user_id),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    last_at         TEXT NOT NULL DEFAULT (datetime('now')),
    staff_seen_id   INTEGER NOT NULL DEFAULT 0  -- last message the team has seen
);
CREATE INDEX IF NOT EXISTS idx_web_chats_tenant ON web_chats(tenant_id, status, last_at);
CREATE TABLE IF NOT EXISTS web_chat_messages (
    message_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id         INTEGER NOT NULL REFERENCES web_chats(chat_id),
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    role            TEXT NOT NULL CHECK (role IN ('visitor','ai','staff','system')),
    text            TEXT NOT NULL,
    user_id         INTEGER REFERENCES users(user_id),
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_web_chat_messages_chat ON web_chat_messages(chat_id, message_id);
"""

MAX_PER_HOUR = 20
MAX_LEN = 1000
HISTORY = 14
HANDOFF = "[[HANDOFF]]"
STATUSES = {"ai": "AI agent", "waiting": "Waiting for a person", "human": "With a person", "closed": "Closed"}


class ChatError(Exception):
    pass


def migrate(db):
    db.executescript(DDL)
    db.commit()


# ---- seams (replaced in tests) ----------------------------------------------------------------

def anthropic_client():
    import image_collector
    return image_collector.anthropic_client()


def model():
    from config import Config
    return Config.ANTHROPIC_MODEL


# ---- chats -----------------------------------------------------------------------------------

def start(db, tenant_id, page=None, ip=None):
    token = secrets.token_urlsafe(24)
    cur = db.execute("INSERT INTO web_chats (tenant_id, token, page, ip) VALUES (?, ?, ?, ?)",
                     (tenant_id, token, (page or "")[:300] or None, ip))
    db.commit()
    return cur.lastrowid, token


def by_token(db, tenant_id, token):
    if not token:
        return None
    return db.execute("SELECT * FROM web_chats WHERE token = ? AND tenant_id = ?", (token, tenant_id)).fetchone()


def add(db, chat, role, text, user_id=None):
    cur = db.execute("INSERT INTO web_chat_messages (chat_id, tenant_id, role, text, user_id) VALUES (?, ?, ?, ?, ?)",
                     (chat["chat_id"], chat["tenant_id"], role, text, user_id))
    db.execute("UPDATE web_chats SET last_at = datetime('now') WHERE chat_id = ?", (chat["chat_id"],))
    db.commit()
    return cur.lastrowid


def messages(db, chat_id, after=0):
    return [dict(r) for r in db.execute(
        "SELECT message_id, role, text, created_at FROM web_chat_messages WHERE chat_id = ? AND message_id > ? "
        "ORDER BY message_id", (chat_id, after or 0))]


def set_status(db, chat, status, user_id=None, note=None):
    db.execute("UPDATE web_chats SET status = ?, staff_user_id = COALESCE(?, staff_user_id) WHERE chat_id = ?",
               (status, user_id, chat["chat_id"]))
    db.commit()
    if note:
        add(db, chat, "system", note)


def ask_for_person(db, chat, name=None, email=None):
    if name or email:
        db.execute("UPDATE web_chats SET visitor_name = COALESCE(?, visitor_name), visitor_email = COALESCE(?, visitor_email) "
                   "WHERE chat_id = ?", ((name or "").strip()[:120] or None, (email or "").strip()[:200] or None,
                                         chat["chat_id"]))
        db.commit()
    if chat["status"] in ("waiting", "human"):
        return
    set_status(db, chat, "waiting", note="A member of our team will join this chat shortly. If we're away, we'll "
                                         "reply by email" + (" to the address you gave." if email else
                                                             " — leave your email address here so we can."))


def visitor_says(db, tenant_id, chat, text):
    """Store the visitor's message and, while the AI handles the chat, its
    answer. Returns the new messages."""
    text = (text or "").strip()
    if not text:
        raise ChatError("Type a message first.")
    if chat["status"] == "closed":
        db.execute("UPDATE web_chats SET status = 'ai' WHERE chat_id = ?", (chat["chat_id"],))
        db.commit()
        chat = db.execute("SELECT * FROM web_chats WHERE chat_id = ?", (chat["chat_id"],)).fetchone()
    recent = db.execute("SELECT COUNT(*) FROM web_chat_messages WHERE chat_id = ? AND role = 'visitor' "
                        "AND created_at >= datetime('now', '-60 minutes')", (chat["chat_id"],)).fetchone()[0]
    if recent >= MAX_PER_HOUR:
        raise ChatError("You've sent a lot of messages in the last hour. Please use the enquiry form, or try again a little later.")
    first_id = add(db, chat, "visitor", text[:MAX_LEN])
    if chat["status"] == "ai":
        answer(db, tenant_id, chat)
    return messages(db, chat["chat_id"], first_id - 1)


def answer(db, tenant_id, chat):
    """The AI agent's reply to the conversation so far."""
    blocked = ai_usage.check_limit(db, tenant_id)
    try:
        client = None if blocked else anthropic_client()
    except Exception:
        client = None
    if client is None:
        ask_for_person(db, chat)
        return None
    history = list(reversed(db.execute(
        "SELECT role, text FROM web_chat_messages WHERE chat_id = ? AND role IN ('visitor','ai','staff') "
        "ORDER BY message_id DESC LIMIT ?", (chat["chat_id"], HISTORY)).fetchall()))
    msgs = []
    for r in history:
        role = "user" if r["role"] == "visitor" else "assistant"
        text = r["text"] if r["role"] != "staff" else f"(Our team member wrote:) {r['text']}"
        if msgs and msgs[-1]["role"] == role:
            msgs[-1]["content"] += "\n\n" + text
        else:
            msgs.append({"role": role, "content": text})
    if not msgs or msgs[0]["role"] != "user":
        msgs.insert(0, {"role": "user", "content": "(The visitor opened the chat.)"})
    try:
        resp = agent_runs.create_message(client, model=model(), max_tokens=900, system=knowledge(db, tenant_id),
                                         messages=msgs)
    except Exception as e:
        print(f"[website chat] AI answer failed: {e.__class__.__name__}: {e}")
        ask_for_person(db, chat)
        return None
    ai_usage.record(db, tenant_id, "Website Chat", model(), getattr(resp, "usage", None), ref_type="web_chats",
                    ref_id=chat["chat_id"])
    text = "".join(getattr(b, "text", "") for b in getattr(resp, "content", []) or [] if getattr(b, "type", "") == "text").strip()
    handoff = HANDOFF in text
    text = text.replace(HANDOFF, "").strip()
    mid = add(db, chat, "ai", text or "Let me pass you to a member of our team.") if (text or handoff) else None
    if handoff:
        ask_for_person(db, chat)
    return mid


def knowledge(db, tenant_id):
    """The agent's instructions and everything the site publishes, as text."""
    s = website.settings(db, tenant_id)
    lines = [
        f"You are the website assistant of {s['site_title']}, a tour operator. You chat with visitors on the company's "
        "website and answer their questions about the tours and everything on the website.",
        "Rules:",
        "- Answer ONLY from the information below. If something isn't there (a price, a date, a hotel, a visa rule), "
        "say you don't have that detail and offer to pass the question to the team. Never invent prices, dates, "
        "availability or policies.",
        "- Prices are in US dollars per person. A booking is a request until the team confirms it and the deposit is "
        "paid; you cannot take bookings or payments yourself. Point visitors to the tour page's Booking form "
        "(\"Book this Tour\") or the enquiry form (Contact Us).",
        "- Be warm, brief and practical: 1-4 short paragraphs or a short list. Use the visitor's language.",
        f"- If the visitor asks for a person, a call, or something only the team can do (a custom quote, a private "
        f"departure, a complaint, changes to a booking), reply in one or two sentences that a team member will join, "
        f"and end your reply with {HANDOFF}",
        "- Don't discuss other companies, and don't give medical, legal or visa advice beyond what is written below.",
        "",
        "== Company ==",
        f"Name: {s['site_title']}",
    ]
    for k, label in (("contact_email", "Email"), ("contact_phone", "Phone"), ("whatsapp", "WhatsApp"), ("address", "Address")):
        if s.get(k):
            lines.append(f"{label}: {s[k]}")
    if s.get("about_text"):
        lines.append(f"About: {s['about_text']}")
    lines += ["", "== Booking, payments and policies (unless a tour says otherwise) ==",
              f"Deposit: {s['deposit_policy']} (default {s['deposit_pct']}% of the tour price)",
              f"Payment terms: {s['payment_terms']}", f"Payment methods: {s['payment_methods']}",
              f"Booking deadline: bookings close {s['deadline_days']} days before departure unless a date says otherwise.",
              f"Booking terms: {s['booking_terms']}", f"Cancellation: {s['cancellation_policy']}",
              f"Refunds: {s['refund_policy']}"]
    groups = {g["id"]: g["caption"] for g in website.public_groups(db, tenant_id)}
    tours = website.public_tours(db, tenant_id)
    lines += ["", f"== Tours ({len(tours)}) =="]
    for card in tours[:25]:
        t = website.tour_detail(db, tenant_id, card["slug"])
        if t is None:
            continue
        lines += ["", f"--- {t['title']} ---", f"Group: {groups.get(t['package_group_id'], '-')}",
                  f"Page: /tours/{t['slug']}"]
        for k, label in (("tagline", "In short"), ("duration_days", "Days"), ("duration_nights", "Nights"),
                         ("difficulty", "Difficulty"), ("max_altitude_m", "Max altitude (m)"), ("lodging", "Lodging")):
            if t.get(k):
                lines.append(f"{label}: {t[k]}")
        if t.get("group_min") or t.get("group_max"):
            lines.append(f"Group size: {t.get('group_min') or 1}-{t.get('group_max') or ''}")
        if t.get("price_usd"):
            lines.append(f"Price from: US${float(t['price_usd']):,.0f} per person")
        if t.get("single_supplement_usd"):
            lines.append(f"Single room supplement: US${float(t['single_supplement_usd']):,.0f}")
        lines.append(f"Deposit: {t['deposit']}; bookings close {t['deadline_days']} days before departure")
        if t["all_departures"]:
            lines.append("Departures: " + "; ".join(
                f"{d['start_date']}" + (f" to {d['end_date']}" if d.get("end_date") else "") +
                (f", US${float(d['price']):,.0f}" if d.get("price") else "") + f", {d['label']}" +
                (f", {d['seats_left']} places left" if d.get("seats_left") is not None else "") +
                (f", book by {d['deadline']}" if d.get("deadline") else "") for d in t["all_departures"]))
        else:
            lines.append("Departures: none scheduled yet (dates on request)")
        if t.get("overview"):
            lines.append(f"Overview: {t['overview'][:1500]}")
        for k, label in (("highlight_list", "Highlights"), ("included_list", "Included"), ("excluded_list", "Not included"),
                         ("packing", "Packing list")):
            if t.get(k):
                lines.append(f"{label}: " + "; ".join((i["head"] + ": " if i["head"] else "") + i["text"] for i in t[k]))
        for k, label in (("who_for", "Who it's for"), ("requirements", "Requirements"),
                         ("payment_terms", "This tour's payment terms"), ("cancellation_policy", "This tour's cancellation")):
            if t.get(k):
                lines.append(f"{label}: {t[k]}")
        if t.get("route"):
            lines.append("Route: " + " -> ".join(f"{r['city'] or '?'}" + (f" ({r['nights']} nights)" if r["nights"] else "")
                                                 for r in t["route"]))
        for d in t.get("days") or []:
            bits = [f"Day {d['day_number']}" + (f": {d['title']}" if d.get("title") else "") + (f" ({d['city']})" if d.get("city") else "")]
            if d.get("description"):
                bits.append(d["description"][:400].replace("\n", " "))
            if d["pois"]:
                bits.append("Visits: " + ", ".join(p["name"] for p in d["pois"]))
            if d["stays"]:
                bits.append("Hotel: " + ", ".join(x["name"] + (f" ({x['grade']})" if x.get("grade") else "") for x in d["stays"]))
            if d["meals"]:
                bits.append("Restaurants: " + ", ".join(x["name"] for x in d["meals"]))
            lines.append(" | ".join(bits))
    return "\n".join(lines)[:60000]


# ---- the team's side ---------------------------------------------------------------------------

def chats(db, tenant_id, show="open"):
    sql = """SELECT c.*, (SELECT COUNT(*) FROM web_chat_messages m WHERE m.chat_id = c.chat_id) AS n,
                    (SELECT COUNT(*) FROM web_chat_messages m WHERE m.chat_id = c.chat_id AND m.role = 'visitor'
                     AND m.message_id > c.staff_seen_id) AS unseen,
                    (SELECT text FROM web_chat_messages m WHERE m.chat_id = c.chat_id AND m.role = 'visitor'
                     ORDER BY m.message_id DESC LIMIT 1) AS last_text,
                    u.display_name AS staff_name
             FROM web_chats c LEFT JOIN users u ON u.user_id = c.staff_user_id
             WHERE c.tenant_id = ? AND EXISTS (SELECT 1 FROM web_chat_messages m WHERE m.chat_id = c.chat_id AND m.role = 'visitor')"""
    if show == "open":
        sql += " AND c.status != 'closed'"
    elif show in STATUSES:
        sql += f" AND c.status = '{show}'"
    return db.execute(sql + " ORDER BY c.status = 'waiting' DESC, c.last_at DESC LIMIT 300", (tenant_id,)).fetchall()


def waiting_count(db, tenant_id):
    try:
        return db.execute("SELECT COUNT(*) FROM web_chats WHERE tenant_id = ? AND status = 'waiting'", (tenant_id,)).fetchone()[0]
    except Exception:
        return 0


def staff_reply(db, chat, text, user_id):
    text = (text or "").strip()
    if not text:
        raise ChatError("Type a reply first.")
    if chat["status"] in ("ai", "waiting", "closed"):
        set_status(db, chat, "human", user_id=user_id)
    return add(db, chat, "staff", text[:4000], user_id=user_id)


def mark_seen(db, chat_id):
    db.execute("UPDATE web_chats SET staff_seen_id = (SELECT COALESCE(MAX(message_id), 0) FROM web_chat_messages "
               "WHERE chat_id = ?) WHERE chat_id = ?", (chat_id, chat_id))
    db.commit()


def as_json(rows):
    return json.dumps(rows)
