"""Backups and the nightly job (database copy + trimming old IRC logs)."""
import glob
import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime

import config
import db as store

BACKUP_RE = re.compile(r"^chat-\d{8}-\d{6}\.db$")
_lock = threading.Lock()


def make_backup():
    """Copy the live database with SQLite's online backup (safe while people are chatting). Returns the file name."""
    os.makedirs(config.BACKUP_DIR, exist_ok=True)
    name = datetime.now().strftime("chat-%Y%m%d-%H%M%S.db")
    path = os.path.join(config.BACKUP_DIR, name)
    with _lock:
        src = sqlite3.connect(config.DB_PATH, timeout=30)
        dst = sqlite3.connect(path)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        prune_backups()
    return name


def list_backups():
    out = []
    for p in sorted(glob.glob(os.path.join(config.BACKUP_DIR, "chat-*.db")), reverse=True):
        n = os.path.basename(p)
        if BACKUP_RE.match(n):
            st = os.stat(p)
            out.append({"name": n, "size": st.st_size, "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")})
    return out


def prune_backups():
    for b in list_backups()[config.BACKUP_KEEP:]:
        try:
            os.remove(os.path.join(config.BACKUP_DIR, b["name"]))
        except OSError:
            pass


def backup_path(name):
    return os.path.join(config.BACKUP_DIR, name) if BACKUP_RE.match(name or "") else None


# ------------------------------------------------------------------ nightly job
def nightly():
    """Backup + trimming of old IRC logs. Returns a short summary."""
    summary = {}
    if config.BACKUPS_ENABLED:
        summary["backup"] = make_backup()
    conn = store.connect()
    try:
        summary["log_lines_removed"] = store.prune_irc_logs(conn)
        store.set_meta(conn, "last_nightly", datetime.now().strftime("%Y-%m-%d"))
    finally:
        conn.close()
    return summary


def _loop(notify):
    while True:
        try:
            now = datetime.now()
            conn = store.connect()
            try:
                done = store.get_meta(conn, "last_nightly")
            finally:
                conn.close()
            if now.hour >= config.BACKUP_HOUR and done != now.strftime("%Y-%m-%d"):
                s = nightly()
                print(f"[nightly] {s}")
                notify()
        except Exception as e:                       # the job must never take the site down
            print(f"[nightly] failed: {e}")
        time.sleep(300)


def start_background(notify=lambda: None):
    t = threading.Thread(target=_loop, args=(notify,), daemon=True, name="nightly")
    t.start()
    return t
