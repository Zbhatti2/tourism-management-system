#!/bin/sh
# TMS nightly backup -- runs on the VPS host from cron (/etc/cron.d/tms-backup).
#
# 1. Asks the running TMS container to make a consistent copy of the live
#    database (flask nightly-backup: SQLite online backup, integrity-checked,
#    keeps the newest 14, listed on System Management -> Backup & Restore).
# 2. Copies the backups AND the two key files to /root/tms-backups, OUTSIDE
#    the folder Coolify manages, so a mistake in Coolify's storage settings
#    can never take the backups with it. Copies there are kept 30 days.
#
# A backup is useless without tenant_master.key -- that's why it's copied too.
set -e
STAMP="$(date '+%Y-%m-%d %H:%M:%S')"
CONTAINER="$(docker ps -q --filter "name=bfovsdfg4pccbqzvijkcsi2y" | head -1)"
if [ -z "$CONTAINER" ]; then
    echo "$STAMP ERROR: TMS container is not running -- no backup made."
    exit 1
fi
docker exec "$CONTAINER" flask --app app nightly-backup --keep 14

mkdir -p /root/tms-backups
cp -u /data/tms/instance/backups/tms_nightly_*.db /root/tms-backups/
cp -u /data/tms/instance/tenant_master.key /data/tms/instance/secret_key /root/tms-backups/
find /root/tms-backups -name 'tms_nightly_*.db' -mtime +30 -delete
echo "$STAMP OK: $(ls /root/tms-backups/tms_nightly_*.db | wc -l) nightly copies in /root/tms-backups"
