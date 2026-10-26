"""App-level tests: local accounts, the IRC connection page, sync, sending, privacy between accounts, feature gating.
Run with:  python -m unittest discover -s tests -v
"""
import json
import os
import re
import sys
import tempfile
import time
import unittest

import _env
TMP = _env.TMP

import app as room                 # noqa: E402
import config                      # noqa: E402
import db as store                 # noqa: E402
from fake_irc import FakeIRC       # noqa: E402

PW = "secret-pass-1"


def tok(c):
    with c.session_transaction() as s:
        return s.get("csrf", "")


def jpost(c, url, data=None, method="post"):
    return getattr(c, method)(url, json=data if data is not None else {}, headers={"X-CSRF-Token": tok(c)})


def form_post(c, url, data):
    c.get(url)
    return c.post(url, data={**data, "csrf": tok(c)})


def register(name, pw=PW, **extra):
    room.limiter._hits.clear()
    c = room.app.test_client()
    r = form_post(c, "/register", {"username": name, "display_name": name, "password": pw, "confirm": pw, **extra})
    assert r.status_code == 302, re.findall(r'login-error[^>]*>([^<]*)', r.data.decode())
    return c


def uid_of(name):
    db = store.connect()
    try:
        return store.get_user_by_name(db, name)["id"]
    finally:
        db.close()


def wait(pred, timeout=6):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(0.03)
    return False


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        room.limiter._hits.clear()
        admin = room.app.test_client()
        if room.needs_setup(store.connect()):
            form_post(admin, "/setup", {"code": room.SETUP_CODE, "username": "boss", "display_name": "Boss",
                                        "password": PW, "confirm": PW})
        else:
            form_post(admin, "/login", {"username": "boss", "password": PW})
        cls.admin = admin

    def setUp(self):
        self.srv = FakeIRC()
        self.users = []

    def tearDown(self):
        for u in self.users:
            room.irc.remove(uid_of(u))
        self.srv.stop()

    def new_user(self, name, connect=True, nick=None, password="pw-alice", account=None, srv=None, **extra):
        c = register(name)
        self.users.append(name)
        srv = srv or self.srv
        if "alice" in (nick or name) and account is None:
            account = "alice"
        body = {"server": "127.0.0.1", "port": srv.port, "tls": False, "nick": nick or name, "account": account or "",
                "password": password, "use_sasl": True, "connect": connect, **extra}
        r = jpost(c, "/api/irc/settings", body)
        assert r.status_code == 200, r.get_json()
        if connect:
            s = room.irc.get(uid_of(name))
            assert wait(lambda: s.ready() and s.client.online), s.status()
        return c

    def sync(self, c, rev=0, room_id=None, nv=None):
        q = f"/api/sync?rev={rev}" + (f"&room={room_id}" if room_id else "") + (f"&nv={nv}" if nv is not None else "")
        r = c.get(q)
        self.assertEqual(r.status_code, 200)
        return r.get_json()

    def rooms(self, c):
        return {r["name"]: r for r in self.sync(c)["rooms"]}

    def send(self, c, room_id, text, **extra):
        room.limiter._hits.clear()                      # the tests send faster than a person would
        return jpost(c, "/api/messages", {"room": room_id, "text": text, **extra})

    def status_room(self, c):
        return next(r["id"] for r in self.sync(c)["rooms"] if r["kind"] == "status")

    def join(self, c, chan):
        sid = wait(lambda: next((r["id"] for r in self.sync(c)["rooms"] if r["kind"] == "status"), None))
        r = self.send(c, sid, f"/join {chan}")
        self.assertEqual(r.status_code, 200, r.get_json())
        rid = wait(lambda: self.rooms(c).get(chan, {}).get("id"))
        self.assertTrue(rid, "channel window never appeared")
        self.assertTrue(wait(lambda: self.rooms(c)[chan]["joined"]))
        return rid


class Flow(Base):
    def test_first_visit_goes_to_irc_setup_then_room(self):
        c = register("flowuser")
        self.users.append("flowuser")
        self.assertIn("/irc", c.get("/room").headers["Location"])
        page = c.get("/irc")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"irc.libera.chat", page.data)

    def test_save_settings_connects_with_sasl_and_stores_encrypted_password(self):
        c = self.new_user("alice1", nick="alice", account="alice")
        s = room.irc.get(uid_of("alice1"))
        self.assertTrue(s.client.authenticated)
        self.assertEqual(s.status()["account"], "alice")
        raw = b""
        for suffix in ("", "-wal"):                                         # the database file and its write-ahead log
            if os.path.exists(config.DB_PATH + suffix):
                with open(config.DB_PATH + suffix, "rb") as fh:
                    raw += fh.read()
        self.assertGreater(len(raw), 1000)
        self.assertNotIn(b"pw-alice", raw)                                  # the password is not in the database file
        row = store.get_irc_account(store.connect(), uid_of("alice1"))
        self.assertTrue(row["password_enc"].startswith("v1:"))
        self.assertEqual(c.get("/room").status_code, 200)
        info = c.get("/api/irc").get_json()
        self.assertTrue(info["settings"]["has_password"])
        self.assertNotIn("password_enc", json.dumps(info))
        self.assertNotIn("pw-alice", json.dumps(info))

    def test_wrong_password_is_reported_and_not_retried_or_restarted(self):
        c = self.new_user("alice2", nick="alice", account="alice", password="bad", connect=False)
        r = jpost(c, "/api/irc/connect")
        self.assertEqual(r.status_code, 200)
        uid = uid_of("alice2")
        row = wait(lambda: (lambda x: x if x["auth_failed"] else None)(store.get_irc_account(store.connect(), uid)))
        self.assertTrue(row, "auth_failed was never saved")
        st = c.get("/api/irc").get_json()["status"]
        self.assertEqual(st["state"], "failed")
        self.assertIn("rejected", st["error"])
        self.assertEqual(len([x for x in self.srv.received if x.startswith("AUTHENTICATE") and x != "AUTHENTICATE PLAIN"]), 1)
        # a restart must not try again with the known-bad password
        room.irc.remove(uid)
        self.assertNotIn(uid, room.irc.autostart())
        # fixing the password clears the failure and connects
        r = jpost(c, "/api/irc/settings", {"password": "pw-alice", "connect": True})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(wait(lambda: room.irc.get(uid).ready() and room.irc.get(uid).client.online))

    def test_channel_messages_reach_the_browser_and_back(self):
        c = self.new_user("alice3", nick="alice", account="alice")
        self.srv.join_as("bob", "#chat", "@")
        rid = self.join(c, "#chat")
        self.srv.say_as("bob", "#chat", "hello alice, welcome")
        d = wait(lambda: [m for m in self.sync(c)["messages"] if m["text"].startswith("hello alice")])
        self.assertTrue(d)
        m = d[0]
        self.assertEqual((m["name"], m["kind"], m["own"], m["room"], m["highlight"]), ("bob", "user", False, rid, True))
        self.assertEqual(m["mentions"], [uid_of("alice3")])                      # named me: shows as a mention
        self.assertEqual(self.send(c, rid, "hi bob!").status_code, 200)
        mine = wait(lambda: [x for x in self.sync(c)["messages"] if x["text"] == "hi bob!"])
        self.assertTrue(mine and mine[0]["own"] and mine[0]["msgid"])            # came back through echo-message
        self.assertTrue(self.srv.got("PRIVMSG #chat :hi bob!"))
        names = self.sync(c, room_id=rid)["names"]
        self.assertEqual([(n["nick"], n["prefix"]) for n in names["list"]], [("bob", "@"), ("alice", "")])
        same = self.sync(c, room_id=rid, nv=names["ver"])["names"]
        self.assertNotIn("list", same)                                              # unchanged list is not sent again

    def test_private_message_opens_a_chat_window(self):
        c = self.new_user("alice4", nick="alice", account="alice")
        self.srv.say_as("bob", "alice", "psst, got a minute?")
        r = wait(lambda: self.rooms(c).get("bob"))
        self.assertTrue(r and r["kind"] == "query")
        msgs = [m for m in self.sync(c)["messages"] if m["room"] == r["id"]]
        self.assertTrue(msgs[0]["highlight"])
        self.assertEqual(self.send(c, r["id"], "sure").status_code, 200)
        self.assertTrue(wait(lambda: self.srv.got("PRIVMSG bob :sure")))

    def test_no_account_can_see_or_use_another_accounts_chats(self):
        a = self.new_user("alice5", nick="alice", account="alice")
        self.srv.accounts["carol"] = "pw-carol"
        b = self.new_user("carol5", nick="carol", account="carol", password="pw-carol")
        rid = self.join(a, "#secret")
        self.send(a, rid, "only alice's connection logs this")
        self.assertTrue(wait(lambda: [m for m in self.sync(a)["messages"] if "only alice" in m["text"]]))
        d = self.sync(b)
        self.assertFalse(any("only alice" in m["text"] for m in d["messages"]))
        self.assertNotIn("#secret", [r["name"] for r in d["rooms"]])
        self.assertEqual(b.get(f"/api/history?room={rid}").status_code, 404)
        self.assertEqual(self.send(b, rid, "intruder").status_code, 404)
        self.assertEqual(b.get(f"/api/sync?rev=0&room={rid}").get_json()["names"], None)
        res = b.get("/api/search?q=alice's").get_json()["results"]
        self.assertEqual(res, [])
        self.assertEqual(jpost(b, f"/api/rooms/{rid}/close").status_code, 404)
        anon = room.app.test_client()
        self.assertEqual(anon.get("/api/sync").status_code, 401)

    def test_slash_commands(self):
        c = self.new_user("alice6", nick="alice", account="alice")
        sid = wait(lambda: next((r["id"] for r in self.sync(c)["rooms"] if r["kind"] == "status"), None))
        rid = self.join(c, "#cmds")
        self.assertEqual(self.send(c, rid, "/me waves hello").status_code, 200)
        self.assertTrue(wait(lambda: self.srv.got("ACTION waves hello")))
        act = wait(lambda: [m for m in self.sync(c)["messages"] if m["kind"] == "action" and m["own"]])
        self.assertEqual(act[0]["text"], "waves hello")
        self.assertEqual(self.send(c, rid, "/topic New topic here").status_code, 200)
        self.assertTrue(wait(lambda: self.rooms(c)["#cmds"]["topic"] == "New topic here"))
        r = self.send(c, rid, "/msg bob hello there")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["open"])
        self.assertEqual(self.send(c, rid, "/nosuchthing").status_code, 400)
        self.assertEqual(self.send(c, rid, "/join").status_code, 400)
        self.assertEqual(self.send(c, rid, "//join literally").status_code, 200)       # escape: sends the text "/join literally"
        self.assertTrue(wait(lambda: self.srv.got("PRIVMSG #cmds :/join literally")))
        self.send(c, rid, "/whois bob")
        self.assertTrue(wait(lambda: [m for m in self.sync(c)["messages"] if "bob is bob@host.test" in m["text"] and m["room"] == rid]))
        self.assertEqual(self.send(c, sid, "just talking").status_code, 400)           # the server window is not a channel
        self.send(c, rid, "/help")
        self.assertTrue(wait(lambda: [m for m in self.sync(c)["messages"] if "/join #channel" in m["text"]]))
        self.assertEqual(jpost(c, f"/api/rooms/{rid}/close").status_code, 200)
        self.assertNotIn("#cmds", self.rooms(c))
        self.assertTrue(wait(lambda: self.srv.got("PART #cmds")))
        self.assertEqual(jpost(c, f"/api/rooms/{sid}/close").status_code, 400)

    def test_ns_commands_never_reach_the_chat_log(self):
        c = self.new_user("alice7", nick="alice", account="alice")
        rid = self.join(c, "#ns")
        self.send(c, rid, "/ns identify supersecret-xyz")
        self.assertTrue(wait(lambda: any("supersecret-xyz" in x for x in self.srv.nickserv)))
        time.sleep(0.2)
        dump = json.dumps(self.sync(c)["messages"])
        self.assertNotIn("supersecret-xyz", dump)
        self.assertNotIn("NickServ", [r["name"] for r in self.sync(c)["rooms"]])           # no window for services either
        raw = b""
        for suffix in ("", "-wal"):
            if os.path.exists(config.DB_PATH + suffix):
                with open(config.DB_PATH + suffix, "rb") as fh:
                    raw += fh.read()
        self.assertNotIn(b"supersecret-xyz", raw)                                              # and not in the database file

    def test_feature_gating_follows_the_server(self):
        c = self.new_user("alice8", nick="alice", account="alice")                  # server allows only +typing
        f = self.sync(c)["features"]
        self.assertTrue(f["typing"] and f["echo"] and f["sasl"])
        self.assertFalse(f["react"] or f["reply"])
        self.assertFalse(f["edit"] or f["delete"] or f["images"] or f["report"])
        srv2 = FakeIRC(clienttagdeny=None)                                           # no CLIENTTAGDENY: tags are allowed
        try:
            c2 = self.new_user("alice9", nick="alice", account="alice", srv=srv2)
            f2 = self.sync(c2)["features"]
            self.assertTrue(f2["typing"] and f2["react"] and f2["reply"])
        finally:
            srv2.stop()
        srv3 = FakeIRC(caps={"sasl": "PLAIN"})                                       # a bare-bones server
        try:
            c3 = self.new_user("alice10", nick="alice", account="alice", srv=srv3)
            f3 = self.sync(c3)["features"]
            self.assertFalse(f3["typing"] or f3["react"] or f3["reply"] or f3["echo"])
            rid = self.join(c3, "#bare")
            self.send(c3, rid, "no echo here")                                       # kept locally because the server will not echo
            self.assertTrue(wait(lambda: [m for m in self.sync(c3)["messages"] if m["text"] == "no echo here" and m["own"]]))
            self.assertEqual(jpost(c3, "/api/typing", {"room": rid}).status_code, 200)
            self.assertFalse(srv3.got("TAGMSG"))
        finally:
            srv3.stop()

    def test_typing_replies_and_reactions_when_supported(self):
        self.srv.stop()
        self.srv = FakeIRC(clienttagdeny=None)
        c = self.new_user("alice11", nick="alice", account="alice")
        self.srv.join_as("bob", "#tags")
        rid = self.join(c, "#tags")
        self.srv.tag_as("bob", "#tags", {"+typing": "active"})
        t = wait(lambda: self.sync(c, room_id=rid)["typing"])
        self.assertEqual(t[0]["name"], "bob")
        self.assertEqual(jpost(c, "/api/typing", {"room": rid}).status_code, 200)
        self.assertTrue(wait(lambda: self.srv.got("+typing=active TAGMSG #tags") or self.srv.got("typing=active")))
        self.srv.say_as("bob", "#tags", "react to me")
        m = wait(lambda: [x for x in self.sync(c)["messages"] if x["text"] == "react to me"])[0]
        self.assertTrue(m["msgid"])
        r = jpost(c, f"/api/messages/{m['id']}/react", {"emoji": "👍"})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertTrue(r.get_json()["added"])
        self.assertTrue(wait(lambda: self.srv.got("+draft/react=👍")))
        again = wait(lambda: [x for x in self.sync(c)["messages"] if x["id"] == m["id"] and x["reactions"]])
        self.assertEqual(again[0]["reactions"], {"👍": ["alice"]})
        self.send(c, rid, "replying", reply_to=m["id"])
        self.assertTrue(wait(lambda: self.srv.got(f"+draft/reply={m['msgid']}")))
        rep = wait(lambda: [x for x in self.sync(c)["messages"] if x["text"] == "replying"])[0]
        self.assertEqual(rep["reply"]["text"], "react to me")
        # someone else's reaction arrives as tags
        self.srv.tag_as("bob", "#tags", {"+draft/react": "🔥", "+draft/reply": m["msgid"]})
        self.assertTrue(wait(lambda: [x for x in self.sync(c)["messages"] if x["id"] == m["id"] and "🔥" in x["reactions"]]))

    def test_join_part_quit_lines_topic_and_unread_state(self):
        c = self.new_user("alice12", nick="alice", account="alice")
        rid = self.join(c, "#events")
        self.srv.join_as("dan", "#events")
        self.srv.part_as("dan", "#events", "see you")
        self.srv.channels["#events"]["topic"] = "x"
        wait(lambda: [m for m in self.sync(c)["messages"] if m["kind"] == "part"])
        kinds = [(m["kind"], m["text"]) for m in self.sync(c)["messages"] if m["room"] == rid]
        self.assertIn(("join", "You joined #events"), kinds)
        self.assertTrue(any(k == "join" and t.startswith("dan (dan@host.test) joined") for k, t in kinds))
        self.assertIn(("part", "dan left (see you)"), kinds)

    def test_sending_is_rate_limited(self):
        c = self.new_user("alice18", nick="alice", account="alice")
        rid = self.join(c, "#rate")
        room.limiter._hits.clear()
        codes = [jpost(c, "/api/messages", {"room": rid, "text": f"m{i}"}).status_code for i in range(config.SEND_BURST + 3)]
        self.assertEqual(codes[:config.SEND_BURST], [200] * config.SEND_BURST)
        self.assertIn(429, codes[config.SEND_BURST:])

    def test_ignore_is_personal(self):
        c = self.new_user("alice13", nick="alice", account="alice")
        r = jpost(c, "/api/ignore", {"nick": "Spammer", "kind": "block"})
        self.assertEqual(r.get_json()["ignores"], {"spammer": "block"})
        self.assertEqual(self.sync(c)["ignores"], {"spammer": "block"})
        self.assertEqual(jpost(c, "/api/ignore", {"nick": "bad nick", "kind": "block"}).status_code, 400)
        self.assertEqual(jpost(c, "/api/ignore", {"nick": "Spammer", "kind": "none"}).get_json()["ignores"], {})

    def test_history_and_search(self):
        c = self.new_user("alice14", nick="alice", account="alice")
        self.srv.join_as("bob", "#hist")
        rid = self.join(c, "#hist")
        for i in range(12):
            self.srv.say_as("bob", "#hist", f"line number {i} needle" if i % 4 == 0 else f"line number {i}")
        wait(lambda: len([m for m in self.sync(c)["messages"] if m["room"] == rid and m["name"] == "bob"]) == 12)
        res = c.get("/api/search?q=needle").get_json()["results"]
        self.assertEqual(len(res), 3)
        self.assertEqual(res[0]["room_name"], "#hist")
        h = c.get(f"/api/history?room={rid}&limit=5").get_json()
        self.assertEqual(len(h["messages"]), 5)
        self.assertTrue(h["more"])

    def test_register_sends_nickserv_command_without_logging_the_password(self):
        c = self.new_user("regnewbie", nick="newbie99", account="", password="my-new-password", connect=False, use_sasl=False)
        bad = jpost(c, "/api/irc/register", {"email": "nope"})
        self.assertEqual(bad.status_code, 400)
        r = jpost(c, "/api/irc/register", {"email": "me@example.org"})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertTrue(wait(lambda: any(x.startswith("REGISTER my-new-password me@example.org") for x in self.srv.nickserv)))
        self.assertFalse(self.srv.got("AUTHENTICATE"))                              # signed up as a guest, no SASL attempt
        uid = uid_of("regnewbie")
        self.assertFalse(store.get_irc_account(store.connect(), uid)["use_sasl"])
        dump = json.dumps(c.get("/api/irc").get_json()) + json.dumps(self.sync(c))
        self.assertNotIn("my-new-password", dump)
        self.assertNotIn("me@example.org", dump)
        s = room.irc.get(uid)
        v = jpost(c, "/api/irc/verify", {"code": "abc123"})
        self.assertEqual(v.status_code, 200, v.get_json())
        self.assertTrue(wait(lambda: any("VERIFY REGISTER newbie99 abc123" in x for x in self.srv.nickserv)))
        self.assertEqual(jpost(c, "/api/irc/verify", {"code": "no good!"}).status_code, 400)

    def test_settings_validation(self):
        c = register("validator")
        self.users.append("validator")
        for bad in ({"server": "not a host!", "nick": "x"}, {"server": "irc.example.org", "nick": "9bad"},
                    {"server": "irc.example.org", "nick": "ok", "port": 99999}, {"server": "irc.example.org", "nick": "has space"}):
            self.assertEqual(jpost(c, "/api/irc/settings", bad).status_code, 400, bad)
        self.assertEqual(jpost(c, "/api/irc/settings", {"nick": "ok"}).status_code, 400)              # a server is required the first time
        anon = room.app.test_client()
        self.assertEqual(anon.post("/api/irc/settings", json={}).status_code, 400)                    # (csrf) never accepted unauthenticated

    def test_localhost_and_odd_ports_refused_by_default(self):
        c = register("guarded")
        self.users.append("guarded")
        old_local, old_any, old_ports = config.IRC_ALLOW_LOCAL, config.IRC_ANY_PORT, set(config.IRC_ALLOWED_PORTS)
        try:
            config.IRC_ALLOW_LOCAL, config.IRC_ANY_PORT, config.IRC_ALLOWED_PORTS = False, False, {6667, 6697}
            r = jpost(c, "/api/irc/settings", {"server": "127.0.0.1", "port": 6667, "nick": "g1", "tls": False})
            self.assertEqual(r.status_code, 400)
            self.assertIn("not allowed", r.get_json()["error"])
            r = jpost(c, "/api/irc/settings", {"server": "irc.example.org", "port": 8080, "nick": "g1"})
            self.assertEqual(r.status_code, 400)
            self.assertIn("port", r.get_json()["error"])
        finally:
            config.IRC_ALLOW_LOCAL, config.IRC_ANY_PORT, config.IRC_ALLOWED_PORTS = old_local, old_any, old_ports

    def test_disconnect_connect_and_admin_actions(self):
        c = self.new_user("alice15", nick="alice", account="alice")
        uid = uid_of("alice15")
        s = room.irc.get(uid)
        self.assertEqual(jpost(c, "/api/irc/disconnect").status_code, 200)
        self.assertFalse(store.get_irc_account(store.connect(), uid)["autoconnect"])
        self.assertEqual(c.get("/api/irc").get_json()["status"]["state"], "stopped")
        r = self.send(c, 1, "x")
        self.assertIn(r.status_code, (404, 409))
        self.assertEqual(jpost(c, "/api/irc/connect").status_code, 200)
        self.assertTrue(wait(lambda: room.irc.get(uid).ready() and room.irc.get(uid).client.online))
        users = self.admin.get("/api/admin/users").get_json()["users"]
        me = next(u for u in users if u["username"] == "alice15")
        self.assertEqual((me["irc"], me["irc_state"]), ("alice on 127.0.0.1", "connected"))
        # disabling the local account hangs up its IRC connection
        self.assertEqual(jpost(self.admin, f"/api/admin/users/{uid}/disable", {"message": "bye"}).status_code, 200)
        self.assertTrue(wait(lambda: room.irc.get(uid).status()["state"] == "stopped"))
        # deleting it removes the saved login and the logs
        self.assertEqual(jpost(self.admin, f"/api/admin/users/{uid}", method="delete").status_code, 200)
        self.assertIsNone(store.get_irc_account(store.connect(), uid))
        self.users.remove("alice15")

    def test_old_local_chat_rooms_are_hidden(self):
        db = store.connect()
        with db:
            db.execute("INSERT INTO rooms (name, kind, created_at) VALUES ('general','channel','now')")
        c = self.new_user("alice16", nick="alice", account="alice")
        self.assertNotIn("general", self.rooms(c))

    def test_nightly_prune(self):
        c = self.new_user("alice17", nick="alice", account="alice")
        rid = self.join(c, "#old")
        db = store.connect()
        for i in range(30):
            store.add_irc_message(db, rid, "user", "bob", f"m{i}", ts="2020-01-01T00:00:00+00:00")
        for i in range(5):
            store.add_irc_message(db, rid, "user", "bob", f"fresh{i}")
        removed = store.prune_irc_logs(db, days=90, per_buffer=4)
        self.assertGreaterEqual(removed, 30)
        left = [r["message"] for r in db.execute("SELECT message FROM messages WHERE room_id=?", (rid,))]
        self.assertLessEqual(len(left), 4)
        self.assertTrue(all(not m.startswith("m") or m.startswith("fresh") for m in left))


if __name__ == "__main__":
    unittest.main()
