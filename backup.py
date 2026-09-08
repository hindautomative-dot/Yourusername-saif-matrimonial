#!/usr/bin/env python3
"""
Saif Matrimonial — standalone backup script.

Complements the in-app "Admin -> Backup -> Download Full Backup Now"
button (app.py's /admin/backup/download route). Use this one if you have
shell/cron access to the server and want backups to happen automatically
on a schedule, without an admin needing to remember to click the button.

WHAT IT DOES
    - Takes a consistent, WAL-safe snapshot of matrimonial.db using
      SQLite's online backup API (same approach as the in-app route —
      safe to run while the live app is writing to the database).
    - Copies every private/uploaded storage folder alongside it.
    - Zips it all into DATA_DIR/backups/backup_YYYYMMDD_HHMMSS.zip.
    - Keeps a rolling window of the most recent N backups (default 14)
      and deletes older ones, so this never silently fills the disk —
      and never overwrites the *only* backup you have, since there's
      always at least one previous one still on disk while a new one
      is being written.

IMPORTANT — read this even if you only run it once:
    A backup sitting in DATA_DIR/backups/ on the SAME disk as the live
    database is only useful if that disk survives a redeploy/restart.
    Whether it does on your current hosting has not been confirmed
    (flagged since Part 1 of this project). Until it's confirmed safe,
    treat this script's output as step 1, not the whole plan — actually
    copy the resulting zip somewhere else (download it, rclone it to
    cloud storage, whatever you have) on whatever schedule you run this.

USAGE
    Manual:
        python3 backup.py

    Cron (daily at 3am, example):
        0 3 * * * cd /path/to/app && /usr/bin/python3 backup.py >> backups/backup.log 2>&1

    Custom retention (keep last 30 instead of default 14):
        python3 backup.py --keep 30

RESTORE
    See CHANGES_PART4.md / the in-app Admin -> Backup page for the full
    restore procedure. Short version: unzip, stop the app, replace
    matrimonial.db and the storage folders with the backed-up versions,
    restart, check /healthz.
"""
import os
import sys
import sqlite3
import zipfile
import argparse
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)
DB_PATH = os.path.join(DATA_DIR, "matrimonial.db")
BACKUP_DIR = os.path.join(DATA_DIR, "backups")

INCLUDE_DIRS = [
    ("profile_originals", os.path.join(DATA_DIR, "storage", "private", "profile_originals")),
    ("payment_proofs", os.path.join(DATA_DIR, "storage", "private", "payment_proofs")),
    ("help_shadi_docs", os.path.join(DATA_DIR, "storage", "private", "help_shadi_docs")),
    ("previews", os.path.join(BASE_DIR, "static", "previews")),
    ("branding", os.path.join(BASE_DIR, "static", "branding")),
    ("banners", os.path.join(BASE_DIR, "static", "banners")),
]


def make_backup():
    if not os.path.exists(DB_PATH):
        print(f"ERROR: database not found at {DB_PATH}", file=sys.stderr)
        sys.exit(1)

    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_path = os.path.join(BACKUP_DIR, f"backup_{stamp}.zip")
    tmp_db_path = os.path.join(BACKUP_DIR, f"_tmp_{stamp}.db")

    # Online, WAL-safe snapshot — takes a consistent copy even if the app
    # is mid-write, unlike `cp matrimonial.db backup.db` which could copy
    # a half-written page.
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(tmp_db_path)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(tmp_db_path, "matrimonial.db")
        for arc_prefix, dir_path in INCLUDE_DIRS:
            if not os.path.isdir(dir_path):
                continue
            for fname in os.listdir(dir_path):
                fpath = os.path.join(dir_path, fname)
                if os.path.isfile(fpath):
                    zf.write(fpath, os.path.join(arc_prefix, fname))

    os.remove(tmp_db_path)
    size_mb = round(os.path.getsize(zip_path) / (1024 * 1024), 2)
    print(f"Backup written: {zip_path} ({size_mb} MB)")
    return zip_path


def prune_old_backups(keep):
    files = sorted(
        (f for f in os.listdir(BACKUP_DIR) if f.startswith("backup_") and f.endswith(".zip")),
        reverse=True,
    )
    for old in files[keep:]:
        path = os.path.join(BACKUP_DIR, old)
        os.remove(path)
        print(f"Pruned old backup: {old}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Back up the Saif Matrimonial database + private uploads.")
    parser.add_argument("--keep", type=int, default=14, help="How many recent backups to keep (default: 14)")
    args = parser.parse_args()

    make_backup()
    prune_old_backups(args.keep)
