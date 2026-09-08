#!/usr/bin/env bash
# Daily backup of the SQLite DB + private uploads (photos, payment proofs,
# banners) to local rolling storage, then synced offsite with rclone.
#
# Setup (one-time):
#   1. sudo apt install rclone sqlite3
#   2. rclone config   # set up a remote named "offsite" (Backblaze B2,
#                       # Google Drive, or any rclone-supported target)
#   3. chmod +x backup.sh
#   4. crontab -e   and add:
#        30 2 * * * /opt/saif-matrimonial/deploy/backup.sh >> /var/log/saif-matrimonial/backup.log 2>&1
#
# Restore instructions are at the bottom of this file (as comments) and
# in README.md.

set -euo pipefail

APP_DIR="/opt/saif-matrimonial"
DB_PATH="${APP_DIR}/matrimonial.db"
STORAGE_DIR="${APP_DIR}/storage"
BACKUP_ROOT="/var/backups/saif-matrimonial"
RCLONE_REMOTE="offsite:saif-matrimonial-backups"   # rclone remote:path
KEEP_DAYS=7

TS="$(date +%Y-%m-%d_%H%M)"
DEST="${BACKUP_ROOT}/${TS}"
mkdir -p "${DEST}"

# 1. Consistent DB snapshot. Use sqlite3's own backup command (not `cp`)
#    so a concurrent WAL write can't produce a corrupt copy.
sqlite3 "${DB_PATH}" ".backup '${DEST}/matrimonial.db'"

# 2. Uploads folder — photos, payment proofs, banners.
tar -czf "${DEST}/storage.tar.gz" -C "${APP_DIR}" storage

# 3. Push offsite.
rclone copy "${DEST}" "${RCLONE_REMOTE}/${TS}" --fast-list

# 4. Prune local copies older than KEEP_DAYS (offsite retention is
#    whatever your rclone remote/bucket lifecycle policy is set to keep —
#    configure at least 7 days there too).
find "${BACKUP_ROOT}" -maxdepth 1 -type d -mtime +${KEEP_DAYS} -exec rm -rf {} \;

echo "[$(date)] Backup complete: ${DEST}"

# ---------------------------------------------------------------------
# RESTORE
# ---------------------------------------------------------------------
# 1. Stop the app first so nothing writes to the DB mid-restore:
#      sudo systemctl stop saif-matrimonial
#
# 2. Pick a backup (locally under /var/backups/saif-matrimonial/<timestamp>/,
#    or pull one down from offsite first):
#      rclone copy offsite:saif-matrimonial-backups/<timestamp> /tmp/restore
#
# 3. Restore the DB (back up the current one first, just in case):
#      cp /opt/saif-matrimonial/matrimonial.db /opt/saif-matrimonial/matrimonial.db.bak
#      cp /tmp/restore/matrimonial.db /opt/saif-matrimonial/matrimonial.db
#
# 4. Restore uploads:
#      tar -xzf /tmp/restore/storage.tar.gz -C /opt/saif-matrimonial
#
# 5. Restart:
#      sudo systemctl start saif-matrimonial
#
# 6. Sanity check /healthz and spot-check a couple of profiles/photos
#    before considering the restore done.
