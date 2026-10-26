"""Connects each local account to its own IRC connection and keeps the database in step with it.

    IRC server  <->  IRCClient (thread)  ->  Session.on_event()  ->  SQLite (rooms/messages)  ->  bump()  ->  browsers

Every local account has at most one Session. A Session is like a small bouncer: the connection stays up while the
browser is closed, and any device that logs into the same local account sees the same conversations.
"""
import json
import re
import threading
import time
from collections import deque

import config
import crypto_box
import db as store
import irc_proto as P
from irc_client import NotConnected, IRCClient, SERVICES_NICK

BIG_CHANNEL = 300          # in channels with more people than this, joins/parts/quits are not logged (they would drown it)
MAX_QUERIES = 60           # at most this many private-chat windows per account (anything more lands in the server window)
HELP = [
    "/join #channel [key]   /part [#channel] [reason]   /close   (closes this window)",
    "/msg nick text   /query nick   /me action   /notice target text",
    "/nick newnick   /topic [new topic]   /whois nick   /away [reason]   /mode ...   /kick nick [reason]   /invite nick",
    "/ns ...  or /nickserv ...  talks to NickServ (never shown in the chat log)   /identify   (log in with your saved password)",
    "/ignore nick   /unignore nick   (only changes what you see)   /quote RAW LINE   /quit [reason]",
]


class CommandError(Exception):
    """A problem the person should be told about."""


class Session:
    def __init__(self, manager, user_id):
        self.m, self.uid = manager, user_id
        self.client, self.gen = None, 0
        self.lock = threading.RLock()
        self.state, self.error, self.retry_in = "stopped", "", 0
        self.reply_room = (None, 0.0)                 # numeric replies (WHOIS ...) go to the window the command came from
        self.typing = {}                              # (room id, nick lower) -> expires
        self.cids = deque(maxlen=300)                 # client ids already sent (a retried offline message is not sent twice)
        self.pending_register = None                  # (password, email) kept only in memory, only until it has been sent
        self.last_typing = {}
        self._tl = threading.local()

    # ------------------------------------------------------------------ plumbing
    def _db(self):
        """A database connection for the client thread (SQLite connections belong to the thread that made them)."""
        d = getattr(self._tl, "db", None)
        if d is None:
            d = self._tl.db = store.connect()
        return d

    def _close_db(self):
        d = getattr(self._tl, "db", None)
        if d is not None:
            d.close()
            self._tl.db = None

    @property
    def casemap(self):
        return self.client.casemap if self.client else "rfc1459"

    @property
    def nick(self):
        return self.client.nick if self.client else ""

    def ready(self):
        return bool(self.client and self.client.is_ready())

    def _build_cfg(self, overrides=None):
        db = store.connect()
        try:
            row = store.get_irc_account(db, self.uid)
        finally:
            db.close()
        if row is None:
            raise ValueError("No IRC settings have been saved yet.")
        pw = crypto_box.decrypt(self.m.secret, row["password_enc"]) if row["password_enc"] else ""
        if row["password_enc"] and pw is None:
            raise ValueError("The saved password can no longer be read (the app's secret key changed). Please enter it again.")
        cfg = {"host": row["host"], "port": row["port"], "tls": bool(row["tls"]), "tls_verify": bool(row["tls_verify"]),
               "nick": row["nick"], "account": row["account"] or row["nick"], "password": pw or "",
               "sasl": bool(row["use_sasl"]) and bool(pw), "realname": row["realname"] or row["nick"],
               "autojoin": json.loads(row["autojoin"] or "[]")}
        cfg.update(overrides or {})
        return cfg

    def start(self, overrides=None):
        cfg = self._build_cfg(overrides)
        with self.lock:
            self._stop_client()
            self.gen += 1
            gen = self.gen
            self.state, self.error, self.retry_in = "connecting", "", 0
            self.client = IRCClient(cfg, lambda ev, g=gen: self.on_event(g, ev))
            self.client.start()
        self.m.bump()

    def _stop_client(self):
        c, self.client = self.client, None
        if c is not None:
            self.gen += 1                       # whatever the old client still reports is ignored from now on
            try:
                c.quit("Leaving")
            except Exception:
                c.stop()

    def stop(self):
        with self.lock:
            self._stop_client()
            self.state, self.error = "stopped", ""
        self.m.bump()

    # ------------------------------------------------------------------ events from the client thread
    def on_event(self, gen, ev):
        if gen != self.gen:
            return
        h = getattr(self, "ev_" + ev["type"], None)
        try:
            if h:
                h(ev)
        except Exception as e:                  # a storage hiccup must never break the IRC connection
            print(f"[irc:{self.uid}] error handling {ev['type']}: {e!r}")
        self.m.bump()

    def _status_room(self, db=None):
        db = db or self._db()
        name = (self.client.network if self.client and self.client.network else None) or self._host() or "server"
        rid, created = store.ensure_buffer(db, self.uid, name, "status", self.casemap, key=store.STATUS_KEY)
        if not created:
            r = store.get_room(db, rid)
            if r["name"] != name:
                store.update_room(db, rid, {"name": name[:80]})
        return rid

    def _host(self):
        return self.client.cfg["host"] if self.client else ""

    def _room(self, name, kind, db=None):
        rid, _ = store.ensure_buffer(db or self._db(), self.uid, name, kind, self.casemap)
        return rid

    def _existing_room(self, name, db=None):
        r = store.find_buffer(db or self._db(), self.uid, name, self.casemap)
        return r["id"] if r else None

    def _line(self, room_id, kind, text, sender="", ts=None, db=None):
        store.add_irc_message(db or self._db(), room_id, kind, sender, text, ts=ts, me_id=self.uid)

    def status_line(self, text, kind="server", sender="", db=None):
        self._line(self._status_room(db), kind, text, sender, db=db)

    def note(self, db, room_id, text, kind="system"):
        """A line only you see, in the window you are looking at (help text, confirmations)."""
        self._line(room_id, kind, text, db=db)
        self.m.bump()

    def _small(self, channel):
        names, total = self.client.names(channel) if self.client else ([], 0)
        return total <= BIG_CHANNEL

    def _save_autojoin(self, channel, add):
        c = self.client
        if c is None:
            return
        cur = list(c.cfg.get("autojoin", []))
        low = [P.casefold(x, self.casemap) for x in cur]
        k = P.casefold(channel, self.casemap)
        if add and k not in low:
            cur.append(channel)
        elif not add and k in low:
            cur = [x for x, l in zip(cur, low) if l != k]
        else:
            return
        c.cfg["autojoin"] = cur
        store.save_irc_account(self._db(), self.uid, {"autojoin": json.dumps(cur)})

    def ev_state(self, ev):
        self.state, self.error, self.retry_in = ev["state"], ev.get("error", ""), ev.get("retry_in", 0)
        if ev["state"] == "disconnected":
            self.status_line(f"Disconnected: {self.error} Trying again in {self.retry_in} s.", "error")
        elif ev["state"] == "failed":
            db = self._db()
            store.save_irc_account(db, self.uid, {"auth_failed": 1 if "rejected" in self.error else 0, "last_error": self.error[:300]})
            self.status_line(self.error, "error")
        elif ev["state"] == "connected":
            store.save_irc_account(self._db(), self.uid, {"auth_failed": 0, "last_error": ""})
        if ev["state"] in ("failed", "stopped"):
            self._close_db()

    def ev_auth_failed(self, ev):
        store.save_irc_account(self._db(), self.uid, {"auth_failed": 1, "last_error": ev["text"][:300]})

    def ev_registered(self, ev):
        if self.pending_register:
            pw, email = self.pending_register
            self.pending_register = None
            try:
                self.client.services(f"REGISTER {pw} {email}".strip())
                self.status_line("Sign-up sent to NickServ. Check your email for a verification code (the password is not shown here).")
            except NotConnected:
                pass

    def ev_features(self, ev):
        if self.client and self.client.registered:
            self._status_room()

    def ev_server(self, ev):
        rid, until = self.reply_room
        room = rid if rid and time.time() < until and store.get_room(self._db(), rid) else self._status_room()
        self._line(room, "error" if ev.get("error") else "server", ev["text"])

    def _highlight(self, text, own, kind, is_channel):
        if own or kind not in store.CHAT_KINDS or (kind == "notice" and not is_channel):
            return False
        if not is_channel:
            return True                                             # a private message to you is always worth a notification
        n = re.escape(self.nick)
        return bool(re.search(rf"(?<![\w\[\]\\`^{{|}}-]){n}(?![\w\[\]\\`^{{|}}-])", text, re.I))

    def ev_message(self, ev):
        db = self._db()
        if ev["buffer"] is None:
            room = self._status_room()
        else:
            kind = "channel" if ev["is_channel"] else "query"
            room = self._existing_room(ev["buffer"])
            if room is None:
                if kind == "query" and db.execute("SELECT COUNT(*) FROM rooms WHERE owner_id=? AND kind='query'", (self.uid,)).fetchone()[0] >= MAX_QUERIES:
                    room = self._status_room()
                else:
                    room = self._room(ev["buffer"], kind)
        reply_to = None
        if ev.get("reply"):
            ref = store.find_by_msgid(db, room, ev["reply"])
            reply_to = ref["id"] if ref else None
        hl = self._highlight(ev["text"], ev["own"], ev["kind"], ev["is_channel"])
        store.add_irc_message(db, room, ev["kind"], ev["sender"], ev["text"], ts=ev["ts"], own=ev["own"], msgid=ev["msgid"],
                              account=ev["account"], highlight=hl, reply_to=reply_to, me_id=self.uid)
        self.typing.pop((room, ev["sender"].lower()), None)

    def ev_join(self, ev):
        ch = ev["channel"]
        if ev["own"]:
            rid = self._room(ch, "channel")
            store.update_room(self._db(), rid, {"joined": 1})
            self._save_autojoin(ch, True)
            self._line(rid, "join", f"You joined {ch}", ts=ev["ts"])
        else:
            rid = self._existing_room(ch)
            if rid and self._small(ch):
                who = f"{ev['nick']} ({ev['userhost']})" if ev["userhost"] else ev["nick"]
                self._line(rid, "join", f"{who} joined", ts=ev["ts"])

    def ev_part(self, ev):
        rid = self._existing_room(ev["channel"])
        if not rid:
            return
        why = f" ({ev['reason']})" if ev["reason"] else ""
        if ev["own"]:
            store.update_room(self._db(), rid, {"joined": 0})
            self._save_autojoin(ev["channel"], False)
            self._line(rid, "part", f"You left {ev['channel']}{why}", ts=ev["ts"])
        elif self._small(ev["channel"]):
            self._line(rid, "part", f"{ev['nick']} left{why}", ts=ev["ts"])

    def ev_kick(self, ev):
        rid = self._existing_room(ev["channel"])
        if not rid:
            return
        why = f" ({ev['reason']})" if ev["reason"] else ""
        if ev["own"]:
            store.update_room(self._db(), rid, {"joined": 0})
            self._save_autojoin(ev["channel"], False)
            self._line(rid, "kick", f"{ev['by']} kicked you out of {ev['channel']}{why}", ts=ev["ts"])
        elif self._small(ev["channel"]):
            self._line(rid, "kick", f"{ev['nick']} was kicked by {ev['by']}{why}", ts=ev["ts"])

    def ev_quit(self, ev):
        why = f" ({ev['reason']})" if ev["reason"] else ""
        for ch in ev["channels"]:
            rid = self._existing_room(ch)
            if rid and self._small(ch):
                self._line(rid, "quit", f"{ev['nick']} quit{why}", ts=ev["ts"])
        for key in [k for k in self.typing if k[1] == ev["nick"].lower()]:
            self.typing.pop(key, None)

    def ev_nick(self, ev):
        text = "You are now known as " + ev["new"] if ev["own"] else f"{ev['old']} is now known as {ev['new']}"
        for ch in ev["channels"]:
            rid = self._existing_room(ch)
            if rid and (ev["own"] or self._small(ch)):
                self._line(rid, "nick", text, ts=ev["ts"])
        if not ev["own"]:
            rid = store.rename_buffer(self._db(), self.uid, ev["old"], ev["new"], self.casemap)
            if rid:
                self._line(rid, "nick", text, ts=ev["ts"])
        else:
            self.status_line(text, "nick")

    def ev_topic(self, ev):
        rid = self._existing_room(ev["channel"])
        if not rid:
            return
        store.update_room(self._db(), rid, {"topic": ev["text"]})
        if ev["initial"]:
            self._line(rid, "topic", f"Topic: {ev['text']}" if ev["text"] else "No topic is set.", ts=ev["ts"])
        else:
            self._line(rid, "topic", f"{ev['by']} changed the topic to: {ev['text']}" if ev["text"] else f"{ev['by']} cleared the topic.", ts=ev["ts"])

    def ev_mode(self, ev):
        rid = self._existing_room(ev["channel"])
        if rid and self._small(ev["channel"]):
            self._line(rid, "mode", f"{ev['by']} sets mode {ev['text']}", ts=ev["ts"])

    def ev_typing(self, ev):
        rid = self._existing_room(ev["buffer"])
        if not rid:
            return
        key = (rid, ev["nick"].lower())
        if ev["state"] == "done":
            self.typing.pop(key, None)
        else:
            self.typing[key] = time.time() + 6

    def ev_react(self, ev):
        db = self._db()
        rid = self._existing_room(ev["buffer"])
        msg = store.find_by_msgid(db, rid, ev["msgid"]) if rid else None
        if msg is None or len(ev["emoji"]) > 16:
            return
        (store.remove_irc_reaction if ev["remove"] else store.add_irc_reaction)(db, msg["id"], ev["nick"], ev["emoji"])

    # ------------------------------------------------------------------ what the web page asks about
    def typing_in(self, room_id):
        now = time.time()
        for k in [k for k, t in self.typing.items() if t <= now]:
            del self.typing[k]
        return [{"name": nick, "ttl": max(1, int(t - now))} for (rid, nick), t in self.typing.items() if rid == room_id]

    def features(self):
        c = self.client
        snap = c.snapshot() if c else {}
        caps, deny = set(snap.get("caps", [])), snap.get("clienttagdeny", "")
        tags = "message-tags" in caps
        pick = lambda names: P.pick_client_tag(deny, names) if tags else None
        return {"connected": self.ready(), "typing": bool(pick(["typing", "draft/typing"])),
                "reply": bool(pick(["draft/reply", "reply"])), "react": bool(pick(["draft/react", "react"])),
                "echo": "echo-message" in caps, "away": "away-notify" in caps,
                "accounts": bool(caps & {"account-tag", "extended-join"}), "sasl": bool(snap.get("authenticated")),
                # IRC itself has none of these, so they are always off:
                "edit": False, "delete": False, "images": False, "report": False}

    def _tag(self, names):
        c = self.client
        deny = c.snapshot().get("clienttagdeny", "") if c else ""
        return P.pick_client_tag(deny, names) if c and c.cap_enabled("message-tags") else None

    def status(self):
        c = self.client
        snap = c.snapshot() if c else {}
        return {"state": snap.get("state") or self.state, "error": snap.get("error") or self.error, "retry_in": self.retry_in,
                "nick": snap.get("nick", ""), "network": snap.get("network", ""), "account": snap.get("account", ""),
                "caps": snap.get("caps", []), "authenticated": bool(snap.get("authenticated"))}

    def names_for(self, room):
        if not self.client or room["kind"] != "channel":
            return {"room": room["id"], "list": [], "total": 0}
        rows, total = self.client.names(room["name"])
        return {"room": room["id"], "list": rows, "total": total}

    def user_card(self, nick, channel=None):
        return self.client.user_info(nick, channel) if self.client else {"nick": nick}

    # ------------------------------------------------------------------ what the person does
    def _need(self):
        if not self.ready():
            raise NotConnected("Not connected to IRC. Check the connection page.")
        return self.client

    def _check_target(self, room):
        if room["kind"] == "status":
            raise CommandError("This is the server window. Use /join #channel, or /msg nick text.")
        if room["kind"] == "channel" and not room["joined"]:
            raise CommandError(f"You are not in {room['name']}. Type /join {room['name']} first.")

    def send_text(self, db, room, text, reply_local_id=None, client_id=None, action=False):
        c = self._need()
        self._check_target(room)
        if client_id:
            if client_id in self.cids:
                return None
            self.cids.append(client_id)
        tags, reply_ref = None, None
        if reply_local_id and not action:
            ref = db.execute("SELECT id, msgid FROM messages WHERE id=? AND room_id=?", (reply_local_id, room["id"])).fetchone()
            tag = self._tag(["draft/reply", "reply"])
            if ref and ref["msgid"] and tag:
                tags, reply_ref = {"+" + tag: ref["msgid"]}, ref["id"]
        parts = c.privmsg(room["name"], text, tags=tags, action=action)
        if not c.cap_enabled("echo-message"):                       # no echo from the server, so keep our own copy
            for i, part in enumerate(parts):
                store.add_irc_message(db, room["id"], "action" if action else "user", c.nick, part, own=True, me_id=self.uid,
                                      reply_to=reply_ref if i == 0 else None)
        self.m.bump()
        return parts

    def send_notice(self, db, target, text):
        c = self._need()
        parts = c.notice(target, text)
        if not c.cap_enabled("echo-message"):
            self.status_line(f"-> -{target}- {' '.join(parts)}", "notice", c.nick, db=db)
        return parts

    def send_typing(self, room):
        tag = self._tag(["typing", "draft/typing"])
        if not tag or not self.ready() or room["kind"] == "status" or (room["kind"] == "channel" and not room["joined"]):
            return False
        now = time.time()
        if now - self.last_typing.get(room["id"], 0) < 3:
            return False
        self.last_typing[room["id"]] = now
        self.client.tagmsg(room["name"], {"+" + tag: "active"})
        return True

    def toggle_reaction(self, db, room, msg, emoji):
        c = self._need()
        react, unreact = self._tag(["draft/react", "react"]), self._tag(["draft/unreact", "unreact"])
        reply = self._tag(["draft/reply", "reply"])
        if not (react and reply) or not msg["msgid"]:
            raise CommandError("This server does not support reactions (or this message has no id).")
        self._check_target(room)
        have = store.has_irc_reaction(db, msg["id"], c.nick, emoji)
        if have:
            if not unreact:
                raise CommandError("This server lets you react but not take a reaction back.")
            c.tagmsg(room["name"], {"+" + unreact: emoji, "+" + reply: msg["msgid"]})
            store.remove_irc_reaction(db, msg["id"], c.nick, emoji)
        else:
            c.tagmsg(room["name"], {"+" + react: emoji, "+" + reply: msg["msgid"]})
            store.add_irc_reaction(db, msg["id"], c.nick, emoji)
        self.m.bump()
        return not have

    def close_buffer(self, db, room):
        if room["kind"] == "status":
            raise CommandError("The server window cannot be closed.")
        if room["kind"] == "channel":
            if room["joined"] and self.ready():
                try:
                    self.client.part(room["name"], "Leaving")
                except NotConnected:
                    pass
            self._save_autojoin_db(db, room["name"])
        store.delete_buffer(db, room["id"])
        self.m.bump()

    def _save_autojoin_db(self, db, channel):
        row = store.get_irc_account(db, self.uid)
        cur = [x for x in json.loads(row["autojoin"] or "[]") if P.casefold(x, self.casemap) != P.casefold(channel, self.casemap)]
        store.save_irc_account(db, self.uid, {"autojoin": json.dumps(cur)})
        if self.client:
            self.client.cfg["autojoin"] = cur

    # ---- slash commands
    def command(self, db, room, line):
        """Run '/something ...'. Returns a dict for the browser, or raises CommandError / NotConnected."""
        cmd, _, rest = line[1:].partition(" ")
        cmd, rest = cmd.lower(), rest.strip()
        self.reply_room = (room["id"], time.time() + 15)
        if cmd in ("help", "?"):
            for h in HELP:
                self.note(db, room["id"], h)
            return {"ok": True}
        if cmd in ("ignore", "unignore"):
            nick = rest.split(" ")[0]
            if not P.valid_nick(nick):
                raise CommandError(f"Usage: /{cmd} nick")
            store.set_ignore(db, self.uid, nick, "block" if cmd == "ignore" else "none")
            self.note(db, room["id"], f"{nick} is now {'ignored' if cmd == 'ignore' else 'no longer ignored'} (only you see this).")
            return {"ok": True, "ignores": store.ignores_of(db, self.uid)}
        if cmd == "close":
            self.close_buffer(db, room)
            return {"ok": True, "closed": room["id"]}
        if cmd == "quit":
            self.m.set_autoconnect(self.uid, False)
            if self.client:
                self.client.quit(rest or "Leaving")
            self.stop()
            return {"ok": True}
        c = self._need()
        if cmd == "join":
            chan, _, key = rest.partition(" ")
            if not chan.startswith(tuple(c.chantypes)) and chan:
                chan = "#" + chan
            if not P.valid_channel(chan, c.chantypes):
                raise CommandError("Usage: /join #channel [key]")
            c.join(chan, key.strip() or None)
            return {"ok": True, "open": chan}
        if cmd == "part":
            chan, _, why = rest.partition(" ")
            if not chan.startswith(tuple(c.chantypes)):
                chan, why = (room["name"] if room["kind"] == "channel" else ""), rest
            if not chan:
                raise CommandError("Usage: /part #channel [reason]")
            c.part(chan, why.strip())
            return {"ok": True}
        if cmd in ("msg", "query", "m", "q"):
            nick, _, text = rest.partition(" ")
            if not P.valid_nick(nick):
                raise CommandError(f"Usage: /{cmd} nick [message]")
            if nick.lower() == SERVICES_NICK.lower():
                raise CommandError("Use /ns ... to talk to NickServ.")
            rid = self._room_for(db, nick, "query")
            if text.strip():
                self.send_text(db, store.get_room(db, rid), text.strip())
            return {"ok": True, "open": rid}
        if cmd in ("me", "action"):
            if not rest:
                raise CommandError("Usage: /me what you do")
            self.send_text(db, room, rest, action=True)
            return {"ok": True}
        if cmd == "notice":
            target, _, text = rest.partition(" ")
            if not target or not text.strip():
                raise CommandError("Usage: /notice target text")
            self.send_notice(db, target, text.strip())
            return {"ok": True}
        if cmd == "nick":
            if not P.valid_nick(rest):
                raise CommandError("Usage: /nick newnick")
            c.set_nick(rest)
            return {"ok": True}
        if cmd == "topic":
            if room["kind"] != "channel":
                raise CommandError("Use /topic inside a channel.")
            c.set_topic(room["name"], rest if rest else None)
            if not rest:
                t, by = c.topic_of(room["name"])
                self.note(db, room["id"], f"Topic: {t}" if t else "No topic is set.")
            return {"ok": True}
        if cmd == "whois":
            if not P.valid_nick(rest.split(" ")[0]):
                raise CommandError("Usage: /whois nick")
            c.whois(rest.split(" ")[0])
            return {"ok": True}
        if cmd == "away":
            c.set_away(rest)
            self.note(db, room["id"], f"You are now away: {rest}" if rest else "You are no longer away.")
            return {"ok": True}
        if cmd == "mode":
            c.raw(f"MODE {rest}" if rest else f"MODE {room['name']}")
            return {"ok": True}
        if cmd == "kick":
            nick, _, why = rest.partition(" ")
            if room["kind"] != "channel" or not nick:
                raise CommandError("Usage (inside a channel): /kick nick [reason]")
            c.raw(P.format_line("KICK", room["name"], nick, *([why] if why else []), trailing=True))
            return {"ok": True}
        if cmd == "invite":
            nick, _, chan = rest.partition(" ")
            chan = chan or (room["name"] if room["kind"] == "channel" else "")
            if not nick or not chan:
                raise CommandError("Usage: /invite nick [#channel]")
            c.raw(P.format_line("INVITE", nick, chan))
            return {"ok": True}
        if cmd in ("ns", "nickserv"):
            if not rest:
                raise CommandError("Usage: /ns HELP")
            c.services(rest)                                          # never stored: it can contain a password
            return {"ok": True}
        if cmd == "identify":
            self.identify()
            return {"ok": True}
        if cmd in ("quote", "raw"):
            if not rest:
                raise CommandError("Usage: /quote RAW IRC LINE")
            c.raw(rest)
            return {"ok": True}
        raise CommandError(f"Unknown command /{cmd}. Type /help for the list.")

    def _room_for(self, db, nick, kind):
        rid = self._existing_room(nick, db)
        if rid is None:
            rid, _ = store.ensure_buffer(db, self.uid, nick, kind, self.casemap)
            self.m.bump()
        return rid

    # ---- services (log in / sign up / verify), used by the connection page
    def _account_and_password(self):
        cfg = self._build_cfg()
        return cfg["account"], cfg["password"]

    def identify(self):
        acct, pw = self._account_and_password()
        if not pw:
            raise CommandError("No password is saved for this account.")
        self._need().services(f"IDENTIFY {acct} {pw}")

    def verify(self, code):
        acct, _pw = self._account_and_password()
        self._need().services(f"VERIFY REGISTER {acct} {code}")


class Manager:
    def __init__(self):
        self.sessions, self.lock = {}, threading.RLock()
        self.secret, self.bump = "", (lambda: None)

    def init(self, secret, bump):
        self.secret, self.bump = secret, bump

    def get(self, uid):
        return self.sessions.get(uid)

    def session(self, uid):
        with self.lock:
            s = self.sessions.get(uid)
            if s is None:
                s = self.sessions[uid] = Session(self, uid)
            return s

    def start(self, uid, overrides=None, pending_register=None):
        s = self.session(uid)
        s.pending_register = pending_register
        s.start(overrides)
        return s

    def stop(self, uid):
        s = self.sessions.get(uid)
        if s:
            s.stop()

    def remove(self, uid):
        """The local account is being deleted."""
        s = self.sessions.pop(uid, None)
        if s:
            s.stop()

    def set_autoconnect(self, uid, on):
        db = store.connect()
        try:
            if store.get_irc_account(db, uid):
                store.save_irc_account(db, uid, {"autoconnect": 1 if on else 0})
        finally:
            db.close()

    def autostart(self):
        """Bring every account that wants to be online back up (after a restart), one every half second."""
        db = store.connect()
        try:
            ids = [r["user_id"] for r in db.execute(
                "SELECT a.user_id FROM irc_accounts a JOIN users u ON u.id=a.user_id"
                " WHERE a.autoconnect=1 AND a.auth_failed=0 AND u.is_disabled=0 AND u.password_hash IS NOT NULL")]
        finally:
            db.close()

        def run():
            for uid in ids:
                try:
                    self.start(uid)
                except Exception as e:
                    print(f"[irc] could not start account {uid}: {e}")
                time.sleep(0.5)
        threading.Thread(target=run, daemon=True, name="irc-autostart").start()
        return ids


manager = Manager()
