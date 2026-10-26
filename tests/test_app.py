"""Run with:  python -m unittest discover -s tests -v   (uses a throw-away database, never touches yours)"""
import json
import os
import re
import sqlite3
import sys
import tempfile
import unittest

import _env
TMP = _env.TMP

import app as room          # noqa: E402
import config               # noqa: E402
import db as store          # noqa: E402


def tok(c):
    with c.session_transaction() as s:
        return s.get("csrf", "")


def jpost(c, url, data=None, method="post"):
    return getattr(c, method)(url, json=data if data is not None else {}, headers={"X-CSRF-Token": tok(c)})


def form_post(c, url, data):
    c.get(url)                                   # makes sure the session has a csrf token
    return c.post(url, data={**data, "csrf": tok(c)})


PW = "secret-pass-1"


def register(name, display=None, pw=PW, **extra):
    room.limiter._hits.pop(("register", "127.0.0.1"), None)     # every test client shares one IP
    c = room.app.test_client()
    r = form_post(c, "/register", {"username": name, "display_name": display or name, "password": pw, "confirm": pw, **extra})
    assert r.status_code == 302, re.findall(r'login-error[^>]*>([^<]*)', r.data.decode())
    return c


def give_irc(name):
    """Give a local account IRC settings (without connecting) so that /room renders instead of sending it to /irc."""
    db = store.connect()
    uid = store.get_user_by_name(db, name)["id"]
    store.save_irc_account(db, uid, {"host": "irc.example.org", "port": 6697, "nick": name, "autoconnect": 0, "use_sasl": 0})
    db.close()
    return uid


class Flow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        room.limiter._hits.clear()
        cls.admin = room.app.test_client()

    def test_01_setup_and_admin(self):
        c = self.admin
        self.assertIn("/setup", c.get("/").headers["Location"])
        r = form_post(c, "/setup", {"code": "WRONG", "username": "boss", "password": PW, "confirm": PW})
        self.assertIn(b"Wrong setup code", r.data)
        r = form_post(c, "/setup", {"code": room.SETUP_CODE, "username": "boss", "display_name": "Boss", "password": PW, "confirm": PW})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(c.get("/admin").status_code, 200)
        self.assertEqual(c.get("/setup").status_code, 302)          # closed once an admin exists

    def test_02_register_login_room(self):
        alice = register("alice", "Alice", preset="gabby")
        self.assertIn("/irc", alice.get("/room").headers["Location"])           # no IRC login yet: set that up first
        give_irc("alice")
        body = alice.get("/room").data.decode()
        self.assertIn('data-theme="gabby"', body)
        self.assertIn("status-chips.js", body)
        self.assertEqual(alice.get("/admin").status_code, 302)       # not an admin
        self.assertEqual(alice.get("/api/admin/users").status_code, 403)
        c = room.app.test_client()                                    # wrong, then right password
        self.assertIn(b"Wrong username", form_post(c, "/login", {"username": "alice", "password": "nope"}).data)
        self.assertEqual(form_post(c, "/login", {"username": "alice", "password": PW}).status_code, 302)
        dup = room.app.test_client()
        self.assertIn(b"already taken", form_post(dup, "/register", {"username": "ALICE", "display_name": "x", "password": PW, "confirm": PW}).data)
        self.assertIn(b"do not match", form_post(dup, "/register", {"username": "zed", "password": PW, "confirm": "other"}).data)
        self.assertIn(b"at least", form_post(dup, "/register", {"username": "zed", "password": "short", "confirm": "short"}).data)

    def test_03_everything_needs_a_login_and_a_csrf_token(self):
        c = register("guarded1")
        give_irc("guarded1")
        self.assertEqual(c.post("/api/messages", json={"text": "no token", "room": 1}).status_code, 400)
        self.assertEqual(c.post("/api/irc/settings", json={"nick": "x"}).status_code, 400)
        anon = room.app.test_client()
        for url in ("/api/sync", "/api/history?room=1", "/api/search?q=hello", "/api/irc"):
            self.assertEqual(anon.get(url).status_code, 401, url)
        for url in ("/api/messages", "/api/irc/settings", "/api/irc/connect", "/api/irc/register", "/api/ignore", "/api/query"):
            self.assertIn(anon.post(url, json={}, headers={"X-CSRF-Token": "x"}).status_code, (400, 401), url)
        d = c.get("/api/sync?rev=0").get_json()
        self.assertEqual((d["rooms"], d["messages"], d["irc"]["state"]), ([], [], "stopped"))   # nothing yet: not connected
        self.assertEqual(jpost(c, "/api/messages", {"text": "hi", "room": 12345}).status_code, 404)

    def test_05_settings_and_custom_code(self):
        c = register("styler", "Styler")
        give_irc("styler")
        r = jpost(c, "/api/settings", {"theme": "custom", "theme_vars": {"bg": "#101820", "accent": "#ff00aa", "font": "mono", "radius": 4},
                                       "gimmicks": ["soft-chime", "not-real"], "custom_css": "body{outline:1px solid red}",
                                       "custom_js": "console.log(1)", "custom_intro": "welcome home"})
        self.assertEqual(r.status_code, 200, r.data)
        s = r.get_json()["settings"]
        self.assertEqual(s["gimmicks"], ["soft-chime"])
        body = c.get("/room").data.decode()
        self.assertIn("--accent:#ff00aa", body)
        self.assertIn("/me/custom.css", body)
        self.assertIn("/me/custom.js", body)
        self.assertIn("welcome home", body)
        self.assertIn("outline:1px", c.get("/me/custom.css").data.decode())
        safe = c.get("/room?safe=1").data.decode()
        self.assertNotIn("/me/custom.css", safe)
        self.assertNotIn("/me/custom.js", safe)
        self.assertEqual(jpost(c, "/api/settings", {"theme_vars": {"bg": "red; background:url(x)"}}).status_code, 400)
        self.assertEqual(jpost(c, "/api/settings", {"name_color": "javascript:1"}).status_code, 400)
        self.assertEqual(jpost(c, "/api/settings", {"theme": "nope"}).status_code, 400)
        self.assertEqual(jpost(c, "/api/settings", {"display_name": "Alice"}).status_code, 400)      # taken
        jpost(c, "/api/settings", {"use_custom_css": False})
        self.assertEqual(c.get("/me/custom.css").data, b"")
        other = register("viewer")
        give_irc("viewer")
        self.assertNotIn("--accent:#ff00aa", other.get("/room").data.decode())                       # only I see my theme
        self.assertEqual(c.get("/settings").status_code, 200)

    def test_06_preview_is_robust(self):
        c = register("previewer")
        for q in ("", "?theme=dream&g=glitch-title,retro-homepage", "?theme=custom&v=%7Bbad", "?theme=zzz&g=../../x&l=[1]"):
            self.assertEqual(c.get("/preview" + q).status_code, 200, q)
        self.assertIn('"preview": true', c.get("/preview?theme=mac").data.decode())

    def test_07_html_is_escaped(self):
        c = register("evil", "<b>X</b>")
        give_irc("evil")
        body = c.get("/room").data.decode()
        self.assertNotIn("<b>X</b>", body)
        self.assertEqual(body.count("</script>"), len(re.findall(r"<script", body)))

    def test_08_disable_enable_delete(self):
        v = register("victim", "Victim")
        uid = give_irc("victim")
        self.assertEqual(jpost(self.admin, f"/api/admin/users/{uid}/disable", {"message": "Cooling off."}).status_code, 200)
        r = v.get("/api/sync?rev=0")
        self.assertEqual((r.status_code, r.get_json()["disabled"]), (403, True))
        self.assertEqual(jpost(v, "/api/messages", {"text": "still here?", "room": 1}).status_code, 403)
        self.assertIn(b"Cooling off.", v.get("/room", follow_redirects=True).data)
        self.assertEqual(jpost(self.admin, f"/api/admin/users/{uid}/enable").status_code, 200)
        self.assertEqual(v.get("/api/sync?rev=0").status_code, 200)
        self.assertEqual(jpost(self.admin, "/api/admin/users/1/disable").status_code, 400)             # not yourself
        self.assertEqual(jpost(self.admin, f"/api/admin/users/{uid}", {}, "delete").status_code, 200)
        self.assertIsNone(store.get_irc_account(store.connect(), uid))                                 # saved IRC login is gone too
        self.assertEqual(v.get("/api/sync").status_code, 401)                                           # their session is gone

    def test_09_registration_controls(self):
        self.assertEqual(jpost(self.admin, "/api/admin/settings", {"invite_code": "Cookie", "registration_open": True}).status_code, 200)
        c = room.app.test_client()
        r = form_post(c, "/register", {"username": "late", "password": PW, "confirm": PW, "invite": "wrong"})
        self.assertIn(b"invite code", r.data)
        r = form_post(c, "/register", {"username": "late", "password": PW, "confirm": PW, "invite": "cookie"})
        self.assertEqual(r.status_code, 302)
        jpost(self.admin, "/api/admin/settings", {"registration_open": False, "invite_code": ""})
        self.assertIn(b"closed", room.app.test_client().get("/register").data)
        jpost(self.admin, "/api/admin/settings", {"registration_open": True})

    def test_10_password_change_signs_out_others(self):
        a, b = register("pwuser"), room.app.test_client()
        form_post(b, "/login", {"username": "pwuser", "password": PW})
        self.assertEqual(jpost(a, "/api/password", {"current": "bad", "new": "newpass-123"}).status_code, 400)
        self.assertEqual(jpost(a, "/api/password", {"current": PW, "new": "newpass-123"}).status_code, 200)
        self.assertEqual(a.get("/api/sync").status_code, 200)
        self.assertEqual(b.get("/api/sync").status_code, 401)

    def test_11_admin_tools(self):
        jpost(self.admin, "/api/admin/settings", {"room_name": "Den"})
        c = register("seer")
        give_irc("seer")
        self.assertEqual(c.get("/api/sync?rev=0").get_json()["room"]["name"], "Den")           # until a network name is known
        for gone in ("/api/admin/announce", "/api/admin/clear", "/api/admin/rooms", "/api/admin/export"):
            self.assertIn(self.admin.post(gone, json={}, headers={"X-CSRF-Token": base_tok(self.admin)}).status_code, (404, 405), gone)
        users = self.admin.get("/api/admin/users").get_json()["users"]
        me = [u for u in users if u["username"] == "seer"][0]
        self.assertEqual((me["irc"], me["irc_state"]), ("seer on irc.example.org", "stopped"))
        self.assertNotIn("password", json.dumps(users).lower().replace("has_password", ""))

    def test_12_stream(self):
        a = register("streamer")
        resp = a.get("/api/stream", buffered=False)
        it = iter(resp.response)
        self.assertIn(b"retry", next(it))
        self.assertIn(b"data:", next(it))
        resp.close()


def base_tok(c):
    return tok(c)


class Migration(unittest.TestCase):
    def test_old_database_is_migrated(self):
        d = tempfile.mkdtemp(prefix="room-legacy-")
        path = os.path.join(d, "chat.db")
        c = sqlite3.connect(path)
        c.executescript("""CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, sender TEXT NOT NULL, message TEXT NOT NULL,
                           timestamp TEXT NOT NULL, is_active INTEGER NOT NULL DEFAULT 1);
                           CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);""")
        c.executemany("INSERT INTO messages (sender, message, timestamp) VALUES (?,?,?)",
                      [("sophia", "hi", "2026-01-01T00:00:00+00:00"), ("terminal", "link ok", "2026-01-01T00:01:00+00:00"),
                       ("sophia", "again", "2026-01-01T00:02:00+00:00")])
        c.commit(); c.close()
        old = (config.DB_DIR, config.DB_PATH)
        config.DB_DIR, config.DB_PATH = d, path
        try:
            store.init_db()
            store.init_db()                      # idempotent
            db = store.connect()
            names = {r["username"]: r for r in db.execute("SELECT * FROM users")}
            self.assertEqual(set(names), {"sophia", "terminal"})
            self.assertIsNone(names["sophia"]["password_hash"])
            rows = db.execute("SELECT m.id, u.display_name, m.message, m.rev FROM messages m JOIN users u ON u.id=m.user_id ORDER BY m.id").fetchall()
            self.assertEqual([(r["id"], r["display_name"], r["message"]) for r in rows], [(1, "GABBY", "hi"), (2, "MAXWELL", "link ok"), (3, "GABBY", "again")])
            self.assertEqual([r["rev"] for r in rows], [1, 2, 3])
            self.assertTrue(os.path.exists(path + ".pre-accounts.bak"))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='messages_old'").fetchone()[0], 0)
            db.close()
        finally:
            config.DB_DIR, config.DB_PATH = old


if __name__ == "__main__":
    unittest.main()
