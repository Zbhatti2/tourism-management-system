"""
Tour Design tools for each user (Zeb, Oct 2026):

* To-Do List -- "an option for tracking a To-Do List for each user creating
  Tour Designs", after the User Notes of Zeb's Book to Movie app: a Subject
  and a Task, the date, and an Open / Closed toggle; filter by subject and
  status, sort by date, subject or status. Opened from the To-Do button
  beside Help on the Tour Design pages. A task added inside a design is
  linked to it.

* Prompt List -- "users must also have their Prompt List ... save their
  prompts, improve them and use them (either copy/paste or point the Agent
  to the Prompt) when starting a new Tour Design". Each user's own list
  with Group (the tenant's Package Groups), Title and Date last used. Open a
  prompt, change it, save it, or save it as a new prompt; delete the ones
  that aren't useful. "Use for a new Tour Design" starts a design with the
  prompt as its request (the Tour Planner agent reads it into the brief).
  New users start with Zeb's sample Tour Design prompt (prompt_seeds/).

Both belong to one user in one tenant; nobody else sees them.
"""
import os

DDL = """
CREATE TABLE IF NOT EXISTS tour_todos (
    todo_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    user_id         INTEGER NOT NULL REFERENCES users(user_id),
    plan_id         INTEGER REFERENCES tour_plans(plan_id),   -- the Tour Design it was added in, if any
    subject         TEXT NOT NULL,
    task            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed')),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    closed_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_tour_todos_user ON tour_todos(tenant_id, user_id, status);
CREATE TABLE IF NOT EXISTS agent_prompts (
    prompt_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    user_id         INTEGER NOT NULL REFERENCES users(user_id),
    agent           TEXT NOT NULL DEFAULT 'tour_design',      -- which AI agent the prompt is for
    package_group_id INTEGER REFERENCES package_groups(package_group_id),
    title           TEXT NOT NULL,
    body            TEXT NOT NULL,
    based_on_id     INTEGER REFERENCES agent_prompts(prompt_id),  -- saved as new from this one
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    last_used_at    TEXT,
    use_count       INTEGER NOT NULL DEFAULT 0,
    is_deleted      INTEGER NOT NULL DEFAULT 0                -- kept so a deleted seed isn't put back
);
CREATE INDEX IF NOT EXISTS idx_agent_prompts_user ON agent_prompts(tenant_id, user_id, is_deleted);
"""

AGENTS = {"tour_design": "Tour Design agent"}
SEED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompt_seeds", "tour_design_karakoram.txt")
SEED_TITLE = "Karakoram Tour: complete tour plan (sample)"
SEED_GROUP = "Northern Areas"


def migrate(db):
    db.executescript(DDL)
    db.commit()


# ---- To-Do List -------------------------------------------------------------------------------

TODO_SORTS = {"created_at": "t.created_at", "subject": "t.subject COLLATE NOCASE", "status": "t.status"}


def todos(db, tenant_id, user_id, subject=None, status=None, sort="created_at", direction="desc", plan_id=None):
    sql = """SELECT t.*, p.name AS plan_name FROM tour_todos t LEFT JOIN tour_plans p ON p.plan_id = t.plan_id
             WHERE t.tenant_id = ? AND t.user_id = ?"""
    args = [tenant_id, user_id]
    if subject:
        sql += " AND t.subject LIKE ?"
        args.append(f"%{subject}%")
    if status in ("open", "closed"):
        sql += " AND t.status = ?"
        args.append(status)
    if plan_id:
        sql += " AND t.plan_id = ?"
        args.append(plan_id)
    order = TODO_SORTS.get(sort, "t.created_at")
    sql += f" ORDER BY {order} {'ASC' if direction == 'asc' else 'DESC'}, t.todo_id DESC"
    return [dict(r) for r in db.execute(sql, args)]


def open_todo_count(db, tenant_id, user_id):
    try:
        return db.execute("SELECT COUNT(*) FROM tour_todos WHERE tenant_id = ? AND user_id = ? AND status = 'open'",
                          (tenant_id, user_id)).fetchone()[0]
    except Exception:
        return 0


def add_todo(db, tenant_id, user_id, subject, task, plan_id=None):
    subject, task = (subject or "").strip()[:120], (task or "").strip()[:1000]
    if not subject:
        raise ValueError("Subject is required.")
    if not task:
        raise ValueError("Task is required.")
    if plan_id and not db.execute("SELECT 1 FROM tour_plans WHERE plan_id = ? AND tenant_id = ?",
                                  (plan_id, tenant_id)).fetchone():
        plan_id = None
    cur = db.execute("INSERT INTO tour_todos (tenant_id, user_id, plan_id, subject, task) VALUES (?, ?, ?, ?, ?)",
                     (tenant_id, user_id, plan_id or None, subject, task))
    db.commit()
    return cur.lastrowid


def set_todo_status(db, tenant_id, user_id, todo_id, status):
    if status not in ("open", "closed"):
        raise ValueError("Status must be open or closed.")
    cur = db.execute("UPDATE tour_todos SET status = ?, closed_at = CASE WHEN ? = 'closed' THEN datetime('now') END "
                     "WHERE todo_id = ? AND tenant_id = ? AND user_id = ?", (status, status, todo_id, tenant_id, user_id))
    db.commit()
    if not cur.rowcount:
        raise ValueError("Task not found.")


def delete_todo(db, tenant_id, user_id, todo_id):
    db.execute("DELETE FROM tour_todos WHERE todo_id = ? AND tenant_id = ? AND user_id = ?", (todo_id, tenant_id, user_id))
    db.commit()


# ---- Prompt List ------------------------------------------------------------------------------

PROMPT_SORTS = {"last_used": "COALESCE(p.last_used_at, '') DESC, p.updated_at DESC",
                "title": "p.title COLLATE NOCASE ASC", "group": "COALESCE(pg.label, 'zzz') COLLATE NOCASE ASC, p.title COLLATE NOCASE",
                "updated": "p.updated_at DESC"}


def seed_prompts(db, tenant_id, user_id):
    """A user's first visit: their list starts with the sample Tour Design
    prompt. Never again once they have had any prompt (deleted ones count)."""
    if db.execute("SELECT 1 FROM agent_prompts WHERE tenant_id = ? AND user_id = ? LIMIT 1",
                  (tenant_id, user_id)).fetchone():
        return False
    try:
        body = open(SEED_FILE, encoding="utf-8").read().strip()
    except OSError:
        return False
    group = db.execute("SELECT package_group_id FROM package_groups WHERE tenant_id = ? AND label = ?",
                       (tenant_id, SEED_GROUP)).fetchone()
    db.execute("INSERT INTO agent_prompts (tenant_id, user_id, package_group_id, title, body) VALUES (?, ?, ?, ?, ?)",
               (tenant_id, user_id, group[0] if group else None, SEED_TITLE, body))
    db.commit()
    return True


def prompts(db, tenant_id, user_id, group=None, q=None, sort="last_used", agent=None):
    sql = """SELECT p.*, pg.label AS group_label FROM agent_prompts p
             LEFT JOIN package_groups pg ON pg.package_group_id = p.package_group_id
             WHERE p.tenant_id = ? AND p.user_id = ? AND p.is_deleted = 0"""
    args = [tenant_id, user_id]
    if group == "none":
        sql += " AND p.package_group_id IS NULL"
    elif group and str(group).isdigit():
        sql += " AND p.package_group_id = ?"
        args.append(int(group))
    if q:
        sql += " AND (p.title LIKE ? OR p.body LIKE ?)"
        args += [f"%{q}%", f"%{q}%"]
    if agent:
        sql += " AND p.agent = ?"
        args.append(agent)
    return db.execute(sql + " ORDER BY " + PROMPT_SORTS.get(sort, PROMPT_SORTS["last_used"]), args).fetchall()


def prompt(db, tenant_id, user_id, prompt_id):
    return db.execute("""SELECT p.*, pg.label AS group_label FROM agent_prompts p
                         LEFT JOIN package_groups pg ON pg.package_group_id = p.package_group_id
                         WHERE p.prompt_id = ? AND p.tenant_id = ? AND p.user_id = ? AND p.is_deleted = 0""",
                      (prompt_id, tenant_id, user_id)).fetchone()


def _clean(title, body):
    title, body = (title or "").strip()[:200], (body or "").strip()
    if not title:
        raise ValueError("Give the prompt a title.")
    if not body:
        raise ValueError("The prompt is empty.")
    return title, body[:50000]


def create_prompt(db, tenant_id, user_id, title, body, group_id=None, based_on=None, agent="tour_design"):
    title, body = _clean(title, body)
    cur = db.execute("INSERT INTO agent_prompts (tenant_id, user_id, agent, package_group_id, title, body, based_on_id) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (tenant_id, user_id, agent if agent in AGENTS else "tour_design", group_id, title, body, based_on))
    db.commit()
    return cur.lastrowid


def update_prompt(db, tenant_id, user_id, prompt_id, title, body, group_id=None):
    title, body = _clean(title, body)
    db.execute("UPDATE agent_prompts SET title = ?, body = ?, package_group_id = ?, updated_at = datetime('now') "
               "WHERE prompt_id = ? AND tenant_id = ? AND user_id = ? AND is_deleted = 0",
               (title, body, group_id, prompt_id, tenant_id, user_id))
    db.commit()


def delete_prompt(db, tenant_id, user_id, prompt_id):
    db.execute("UPDATE agent_prompts SET is_deleted = 1, updated_at = datetime('now') "
               "WHERE prompt_id = ? AND tenant_id = ? AND user_id = ?", (prompt_id, tenant_id, user_id))
    db.commit()


def mark_used(db, tenant_id, user_id, prompt_id):
    db.execute("UPDATE agent_prompts SET last_used_at = datetime('now'), use_count = use_count + 1 "
               "WHERE prompt_id = ? AND tenant_id = ? AND user_id = ? AND is_deleted = 0", (prompt_id, tenant_id, user_id))
    db.commit()


def untitled_copy_title(db, tenant_id, user_id, title):
    """'Karakoram Tour (2)' -- a free title for Save as new."""
    base = title.rsplit(" (", 1)[0] if title.endswith(")") and title.rsplit(" (", 1)[-1][:-1].isdigit() else title
    n = 2
    while db.execute("SELECT 1 FROM agent_prompts WHERE tenant_id = ? AND user_id = ? AND is_deleted = 0 AND title = ?",
                     (tenant_id, user_id, f"{base} ({n})")).fetchone():
        n += 1
    return f"{base} ({n})"
