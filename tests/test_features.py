"""Tests for the look-and-feel, admin and reliability features that stay in the IRC client."""
import _env
import json
import os
import re
import sqlite3
import tempfile
import time
import unittest
import zlib

import test_app as base                      # noqa: E402  (sets up the temp database and imports the app)
from test_app import PW, form_post, give_irc, jpost, register, room   # noqa: E402

import config                                # noqa: E402
import db as store                           # noqa: E402
import themes                                # noqa: E402


def admin_client():
    c = room.app.test_client()
    if store.connect().execute("SELECT 1 FROM users WHERE is_admin=1 AND password_hash IS NOT NULL").fetchone() is None:
        form_post(c, "/setup", {"code": room.SETUP_CODE, "username": "boss", "display_name": "Boss", "password": PW, "confirm": PW})
    else:
        form_post(c, "/login", {"username": "boss", "password": PW})
    return c


def sync(c, rev=0):
    return c.get(f"/api/sync?rev={rev}").get_json()


def uid_of(name):
    return store.connect().execute("SELECT id FROM users WHERE username=?", (name,)).fetchone()["id"]


def member(name, display=None, **extra):
    c = register(name, display or name, **extra)
    give_irc(name)
    return c


class Features(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.admin = admin_client()
        cls.u1 = member("fax", "Fa Alpha")

    def setUp(self):
        room.limiter._hits.clear()

    def test_10_settings_validation(self):
        c = member("looker", "Looker")
        self.assertEqual(jpost(c, "/api/settings", {"msg_effect": "sparkle", "show_effects": False}).status_code, 200)
        s = c.get("/settings").data.decode()
        self.assertIn('"msg_effect": "sparkle"', s)
        self.assertEqual(jpost(c, "/api/settings", {"msg_effect": "explode"}).status_code, 400)
        body = c.get("/room").data.decode()
        self.assertIn('"showEffects": false', body)
        self.assertIn('"myEffect": "sparkle"', body)                  # the effect is a local, per-browser choice
        self.assertEqual(jpost(c, "/api/settings", {"schedule": [{"preset": "nope", "from": "00:00", "to": "05:00"}]}).status_code, 400)
        self.assertEqual(jpost(c, "/api/settings", {"schedule": [{"preset": "watcher", "from": "25:00", "to": "05:00"}]}).status_code, 400)
        self.assertEqual(jpost(c, "/api/settings", {"schedule": [{"preset": "watcher", "from": "05:00", "to": "05:00"}]}).status_code, 400)

    def test_11_scheduled_look(self):
        c = member("nightowl", "Night Owl")
        jpost(c, "/api/settings", {"theme": "classic", "schedule": [{"preset": "watcher", "from": "00:00", "to": "05:00"}]})
        real = room.local_minutes
        try:
            for mins, theme in ((2 * 60, "dream"), (12 * 60, "classic")):
                room.local_minutes = lambda m=mins: m                # noqa: E731
                body = c.get("/room").data.decode()
                self.assertIn(f'data-theme="{theme}"', body, mins)
                self.assertEqual("glitch-title.js" in body, theme == "dream")
                self.assertIn('"activeRule": ' + ("0" if theme == "dream" else "null"), body)
        finally:
            room.local_minutes = real
        with room.app.test_request_context("/", headers={"Cookie": "tzo=-120"}):          # the viewer's own clock is used
            utc = time.gmtime()
            self.assertEqual(room.local_minutes(), (utc.tm_hour * 60 + utc.tm_min + 120) % 1440)
        self.assertEqual(themes.active_rule([{"preset": "watcher", "from": "22:00", "to": "06:00"}], 23 * 60), 0)   # crosses midnight
        self.assertEqual(themes.active_rule([{"preset": "watcher", "from": "22:00", "to": "06:00"}], 12 * 60), None)

    def test_12_event_theme(self):
        c = member("evfan", "Ev Fan")
        c2 = member("evskeptic", "Ev Skeptic")
        jpost(c2, "/api/settings", {"follow_events": False})
        self.assertEqual(jpost(self.admin, "/api/admin/event", {"preset": "nope"}).status_code, 400)
        self.assertEqual(jpost(self.admin, "/api/admin/event", {"preset": "spooky", "hours": 2, "title": "Spooky night"}).status_code, 200)
        body = c.get("/room").data.decode()
        self.assertIn('data-theme="spooky"', body)
        self.assertIn("Spooky night", body)
        self.assertEqual(sync(c)["event"]["id"], "spooky")
        self.assertNotIn('data-theme="spooky"', c2.get("/room").data.decode())                             # opted out
        self.assertEqual(jpost(c, "/api/admin/event", {"off": True}).status_code, 403)
        store.set_meta(store.connect(), "event", json.dumps({"preset": "frost", "title": "t", "until": "2000-01-01T00:00:00+00:00"}))
        self.assertIsNone(sync(c)["event"])                                                                # it switches itself off
        jpost(self.admin, "/api/admin/event", {"preset": "frost", "hours": 0})
        self.assertIn('data-theme="frost"', c.get("/room").data.decode())
        jpost(self.admin, "/api/admin/event", {"off": True})
        self.assertNotIn('data-theme="frost"', c.get("/room").data.decode())

    def test_13_share_and_gallery(self):
        c, d = register("sharer", "Sharer"), register("taker", "Taker")
        payload = {"theme": "custom", "gimmicks": ["soft-chime", "bogus"], "theme_vars": {"accent": "#ff00aa", "radius": 3},
                   "labels": {"room_title": "My Place"}, "custom_intro": "hi", "custom_css": ".msg{outline:1px solid red}",
                   "custom_js": "console.log('hi')"}
        r = jpost(c, "/api/theme/export", payload).get_json()
        self.assertTrue(r["code"].startswith("ROOMTHEME1:") and not r["has_js"])
        b = jpost(d, "/api/theme/import", {"code": r["code"]}).get_json()["bundle"]
        self.assertEqual((b["theme"], b["gimmicks"], b["vars"]["accent"], b["labels"], b["css"]), ("custom", ["soft-chime"], "#ff00aa", {"room_title": "My Place"}, ".msg{outline:1px solid red}"))
        self.assertNotIn("js", b)
        withjs = jpost(c, "/api/theme/export", {**payload, "include_js": True}).get_json()
        self.assertTrue(withjs["has_js"])
        self.assertEqual(jpost(d, "/api/theme/import", {"code": withjs["code"]}).get_json()["bundle"]["js"], "console.log('hi')")
        for bad in ("hello", "ROOMTHEME1:!!!", "ROOMTHEME1:" + r["code"][11:30]):
            self.assertEqual(jpost(d, "/api/theme/import", {"code": bad}).status_code, 400, bad)
        evil = themes.encode_bundle({"v": 1, "theme": "custom", "vars": {"bg": "red;background:url(x)"}})      # tampered contents are re-validated
        self.assertEqual(jpost(d, "/api/theme/import", {"code": evil}).status_code, 400)
        bomb = "ROOMTHEME1:" + __import__("base64").urlsafe_b64encode(zlib.compress(b"0" * 5_000_000)).decode()
        self.assertEqual(jpost(d, "/api/theme/import", {"code": bomb}).status_code, 400)
        self.assertEqual(jpost(c, "/api/gallery", {**payload, "title": ""}).status_code, 400)
        pub = jpost(c, "/api/gallery", {**payload, "title": "Pink Place", "description": "pink!"})
        self.assertEqual(pub.status_code, 201)
        tid = pub.get_json()["id"]
        lst = d.get("/api/gallery").get_json()["themes"]
        self.assertEqual([(t["title"], t["author"], t["has_js"], t["mine"]) for t in lst if t["id"] == tid], [("Pink Place", "Sharer", False, False)])
        full = d.get(f"/api/gallery/{tid}").get_json()
        self.assertEqual(themes.decode_bundle(full["code"]), full["bundle"])
        self.assertEqual(jpost(d, f"/api/gallery/{tid}", method="delete").status_code, 403)
        self.assertEqual(jpost(c, f"/api/gallery/{tid}", method="delete").status_code, 200)
        self.assertEqual(d.get("/gallery").status_code, 200)
        self.assertEqual(d.get("/sandbox").status_code, 200)

    def test_16_audit_log(self):
        c = register("auditee", "Auditee")
        give_irc("auditee")
        jpost(self.admin, f"/api/admin/users/{uid_of('auditee')}/disable", {"message": "bye"})
        jpost(self.admin, f"/api/admin/users/{uid_of('auditee')}/enable")
        log = self.admin.get("/api/admin/audit").get_json()["entries"]
        acts = [(e["actor"], e["action"], e["target"]) for e in log]
        self.assertIn(("Boss", "disable", "Auditee"), acts)
        self.assertIn(("Boss", "enable", "Auditee"), acts)
        self.assertEqual(c.get("/api/admin/audit").status_code, 403)
        self.assertGreater(log[0]["ts"], log[-1]["ts"])                                                     # newest first
        self.assertNotIn(PW, json.dumps(log))                                                               # never logs passwords
        jpost(self.admin, f"/api/admin/users/{uid_of('auditee')}/password", {"password": "hunter22-xx"})
        self.assertNotIn("hunter22", json.dumps(self.admin.get("/api/admin/audit").get_json()))

    def test_18_backup(self):
        db = store.connect()
        rid, _ = store.ensure_buffer(db, uid_of("fax"), "#backup", "channel")
        store.add_irc_message(db, rid, "user", "bob", "keep me 😀")
        r = jpost(self.admin, "/api/admin/backup")
        name = r.get_json()["name"]
        self.assertIn(name, [b["name"] for b in self.admin.get("/api/admin/maintenance").get_json()["backups"]])
        raw = self.admin.get(f"/api/admin/backups/{name}").data
        self.assertTrue(raw.startswith(b"SQLite format 3"))
        self.assertEqual(self.admin.get("/api/admin/backups/..%2Fchat.db").status_code, 404)
        self.assertEqual(self.u1.get(f"/api/admin/backups/{name}").status_code, 403)
        copy = sqlite3.connect(os.path.join(config.BACKUP_DIR, name))
        self.assertGreater(copy.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)
        copy.close()
        import maintenance
        for i in range(config.BACKUP_KEEP + 3):                     # old copies are pruned
            with open(os.path.join(config.BACKUP_DIR, f"chat-2020010{i % 10}-0000{i:02d}.db"), "wb") as fh:
                fh.write(b"x")
        maintenance.prune_backups()
        self.assertEqual(len(maintenance.list_backups()), config.BACKUP_KEEP)
        self.assertIn("log_lines_removed", maintenance.nightly())                                           # nightly also trims old IRC logs

    def test_19_editable_presets(self):
        original = self.admin.get("/api/admin/presets").get_json()
        self.assertTrue(any(p["id"] == "watcher" for p in original["presets"]))
        mine = [{"id": "neon", "name": "Neon Night", "description": "d", "theme": "vapor", "gimmicks": ["soft-chime", "bogus"], "name_color": "#ff00ff"}]
        self.assertEqual(jpost(self.admin, "/api/admin/presets", {"presets": mine}).status_code, 200)
        self.assertEqual([p["id"] for p in themes.catalog()["presets"]], ["neon"])
        self.assertEqual(themes.catalog()["presets"][0]["gimmicks"], ["soft-chime"])
        reg = room.app.test_client().get("/register").data.decode()
        self.assertIn("Neon Night", reg)
        self.assertNotIn("Maxwell", reg)
        n = member("newbie", "Newbie", preset="neon")
        self.assertIn('data-theme="vapor"', n.get("/room").data.decode())
        for bad in ([], [{"id": "Bad Id", "name": "x", "theme": "vapor"}], [{"id": "ok", "name": "x", "theme": "nope"}],
                    [{"id": "dup", "name": "a", "theme": "vapor"}, {"id": "dup", "name": "b", "theme": "vapor"}]):
            self.assertEqual(jpost(self.admin, "/api/admin/presets", {"presets": bad}).status_code, 400, bad)
        self.assertEqual(jpost(self.u1, "/api/admin/presets", {"presets": mine}).status_code, 403)
        themes.apply_presets(store.get_meta(store.connect(), "presets"))                                      # survives a restart
        self.assertEqual([p["id"] for p in themes.catalog()["presets"]], ["neon"])
        self.assertEqual(jpost(self.admin, "/api/admin/presets", {"reset": True}).status_code, 200)
        self.assertTrue(any(p["id"] == "watcher" for p in themes.catalog()["presets"]))

    def test_20_pwa_and_limits(self):
        anon = room.app.test_client()
        m = anon.get("/manifest.webmanifest")
        self.assertEqual(m.status_code, 200)
        man = json.loads(m.data)
        self.assertEqual((man["display"], man["start_url"]), ("standalone", "/room"))
        for icon in man["icons"]:
            self.assertEqual(anon.get(icon["src"]).status_code, 200)
        sw = anon.get("/sw.js")
        self.assertEqual((sw.status_code, sw.headers["Service-Worker-Allowed"]), (200, "/"))
        self.assertEqual(anon.get("/offline").status_code, 200)
        body = self.u1.get("/room").data.decode()
        self.assertIn('rel="manifest"', body)
        self.assertIn("apple-mobile-web-app-capable", body)
        big = self.u1.post("/api/typing", data=b"x" * (300 * 1024), headers={"X-CSRF-Token": base.tok(self.u1), "Content-Type": "application/json"})
        self.assertEqual(big.status_code, 413)                       # nothing needs a large request body any more

    def test_21_xss_and_escaping(self):
        c = member("xss", "<img src=x>")
        jpost(c, "/api/settings", {"custom_intro": "<script>alert(1)</script>"})
        body = c.get("/room").data.decode()
        self.assertEqual(body.count("</script>"), len(re.findall(r"<script", body)))
        for f in ("room.js", "settings.js", "admin.js", "gallery.js", "sandbox.js", "irc.js"):
            with open(os.path.join(config.BASE_DIR, "static", "js", f), encoding="utf-8") as fh:
                src = fh.read()
            self.assertNotRegex(src.replace("insertAdjacentHTML", ""), r"\.innerHTML\s*=", f)


class UpgradeMigration(unittest.TestCase):
    def test_pre_features_database_is_upgraded_in_place(self):
        d = tempfile.mkdtemp(prefix="room-upgrade-")
        path = os.path.join(d, "chat.db")
        c = sqlite3.connect(path)
        c.executescript("""
          CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            display_name TEXT NOT NULL UNIQUE COLLATE NOCASE, password_hash TEXT, is_admin INTEGER NOT NULL DEFAULT 0,
            is_disabled INTEGER NOT NULL DEFAULT 0, disabled_message TEXT NOT NULL DEFAULT '', sv INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL, last_seen TEXT, name_color TEXT NOT NULL DEFAULT '', status_text TEXT NOT NULL DEFAULT '',
            theme TEXT NOT NULL DEFAULT 'classic', gimmicks TEXT NOT NULL DEFAULT '[]', theme_vars TEXT NOT NULL DEFAULT '{}',
            labels TEXT NOT NULL DEFAULT '{}', custom_css TEXT NOT NULL DEFAULT '', custom_js TEXT NOT NULL DEFAULT '',
            custom_intro TEXT NOT NULL DEFAULT '', use_custom_css INTEGER NOT NULL DEFAULT 1, use_custom_js INTEGER NOT NULL DEFAULT 1,
            intro_mode TEXT NOT NULL DEFAULT 'once', show_name_colors INTEGER NOT NULL DEFAULT 1);
          CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, author_snapshot TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL DEFAULT 'user', message TEXT NOT NULL, timestamp TEXT NOT NULL, edited_at TEXT,
            deleted INTEGER NOT NULL DEFAULT 0, rev INTEGER NOT NULL DEFAULT 0);
          CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
          INSERT INTO users (username, display_name, password_hash, created_at) VALUES ('old', 'Old', 'x', '2026-01-01T00:00:00+00:00');
          INSERT INTO messages (user_id, message, timestamp, rev) VALUES (1, 'from before', '2026-01-01T00:00:00+00:00', 1);
        """)
        c.commit(); c.close()
        old = (config.DB_DIR, config.DB_PATH)
        config.DB_DIR, config.DB_PATH = d, path
        try:
            store.init_db(); store.init_db()                         # idempotent
            db = store.connect()
            self.assertEqual(db.execute("SELECT COUNT(*) FROM rooms WHERE owner_id IS NOT NULL").fetchone()[0], 0)
            m = db.execute("SELECT room_id, mentions, reply_to, sender, own, msgid FROM messages").fetchone()
            self.assertEqual((m["room_id"], m["mentions"], m["reply_to"], m["sender"], m["own"], m["msgid"]), (1, "[]", None, "", 0, ""))
            u = db.execute("SELECT bio, msg_effect, show_effects, schedule FROM users").fetchone()
            self.assertEqual(tuple(u), ("", "none", 1, "[]"))
            self.assertEqual(store.visible_rooms(db, {"id": 1}), [])                    # old local-chat history stays in the file, hidden
            rid, _ = store.ensure_buffer(db, 1, "#new", "channel")                    # and the new IRC tables work on the upgraded file
            store.add_irc_message(db, rid, "user", "bob", "after upgrade")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 2)
            db.close()
        finally:
            config.DB_DIR, config.DB_PATH = old


if __name__ == "__main__":
    unittest.main()
