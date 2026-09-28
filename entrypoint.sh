#!/bin/sh
# TMS container entrypoint.
#
# * Schema upgrades for an EXISTING database run automatically every time
#   the app starts (app.py -> db.run_pending_migrations()), so a redeploy
#   with new code upgrades the live database by itself.
#
# * Unlike GSS, a missing database is NOT silently replaced with a new empty
#   one. TMS goes live by UPLOADING the existing, populated tms.db (plus
#   tenant_master.key and secret_key) into the persistent volume. If the
#   container starts before that upload, it waits instead of creating an
#   empty tms.db where the real one belongs. Set TMS_ALLOW_INIT=true only if
#   you really want a brand-new empty system (init-db + seed-tenant).
#
# * Exactly ONE gunicorn worker, on purpose: each logged-in tenant's
#   decrypted field-encryption key is cached in this process's memory (see
#   security/session_keys.py), never on disk or in the cookie. A second
#   worker wouldn't have that cache and would bounce users back to /login.
#   Move the cache to a shared store before ever raising -w.
#
# * --timeout 300: AI agent runs make blocking Claude API calls inside a
#   single request; gunicorn's default 30s would kill them mid-run.
set -e

if [ ! -f "instance/tms.db" ]; then
    if [ "$TMS_ALLOW_INIT" = "true" ]; then
        echo "[entrypoint] No database found and TMS_ALLOW_INIT=true -- creating a NEW, EMPTY database..."
        flask --app app init-db
        flask --app app seed-tenant
        echo "[entrypoint] Done. Next: flask --app app create-system-admin"
    else
        echo "[entrypoint] instance/tms.db not found."
        echo "[entrypoint] Upload tms.db, tenant_master.key and secret_key into the persistent volume"
        echo "[entrypoint] (mounted at /app/instance), then restart. Waiting -- nothing has been created."
        while [ ! -f "instance/tms.db" ]; do sleep 30; done
        echo "[entrypoint] tms.db found -- starting."
    fi
fi

if [ ! -f "instance/tenant_master.key" ]; then
    echo "[entrypoint] WARNING: instance/tenant_master.key is missing. Encrypted fields in an uploaded"
    echo "[entrypoint] database cannot be read without the ORIGINAL key -- upload it before logging in."
fi

exec gunicorn -w 1 -b 0.0.0.0:5000 --timeout 300 --access-logfile - --error-logfile - app:app
