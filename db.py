"""SQLite storage: schema, one-time migration from the old five-identity version, message helpers."""
import json
import os
import re
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone

import config
import irc_proto as P
import themes

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL UNIQUE COLLATE NOCASE,
  display_name TEXT NOT NULL UNIQUE COLLATE NOCASE,
  password_hash TEXT,                 -- NULL = nobody can log in as this user (archived / legacy)
  is_admin INTEGER NOT NULL DEFAULT 0,
  is_disabled INTEGER NOT NULL DEFAULT 0,
  disabled_message TEXT NOT NULL DEFAULT '',
  sv INTEGER NOT NULL DEFAULT 1,      -- session version: bump to log the user out everywhere
  created_at TEXT NOT NULL,
  last_seen TEXT,
  name_color TEXT NOT NULL DEFAULT '',
  status_text TEXT NOT NULL DEFAULT '',
  theme TEXT NOT NULL DEFAULT 'classic',
  gimmicks TEXT NOT NULL DEFAULT '[]',
  theme_vars TEXT NOT NULL DEFAULT '{}',
  labels TEXT NOT NULL DEFAULT '{}',
  custom_css TEXT NOT NULL DEFAULT '',
  custom_js TEXT NOT NULL DEFAULT '',
  custom_intro TEXT NOT NULL DEFAULT '',
  use_custom_css INTEGER NOT NULL DEFAULT 1,
  use_custom_js INTEGER NOT NULL DEFAULT 1,
  intro_mode TEXT NOT NULL DEFAULT 'once',
  show_name_colors INTEGER NOT NULL DEFAULT 1,
  bio TEXT NOT NULL DEFAULT '',
  msg_effect TEXT NOT NULL DEFAULT 'none',   -- plays on this user's own messages for everyone (opt-in)
  show_effects INTEGER NOT NULL DEFAULT 1,   -- does this user want to SEE other people's effects?
  schedule TEXT NOT NULL DEFAULT '[]',       -- [{preset, from, to}] scheduled look changes
  follow_events INTEGER NOT NULL DEFAULT 1,  -- let room-wide event themes override my look
  slow_mode_s INTEGER NOT NULL DEFAULT 0,    -- admin-set: minimum seconds between this user's messages
  muted_until TEXT NOT NULL DEFAULT ''       -- admin-set temporary mute (ISO time)
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER,                    -- NULL for system messages and deleted users
  author_snapshot TEXT NOT NULL DEFAULT '',   -- name kept if the user is later deleted
  kind TEXT NOT NULL DEFAULT 'user',          -- 'user' | 'system'
  message TEXT NOT NULL,
  timestamp TEXT NOT NULL,
  edited_at TEXT,
  deleted INTEGER NOT NULL DEFAULT 0,
  rev INTEGER NOT NULL DEFAULT 0,     -- bumped on every insert/edit/delete/reaction; clients sync on it
  room_id INTEGER NOT NULL DEFAULT 1,
  reply_to INTEGER,
  image TEXT NOT NULL DEFAULT '',
  image_expired INTEGER NOT NULL DEFAULT 0,
  mentions TEXT NOT NULL DEFAULT '[]',        -- JSON list of user ids named with @username
  client_id TEXT                              -- lets an offline-queued message be retried without duplicating
);
CREATE INDEX IF NOT EXISTS idx_messages_rev ON messages(rev);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS rooms (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'channel',       -- 'channel' | 'dm'
  admin_only INTEGER NOT NULL DEFAULT 0,
  dm_a INTEGER, dm_b INTEGER,                 -- the two people in a direct message
  position INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reactions (
  message_id INTEGER NOT NULL, user_id INTEGER NOT NULL, emoji TEXT NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY (message_id, user_id, emoji)
);
CREATE TABLE IF NOT EXISTS blocks (
  user_id INTEGER NOT NULL, target_id INTEGER NOT NULL, kind TEXT NOT NULL,   -- 'mute' | 'block'
  PRIMARY KEY (user_id, target_id)
);
CREATE TABLE IF NOT EXISTS reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  message_id INTEGER NOT NULL, reporter_id INTEGER, reason TEXT NOT NULL DEFAULT '',
  author_id INTEGER, author_name TEXT NOT NULL DEFAULT '', text_snapshot TEXT NOT NULL DEFAULT '',
  room_name TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open', resolved_by TEXT NOT NULL DEFAULT '', resolved_at TEXT, note TEXT NOT NULL DEFAULT '',
  UNIQUE (message_id, reporter_id)
);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL, actor_id INTEGER, actor_name TEXT NOT NULL DEFAULT '',
  action TEXT NOT NULL, target TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS shared_themes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  author_id INTEGER, author_name TEXT NOT NULL DEFAULT '', title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
  payload TEXT NOT NULL, has_js INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
);
-- one IRC login per local account; the password is stored encrypted (crypto_box.py)
CREATE TABLE IF NOT EXISTS irc_accounts (
  user_id INTEGER PRIMARY KEY,
  host TEXT NOT NULL, port INTEGER NOT NULL DEFAULT 6697, tls INTEGER NOT NULL DEFAULT 1, tls_verify INTEGER NOT NULL DEFAULT 1,
  nick TEXT NOT NULL, account TEXT NOT NULL DEFAULT '',          -- account = SASL login name (empty = same as nick)
  password_enc TEXT NOT NULL DEFAULT '', use_sasl INTEGER NOT NULL DEFAULT 1,
  realname TEXT NOT NULL DEFAULT '', autoconnect INTEGER NOT NULL DEFAULT 1,
  autojoin TEXT NOT NULL DEFAULT '[]',                           -- channels to join on connect (kept up to date by /join and /part)
  auth_failed INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS irc_reactions (
  message_id INTEGER NOT NULL, nick TEXT NOT NULL COLLATE NOCASE, emoji TEXT NOT NULL,
  PRIMARY KEY (message_id, nick, emoji)
);
CREATE TABLE IF NOT EXISTS irc_ignores (                          -- mute / block: only ever changes what this person sees
  user_id INTEGER NOT NULL, nick TEXT NOT NULL COLLATE NOCASE, kind TEXT NOT NULL,
  PRIMARY KEY (user_id, nick)
);
"""

# columns added after the first release: (table, column, definition). Old databases get them with ALTER TABLE.
UPGRADES = [
    ("users", "bio", "TEXT NOT NULL DEFAULT ''"), ("users", "msg_effect", "TEXT NOT NULL DEFAULT 'none'"),
    ("users", "show_effects", "INTEGER NOT NULL DEFAULT 1"), ("users", "schedule", "TEXT NOT NULL DEFAULT '[]'"),
    ("users", "follow_events", "INTEGER NOT NULL DEFAULT 1"), ("users", "slow_mode_s", "INTEGER NOT NULL DEFAULT 0"),
    ("users", "muted_until", "TEXT NOT NULL DEFAULT ''"),
    ("messages", "room_id", "INTEGER NOT NULL DEFAULT 1"), ("messages", "reply_to", "INTEGER"),
    ("messages", "image", "TEXT NOT NULL DEFAULT ''"), ("messages", "image_expired", "INTEGER NOT NULL DEFAULT 0"),
    ("messages", "mentions", "TEXT NOT NULL DEFAULT '[]'"), ("messages", "client_id", "TEXT"),
    # IRC: every buffer (channel, private chat, server window) belongs to one local account
    ("rooms", "owner_id", "INTEGER"), ("rooms", "key", "TEXT NOT NULL DEFAULT ''"),
    ("rooms", "topic", "TEXT NOT NULL DEFAULT ''"), ("rooms", "joined", "INTEGER NOT NULL DEFAULT 1"),
    ("messages", "sender", "TEXT NOT NULL DEFAULT ''"), ("messages", "account", "TEXT NOT NULL DEFAULT ''"),
    ("messages", "msgid", "TEXT NOT NULL DEFAULT ''"), ("messages", "own", "INTEGER NOT NULL DEFAULT 0"),
    ("messages", "highlight", "INTEGER NOT NULL DEFAULT 0"),
]
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_messages_room ON messages(room_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_client ON messages(user_id, client_id) WHERE client_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_reactions_msg ON reactions(message_id);
CREATE INDEX IF NOT EXISTS idx_audit_id ON audit(id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_rooms_owner_key ON rooms(owner_id, key) WHERE owner_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_messages_msgid ON messages(room_id, msgid) WHERE msgid <> '';
"""
META_DEFAULTS = {"registration_open": "1", "invite_code": "", "room_name": "IRC Room", "motd": ""}

# The old hard-coded identities, used only to migrate an existing chat.db.
LEGACY = {
    "sophia":   ("GABBY", "gabby", "#ff7ab8", ["status-chips"]),
    "dream":    ("THE WATCHER", "dream", "#aaaaee", ["glitch-title", "retro-homepage", "impossible-dates"]),
    "lucy":     ("LUCY JONES", "lucy", "#99aab0", ["archive-memories"]),
    "terminal": ("MAXWELL", "terminal", "#44dd77", ["terminal-diagnostics"]),
    "melody":   ("CLOVER", "melody", "#33ccaa", ["floating-clovers", "class-board", "bunting"]),
    "stacy":    ("STACY", "stacy", "#d934a2", []),
    "stella":   ("STELLA", "vapor", "#ff68d1", []),
    "vapor":    ("STELLA", "vapor", "#ff68d1", []),
    "playmate": ("COMPUTER/USER", "mac", "#8c8c8c", []),
    "mac":      ("COMPUTER/USER", "mac", "#8c8c8c", []),
}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def connect():
    conn = sqlite3.connect(config.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


class MessageError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message, self.status = message, status


# ------------------------------------------------------------------ meta
def get_meta(db, key):
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else META_DEFAULTS.get(key, "")


def set_meta(db, key, value):
    with db:
        db.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, str(value)))


# ------------------------------------------------------------------ init / migration
def init_db():
    os.makedirs(config.DB_DIR, exist_ok=True)
    conn = connect()
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        legacy = False
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(messages)")]
        if cols and "sender" in cols and "user_id" not in cols:
            legacy = True
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.close()
            shutil.copy2(config.DB_PATH, config.DB_PATH + ".pre-accounts.bak")
            conn = connect()
            with conn:
                conn.execute("ALTER TABLE messages RENAME TO messages_old")
        with conn:
            conn.executescript(SCHEMA)
        _upgrade(conn)
        if legacy:
            _migrate_legacy(conn)
    finally:
        conn.close()


def _upgrade(conn):
    """Add columns / tables introduced after the first release."""
    with conn:
        for table, col, definition in UPGRADES:
            have = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {definition}")
        conn.executescript(INDEXES)


def _migrate_legacy(conn):
    """Old messages keep their senders: each old identity becomes an archived user nobody can log in as
    (the admin can give one a password from the admin panel to 'claim' it)."""
    with conn:
        users = {}
        for row in conn.execute("SELECT DISTINCT sender FROM messages_old").fetchall():
            key = row["sender"]
            name, theme, color, gimmicks = LEGACY.get(key, (key.upper(), "classic", "", []))
            existing = conn.execute("SELECT id FROM users WHERE display_name=?", (name,)).fetchone()
            if existing:
                users[key] = existing["id"]
                continue
            cur = conn.execute(
                "INSERT INTO users (username, display_name, password_hash, is_disabled, disabled_message, created_at,"
                " name_color, theme, gimmicks) VALUES (?,?,?,?,?,?,?,?,?)",
                (key, name, None, 1, "Imported from the old room. An admin can give this user a password.",
                 now_iso(), color, theme, json.dumps(gimmicks)))
            users[key] = cur.lastrowid
        for r in conn.execute("SELECT * FROM messages_old ORDER BY id").fetchall():
            conn.execute(
                "INSERT INTO messages (id, user_id, author_snapshot, kind, message, timestamp, rev) VALUES (?,?,?,?,?,?,?)",
                (r["id"], users[r["sender"]], LEGACY.get(r["sender"], (r["sender"],))[0], "user", r["message"],
                 r["timestamp"], r["id"]))
        conn.execute("DROP TABLE messages_old")


# ------------------------------------------------------------------ users
def create_user(db, username, display_name, password_hash, preset_id=None, is_admin=False):
    p = themes.preset(preset_id) if preset_id else None
    p = p or themes.preset("classic") or {}
    with db:
        cur = db.execute(
            "INSERT INTO users (username, display_name, password_hash, is_admin, created_at, name_color, theme, gimmicks)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (username, display_name, password_hash, 1 if is_admin else 0, now_iso(), p.get("name_color", ""),
             p.get("theme", "classic"), json.dumps(p.get("gimmicks", []))))
    return cur.lastrowid


def get_user(db, uid):
    return db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


def get_user_by_name(db, username):
    return db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()


def update_user(db, uid, fields):
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with db:
        db.execute(f"UPDATE users SET {cols} WHERE id=?", (*fields.values(), uid))


# ------------------------------------------------------------------ IRC accounts (one per local account)
def get_irc_account(db, user_id):
    return db.execute("SELECT * FROM irc_accounts WHERE user_id=?", (user_id,)).fetchone()


def save_irc_account(db, user_id, fields):
    """Create or update the IRC login of a local account. `fields` holds only the columns to set."""
    now = now_iso()
    with db:
        if get_irc_account(db, user_id) is None:
            row = {"host": "", "port": 6697, "tls": 1, "tls_verify": 1, "nick": "", "created_at": now, "updated_at": now}
            row.update(fields)
            cols = ", ".join(["user_id"] + list(row))
            db.execute(f"INSERT INTO irc_accounts ({cols}) VALUES ({','.join('?' for _ in range(len(row) + 1))})",
                       (user_id, *row.values()))
        else:
            fields = {**fields, "updated_at": now}
            db.execute("UPDATE irc_accounts SET " + ", ".join(f"{k}=?" for k in fields) + " WHERE user_id=?",
                       (*fields.values(), user_id))


def delete_user_irc_data(db, user_id):
    """Everything IRC-related that belongs to a local account (used when the account is deleted)."""
    with db:
        ids = "SELECT id FROM rooms WHERE owner_id=?"
        db.execute(f"DELETE FROM irc_reactions WHERE message_id IN (SELECT id FROM messages WHERE room_id IN ({ids}))", (user_id,))
        db.execute(f"DELETE FROM messages WHERE room_id IN ({ids})", (user_id,))
        db.execute("DELETE FROM rooms WHERE owner_id=?", (user_id,))
        db.execute("DELETE FROM irc_accounts WHERE user_id=?", (user_id,))
        db.execute("DELETE FROM irc_ignores WHERE user_id=?", (user_id,))


# ------------------------------------------------------------------ buffers (rooms): status window, channels, private chats
STATUS_KEY = "\x00status"       # the server window has a fixed key, so renaming the network never creates a second one


def ensure_buffer(db, owner_id, name, kind, casemap="rfc1459", key=None):
    """The id of this person's buffer called `name`, creating it if needed. Returns (room_id, created)."""
    key = key or P.casefold(name, casemap)
    row = db.execute("SELECT id FROM rooms WHERE owner_id=? AND key=?", (owner_id, key)).fetchone()
    if row:
        return row["id"], False
    pos = db.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM rooms WHERE owner_id=?", (owner_id,)).fetchone()[0]
    with db:
        cur = db.execute("INSERT INTO rooms (name, kind, admin_only, position, created_at, owner_id, key, joined)"
                         " VALUES (?,?,0,?,?,?,?,1)", (name[:80], kind, pos, now_iso(), owner_id, key))
    return cur.lastrowid, True


def find_buffer(db, owner_id, name, casemap="rfc1459"):
    return db.execute("SELECT * FROM rooms WHERE owner_id=? AND key=?", (owner_id, P.casefold(name, casemap))).fetchone()


def rename_buffer(db, owner_id, old, new, casemap="rfc1459"):
    """A person you were chatting with changed nick: keep the conversation, follow the new name."""
    r = find_buffer(db, owner_id, old, casemap)
    if r is None or r["kind"] != "query" or find_buffer(db, owner_id, new, casemap) is not None:
        return None
    with db:
        db.execute("UPDATE rooms SET name=?, key=? WHERE id=?", (new[:80], P.casefold(new, casemap), r["id"]))
    return r["id"]


def update_room(db, room_id, fields):
    with db:
        db.execute("UPDATE rooms SET " + ", ".join(f"{k}=?" for k in fields) + " WHERE id=?", (*fields.values(), room_id))


def visible_rooms(db, user):
    """The buffers this local account owns: the server window first, then channels, then private chats."""
    return db.execute("SELECT * FROM rooms WHERE owner_id=? ORDER BY CASE kind WHEN 'status' THEN 0 WHEN 'channel' THEN 1 ELSE 2 END,"
                      " key", (user["id"],)).fetchall()


def get_room(db, room_id):
    return db.execute("SELECT * FROM rooms WHERE id=?", (room_id,)).fetchone()


def can_see_room(db, user, room_id):
    r = get_room(db, room_id)
    return r if r is not None and r["owner_id"] == user["id"] else None


def delete_buffer(db, room_id):
    with db:
        db.execute("DELETE FROM irc_reactions WHERE message_id IN (SELECT id FROM messages WHERE room_id=?)", (room_id,))
        db.execute("DELETE FROM messages WHERE room_id=?", (room_id,))
        db.execute("DELETE FROM rooms WHERE id=?", (room_id,))


# ------------------------------------------------------------------ messages
_REV = "(SELECT COALESCE(MAX(rev), 0) + 1 FROM messages)"
CHAT_KINDS = ("user", "action", "notice")


def add_irc_message(db, room_id, kind, sender, text, ts=None, own=False, msgid="", account="", highlight=False,
                    reply_to=None, me_id=None):
    """Store one line of an IRC conversation. Kinds: user, action, notice (spoken) and join, part, quit, kick, nick,
    topic, mode, server, error, system (events)."""
    mentions = json.dumps([me_id]) if highlight and me_id is not None else "[]"
    with db:
        cur = db.execute(
            "INSERT INTO messages (user_id, kind, message, timestamp, room_id, reply_to, mentions, rev, sender, account, msgid, own, highlight)"
            f" VALUES (NULL,?,?,?,?,?,?,{_REV},?,?,?,?,?)",
            (kind, text, ts or now_iso(), room_id, reply_to, mentions, sender, account or "", msgid or "",
             1 if own else 0, 1 if highlight else 0))
    return cur.lastrowid


def find_by_msgid(db, room_id, msgid):
    if not msgid:
        return None
    return db.execute("SELECT * FROM messages WHERE room_id=? AND msgid=? AND deleted=0", (room_id, msgid)).fetchone()


def touch_message(db, msg_id):
    """Bump a message's rev so every open page re-reads it (used when its reactions change)."""
    with db:
        db.execute(f"UPDATE messages SET rev={_REV} WHERE id=?", (msg_id,))


def prune_irc_logs(db, days=None, per_buffer=None):
    """Nightly housekeeping: drop IRC lines older than `days` and keep each buffer below `per_buffer` lines."""
    days = config.IRC_LOG_DAYS if days is None else days
    per_buffer = config.IRC_LOG_MAX_PER_BUFFER if per_buffer is None else per_buffer
    removed = 0
    with db:
        if days > 0:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
            removed += db.execute("DELETE FROM messages WHERE room_id IN (SELECT id FROM rooms WHERE owner_id IS NOT NULL)"
                                  " AND timestamp < ?", (cutoff,)).rowcount
        for r in db.execute("SELECT room_id, COUNT(*) AS n FROM messages WHERE room_id IN (SELECT id FROM rooms WHERE owner_id IS NOT NULL)"
                            " GROUP BY room_id HAVING n > ?", (per_buffer,)).fetchall():
            removed += db.execute("DELETE FROM messages WHERE id IN (SELECT id FROM messages WHERE room_id=?"
                                  " ORDER BY id DESC LIMIT -1 OFFSET ?)", (r["room_id"], per_buffer)).rowcount
        db.execute("DELETE FROM irc_reactions WHERE message_id NOT IN (SELECT id FROM messages)")
    return removed


# ------------------------------------------------------------------ reactions (IRCv3 +draft/react)
def add_irc_reaction(db, message_id, nick, emoji):
    with db:
        db.execute("INSERT OR IGNORE INTO irc_reactions (message_id, nick, emoji) VALUES (?,?,?)", (message_id, nick, emoji))
    touch_message(db, message_id)


def remove_irc_reaction(db, message_id, nick, emoji):
    with db:
        db.execute("DELETE FROM irc_reactions WHERE message_id=? AND nick=? AND emoji=?", (message_id, nick, emoji))
    touch_message(db, message_id)


def has_irc_reaction(db, message_id, nick, emoji):
    return db.execute("SELECT 1 FROM irc_reactions WHERE message_id=? AND nick=? AND emoji=?",
                      (message_id, nick, emoji)).fetchone() is not None


def reactions_for(db, ids):
    """{message id: {emoji: [nicks]}} for the given messages."""
    out, ids = {}, list(ids)
    for k in range(0, len(ids), 500):
        part = ids[k:k + 500]
        marks = ",".join("?" for _ in part)
        for r in db.execute(f"SELECT message_id, emoji, nick FROM irc_reactions WHERE message_id IN ({marks}) ORDER BY rowid", part):
            out.setdefault(r["message_id"], {}).setdefault(r["emoji"], []).append(r["nick"])
    return out


# ------------------------------------------------------------------ mute / block (each person's own view only)
def set_ignore(db, user_id, nick, kind):
    with db:
        if kind in ("mute", "block"):
            db.execute("INSERT INTO irc_ignores (user_id, nick, kind) VALUES (?,?,?)"
                       " ON CONFLICT(user_id, nick) DO UPDATE SET kind=excluded.kind", (user_id, nick, kind))
        else:
            db.execute("DELETE FROM irc_ignores WHERE user_id=? AND nick=?", (user_id, nick))


def ignores_of(db, user_id):
    """{lower-case nick: 'mute' | 'block'}"""
    return {r["nick"].lower(): r["kind"] for r in db.execute("SELECT nick, kind FROM irc_ignores WHERE user_id=?", (user_id,))}


# ------------------------------------------------------------------ audit log
def log_audit(db, actor, action, target="", detail=""):
    with db:
        db.execute("INSERT INTO audit (ts, actor_id, actor_name, action, target, detail) VALUES (?,?,?,?,?,?)",
                   (now_iso(), actor["id"] if actor else None, (actor["display_name"] if actor else "system"),
                    action, str(target)[:120], str(detail)[:400]))


# ------------------------------------------------------------------ shared themes (gallery)
def add_shared_theme(db, author, title, description, payload, has_js):
    with db:
        cur = db.execute("INSERT INTO shared_themes (author_id, author_name, title, description, payload, has_js, created_at)"
                         " VALUES (?,?,?,?,?,?,?)",
                         (author["id"], author["display_name"], title, description, payload, 1 if has_js else 0, now_iso()))
    return cur.lastrowid
