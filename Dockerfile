# Tourism Management System (TMS) -- production image for Hostinger VPS2 /
# Coolify, same setup as the GSS app.
#
# Local use is UNCHANGED: Start_TMS.bat still runs the app directly with
# `flask run` on your PC. This file is only used by Coolify on the server.
FROM python:3.12-slim

WORKDIR /app

# Dependencies first (own layer), so a code-only change doesn't reinstall them.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code. .dockerignore keeps instance/, .env, venv/ and .git out.
COPY . .

RUN chmod +x entrypoint.sh

ENV FLASK_APP=app \
    PYTHONUNBUFFERED=1

# instance/ holds tms.db, tenant_master.key, secret_key, uploads/ and
# backups/. A Coolify PERSISTENT VOLUME must be mounted here -- without it,
# every redeploy would start from an empty folder and lose the database and
# the key that decrypts it.
VOLUME ["/app/instance"]

EXPOSE 5000

ENTRYPOINT ["./entrypoint.sh"]
