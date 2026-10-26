"""Run with:  python -m unittest discover -s tests -v
These tests talk to a fake IRC server on 127.0.0.1; nothing leaves your machine."""
import os
import sys
import tempfile
import threading
import time
import unittest

os.environ.setdefault("ROOM_DB_DIR", tempfile.mkdtemp(prefix="room-test-"))
os.environ["ROOM_IRC_ALLOW_LOCAL"] = "1"
os.environ["ROOM_IRC_PORTS"] = "any"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import crypto_box                  # noqa: E402
import irc_client as C             # noqa: E402
import irc_proto as P              # noqa: E402
from fake_irc import FakeIRC       # noqa: E402


class Proto(unittest.TestCase):
    def test_parse_tags_prefix_params(self):
        m = P.parse("@time=2026-01-01T00:00:00.000Z;+typing=active;x=a\\sb\\:c :bob!b@h.test PRIVMSG #c :hello  world")
        self.assertEqual((m.command, m.nick, m.userhost, m.params), ("PRIVMSG", "bob", "b@h.test", ["#c", "hello  world"]))
        self.assertEqual(m.tags["x"], "a b;c")
        self.assertEqual(m.tags["+typing"], "active")
        self.assertIsNone(P.parse("   "))
        self.assertEqual(P.parse("PING :irc.test").params, ["irc.test"])

    def test_format_blocks_injection(self):
        self.assertEqual(P.format_line("PRIVMSG", "#c", "hi\r\nQUIT :bye"), "PRIVMSG #c :hi QUIT :bye")
        self.assertEqual(P.format_line("PRIVMSG", "#c", "hello there", tags={"+typing": "active"}), "@+typing=active PRIVMSG #c :hello there")
        with self.assertRaises(ValueError):
            P.format_line("PRIVMSG", "a b", "x")

    def test_split_text(self):
        self.assertEqual(P.split_text("a\n\nb", 100), ["a", "b"])
        parts = P.split_text("word " * 100, 60)
        self.assertTrue(all(len(p.encode()) <= 60 for p in parts))
        self.assertEqual(" ".join(parts).split(), ["word"] * 100)
        emoji = P.split_text("😀" * 50, 21)
        self.assertTrue(all(len(p.encode()) <= 21 for p in emoji))
        self.assertEqual("".join(emoji), "😀" * 50)

    def test_strip_format_and_casefold(self):
        self.assertEqual(P.strip_format("\x02bold\x0f \x0304,12colour\x03 \x1ditalic"), "bold colour italic")
        self.assertEqual(P.casefold("Nick[A]"), P.casefold("nick{a}"))
        self.assertNotEqual(P.casefold("Nick[A]", "ascii"), P.casefold("nick{a}", "ascii"))

    def test_client_tag_rules(self):
        self.assertTrue(P.client_tag_allowed("", "typing"))
        self.assertFalse(P.client_tag_allowed("*,-typing", "draft/react"))
        self.assertTrue(P.client_tag_allowed("*,-typing", "typing"))
        self.assertFalse(P.client_tag_allowed("typing", "typing"))
        self.assertEqual(P.pick_client_tag("*,-draft/typing", ["typing", "draft/typing"]), "draft/typing")

    def test_sasl_chunks_and_server_parsing(self):
        self.assertEqual(P.sasl_chunks("a" * 400), ["a" * 400, "+"])
        self.assertEqual(P.sasl_chunks("abc"), ["abc"])
        self.assertEqual(P.parse_server("irc.libera.chat"), ("irc.libera.chat", 6697, True))
        self.assertEqual(P.parse_server("irc://example.org"), ("example.org", 6667, False))
        self.assertEqual(P.parse_server("ircs://example.org:7000"), ("example.org", 7000, True))
        with self.assertRaises(ValueError):
            P.parse_server("not a host!")

    def test_crypto_box(self):
        t = crypto_box.encrypt("k1", "hunter2 \u00e9")
        self.assertNotIn("hunter2", t)
        self.assertEqual(crypto_box.decrypt("k1", t), "hunter2 \u00e9")
        self.assertIsNone(crypto_box.decrypt("other", t))
        self.assertIsNone(crypto_box.decrypt("k1", t[:-4] + "AAAA"))
        self.assertNotEqual(crypto_box.encrypt("k1", "same"), crypto_box.encrypt("k1", "same"))


class Collector:
    def __init__(self):
        self.events, self.lock = [], threading.Lock()

    def __call__(self, ev):
        with self.lock:
            self.events.append(ev)

    def find(self, type_, **match):
        with self.lock:
            return [e for e in self.events if e["type"] == type_ and all(e.get(k) == v for k, v in match.items())]

    def wait(self, type_, timeout=5, **match):
        end = time.time() + timeout
        while time.time() < end:
            f = self.find(type_, **match)
            if f:
                return f[-1]
            time.sleep(0.02)
        raise AssertionError(f"no {type_} {match}; saw {[ (e['type'], e.get('state') or e.get('text')) for e in self.events][-12:]}")


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.srv = FakeIRC()
        self.col = Collector()
        self.clients = []

    def tearDown(self):
        for c in self.clients:
            c.stop()
        self.srv.stop()

    def make(self, **over):
        cfg = {"host": "127.0.0.1", "port": self.srv.port, "tls": False, "nick": "alice", "account": "alice",
               "password": "pw-alice", "sasl": True, "realname": "Alice", "autojoin": ["#chat"]}
        cfg.update(over)
        c = C.IRCClient(cfg, self.col)
        self.clients.append(c)
        c.start()
        return c

    def test_sasl_login_caps_join_and_names(self):
        self.srv.join_as("bob", "#chat", "@")
        self.srv.channels["#chat"]["topic"] = "Welcome to chat"
        c = self.make()
        self.col.wait("ready")
        self.assertTrue(c.authenticated)
        self.assertEqual(c.account, "alice")
        self.assertTrue({"sasl", "message-tags", "echo-message", "server-time"} <= c.caps)
        self.assertEqual(c.network, "FakeNet")
        self.assertTrue(self.srv.got("AUTHENTICATE"))
        self.assertFalse(any("pw-alice" in l for l in self.srv.received))          # the password is only sent base64-encoded
        self.col.wait("names", channel="#chat")
        names, total = c.names("#chat")
        self.assertEqual([(n["nick"], n["prefix"]) for n in names], [("bob", "@"), ("alice", "")])
        self.assertEqual(c.topic_of("#chat")[0], "Welcome to chat")

    def test_wrong_password_fails_and_never_retries(self):
        c = self.make(password="nope")
        ev = self.col.wait("auth_failed")
        self.assertIn("rejected", ev["text"])
        self.col.wait("state", state="failed")
        time.sleep(0.4)
        self.assertEqual(len([e for e in self.col.find("state", state="connecting")]), 1)   # no reconnect loop
        self.assertFalse(c.is_alive() and c.state != "failed")

    def test_no_sasl_offered_falls_back_to_nickserv_identify(self):
        self.srv.stop()
        self.srv = FakeIRC(caps={"message-tags": "", "server-time": ""})
        c = self.make()
        self.col.wait("ready")
        self.assertTrue(self.srv.wait_for(lambda: any("IDENTIFY" in x for x in self.srv.nickserv), 5))
        self.assertIn("IDENTIFY alice pw-alice", self.srv.nickserv[0])

    def test_guest_connection_sends_no_credentials(self):
        self.make(sasl=False, password="")
        self.col.wait("ready")
        self.assertFalse(self.srv.got("AUTHENTICATE"))

    def test_messages_actions_notices_and_tags(self):
        self.srv.join_as("bob", "#chat")
        c = self.make()
        self.col.wait("names", channel="#chat")
        self.srv.say_as("bob", "#chat", "\x02hello\x02 alice", extra_tags={"+draft/reply": "m1"})
        ev = self.col.wait("message", buffer="#chat")
        self.assertEqual((ev["kind"], ev["sender"], ev["text"], ev["own"], ev["reply"]), ("user", "bob", "hello alice", False, "m1"))
        self.assertTrue(ev["msgid"].startswith("m"))
        self.srv.say_as("bob", "#chat", "\x01ACTION waves\x01")
        self.assertEqual(self.col.wait("message", kind="action")["text"], "waves")
        self.srv.say_as("bob", "alice", "psst")
        self.assertEqual(self.col.wait("message", buffer="bob")["text"], "psst")        # a private message lives in bob's buffer
        self.srv.say_as("bob", "alice", "notice me", notice=True)
        self.assertIsNone(self.col.wait("message", kind="notice")["buffer"])           # notices go to the status window

    def test_sending_with_echo_and_splitting(self):
        c = self.make()
        self.col.wait("names", channel="#chat")
        sent = c.privmsg("#chat", "hi there")
        self.assertEqual(sent, ["hi there"])
        ev = self.col.wait("message", own=True, buffer="#chat")                         # echo-message brings it back with a msgid
        self.assertEqual(ev["text"], "hi there")
        self.assertTrue(ev["msgid"])
        long = " ".join(["lorem"] * 400)
        parts = c.privmsg("#chat", long)
        self.assertGreater(len(parts), 3)
        self.assertEqual(" ".join(parts).split(), long.split())
        # the line the server relays to others is ":alice!alice@host.test PRIVMSG #chat :<text>\r\n" and must fit in 512 bytes
        self.assertTrue(all(len(f":alice!alice@host.test PRIVMSG #chat :{p}".encode()) + 2 <= 512 for p in parts))
        c.privmsg("#chat", "wave", action=True)
        self.assertTrue(self.srv.wait_for(lambda: self.srv.got("ACTION wave"), 5))

    def test_echoed_messages_to_services_are_never_reported(self):
        c = self.make()
        self.col.wait("ready")
        c.services("IDENTIFY alice hunter2-secret")
        self.assertTrue(self.srv.wait_for(lambda: any("IDENTIFY" in x for x in self.srv.nickserv), 5))
        self.col.wait("message", kind="notice")                                         # NickServ's answer arrives...
        time.sleep(0.3)
        self.assertNotIn("hunter2-secret", repr(self.col.events))                      # ...but our own echoed password never does

    def test_injection_is_impossible(self):
        c = self.make()
        self.col.wait("names", channel="#chat")
        c.privmsg("#chat", "one\r\nQUIT :pwned\nJOIN #evil")
        time.sleep(0.5)
        self.assertFalse(any(l.startswith("QUIT") and "pwned" in l for l in self.srv.received))
        self.assertFalse(any(l.startswith("JOIN #evil") for l in self.srv.received))

    def test_membership_events_nick_and_modes(self):
        c = self.make()
        self.col.wait("names", channel="#chat")
        self.srv.join_as("carol", "#chat")
        self.assertEqual(self.col.wait("join", nick="carol")["own"], False)
        self.assertIn("carol", [n["nick"] for n in c.names("#chat")[0]])
        self.srv.op("#chat", "carol")
        self.srv._to_channel("#chat", "irc.test", "MODE", "#chat", "+o", "carol")
        self.col.wait("mode")
        self.assertEqual([n["prefix"] for n in c.names("#chat")[0] if n["nick"] == "carol"], ["@"])
        self.srv.part_as("carol", "#chat", "later")
        self.assertEqual(self.col.wait("part", nick="carol")["reason"], "later")
        self.srv.join_as("dave", "#chat")
        self.col.wait("join", nick="dave")
        self.srv.quit_as("dave")
        ev = self.col.wait("quit", nick="dave")
        self.assertEqual(ev["channels"], ["#chat"])
        self.assertNotIn("dave", [n["nick"] for n in c.names("#chat")[0]])

    def test_tagmsg_typing_and_reactions(self):
        self.srv.join_as("bob", "#chat")
        c = self.make()
        self.col.wait("names", channel="#chat")
        self.srv.tag_as("bob", "#chat", {"+typing": "active"})
        self.assertEqual(self.col.wait("typing")["nick"], "bob")
        self.srv.tag_as("bob", "#chat", {"+draft/react": "👍", "+draft/reply": "m9"})
        r = self.col.wait("react")
        self.assertEqual((r["nick"], r["emoji"], r["msgid"], r["remove"]), ("bob", "👍", "m9", False))
        c.tagmsg("#chat", {"+typing": "active"})
        self.assertTrue(self.srv.wait_for(lambda: self.srv.got("TAGMSG #chat"), 5))

    def test_whois_numerics_and_unknown_command_errors(self):
        c = self.make()
        self.col.wait("ready")
        c.whois("bob")
        ev = self.col.wait("server", numeric="311")
        self.assertIn("bob is bob@host.test", ev["text"])
        c.raw("FROBNICATE now")
        err = self.col.wait("server", numeric="421")
        self.assertTrue(err["error"])

    def test_reconnects_after_the_connection_drops(self):
        c = self.make()
        self.col.wait("ready")
        with c._lock:
            pass
        c.cfg["nick"] = "alice"
        self.srv.drop_clients()
        self.col.wait("state", state="disconnected")
        self.assertTrue(self.col.find("state", state="disconnected")[-1]["retry_in"] >= 5)

    def test_localhost_is_refused_unless_allowed(self):
        old = C.config.IRC_ALLOW_LOCAL
        C.config.IRC_ALLOW_LOCAL = False
        try:
            with self.assertRaises(ValueError):
                C.check_host("127.0.0.1")
        finally:
            C.config.IRC_ALLOW_LOCAL = old

    def test_nick_in_use_while_registering(self):
        c1 = self.make(nick="zed", account="", password="", sasl=False, autojoin=[])
        self.col.wait("ready")
        col2 = Collector()
        cfg = {"host": "127.0.0.1", "port": self.srv.port, "tls": False, "nick": "zed", "password": "", "sasl": False, "autojoin": []}
        c2 = C.IRCClient(cfg, col2)
        self.clients.append(c2)
        c2.start()
        col2.wait("ready")
        self.assertEqual(c2.nick, "zed_")


if __name__ == "__main__":
    unittest.main()
