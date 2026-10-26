"""A tiny fake IRC server for the tests: CAP 302, SASL PLAIN, registration, channels, messages with tags, NickServ.

It also lets a test play 'other people': say_as("bob", "#chan", "hi"), join_as(...), etc.
"""
import base64
import itertools
import socket
import threading
import time
from datetime import datetime, timezone

import irc_proto as P

DEFAULT_CAPS = {"message-tags": "", "server-time": "", "echo-message": "", "sasl": "PLAIN,EXTERNAL", "account-tag": "",
                "away-notify": "", "multi-prefix": "", "extended-join": "", "userhost-in-names": "", "cap-notify": ""}


class Conn:
    def __init__(self, sock, server):
        self.sock, self.server = sock, server
        self.nick = self.user = self.real = ""
        self.caps = set()
        self.registered = self.cap_negotiating = False
        self.account = ""
        self.sasl_pending = False
        self.channels = set()
        self.wlock = threading.Lock()

    @property
    def prefix(self):
        return f"{self.nick}!{self.user}@host.test"

    def send(self, line):
        try:
            with self.wlock:
                self.sock.sendall((line + "\r\n").encode("utf-8"))
        except OSError:
            pass


class FakeIRC:
    def __init__(self, accounts=None, caps=None, clienttagdeny="*,-typing", network="FakeNet", reject_sasl=False):
        self.accounts = accounts if accounts is not None else {"alice": "pw-alice"}
        self.caps_offer = dict(DEFAULT_CAPS if caps is None else caps)
        self.clienttagdeny, self.network = clienttagdeny, network
        self.clients, self.virtual, self.channels = [], {}, {}
        self.received, self.nickserv = [], []
        self.lock = threading.RLock()
        self._ids = itertools.count(1)
        self._srv = socket.socket()
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(8)
        self.port = self._srv.getsockname()[1]
        self._stopped = False
        threading.Thread(target=self._accept, daemon=True).start()

    # ---------------------------------------------------------------- control
    def stop(self):
        self._stopped = True
        try:
            self._srv.close()
        except OSError:
            pass
        for c in list(self.clients):
            try:
                c.sock.close()
            except OSError:
                pass

    def drop_clients(self):
        for c in list(self.clients):
            try:
                c.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def wait_for(self, predicate, timeout=5):
        end = time.time() + timeout
        while time.time() < end:
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def got(self, text):
        return any(text in l for l in self.received)

    def client(self, nick):
        for c in self.clients:
            if c.nick.lower() == nick.lower() and c.registered:
                return c

    # ---------------------------------------------------------------- pretend other users
    def _tags(self, c, extra=None):
        t = {}
        if "server-time" in c.caps:
            t["time"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        if "message-tags" in c.caps:
            t["msgid"] = f"m{next(self._ids)}"
            if extra:
                t.update(extra)
        return t

    def _to_channel(self, chan, prefix, cmd, *params, skip=None, extra_tags=None):
        for c in list(self.clients):
            if c is skip or not c.registered or chan.lower() not in {x.lower() for x in c.channels}:
                continue
            c.send(self._wire(c, prefix, cmd, *params, extra_tags=extra_tags))

    def _wire(self, c, prefix, cmd, *params, extra_tags=None):
        t = self._tags(c, extra_tags) if cmd in ("PRIVMSG", "NOTICE", "TAGMSG") else (
            {"time": self._tags(c).get("time")} if "server-time" in c.caps else {})
        t = {k: v for k, v in t.items() if v is not None}
        head = ("@" + ";".join(f"{k}={P.escape_tag(v)}" for k, v in t.items()) + " ") if t else ""
        return head + f":{prefix} " + P.format_line(cmd, *params)

    def say_as(self, nick, target, text, extra_tags=None, notice=False):
        self.virtual.setdefault(nick, {"user": nick.lower(), "host": "host.test"})
        pfx = f"{nick}!{self.virtual[nick]['user']}@host.test"
        cmd = "NOTICE" if notice else "PRIVMSG"
        if target.startswith("#"):
            self._to_channel(target, pfx, cmd, target, text, extra_tags=extra_tags)
        else:
            c = self.client(target)
            if c:
                c.send(self._wire(c, pfx, cmd, target, text, extra_tags=extra_tags))

    def tag_as(self, nick, target, tags):
        pfx = f"{nick}!{nick.lower()}@host.test"
        for c in list(self.clients):
            if c.registered and "message-tags" in c.caps and (not target.startswith("#") or target.lower() in {x.lower() for x in c.channels}):
                if target.startswith("#") or c.nick.lower() == target.lower():
                    head = "@" + ";".join(f"{k}={P.escape_tag(v)}" for k, v in tags.items()) + " "
                    c.send(head + f":{pfx} TAGMSG {target}")

    def join_as(self, nick, chan, prefix=""):
        self.virtual.setdefault(nick, {"user": nick.lower(), "host": "host.test"})
        self.channels.setdefault(chan.lower(), {"name": chan, "topic": "", "members": {}})["members"][nick] = prefix
        self._to_channel(chan, f"{nick}!{nick.lower()}@host.test", "JOIN", chan, "*", nick)

    def part_as(self, nick, chan, reason="bye"):
        self._to_channel(chan, f"{nick}!{nick.lower()}@host.test", "PART", chan, reason)
        self.channels.get(chan.lower(), {"members": {}})["members"].pop(nick, None)

    def quit_as(self, nick, reason="Quit: gone"):
        for ch in self.channels.values():
            ch["members"].pop(nick, None)
        for c in list(self.clients):
            if c.registered:
                c.send(self._wire(c, f"{nick}!{nick.lower()}@host.test", "QUIT", reason))

    def op(self, chan, nick, sym="@"):
        self.channels[chan.lower()]["members"][nick] = sym

    # ---------------------------------------------------------------- connection handling
    def _accept(self):
        while not self._stopped:
            try:
                s, _ = self._srv.accept()
            except OSError:
                return
            c = Conn(s, self)
            self.clients.append(c)
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c):
        buf = b""
        try:
            while True:
                data = c.sock.recv(4096)
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    m = P.parse(raw.decode("utf-8", "replace"))
                    if m:
                        with self.lock:
                            self.received.append(raw.decode("utf-8", "replace").strip())
                            self._handle(c, m)
        except OSError:
            pass
        finally:
            with self.lock:
                for ch in c.channels:
                    self._to_channel(ch, c.prefix, "QUIT", "Quit: disconnected", skip=c)
                if c in self.clients:
                    self.clients.remove(c)
            try:
                c.sock.close()
            except OSError:
                pass

    def _members_of(self, chan):
        ch = self.channels.setdefault(chan.lower(), {"name": chan, "topic": "", "members": {}})
        return ch

    def _handle(self, c, m):
        cmd, ps = m.command, m.params
        if cmd == "CAP":
            sub = ps[0].upper()
            if sub == "LS":
                c.cap_negotiating = True
                items = [k + (f"={v}" if v else "") for k, v in self.caps_offer.items()]
                half = len(items) // 2
                c.send("CAP * LS * :" + " ".join(items[:half]))                 # multi-line LS on purpose
                c.send("CAP * LS :" + " ".join(items[half:]))
            elif sub == "REQ":
                want = ps[1].split()
                if all(w in self.caps_offer for w in want):
                    c.caps.update(want)
                    c.send("CAP * ACK :" + " ".join(want))
                else:
                    c.send("CAP * NAK :" + " ".join(want))
            elif sub == "END":
                c.cap_negotiating = False
                self._maybe_register(c)
        elif cmd == "NICK":
            old = c.nick
            if any(o is not c and o.nick.lower() == ps[0].lower() for o in self.clients) and not c.registered:
                c.send(f":irc.test 433 * {ps[0]} :Nickname is already in use")
                return
            c.nick = ps[0]
            if c.registered:
                for ch in c.channels:
                    mem = self._members_of(ch)["members"]
                    if old in mem:
                        mem[ps[0]] = mem.pop(old)
                for o in self.clients:
                    if o.registered:
                        o.send(self._wire(o, f"{old}!{c.user}@host.test", "NICK", ps[0]))
            else:
                self._maybe_register(c)
        elif cmd == "USER":
            c.user, c.real = ps[0], ps[3] if len(ps) > 3 else ""
            self._maybe_register(c)
        elif cmd == "AUTHENTICATE":
            if ps[0] == "PLAIN":
                c.send("AUTHENTICATE +")
            else:
                try:
                    _az, user, pw = base64.b64decode(ps[0]).decode().split("\0")
                except ValueError:
                    c.send(f":irc.test 905 {c.nick or '*'} :SASL message too long")
                    return
                if self.accounts.get(user) == pw:
                    c.account = user
                    c.send(f":irc.test 900 {c.nick or '*'} {c.nick or '*'}!{c.user}@host.test {user} :You are now logged in as {user}")
                    c.send(f":irc.test 903 {c.nick or '*'} :SASL authentication successful")
                else:
                    c.send(f":irc.test 904 {c.nick or '*'} :SASL authentication failed")
        elif cmd == "PING":
            c.send(f":irc.test PONG irc.test :{ps[0] if ps else ''}")
        elif not c.registered:
            return
        elif cmd == "JOIN":
            for chan in ps[0].split(","):
                ch = self._members_of(chan)
                ch["members"].setdefault(c.nick, "")
                c.channels.add(chan)
                acct = c.account or "*"
                for o in self.clients:
                    if o.registered and chan.lower() in {x.lower() for x in o.channels}:
                        if "extended-join" in o.caps:
                            o.send(self._wire(o, c.prefix, "JOIN", chan, acct, c.real or c.nick))
                        else:
                            o.send(self._wire(o, c.prefix, "JOIN", chan))
                if ch["topic"]:
                    c.send(f":irc.test 332 {c.nick} {chan} :{ch['topic']}")
                names = []
                for n, pfx in ch["members"].items():
                    names.append(pfx + n)
                c.send(f":irc.test 353 {c.nick} = {chan} :" + " ".join(names))
                c.send(f":irc.test 366 {c.nick} {chan} :End of /NAMES list.")
        elif cmd == "PART":
            chan = ps[0]
            self._to_channel(chan, c.prefix, "PART", chan, *(ps[1:2]))
            c.channels = {x for x in c.channels if x.lower() != chan.lower()}
            self._members_of(chan)["members"].pop(c.nick, None)
        elif cmd == "TOPIC":
            ch = self._members_of(ps[0])
            if len(ps) > 1:
                ch["topic"] = ps[1]
                self._to_channel(ps[0], c.prefix, "TOPIC", ps[0], ps[1])
        elif cmd in ("PRIVMSG", "NOTICE"):
            target, text = ps[0], ps[1]
            if target.lower() == "nickserv":
                self.nickserv.append(text)
                verb = text.split()[0].upper() if text.split() else ""
                reply = {"IDENTIFY": "You are now identified.", "REGISTER": "An email has been sent with a verification code.",
                         "VERIFY": "verification successful"}.get(verb, "Unknown command.")
                if "echo-message" in c.caps:                          # real servers echo this too, password and all
                    c.send(self._wire(c, c.prefix, cmd, target, text))
                c.send(f":NickServ!NickServ@services.test NOTICE {c.nick} :{reply}")
                return
            extra = {k: v for k, v in m.tags.items() if k.startswith("+")}
            echo_id = None
            if target.startswith("#"):
                if target.lower() not in {x.lower() for x in c.channels}:
                    c.send(f":irc.test 404 {c.nick} {target} :Cannot send to channel")
                    return
                self._to_channel(target, c.prefix, cmd, target, text, skip=c, extra_tags=extra)
            else:
                o = self.client(target)
                if o is None:
                    c.send(f":irc.test 401 {c.nick} {target} :No such nick/channel")
                    return
                o.send(self._wire(o, c.prefix, cmd, target, text, extra_tags=extra))
            if "echo-message" in c.caps:
                c.send(self._wire(c, c.prefix, cmd, target, text, extra_tags=extra))
        elif cmd == "TAGMSG":
            target = ps[0]
            for o in list(self.clients):
                if o is c or not o.registered or "message-tags" not in o.caps:
                    continue
                if (target.startswith("#") and target.lower() in {x.lower() for x in o.channels}) or o.nick.lower() == target.lower():
                    head = "@" + ";".join(f"{k}={P.escape_tag(v)}" for k, v in m.tags.items()) + " "
                    o.send(head + f":{c.prefix} TAGMSG {target}")
        elif cmd == "WHOIS":
            n = ps[0]
            c.send(f":irc.test 311 {c.nick} {n} {n.lower()} host.test * :{n} Real")
            c.send(f":irc.test 312 {c.nick} {n} irc.test :Fake server")
            c.send(f":irc.test 318 {c.nick} {n} :End of /WHOIS list.")
        elif cmd == "QUIT":
            try:
                c.sock.close()
            except OSError:
                pass
        elif cmd == "AWAY":
            for o in self.clients:
                if o.registered and "away-notify" in o.caps and o is not c:
                    o.send(self._wire(o, c.prefix, "AWAY", *(ps[:1])))
        else:
            c.send(f":irc.test 421 {c.nick} {cmd} :Unknown command")

    def _maybe_register(self, c):
        if c.registered or c.cap_negotiating or not c.nick or not c.user:
            return
        c.registered = True
        c.send(f":irc.test 001 {c.nick} :Welcome to {self.network}, {c.nick}")
        tokens = f"CHANTYPES=# PREFIX=(ov)@+ CASEMAPPING=rfc1459 NETWORK={self.network} CHANMODES=b,k,l,imnpst"
        if self.clienttagdeny is not None:
            tokens += f" CLIENTTAGDENY={self.clienttagdeny}"
        c.send(f":irc.test 005 {c.nick} {tokens} :are supported by this server")
        c.send(f":irc.test 372 {c.nick} :- Fake message of the day")
        c.send(f":irc.test 376 {c.nick} :End of /MOTD command.")
