"""
Package Groups (Zeb, Oct 2026): a tenant's own grouping of its tours --
"Gurdwaras Tour", "Northern Areas" -- a standard lookup kept in Table
Maintenance. Used on Package Management and on Tour Design (a design's
group goes with it into the Package made from it).
"""


def choices(db, tenant_id, current_id=None):
    """Active groups for a dropdown, plus the record's own even if since
    deactivated."""
    return db.execute("SELECT package_group_id, label, is_active FROM package_groups WHERE tenant_id = ? "
                      "AND (is_active = 1 OR package_group_id = ?) ORDER BY sort_order, label COLLATE NOCASE",
                      (tenant_id, current_id or 0)).fetchall()


def valid_id(db, tenant_id, raw):
    """A submitted group id, only if it is this tenant's; else None."""
    if raw is None or not str(raw).strip().isdigit():
        return None
    row = db.execute("SELECT package_group_id FROM package_groups WHERE package_group_id = ? AND tenant_id = ?",
                     (int(raw), tenant_id)).fetchone()
    return row[0] if row else None


def label(db, group_id):
    if not group_id:
        return None
    row = db.execute("SELECT label FROM package_groups WHERE package_group_id = ?", (group_id,)).fetchone()
    return row[0] if row else None
