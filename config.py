"""All tunables in one place. Environment variables override the defaults."""
import os

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
HOST = os.environ.get("ROOM_HOST", "0.0.0.0")      # all interfaces -> reachable from your LAN
PORT = int(os.environ.get("ROOM_PORT", "7777"))
DB_DIR = os.environ.get("ROOM_DB_DIR", os.path.join(BASE_DIR, "database"))
DB_PATH = os.path.join(DB_DIR, "chat.db")
KEY_PATH = os.path.join(DB_DIR, "secret.key")

# messages
MAX_MESSAGE_LENGTH = 1000   # longer texts are split into several IRC lines automatically
HISTORY_LIMIT = int(os.environ.get("ROOM_HISTORY_LIMIT", "300"))   # messages loaded per channel when the page first opens
SYNC_BATCH_LIMIT = 200       # max changed messages returned per sync
SEND_BURST = 5               # at most this many messages ...
SEND_WINDOW_S = 6            # ... per this many seconds, per user

# accounts
MIN_PASSWORD_LENGTH = 8
SESSION_DAYS = 30
LOGIN_MAX_FAILS = 6          # wrong passwords before a short lockout (per IP + username)
LOGIN_LOCK_S = 60

# presence
ONLINE_WINDOW_S = 35         # seen within this many seconds = online
TYPING_TTL_S = 4

# per-user customisation limits
MAX_CUSTOM_CSS = 30000
MAX_CUSTOM_JS = 30000
MAX_INTRO = 300

# MacroDroid signal (optional). Visiting / or /room from anything other than
# localhost pings this webhook. Set MACRODROID_WEBHOOK_URL="" to turn it off.
# SIGNAL_ONLY_FROM = comma separated IPs allowed to trigger it (empty = anyone).
SIGNAL_WEBHOOK_URL = os.environ.get("MACRODROID_WEBHOOK_URL", "http://192.168.1.241:8080/gabby_curious")
SIGNAL_MOOD = "CURIOUS"
SIGNAL_COOLDOWN_S = 10
SIGNAL_ONLY_FROM = [x.strip() for x in os.environ.get("SIGNAL_ONLY_FROM", "").split(",") if x.strip()]

# ---- chat features ----------------------------------------------------------------------------------------------
REACTIONS = ["👍", "❤️", "😂", "😮", "😢", "🎉", "🔥", "👀"]   # the emoji people can react with
HISTORY_PAGE = 50            # messages per "load older" request
SEARCH_LIMIT = 60            # max search results
MAX_BIO = 160
MAX_REPORT_REASON = 200
MAX_ROOM_NAME = 24
MSG_EFFECTS = ("none", "sparkle", "glitch", "typewriter")

# ---- image sharing ------------------------------------------------------------------------------------------------
UPLOAD_DIR = os.environ.get("ROOM_UPLOAD_DIR", os.path.join(DB_DIR, "uploads"))
MAX_IMAGE_BYTES = 4 * 1024 * 1024        # per picture (4 MB)
IMAGE_MAX_EDGE = 1600                    # if Pillow is installed, bigger pictures are shrunk to this many pixels
UPLOAD_MAX_AGE_DAYS = 30                 # pictures older than this are deleted (the message stays, marked "expired")
UPLOAD_MAX_TOTAL_MB = 300                # if all pictures together exceed this, the oldest go first
UPLOAD_SEND_BURST = 4                    # at most this many pictures ...
UPLOAD_SEND_WINDOW_S = 60                # ... per this many seconds, per user

# ---- backups ------------------------------------------------------------------------------------------------------
BACKUP_DIR = os.environ.get("ROOM_BACKUP_DIR", os.path.join(DB_DIR, "backups"))
BACKUP_HOUR = 3                          # nightly backup + picture cleanup at this local hour (0-23)
BACKUP_KEEP = 7                          # how many nightly copies to keep
BACKUPS_ENABLED = os.environ.get("ROOM_BACKUPS", "1") != "0"

# ---- optional HTTPS (needed for the PWA service worker on other devices) ------------------------------------------
SSL_CERT = os.environ.get("ROOM_SSL_CERT", "")
SSL_KEY = os.environ.get("ROOM_SSL_KEY", "")

# ---- theme sharing ------------------------------------------------------------------------------------------------
MAX_SHARED_CODE = 60000                  # largest theme code we accept
MAX_GALLERY_TITLE = 40
MAX_GALLERY_DESC = 200
MAX_SCHEDULE_RULES = 6

# ---- IRC -----------------------------------------------------------------------------------------------------------
IRC_DEFAULT_SERVER = os.environ.get("ROOM_IRC_SERVER", "irc.libera.chat")
IRC_AUTOSTART = os.environ.get("ROOM_IRC_AUTOSTART", "1") != "0"            # reconnect everyone's IRC accounts when the app starts
IRC_ALLOW_LOCAL = os.environ.get("ROOM_IRC_ALLOW_LOCAL", "0") == "1"         # allow servers on this very machine (127.0.0.1)
# Ports people may connect to. This server opens the connection, so it must not be usable to knock on other services.
# ROOM_IRC_PORTS="any" switches the check off; or give your own list, e.g. "6667,6697,7000".
_ports = os.environ.get("ROOM_IRC_PORTS", "6660-6669,6697,7000,7070,9999")
IRC_ANY_PORT = _ports.strip().lower() == "any"
IRC_ALLOWED_PORTS = set()
if not IRC_ANY_PORT:
    for _part in _ports.split(","):
        _part = _part.strip()
        if "-" in _part:
            _a, _b = _part.split("-", 1)
            IRC_ALLOWED_PORTS.update(range(int(_a), int(_b) + 1))
        elif _part:
            IRC_ALLOWED_PORTS.add(int(_part))
IRC_LOG_DAYS = int(os.environ.get("ROOM_IRC_LOG_DAYS", "90"))                # nightly cleanup: older IRC messages are removed
IRC_LOG_MAX_PER_BUFFER = 20000                                               # ... and no buffer keeps more than this
