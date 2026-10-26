"""
LAN chat room -- Flask + SQLite.

Run:  python app.py        (listens on 0.0.0.0:7777; see config.py)

A web IRC client for your LAN (think KiwiIRC).

* Anyone on the LAN can create a local chat account (admin can close registration or require an invite code).
  That account holds the person's look (themes, CSS, JS gimmicks) and their IRC login. Nobody else ever sees the look.
* Each local account connects to IRC with its own nick, password (SASL) and server, and keeps its own private logs.
* The server keeps the IRC connection open like a small bouncer, so any device that logs in sees the same chats.
* The admin (created on first run with the setup code printed in this console) gets /admin.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
import zlib
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import (Flask, Response, g, jsonify, redirect, render_template, request, send_from_directory, session,
                   stream_with_context, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

import config
import crypto_box
import db as store
import irc_proto as P
import maintenance
import themes
from irc_client import NotConnected, check_host
from irc_manager import CommandError, manager as irc

# ---------------------------------------------------------------------------- app setup
def _load_secret_key():
    env = os.environ.get("ROOM_SECRET")
    if env:
        return env
    os.makedirs(config.DB_DIR, exist_ok=True)
    if os.path.exists(config.KEY_PATH):
        with open(config.KEY_PATH, encoding="utf-8") as f:
            key = f.read().strip()
            if key:
                return key
    key = secrets.token_hex(32)
    with open(config.KEY_PATH, "w", encoding="utf-8") as f:
        f.write(key)
    try:
        os.chmod(config.KEY_PATH, 0o600)
    except OSError:
        pass
    return key


app = Flask(__name__)
app.config.update(
    SECRET_KEY=_load_secret_key(),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    MAX_CONTENT_LENGTH=256 * 1024,
    PERMANENT_SESSION_LIFETIME=timedelta(days=config.SESSION_DAYS),
)
store.init_db()
JSON_LIMIT = 256 * 1024
_boot_db = store.connect()
themes.apply_presets(store.get_meta(_boot_db, "presets"))
_boot_db.close()
SETUP_CODE = secrets.token_hex(3).upper()      # printed in the console; needed to create the first admin


def get_db():
    if "db" not in g:
        g.db = store.connect()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    d = g.pop("db", None)
    if d is not None:
        d.close()


def needs_setup(db):
    return db.execute("SELECT 1 FROM users WHERE is_admin=1 AND password_hash IS NOT NULL").fetchone() is None


# ---------------------------------------------------------------------------- live state (in memory)
_cond = threading.Condition()
_version = 0
_seen = {}          # user id -> last time we heard from them
_persisted = {}     # user id -> last time last_seen was written to disk


def bump():
    """Tell every open event stream that something changed."""
    global _version
    with _cond:
        _version += 1
        _cond.notify_all()


irc.init(app.config["SECRET_KEY"], bump)          # (bump is defined just above)


def touch(uid):
    now = time.time()
    prev = _seen.get(uid, 0)
    _seen[uid] = now
    if now - prev > config.ONLINE_WINDOW_S:
        bump()                                    # they just came online
    if now - _persisted.get(uid, 0) > 60:
        _persisted[uid] = now
        try:
            store.update_user(get_db(), uid, {"last_seen": store.now_iso()})
        except sqlite3.Error:
            pass


def online_ids():
    cutoff = time.time() - config.ONLINE_WINDOW_S
    return {uid for uid, t in _seen.items() if t >= cutoff}


class Limiter:
    def __init__(self):
        self._hits, self._lock = defaultdict(deque), threading.Lock()

    def _prune(self, key, window):
        q, cutoff = self._hits[key], time.time() - window
        while q and q[0] < cutoff:
            q.popleft()
        return q

    def count(self, key, window):
        with self._lock:
            return len(self._prune(key, window))

    def add(self, key):
        with self._lock:
            self._hits[key].append(time.time())

    def allow(self, key, limit, window):
        with self._lock:
            q = self._prune(key, window)
            if len(q) >= limit:
                return False
            q.append(time.time())
            return True


limiter = Limiter()

# ---------------------------------------------------------------------------- request pipeline
def _is_api():
    return request.path.startswith("/api/")


def _fail(message, status=400, **extra):
    if _is_api():
        return jsonify(error=message, **extra), status
    return Response(message, status=status, mimetype="text/plain")


def csrf_token():
    tok = session.get("csrf")
    if not tok:
        tok = session["csrf"] = secrets.token_urlsafe(24)
    return tok


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def before():
    ep = request.endpoint
    if ep in (None, "static", "manifest", "service_worker", "offline_page"):
        return None
    if (request.content_length or 0) > JSON_LIMIT:
        return _fail("That is too large.", 413)
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        sent = request.headers.get("X-CSRF-Token") or request.form.get("csrf") or ""
        if not sent or not hmac.compare_digest(sent.encode(), session.get("csrf", "").encode()):
            return _fail("This page has expired. Reload it and try again.", 400)
    db = get_db()
    g.user = None
    uid = session.get("uid")
    if uid:
        u = store.get_user(db, uid)
        if u and u["password_hash"] and u["sv"] == session.get("sv"):
            g.user = u
        else:
            session.pop("uid", None)
    if ep != "setup" and needs_setup(db):
        return _fail("The room has not been set up yet.", 503) if _is_api() else redirect(url_for("setup"))
    if g.user and g.user["is_disabled"] and ep not in ("disabled", "logout"):
        if _is_api():
            return jsonify(error="Account disabled.", disabled=True), 403
        return redirect(url_for("disabled"))
    return None


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if g.user is None:
            return _fail("Not logged in.", 401) if _is_api() else redirect(url_for("login"))
        return fn(*a, **kw)
    return wrapper


def admin_required(fn):
    @wraps(fn)
    @login_required
    def wrapper(*a, **kw):
        if not g.user["is_admin"]:
            return _fail("Admins only.", 403) if _is_api() else redirect(url_for("room"))
        return fn(*a, **kw)
    return wrapper


@app.after_request
def headers(resp):
    if request.path.startswith("/api/") or request.path in ("/", "/room", "/settings", "/admin", "/preview", "/gallery", "/sandbox") \
            or request.path.startswith("/me/"):
        resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    resp.headers["Referrer-Policy"] = "same-origin"
    return resp


@app.errorhandler(404)
def _404(_e):
    return _fail("Not found.", 404)


@app.errorhandler(405)
def _405(_e):
    return _fail("Method not allowed.", 405)


@app.errorhandler(413)
def _413(_e):
    return _fail("That is too large.", 413)


# ---------------------------------------------------------------------------- MacroDroid signal (optional)
_signal_lock, _signal_last = threading.Lock(), 0.0
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}


def _signal_url():
    url = (config.SIGNAL_WEBHOOK_URL or "").strip()
    return url if not url or "://" in url else "http://" + url     # the old default had no scheme and always failed


def _is_local_visit():
    host = request.host or ""
    host = host.split("]")[0] + "]" if host.startswith("[") else host.split(":")[0]
    return host.lower() in LOCAL_HOSTS or request.remote_addr in ("127.0.0.1", "::1")


def _fire_webhook(url):
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            print(f"[signal] GabbyMood={config.SIGNAL_MOOD} sent (HTTP {r.status})")
    except Exception as e:                                        # a failed ping must never affect the site
        print(f"[signal] FAILED to reach MacroDroid webhook: {e}")


@app.before_request
def gabby_signal_on_visit():
    global _signal_last
    url = _signal_url()
    if not url or request.method != "GET" or request.path not in ("/", "/room", "/login") or _is_local_visit():
        return
    if config.SIGNAL_ONLY_FROM and request.remote_addr not in config.SIGNAL_ONLY_FROM:
        return
    with _signal_lock:
        now = time.monotonic()
        if now - _signal_last < config.SIGNAL_COOLDOWN_S:
            return
        _signal_last = now
    sep = "&" if "?" in url else "?"
    threading.Thread(target=_fire_webhook, args=(url + sep + urllib.parse.urlencode({"mood": config.SIGNAL_MOOD}),),
                     daemon=True).start()


# ---------------------------------------------------------------------------- helpers
def _jload(text, default):
    try:
        val = json.loads(text or "")
        return val if isinstance(val, type(default)) else default
    except ValueError:
        return default


def user_settings(u):
    try:
        tv = themes.clean_vars(_jload(u["theme_vars"], {}))
    except ValueError:
        tv = dict(themes.DEFAULT_VARS)
    return {"username": u["username"], "display_name": u["display_name"], "name_color": u["name_color"],
            "status_text": u["status_text"], "theme": u["theme"], "gimmicks": _jload(u["gimmicks"], []),
            "theme_vars": tv, "labels": _jload(u["labels"], {}), "custom_css": u["custom_css"],
            "custom_js": u["custom_js"], "custom_intro": u["custom_intro"],
            "use_custom_css": bool(u["use_custom_css"]), "use_custom_js": bool(u["use_custom_js"]),
            "intro_mode": u["intro_mode"], "show_name_colors": bool(u["show_name_colors"]),
            "bio": u["bio"], "msg_effect": u["msg_effect"], "show_effects": bool(u["show_effects"]),
            "follow_events": bool(u["follow_events"]), "schedule": _jload(u["schedule"], [])}


def login_user(u, remember=True):
    session.clear()
    session["uid"], session["sv"] = u["id"], u["sv"]
    session["csrf"] = secrets.token_urlsafe(24)
    session.permanent = bool(remember)


def _ver(text):
    return hashlib.md5((text or "").encode()).hexdigest()[:8]


def safe_mode():
    return request.args.get("safe") == "1"


def local_minutes():
    """Minutes since midnight in the VIEWER's time zone (the page stores their offset in a cookie), else the server's."""
    try:
        off = int(request.cookies.get("tzo", ""))
        if -840 <= off <= 840:
            t = time.gmtime(time.time() - off * 60)
            return t.tm_hour * 60 + t.tm_min
    except ValueError:
        pass
    t = time.localtime()
    return t.tm_hour * 60 + t.tm_min


def active_event(db):
    """The room-wide event look, or None. Expired events switch themselves off."""
    raw = store.get_meta(db, "event")
    if not raw:
        return None
    try:
        e = json.loads(raw)
        if e.get("until") and e["until"] <= store.now_iso():
            store.set_meta(db, "event", "")
            return None
        p = themes.preset(e.get("preset"))
        if not p:
            return None
        return {"id": p["id"], "title": e.get("title") or p["name"], "until": e.get("until", ""),
                "theme": p["theme"], "gimmicks": p["gimmicks"]}
    except (ValueError, TypeError, AttributeError):
        return None


def render_chat(u, preview=False, override=None):
    cat, db = themes.catalog(), get_db()
    room_name = store.get_meta(db, "room_name")
    s = user_settings(u)
    theme_id = s["theme"] if s["theme"] in cat["themes"] else "classic"
    gimmick_ids, tvars, labels_over, intro_over = s["gimmicks"], s["theme_vars"], s["labels"], s["custom_intro"]
    if override:
        theme_id, gimmick_ids, tvars = override["theme"], override["gimmicks"], override["vars"]
        labels_over, intro_over = override["labels"], override["intro"]
    # a room-wide event look beats a personal schedule, which beats the user's normal look
    ev = None if (override or not s["follow_events"] or safe_mode()) else active_event(db)
    rules = s["schedule"] if not override else []
    sched_idx = None if (override or ev) else themes.active_rule(rules, local_minutes())
    if ev:
        theme_id, gimmick_ids, tvars, labels_over, intro_over = ev["theme"], ev["gimmicks"], dict(themes.DEFAULT_VARS), {}, ""
    elif sched_idx is not None:
        p = themes.preset(rules[sched_idx]["preset"])
        theme_id, gimmick_ids, tvars = p["theme"], p["gimmicks"], dict(themes.DEFAULT_VARS)
    gimmick_ids = [x for x in gimmick_ids if x in cat["gimmick_ids"]]
    labels = themes.effective_labels(theme_id, labels_over, room_name)
    intro = (intro_over or "").strip() or labels.get("intro", "")
    safe = request.args.get("safe") == "1"
    theme = cat["themes"][theme_id]
    use_css = bool(s["use_custom_css"] and s["custom_css"].strip()) and not safe and not preview and not ev
    use_js = bool(s["use_custom_js"] and s["custom_js"].strip()) and not safe and not preview
    boot = {
        "me": {"id": u["id"], "name": u["display_name"], "color": u["name_color"], "admin": bool(u["is_admin"])},
        "theme": theme_id, "timeFormat": theme.get("time_format", "short"), "maxLength": config.MAX_MESSAGE_LENGTH,
        "labels": labels, "intro": intro, "introMode": "always" if preview else s["intro_mode"],
        "showNameColors": bool(s["show_name_colors"]), "gimmicks": gimmick_ids, "csrf": csrf_token(),
        "preview": preview, "roomName": room_name, "pageTitle": labels["room_title"],
        "reactions": config.REACTIONS, "showEffects": bool(s["show_effects"]), "myEffect": s["msg_effect"],
        "schedule": rules, "activeRule": sched_idx, "eventId": ev["id"] if ev else "",
        "eventTitle": ev["title"] if ev else "", "historyPage": config.HISTORY_PAGE, "historyLimit": config.HISTORY_LIMIT,
    }
    return render_template(
        "chat.html", theme=theme_id, body_style=themes.vars_to_style(tvars) if theme_id == "custom" else "",
        theme_css=theme_id if theme["has_css"] else None, gimmick_ids=gimmick_ids, labels=labels, intro=intro,
        boot=boot, me=u, use_css=use_css, use_js=use_js, css_v=_ver(s["custom_css"]), js_v=_ver(s["custom_js"]),
        safe=safe, preview=preview)


_MSG_SQL = ("SELECT m.id, m.kind, m.message, m.timestamp, m.rev, m.room_id, m.reply_to, m.mentions, m.sender, m.account,"
            " m.msgid, m.own, m.highlight, rm.message AS r_text, rm.sender AS r_name"
            " FROM messages m LEFT JOIN messages rm ON rm.id = m.reply_to")


def msg_dict(r, rx=None, me_id=None):
    d = {"id": r["id"], "uid": me_id if r["own"] else None, "name": r["sender"], "text": r["message"], "ts": r["timestamp"],
         "kind": r["kind"], "room": r["room_id"], "own": bool(r["own"]), "account": r["account"], "msgid": r["msgid"],
         "mentions": _jload(r["mentions"], []), "highlight": bool(r["highlight"]), "reactions": (rx or {}).get(r["id"], {}),
         "reply": None, "deleted": False, "edited": False}
    if r["reply_to"]:
        d["reply"] = {"id": r["reply_to"], "name": r["r_name"] or "someone", "deleted": r["r_text"] is None,
                      "text": (r["r_text"] or "")[:100]}
    return d


def hydrate(db, rows, me_id=None):
    """Message rows -> dicts, with their reactions attached (one extra query for the whole batch)."""
    rows = list(rows)
    rx = store.reactions_for(db, [r["id"] for r in rows])
    return [msg_dict(r, rx, me_id) for r in rows]


def room_list(db, me):
    """The buffers (server window, channels, private chats) this local account owns, ready for the client."""
    return [{"id": r["id"], "name": r["name"], "kind": r["kind"], "joined": bool(r["joined"]), "topic": r["topic"],
             "admin_only": False} for r in store.visible_rooms(db, me)]


def ensure_session(uid):
    """The IRC session of this account. If it should be online but is not running (after an app restart), start it."""
    s = irc.session(uid)
    if s.client is None and not getattr(s, "tried", False):
        s.tried = True
        db = get_db()
        row = store.get_irc_account(db, uid)
        if row is not None and row["autoconnect"] and not row["auth_failed"]:
            try:
                s.start()
            except ValueError as e:
                s.error = str(e)
    return s


# ---------------------------------------------------------------------------- pages: auth
@app.route("/")
def index():
    return redirect(url_for("room" if g.user else "login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("room"))
    db, error = get_db(), None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        key = ("login", request.remote_addr, username.lower())
        if limiter.count(key, config.LOGIN_LOCK_S) >= config.LOGIN_MAX_FAILS:
            error = f"Too many wrong attempts. Wait about {config.LOGIN_LOCK_S} seconds."
        else:
            u = store.get_user_by_name(db, username)
            if u and u["password_hash"] and check_password_hash(u["password_hash"], request.form.get("password", "")):
                login_user(u, remember=bool(request.form.get("remember")))
                return redirect(url_for("room"))
            limiter.add(key)
            error = "Wrong username or password."
    return render_template("login.html", error=error, registration_open=store.get_meta(db, "registration_open") == "1")


@app.route("/register", methods=["GET", "POST"])
def register():
    if g.user:
        return redirect(url_for("room"))
    db, error = get_db(), None
    closed = store.get_meta(db, "registration_open") != "1"
    form = request.form
    if request.method == "POST" and not closed:
        try:
            if not limiter.allow(("register", request.remote_addr), 10, 600):
                raise ValueError("Too many sign-ups from this device. Try again later.")
            username = themes.clean_username(form.get("username"))
            display = themes.clean_display_name(form.get("display_name") or username)
            pw = form.get("password", "")
            if len(pw) < config.MIN_PASSWORD_LENGTH:
                raise ValueError(f"Password must be at least {config.MIN_PASSWORD_LENGTH} characters.")
            if pw != form.get("confirm", ""):
                raise ValueError("Passwords do not match.")
            invite = store.get_meta(db, "invite_code").strip()
            if invite and not hmac.compare_digest(invite.lower().encode(), form.get("invite", "").strip().lower().encode()):
                raise ValueError("That invite code is not right.")
            uid = store.create_user(db, username, display, generate_password_hash(pw, method="pbkdf2:sha256"),
                                    preset_id=form.get("preset"))
            bump()
            login_user(store.get_user(db, uid))
            return redirect(url_for("settings_page") if form.get("customize") else url_for("room"))
        except sqlite3.IntegrityError:
            error = "That username or display name is already taken."
        except ValueError as e:
            error = str(e)
    return render_template("register.html", error=error, closed=closed, presets=themes.catalog()["presets"],
                           invite_needed=bool(store.get_meta(db, "invite_code").strip()), form=form)


@app.route("/setup", methods=["GET", "POST"])
def setup():
    db, error = get_db(), None
    if not needs_setup(db):
        return redirect(url_for("index"))
    form = request.form
    if request.method == "POST":
        try:
            if not hmac.compare_digest(SETUP_CODE.encode(), form.get("code", "").strip().upper().encode()):
                raise ValueError("Wrong setup code. It is printed in the console where the server is running.")
            username = themes.clean_username(form.get("username"))
            display = themes.clean_display_name(form.get("display_name") or username)
            pw = form.get("password", "")
            if len(pw) < config.MIN_PASSWORD_LENGTH:
                raise ValueError(f"Password must be at least {config.MIN_PASSWORD_LENGTH} characters.")
            if pw != form.get("confirm", ""):
                raise ValueError("Passwords do not match.")
            uid = store.create_user(db, username, display, generate_password_hash(pw, method="pbkdf2:sha256"),
                                    preset_id="classic", is_admin=True)
            bump()
            login_user(store.get_user(db, uid))
            return redirect(url_for("admin_page"))
        except sqlite3.IntegrityError:
            error = "That username or display name is already taken (old imported users keep their names)."
        except ValueError as e:
            error = str(e)
    return render_template("setup.html", error=error, form=form)


@app.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/disabled")
@login_required
def disabled():
    if not g.user["is_disabled"]:
        return redirect(url_for("room"))
    return render_template("disabled.html", message=g.user["disabled_message"], name=g.user["display_name"])


# ---------------------------------------------------------------------------- pages: room / settings / preview
@app.route("/room")
@login_required
def room():
    if store.get_irc_account(get_db(), g.user["id"]) is None:
        return redirect(url_for("irc_page"))              # first time: say which IRC server and account to use
    ensure_session(g.user["id"])
    return render_chat(g.user)


@app.route("/preview")
@login_required
def preview():
    q, cat = request.args, themes.catalog()
    try:
        theme_id = q.get("theme", "classic")
        if theme_id not in cat["themes"]:
            theme_id = "classic"
        override = {"theme": theme_id,
                    "gimmicks": themes.clean_gimmicks([x for x in (q.get("g") or "").split(",") if x]),
                    "vars": themes.clean_vars(json.loads(q.get("v") or "{}")),
                    "labels": themes.clean_labels(json.loads(q.get("l") or "{}")),
                    "intro": (q.get("i") or "")[:config.MAX_INTRO]}
    except ValueError:
        override = {"theme": "classic", "gimmicks": [], "vars": dict(themes.DEFAULT_VARS), "labels": {}, "intro": ""}
    return render_chat(g.user, preview=True, override=override)


@app.route("/settings")
@login_required
def settings_page():
    cat = themes.catalog()
    data = {
        "settings": user_settings(g.user),
        "themes": [{"id": t["id"], "name": t["name"], "description": t.get("description", ""),
                    "labels": themes.theme_labels(t["id"])} for t in cat["themes"].values()],
        "gimmicks": cat["gimmicks"], "presets": cat["presets"], "fonts": list(themes.FONTS),
        "varDefaults": themes.DEFAULT_VARS, "labelKeys": list(themes.LABEL_KEYS),
        "roomName": store.get_meta(get_db(), "room_name"), "maxIntro": config.MAX_INTRO,
        "minPassword": config.MIN_PASSWORD_LENGTH,
    }
    return render_template("settings.html", data=data, user=g.user)


@app.get("/me/custom.css")
@login_required
def my_css():
    return Response(g.user["custom_css"] if g.user["use_custom_css"] else "", mimetype="text/css")


@app.get("/me/custom.js")
@login_required
def my_js():
    return Response(g.user["custom_js"] if g.user["use_custom_js"] else "", mimetype="text/javascript")


# ---------------------------------------------------------------------------- API: sync / stream / messages
def _int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _my_room(data_or_args, key="room"):
    return store.can_see_room(get_db(), g.user, _int(data_or_args.get(key)))


def _names_payload(s, room, known_ver):
    """Nick list of the open channel. Sent only when it changed since the browser's version (big channels are heavy)."""
    n = s.names_for(room)
    rows = n["list"][:1500]
    ver = zlib.crc32("\n".join(f"{r['prefix']}{r['nick']}{int(r['away'])}" for r in rows).encode()) & 0xFFFFFFFF
    out = {"room": room["id"], "total": n["total"], "ver": ver}
    if known_ver != ver:
        out["list"] = rows
    return out


@app.get("/api/sync")
@login_required
def api_sync():
    db, me = get_db(), g.user
    touch(me["id"])
    s = ensure_session(me["id"])
    rev = _int(request.args.get("rev"), 0)
    rooms = room_list(db, me)
    room_ids = [r["id"] for r in rooms]
    marks = ",".join("?" for _ in room_ids) or "NULL"
    max_rev = db.execute("SELECT COALESCE(MAX(rev), 0) AS r FROM messages").fetchone()["r"]
    more = False
    if rev <= 0:
        rows = []
        for rid in room_ids:                      # the latest messages of every window I can see
            rows += db.execute(_MSG_SQL + " WHERE m.room_id=? AND m.deleted=0 AND m.rev<=? ORDER BY m.id DESC LIMIT ?",
                               (rid, max_rev, config.HISTORY_LIMIT)).fetchall()
        rows.sort(key=lambda r: r["id"])
        new_rev = max_rev
    else:
        rows = db.execute(_MSG_SQL + f" WHERE m.rev>? AND m.rev<=? AND m.room_id IN ({marks}) ORDER BY m.rev ASC LIMIT ?",
                          (rev, max_rev, *room_ids, config.SYNC_BATCH_LIMIT + 1)).fetchall()
        more = len(rows) > config.SYNC_BATCH_LIMIT
        rows = rows[:config.SYNC_BATCH_LIMIT]
        new_rev = rows[-1]["rev"] if more else max(rev, max_rev)
    cur = _my_room(request.args)
    names, typing = None, []
    if cur is not None:
        names = _names_payload(s, cur, _int(request.args.get("nv")))
        typing = s.typing_in(cur["id"])
    ev = active_event(db)
    status = s.status()
    return jsonify(rev=new_rev, more=more, messages=hydrate(db, rows, me["id"]), rooms=rooms, names=names, typing=typing,
                   irc=status, features=s.features(), ignores=store.ignores_of(db, me["id"]),
                   room={"name": status["network"] or store.get_meta(db, "room_name"), "motd": ""},
                   event={"id": ev["id"], "title": ev["title"]} if ev else None)


@app.get("/api/stream")
@login_required
def api_stream():
    uid = g.user["id"]

    def gen():
        last = -1
        yield "retry: 3000\n\n"
        while True:
            with _cond:
                if _version == last:
                    _cond.wait(timeout=12)          # nothing changed: wake up anyway as a heartbeat
                v = _version
            _seen[uid] = time.time()
            yield f"data: {v}\n\n"
            last = v

    return Response(stream_with_context(gen()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,40}$")


@app.post("/api/messages")
@login_required
def api_send():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error="Bad request."), 400
    db, me = get_db(), g.user
    room = _my_room(data)
    if room is None:
        return jsonify(error="No such window."), 404
    text = data.get("text")
    if not isinstance(text, str) or not text.strip():
        return jsonify(error="Message is empty."), 400
    text = text.replace("\r\n", "\n").strip()
    if len(text) > config.MAX_MESSAGE_LENGTH:
        return jsonify(error=f"Message too long (max {config.MAX_MESSAGE_LENGTH} characters)."), 400
    if not limiter.allow(("send", me["id"]), config.SEND_BURST, config.SEND_WINDOW_S):
        return jsonify(error="Slow down a little."), 429
    client_id = str(data.get("client_id") or "")
    if client_id and not CLIENT_ID_RE.match(client_id):
        client_id = ""
    s = irc.get(me["id"])
    try:
        if s is None:
            raise NotConnected("Not connected to IRC. Check the connection page.")
        if text.startswith("/") and not text.startswith("//"):
            res = s.command(db, room, text.split("\n")[0])
        else:
            if text.startswith("//"):
                text = text[1:]                      # "//join" sends the text "/join"
            s.send_text(db, room, text, reply_local_id=_int(data.get("reply_to")), client_id=client_id)
            res = {"ok": True}
    except CommandError as e:
        return jsonify(error=str(e)), 400
    except NotConnected as e:
        return jsonify(error=str(e)), 409
    except ValueError as e:
        return jsonify(error=str(e)), 400
    touch(me["id"])
    bump()
    return jsonify(res)


@app.post("/api/typing")
@login_required
def api_typing():
    room = _my_room(request.get_json(silent=True) or {})
    s = irc.get(g.user["id"])
    touch(g.user["id"])
    if room is not None and s is not None:
        try:
            s.send_typing(room)
        except NotConnected:
            pass
    return jsonify(ok=True)


# ---------------------------------------------------------------------------- API: settings / password
@app.post("/api/settings")
@login_required
def api_settings():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error="Bad request."), 400
    db, uid = get_db(), g.user["id"]
    try:
        fields = themes.clean_settings(data)
        if "display_name" in data:
            dn = themes.clean_display_name(data["display_name"])
            if db.execute("SELECT 1 FROM users WHERE display_name=? AND id<>?", (dn, uid)).fetchone():
                raise ValueError("That display name is taken.")
            fields["display_name"] = dn
        store.update_user(db, uid, fields)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    except sqlite3.IntegrityError:
        return jsonify(error="That display name is taken."), 400
    bump()
    return jsonify(ok=True, settings=user_settings(store.get_user(db, uid)))


@app.post("/api/password")
@login_required
def api_password():
    data = request.get_json(silent=True) or {}
    if not check_password_hash(g.user["password_hash"], str(data.get("current", ""))):
        return jsonify(error="Current password is wrong."), 400
    new = str(data.get("new", ""))
    if len(new) < config.MIN_PASSWORD_LENGTH:
        return jsonify(error=f"Password must be at least {config.MIN_PASSWORD_LENGTH} characters."), 400
    db = get_db()
    sv = g.user["sv"] + 1
    store.update_user(db, g.user["id"], {"password_hash": generate_password_hash(new, method="pbkdf2:sha256"), "sv": sv})
    session["sv"] = sv                              # this browser stays signed in, every other session is signed out
    return jsonify(ok=True)


# ---------------------------------------------------------------------------- admin
@app.route("/admin")
@admin_required
def admin_page():
    return render_template("admin.html", user=g.user)


@app.get("/api/admin/users")
@admin_required
def admin_users():
    db, online = get_db(), online_ids()
    rows = db.execute("SELECT u.*, a.nick AS irc_nick, a.host AS irc_host FROM users u"
                      " LEFT JOIN irc_accounts a ON a.user_id=u.id ORDER BY u.created_at, u.id").fetchall()

    def irc_state(uid):
        s = irc.get(uid)
        return s.status()["state"] if s else "off"
    users = [{"id": r["id"], "username": r["username"], "display_name": r["display_name"], "is_admin": bool(r["is_admin"]),
              "is_disabled": bool(r["is_disabled"]), "disabled_message": r["disabled_message"],
              "has_password": r["password_hash"] is not None, "created_at": r["created_at"],
              "last_seen": r["last_seen"], "online": r["id"] in online, "theme": r["theme"],
              "irc": f"{r['irc_nick']} on {r['irc_host']}" if r["irc_nick"] else "", "irc_state": irc_state(r["id"])} for r in rows]
    settings = {k: store.get_meta(db, k) for k in ("registration_open", "invite_code", "room_name", "motd")}
    return jsonify(users=users, settings=settings, me=g.user["id"])


def _target(uid):
    u = store.get_user(get_db(), uid)
    return (u, None) if u else (None, (jsonify(error="No such user."), 404))


@app.post("/api/admin/users/<int:uid>/<action>")
@admin_required
def admin_user_action(uid, action):
    u, err = _target(uid)
    if err:
        return err
    data, db = request.get_json(silent=True) or {}, get_db()
    me_id, detail = g.user["id"], ""
    if action in ("disable", "admin") and uid == me_id:
        return jsonify(error="You cannot do that to your own account."), 400
    try:
        if action == "disable":
            msg = str(data.get("message") or "").strip()[:300] or "Your account has been disabled by the admin."
            store.update_user(db, uid, {"is_disabled": 1, "disabled_message": msg})
            irc.stop(uid)                           # a disabled account must not keep an IRC connection open
        elif action == "enable":
            store.update_user(db, uid, {"is_disabled": 0, "disabled_message": ""})
            if uid in irc.sessions:
                irc.sessions[uid].tried = False     # comes back online the next time they open the page
        elif action == "password":
            pw = str(data.get("password", ""))
            if len(pw) < config.MIN_PASSWORD_LENGTH:
                raise ValueError(f"Password must be at least {config.MIN_PASSWORD_LENGTH} characters.")
            fields = {"password_hash": generate_password_hash(pw, method="pbkdf2:sha256"), "sv": u["sv"] + 1}
            if data.get("enable"):
                fields.update(is_disabled=0, disabled_message="")
            store.update_user(db, uid, fields)
        elif action == "admin":
            store.update_user(db, uid, {"is_admin": 1 if data.get("value") else 0})
        elif action == "rename":
            dn = themes.clean_display_name(data.get("display_name"))
            store.update_user(db, uid, {"display_name": dn})
        elif action == "logout":
            store.update_user(db, uid, {"sv": u["sv"] + 1})
        else:
            return jsonify(error="Unknown action."), 404
    except ValueError as e:
        return jsonify(error=str(e)), 400
    except sqlite3.IntegrityError:
        return jsonify(error="That display name is taken."), 400
    if action == "disable":
        detail = msg
    elif action == "rename":
        detail = f"{u['display_name']} -> {dn}"
    elif action == "admin":
        detail = "made admin" if data.get("value") else "removed admin"
    store.log_audit(db, g.user, action, u["display_name"], detail)
    bump()
    return jsonify(ok=True)


@app.delete("/api/admin/users/<int:uid>")
@admin_required
def admin_delete_user(uid):
    u, err = _target(uid)
    if err:
        return err
    if uid == g.user["id"]:
        return jsonify(error="You cannot delete your own account."), 400
    db = get_db()
    irc.remove(uid)                                 # hang up their IRC connection first
    store.delete_user_irc_data(db, uid)             # their chats, logs, saved IRC login
    with db:
        db.execute("DELETE FROM users WHERE id=?", (uid,))
    store.log_audit(db, g.user, "delete user", u["display_name"], "their IRC logs and saved login were removed too")
    _seen.pop(uid, None)
    bump()
    return jsonify(ok=True)


@app.post("/api/admin/settings")
@admin_required
def admin_settings():
    data, db = request.get_json(silent=True) or {}, get_db()
    if "registration_open" in data:
        store.set_meta(db, "registration_open", "1" if data["registration_open"] else "0")
    if "invite_code" in data:
        store.set_meta(db, "invite_code", str(data["invite_code"] or "").strip()[:40])
    if "room_name" in data:
        name = " ".join(str(data["room_name"] or "").split())[:40]
        if not name:
            return jsonify(error="Room name cannot be empty."), 400
        store.set_meta(db, "room_name", name)
    if "motd" in data:
        store.set_meta(db, "motd", str(data["motd"] or "").strip()[:200])
    store.log_audit(db, g.user, "room settings", "", ", ".join(k for k in data if k != "invite_code")
                    + (", invite_code" if "invite_code" in data else ""))
    bump()
    return jsonify(ok=True)


# ---------------------------------------------------------------------------- API: reactions / history / search / people
def _visible_message(mid):
    """(message row, room row, error response) for a message in a window this account owns."""
    db = get_db()
    m = db.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
    room = store.can_see_room(db, g.user, m["room_id"]) if m is not None and not m["deleted"] else None
    if room is None:
        return None, None, (jsonify(error="No such message."), 404)
    return m, room, None


@app.post("/api/messages/<int:mid>/react")
@login_required
def api_react(mid):
    m, room, err = _visible_message(mid)
    if err:
        return err
    emoji = str((request.get_json(silent=True) or {}).get("emoji") or "")
    if emoji not in config.REACTIONS or m["kind"] not in store.CHAT_KINDS:
        return jsonify(error="You cannot react with that."), 400
    if not limiter.allow(("react", g.user["id"]), 20, 10):
        return jsonify(error="Slow down a little."), 429
    s = irc.get(g.user["id"])
    try:
        if s is None:
            raise NotConnected("Not connected to IRC.")
        added = s.toggle_reaction(get_db(), room, m, emoji)
    except CommandError as e:
        return jsonify(error=str(e)), 400
    except NotConnected as e:
        return jsonify(error=str(e)), 409
    bump()
    return jsonify(ok=True, added=added)


@app.get("/api/history")
@login_required
def api_history():
    db, me = get_db(), g.user
    room_id, before = _int(request.args.get("room")), _int(request.args.get("before"))
    if not room_id or store.can_see_room(db, me, room_id) is None:
        return jsonify(error="No such window."), 404
    limit = max(1, min(100, _int(request.args.get("limit"), config.HISTORY_PAGE)))
    sql, args = _MSG_SQL + " WHERE m.room_id=? AND m.deleted=0", [room_id]
    if before:
        sql, args = sql + " AND m.id<?", args + [before]
    rows = db.execute(sql + " ORDER BY m.id DESC LIMIT ?", args + [limit + 1]).fetchall()
    more = len(rows) > limit
    return jsonify(messages=hydrate(db, rows[:limit][::-1], me["id"]), more=more)


@app.get("/api/search")
@login_required
def api_search():
    db, me = get_db(), g.user
    q = " ".join(str(request.args.get("q") or "").split())
    if len(q) < 2:
        return jsonify(results=[], error="Type at least 2 letters.")
    if not limiter.allow(("search", me["id"]), 20, 20):
        return jsonify(error="Slow down a little."), 429
    rooms = {r["id"]: r for r in store.visible_rooms(db, me)}
    room_id = _int(request.args.get("room"))
    ids = [room_id] if room_id in rooms else list(rooms)
    like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    marks = ",".join("?" for _ in ids) or "NULL"
    rows = db.execute(_MSG_SQL + f" WHERE m.deleted=0 AND m.kind IN ('user','action','notice') AND m.room_id IN ({marks})"
                      " AND (m.message LIKE ? ESCAPE '\\' OR m.sender LIKE ? ESCAPE '\\')"
                      " ORDER BY m.id DESC LIMIT ?", (*ids, like, like, config.SEARCH_LIMIT)).fetchall()
    out = hydrate(db, rows, me["id"])
    for d in out:
        d["room_name"] = rooms[d["room"]]["name"] if d["room"] in rooms else ""
    return jsonify(results=out)


@app.get("/api/irc/user/<nick>")
@login_required
def api_irc_user(nick):
    if not P.valid_nick(nick):
        return jsonify(error="No such person."), 404
    s = irc.get(g.user["id"])
    room = _my_room(request.args)
    info = s.user_card(nick, room["name"] if room is not None and room["kind"] == "channel" else None) if s else {"nick": nick}
    info["relation"] = store.ignores_of(get_db(), g.user["id"]).get(nick.lower(), "none")
    info["me"] = bool(s and s.nick and s.nick.lower() == nick.lower())
    return jsonify(info)


@app.post("/api/ignore")
@login_required
def api_ignore():
    data = request.get_json(silent=True) or {}
    nick, kind = str(data.get("nick") or ""), data.get("kind")
    if kind not in ("none", "mute", "block") or not P.valid_nick(nick):
        return jsonify(error="Bad request."), 400
    store.set_ignore(get_db(), g.user["id"], nick, kind)
    return jsonify(ok=True, ignores=store.ignores_of(get_db(), g.user["id"]))


@app.post("/api/query")
@login_required
def api_query():
    nick = str((request.get_json(silent=True) or {}).get("nick") or "")
    s = irc.get(g.user["id"])
    if not P.valid_nick(nick) or s is None:
        return jsonify(error="You cannot message that person."), 400
    rid = s._room_for(get_db(), nick, "query")
    bump()
    return jsonify(ok=True, room=rid)


@app.post("/api/rooms/<int:rid>/close")
@login_required
def api_close_room(rid):
    room = store.can_see_room(get_db(), g.user, rid)
    if room is None:
        return jsonify(error="No such window."), 404
    s = irc.get(g.user["id"])
    try:
        (s.close_buffer(get_db(), room) if s else store.delete_buffer(get_db(), rid))
    except CommandError as e:
        return jsonify(error=str(e)), 400
    bump()
    return jsonify(ok=True)


# ---------------------------------------------------------------------------- IRC connection settings
def _irc_settings(row):
    if row is None:
        return {"configured": False, "server": config.IRC_DEFAULT_SERVER, "port": 6697, "tls": True, "tls_verify": True,
                "nick": "", "account": "", "has_password": False, "use_sasl": True, "realname": "", "autoconnect": True}
    return {"configured": True, "server": row["host"], "port": row["port"], "tls": bool(row["tls"]),
            "tls_verify": bool(row["tls_verify"]), "nick": row["nick"], "account": row["account"],
            "has_password": bool(row["password_enc"]), "use_sasl": bool(row["use_sasl"]), "realname": row["realname"],
            "autoconnect": bool(row["autoconnect"]), "auth_failed": bool(row["auth_failed"]), "last_error": row["last_error"]}


def _irc_status(db, uid):
    s = irc.get(uid)
    st = s.status() if s else {"state": "stopped", "error": "", "retry_in": 0, "nick": "", "network": "", "account": "",
                               "caps": [], "authenticated": False}
    lines = []
    if s:
        rid = db.execute("SELECT id FROM rooms WHERE owner_id=? AND kind='status'", (uid,)).fetchone()
        if rid:
            rows = db.execute("SELECT kind, sender, message, timestamp FROM messages WHERE room_id=? ORDER BY id DESC LIMIT 12",
                              (rid["id"],)).fetchall()
            lines = [{"kind": r["kind"], "sender": r["sender"], "text": r["message"], "ts": r["timestamp"]} for r in rows[::-1]]
    return {**st, "lines": lines, "features": s.features() if s else {}}


def clean_irc_settings(data, row):
    """Validate the connection form. Returns the columns to save (password already encrypted)."""
    out = {}
    server = str(data.get("server") or "").strip()
    if not server and row is None:
        raise ValueError("Enter the IRC server (for example irc.libera.chat).")
    if server or "port" in data or "tls" in data:
        host, port, tls = P.parse_server(server or (row["host"] if row else config.IRC_DEFAULT_SERVER))
        if data.get("port") not in (None, ""):
            port = _int(data.get("port"))
            if port is None or not 1 <= port <= 65535:
                raise ValueError("The port must be a number between 1 and 65535.")
        elif server == "" and row is not None:
            port = row["port"]
        if "tls" in data:
            tls = bool(data["tls"])
            if data.get("port") in (None, "") and ":" not in server and "://" not in server:
                port = 6697 if tls else 6667
        if not config.IRC_ANY_PORT and port not in config.IRC_ALLOWED_PORTS:
            raise ValueError("That port is not allowed here (IRC servers normally use 6697 or 6667).")
        check_host(host)                                     # also refuses this machine itself
        out.update(host=host, port=port, tls=1 if tls else 0)
    if "tls_verify" in data:
        out["tls_verify"] = 1 if data["tls_verify"] else 0
    nick = str(data.get("nick") if "nick" in data else (row["nick"] if row else "")).strip()
    if not P.valid_nick(nick):
        raise ValueError("Your IRC nick may use letters, digits and - _ [ ] { } \\ ` ^ | (up to 30, not starting with a digit).")
    out["nick"] = nick
    if "account" in data:
        acct = str(data["account"] or "").strip()
        if len(acct) > 40 or any(c.isspace() or ord(c) < 32 for c in acct):
            raise ValueError("The account name may not contain spaces.")
        out["account"] = acct
    pw = data.get("password")
    if data.get("clear_password"):
        out["password_enc"] = ""
    elif isinstance(pw, str) and pw != "":
        if len(pw) > 200 or any(ord(c) < 32 for c in pw):
            raise ValueError("That password cannot be used.")
        out["password_enc"] = crypto_box.encrypt(app.config["SECRET_KEY"], pw)
    if "use_sasl" in data:
        out["use_sasl"] = 1 if data["use_sasl"] else 0
    if "realname" in data:
        out["realname"] = " ".join(str(data["realname"] or "").split())[:50]
    if "autoconnect" in data:
        out["autoconnect"] = 1 if data["autoconnect"] else 0
    if row is None and "password_enc" not in out and out.get("use_sasl", 1):
        out.setdefault("use_sasl", 0)                        # no password yet: connect as a guest until one is saved
    if row is not None and any(k in out for k in ("host", "port", "nick", "account", "password_enc", "use_sasl")):
        out.update(auth_failed=0, last_error="")             # new details: a fresh attempt is allowed
    return out


@app.route("/irc")
@login_required
def irc_page():
    row = store.get_irc_account(get_db(), g.user["id"])
    data = {"settings": _irc_settings(row), "ports": "any" if config.IRC_ANY_PORT else sorted(config.IRC_ALLOWED_PORTS)[:30],
            "defaultServer": config.IRC_DEFAULT_SERVER}
    return render_template("irc.html", data=data, user=g.user)


@app.get("/api/irc")
@login_required
def api_irc():
    db = get_db()
    return jsonify(settings=_irc_settings(store.get_irc_account(db, g.user["id"])), status=_irc_status(db, g.user["id"]))


@app.post("/api/irc/settings")
@login_required
def api_irc_settings():
    data, db, uid = request.get_json(silent=True), get_db(), g.user["id"]
    if not isinstance(data, dict):
        return jsonify(error="Bad request."), 400
    if not limiter.allow(("irc-save", uid), 20, 600):
        return jsonify(error="Slow down a little."), 429
    row = store.get_irc_account(db, uid)
    try:
        fields = clean_irc_settings(data, row)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    store.save_irc_account(db, uid, fields)
    if data.get("connect"):
        store.save_irc_account(db, uid, {"autoconnect": 1})
        try:
            irc.start(uid)
        except ValueError as e:
            return jsonify(error=str(e)), 400
    return jsonify(ok=True, settings=_irc_settings(store.get_irc_account(db, uid)))


@app.post("/api/irc/connect")
@login_required
def api_irc_connect():
    db, uid = get_db(), g.user["id"]
    if store.get_irc_account(db, uid) is None:
        return jsonify(error="Save your IRC settings first."), 400
    store.save_irc_account(db, uid, {"autoconnect": 1, "auth_failed": 0, "last_error": ""})
    try:
        irc.start(uid)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    return jsonify(ok=True)


@app.post("/api/irc/disconnect")
@login_required
def api_irc_disconnect():
    irc.set_autoconnect(g.user["id"], False)
    irc.stop(g.user["id"])
    return jsonify(ok=True)


@app.post("/api/irc/register")
@login_required
def api_irc_register():
    """Sign up for a NickServ account: connect as a guest and send the REGISTER command (the password is the one saved
    in the connection settings; the email is used for this one command and not kept)."""
    data, db, uid = request.get_json(silent=True) or {}, get_db(), g.user["id"]
    row = store.get_irc_account(db, uid)
    email = str(data.get("email") or "").strip()
    if row is None or not row["password_enc"]:
        return jsonify(error="Save a nick and a password in the connection settings first."), 400
    if len(email) > 120 or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return jsonify(error="Enter a working email address (the network sends a verification code to it)."), 400
    if not limiter.allow(("irc-register", request.remote_addr), 5, 3600):
        return jsonify(error="Too many sign-up attempts. Try again later."), 429
    pw = crypto_box.decrypt(app.config["SECRET_KEY"], row["password_enc"])
    if pw is None:
        return jsonify(error="The saved password cannot be read. Enter it again and save."), 400
    store.save_irc_account(db, uid, {"use_sasl": 0, "autoconnect": 1, "auth_failed": 0, "last_error": ""})
    try:
        irc.start(uid, overrides={"sasl": False, "password": ""}, pending_register=(pw, email))
    except ValueError as e:
        return jsonify(error=str(e)), 400
    return jsonify(ok=True)


@app.post("/api/irc/verify")
@login_required
def api_irc_verify():
    code = str((request.get_json(silent=True) or {}).get("code") or "").strip()
    if not re.match(r"^[A-Za-z0-9]{3,64}$", code):
        return jsonify(error="That does not look like a verification code."), 400
    s = irc.get(g.user["id"])
    try:
        if s is None:
            raise NotConnected("Not connected to IRC.")
        s.verify(code)
    except (NotConnected, CommandError) as e:
        return jsonify(error=str(e)), 409
    return jsonify(ok=True)


@app.post("/api/irc/identify")
@login_required
def api_irc_identify():
    s = irc.get(g.user["id"])
    try:
        if s is None:
            raise NotConnected("Not connected to IRC.")
        s.identify()
    except (NotConnected, CommandError) as e:
        return jsonify(error=str(e)), 409
    return jsonify(ok=True)


# ---------------------------------------------------------------------------- theme sharing / gallery / sandbox
def _bundle_input(data):
    """Validated bundle built from a settings-like payload (what the settings page sends)."""
    return themes.make_bundle({**data, "custom_css": data.get("custom_css") if data.get("use_css", True) else ""},
                              include_js=bool(data.get("include_js")))


@app.post("/api/theme/export")
@login_required
def api_theme_export():
    try:
        bundle = _bundle_input(request.get_json(silent=True) or {})
    except ValueError as e:
        return jsonify(error=str(e)), 400
    return jsonify(ok=True, code=themes.encode_bundle(bundle), has_js=bool(bundle.get("js", "").strip()))


@app.post("/api/theme/import")
@login_required
def api_theme_import():
    try:
        bundle = themes.decode_bundle((request.get_json(silent=True) or {}).get("code"))
    except ValueError as e:
        return jsonify(error=str(e)), 400
    return jsonify(ok=True, bundle=bundle, has_js=bool(bundle.get("js", "").strip()))


@app.route("/gallery")
@login_required
def gallery_page():
    return render_template("gallery.html", user=g.user)


def _gallery_row(r, full=False):
    try:
        payload = json.loads(r["payload"])
    except ValueError:
        payload = {}
    d = {"id": r["id"], "title": r["title"], "description": r["description"], "author": r["author_name"],
         "author_id": r["author_id"], "created": r["created_at"], "has_js": bool(r["has_js"]),
         "theme": payload.get("theme"), "has_css": bool((payload.get("css") or "").strip()),
         "gimmicks": payload.get("gimmicks", []), "colors": None,
         "mine": r["author_id"] == g.user["id"]}
    if payload.get("theme") == "custom":
        v = payload.get("vars") or {}
        d["colors"] = [v.get(k) for k in ("bg", "panel", "accent", "text") if v.get(k)]
    if full:
        d["bundle"], d["code"] = payload, themes.encode_bundle(payload)
    return d


@app.get("/api/gallery")
@login_required
def api_gallery():
    rows = get_db().execute("SELECT * FROM shared_themes ORDER BY id DESC LIMIT 200").fetchall()
    cat = themes.catalog()["themes"]
    out = []
    for r in rows:
        d = _gallery_row(r)
        d["theme_name"] = (cat.get(d["theme"]) or {}).get("name", d["theme"])
        out.append(d)
    return jsonify(themes=out)


@app.get("/api/gallery/<int:tid>")
@login_required
def api_gallery_get(tid):
    r = get_db().execute("SELECT * FROM shared_themes WHERE id=?", (tid,)).fetchone()
    if r is None:
        return jsonify(error="No such theme."), 404
    return jsonify(ok=True, **_gallery_row(r, full=True))


@app.post("/api/gallery")
@login_required
def api_gallery_publish():
    data = request.get_json(silent=True) or {}
    title = " ".join(str(data.get("title") or "").split())[:config.MAX_GALLERY_TITLE]
    if not title:
        return jsonify(error="Give your theme a title."), 400
    desc = " ".join(str(data.get("description") or "").split())[:config.MAX_GALLERY_DESC]
    if not limiter.allow(("publish", g.user["id"]), 5, 600):
        return jsonify(error="Slow down a little."), 429
    try:
        bundle = _bundle_input({**data, "name": title})
    except ValueError as e:
        return jsonify(error=str(e)), 400
    tid = store.add_shared_theme(get_db(), g.user, title, desc, json.dumps(bundle), bool(bundle.get("js", "").strip()))
    return jsonify(ok=True, id=tid), 201


@app.delete("/api/gallery/<int:tid>")
@login_required
def api_gallery_delete(tid):
    db = get_db()
    r = db.execute("SELECT * FROM shared_themes WHERE id=?", (tid,)).fetchone()
    if r is None:
        return jsonify(error="No such theme."), 404
    if r["author_id"] != g.user["id"] and not g.user["is_admin"]:
        return jsonify(error="That is not your theme."), 403
    with db:
        db.execute("DELETE FROM shared_themes WHERE id=?", (tid,))
    if r["author_id"] != g.user["id"]:
        store.log_audit(db, g.user, "delete gallery theme", r["title"], "by " + r["author_name"])
    return jsonify(ok=True)


@app.route("/sandbox")
@login_required
def sandbox_page():
    s = user_settings(g.user)
    return render_template("sandbox.html", user=g.user, data={"theme": s["theme"], "js": s["custom_js"],
                                                              "use_js": s["use_custom_js"], "max": config.MAX_CUSTOM_JS})


# ---------------------------------------------------------------------------- PWA: install as an app
@app.get("/manifest.webmanifest")
def manifest():
    db = get_db()
    name = store.get_meta(db, "room_name")
    body = {"name": name, "short_name": name[:12], "start_url": "/room", "scope": "/", "display": "standalone",
            "orientation": "portrait-primary", "background_color": "#111111", "theme_color": "#111111",
            "description": "A chat room on your own network.",
            "icons": [{"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
                      {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
                      {"src": "/static/icons/maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}]}
    return Response(json.dumps(body), mimetype="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    resp = send_from_directory(os.path.join(config.BASE_DIR, "static", "js"), "sw.js", mimetype="text/javascript")
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/offline")
def offline_page():
    return render_template("offline.html")


# ---------------------------------------------------------------------------- admin: audit, backups, presets, events
@app.get("/api/admin/audit")
@admin_required
def admin_audit():
    before = _int(request.args.get("before"))
    sql, args = "SELECT * FROM audit", []
    if before:
        sql, args = sql + " WHERE id<?", [before]
    rows = get_db().execute(sql + " ORDER BY id DESC LIMIT 101", args).fetchall()
    return jsonify(more=len(rows) > 100, entries=[{"id": r["id"], "ts": r["ts"], "actor": r["actor_name"], "action": r["action"],
                                                   "target": r["target"], "detail": r["detail"]} for r in rows[:100]])


@app.get("/api/admin/maintenance")
@admin_required
def admin_maintenance():
    return jsonify(backups=maintenance.list_backups(), log_days=config.IRC_LOG_DAYS,
                   last_nightly=store.get_meta(get_db(), "last_nightly"), backups_enabled=config.BACKUPS_ENABLED,
                   backup_hour=config.BACKUP_HOUR)


@app.post("/api/admin/backup")
@admin_required
def admin_backup():
    name = maintenance.make_backup()
    store.log_audit(get_db(), g.user, "backup", name)
    return jsonify(ok=True, name=name)


@app.get("/api/admin/backups/<name>")
@admin_required
def admin_backup_download(name):
    p = maintenance.backup_path(name)
    if not p or not os.path.exists(p):
        return jsonify(error="No such backup."), 404
    store.log_audit(get_db(), g.user, "download backup", name)
    return send_from_directory(config.BACKUP_DIR, name, as_attachment=True)


@app.get("/api/admin/presets")
@admin_required
def admin_presets():
    cat = themes.catalog()
    return jsonify(presets=cat["presets"], themes=[{"id": t["id"], "name": t["name"]} for t in cat["themes"].values()],
                   gimmicks=cat["gimmicks"], customised=bool(store.get_meta(get_db(), "presets")))


@app.post("/api/admin/presets")
@admin_required
def admin_presets_save():
    data, db = request.get_json(silent=True) or {}, get_db()
    if data.get("reset"):
        store.set_meta(db, "presets", "")
        themes.apply_presets("")
        store.log_audit(db, g.user, "presets", "", "reset to presets.json")
    else:
        try:
            clean = themes.clean_presets(data.get("presets"))
        except ValueError as e:
            return jsonify(error=str(e)), 400
        store.set_meta(db, "presets", json.dumps(clean))
        themes.apply_presets(json.dumps(clean))
        store.log_audit(db, g.user, "presets", "", f"{len(clean)} presets saved")
    bump()
    return jsonify(ok=True, presets=themes.catalog()["presets"])


@app.get("/api/admin/event")
@admin_required
def admin_event_get():
    ev = active_event(get_db())
    return jsonify(event=ev)


@app.post("/api/admin/event")
@admin_required
def admin_event_set():
    data, db = request.get_json(silent=True) or {}, get_db()
    if data.get("off"):
        store.set_meta(db, "event", "")
        store.log_audit(db, g.user, "event theme", "", "switched off")
    else:
        p = themes.preset(data.get("preset"))
        if not p:
            return jsonify(error="Pick a preset for the event."), 400
        hours = max(0, min(24 * 60, _int(data.get("hours"), 0)))
        until = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat() if hours else ""
        title = " ".join(str(data.get("title") or "").split())[:40] or p["name"]
        store.set_meta(db, "event", json.dumps({"preset": p["id"], "title": title, "until": until}))
        store.log_audit(db, g.user, "event theme", p["name"], f"{hours} h" if hours else "until switched off")
    bump()
    return jsonify(ok=True, event=active_event(db))


# ---------------------------------------------------------------------------- startup
def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def banner():
    ip = lan_ip()
    conn = store.connect()
    try:
        first_run = needs_setup(conn)
    finally:
        conn.close()
    print("=" * 60)
    print(" Chat room is starting...")
    scheme = "https" if config.SSL_CERT and config.SSL_KEY else "http"
    print(f"   This device   : {scheme}://localhost:{config.PORT}/")
    print(f"   Other devices : {scheme}://{ip}:{config.PORT}/   (same Wi-Fi/LAN)")
    print(f"   Nightly backup: {'ON, after %02d:00 -> ' % config.BACKUP_HOUR + config.BACKUP_DIR if config.BACKUPS_ENABLED else 'OFF'}")
    print("   MacroDroid signal: " + ("ON -> " + _signal_url() if _signal_url() else "OFF"))
    print("   IRC: each account connects with its own login; " + ("accounts reconnect at start-up" if config.IRC_AUTOSTART else "auto-reconnect at start-up is OFF"))
    if not (config.SSL_CERT and config.SSL_KEY):
        print("   WARNING: plain http. IRC passwords are typed into this site, so turn on HTTPS (see README) before sharing the Wi-Fi.")
    if first_run:
        print(f"   FIRST RUN: open the site and create the admin account.  SETUP CODE: {SETUP_CODE}")
    print("=" * 60)


if __name__ == "__main__":
    banner()
    maintenance.start_background(bump)
    if config.IRC_AUTOSTART:
        irc.autostart()
    ssl_ctx = (config.SSL_CERT, config.SSL_KEY) if config.SSL_CERT and config.SSL_KEY else None
    app.run(host=config.HOST, port=config.PORT, threaded=True, debug=False, ssl_context=ssl_ctx)
