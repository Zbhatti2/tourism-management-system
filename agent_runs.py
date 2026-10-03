"""
Progress, liveness and Stop for background agent runs -- shared by the AI
Image Collector (image_agent_runs) and the TMS Agents (platform_agent_runs).

Each run table carries:
* heartbeat_at      -- updated whenever the run writes progress; a run whose
                       heartbeat is older than STALE_MINUTES has stopped
                       responding (a hung request, or the server restarted)
                       and is marked failed;
* items_total / items_done -- for the progress bar (suppliers / POIs /
                       records researched);
* cancel_requested  -- set by Stop; the run's thread checks it between items.

Stop marks the run failed straight away (so a run that is hung inside one
request doesn't keep the page "working"); the thread, if it ever wakes up,
sees cancel_requested and ends without overwriting that.
"""
STALE_MINUTES = 10
COLUMNS = [("heartbeat_at", "TEXT"), ("items_total", "INTEGER"), ("items_done", "INTEGER NOT NULL DEFAULT 0"),
           ("cancel_requested", "INTEGER NOT NULL DEFAULT 0")]
TABLES = ("image_agent_runs", "platform_agent_runs")

# Limits that keep one hung call from stalling a run for ever.
API_TIMEOUT_SECONDS = 150
API_MAX_RETRIES = 1


def mark_stale(db, table, where="", params=()):
    db.execute(f"""UPDATE {table} SET status = 'failed', finished_at = datetime('now'),
                       error = 'Stopped responding: no activity for {STALE_MINUTES} minutes (a web page or the AI didn''t answer, or the server restarted). Anything already found is kept.'
                   WHERE status IN ('queued','running')
                     AND COALESCE(heartbeat_at, started_at, created_at) < datetime('now', '-{STALE_MINUTES} minutes'){where}""",
               params)
    db.commit()


def cancelled(db, table, run_id):
    r = db.execute(f"SELECT cancel_requested FROM {table} WHERE run_id = ?", (run_id,)).fetchone()
    return bool(r and r[0])


def stop(db, table, run_id, who=None):
    """Returns True if the run was still working."""
    n = db.execute(f"""UPDATE {table} SET cancel_requested = 1, status = 'failed', finished_at = datetime('now'),
                           error = ? WHERE run_id = ? AND status IN ('queued','running')""",
                   (f"Stopped{' by ' + who if who else ''}. Anything already found is kept.", run_id)).rowcount
    db.commit()
    return bool(n)


def status_json(db, table, run_id, extra_cols=()):
    cols = ["status", "progress", "error", "created_at", "started_at", "finished_at", "heartbeat_at", "items_total",
            "items_done"] + list(extra_cols)
    r = db.execute(f"SELECT {', '.join(cols)}, datetime('now') AS now FROM {table} WHERE run_id = ?", (run_id,)).fetchone()
    return dict(r) if r else None
