"""A small IRC client (one TCP connection) that runs in its own thread.

* TLS with certificate checking (can be relaxed per account for self-signed servers).
* IRCv3 capability negotiation (CAP LS 302), SASL PLAIN, message tags, server-time, echo-message, ...
* Tracks channels, topics, nick lists, accounts, away state, and what the server supports (ISUPPORT).
* Reports everything that happens to a callback as plain dicts: emit({"type": ..., ...}).
* Reconnects by itself with a growing delay -- but never after a rejected login (that would only lock you out).

It knows nothing about Flask or the database; irc_manager.py connects it to those.
"""
import ipaddress
import queue
import socket
import ssl
import threading
import time
from datetime import datetime, timezone

import config
import irc_proto as P

CLIENT_NAME = "newroom IRC client"
SERVICES_NICK = "NickServ"
# Network services bots. What YOU send them can contain passwords (IDENTIFY, REGISTER, ...), and servers with the
# echo-message capability send your own messages back to you, so those echoes are dropped and never logged.
SERVICE_NICKS = {"nickserv", "chanserv", "memoserv", "operserv", "hostserv", "botserv", "saslserv", "authserv"}

# Capabilities we ask for when the server offers them. 'sasl' is only requested when we have a password to send.
WANT_CAPS = ["message-tags", "server-time", "echo-message", "account-tag", "account-notify", "extended-join",
             "away-notify", "multi-prefix", "userhost-in-names", "chghost", "cap-notify", "sasl"]

# numerics shown as plain information lines (everything else >= 400 is shown as an error)
_SILENT = {"002", "003", "004", "250", "251", "252", "253", "254", "255", "265", "266", "328", "329", "366", "375", "376",
           "422", "318", "333", "354", "396"}


class NotConnected(Exception):
    pass


def check_host(host):
    """Resolve `host` and return the addresses we are allowed to connect to. Loopback, link-local and
    unspecified addresses are refused (unless ROOM_IRC_ALLOW_LOCAL=1): this server must not be usable to
    poke services on the machine it runs on. Ordinary LAN addresses are fine (you may run your own IRC server)."""
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise ValueError(f"Could not find that server ({e.strerror or e}).")
    ok = []
    for fam, _t, _p, _c, sa in infos:
        ip = ipaddress.ip_address(sa[0].split("%")[0])
        if (ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast) and not config.IRC_ALLOW_LOCAL:
            continue
        ok.append((fam, sa[0]))
    if not ok:
        raise ValueError("That address is not allowed (it points at this machine or a reserved range).")
    return ok


def _iso_now():
    return datetime.now(timezone.utc).isoformat()


def _iso(tag):
    try:
        return datetime.fromisoformat(tag.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
    except (ValueError, AttributeError):
        return _iso_now()


class Chan:
    def __init__(self, name):
        self.name, self.topic, self.topic_by = name, "", ""
        self.names = {}            # nick key -> {"nick", "prefix"}
        self.pending = None        # a NAMES list being received
        self.modes = ""


class IRCClient(threading.Thread):
    def __init__(self, cfg, emit):
        """cfg: host, port, tls, tls_verify, nick, account, password, sasl, realname, autojoin(list)."""
        super().__init__(daemon=True, name="irc-" + str(cfg.get("nick")))
        self.cfg = dict(cfg)
        self._emit_cb = emit
        self._lock = threading.RLock()
        self._wlock = threading.Lock()
        self._halt = threading.Event()
        self._sock = None
        self._out = queue.Queue()
        self.fatal = False
        self.state = "idle"
        self.error = ""
        self._reset()

    # ------------------------------------------------------------------ state
    def _reset(self):
        with self._lock:
            self.registered = False
            self.nick = self.cfg["nick"]
            self.account = ""
            self.authenticated = False
            self.caps_avail, self.caps = {}, set()
            self.isupport = {}
            self.casemap, self.chantypes = "rfc1459", "#&"
            self.prefix_map, self.prefix_chars = {"o": "@", "v": "+"}, "@+"
            self.chanmodes = ("beI", "k", "l", "imnpst")
            self.network, self.server_name = "", ""
            self.chans, self.users = {}, {}
            self.own_userhost = ""
            self._ls_buf, self._pending_reqs = {}, []
            self._cap_phase, self._ready_done, self._sasl_active = True, False, False
            self._nick_tries = 0
            self._served_notices = 0

    def _key(self, s):
        return P.casefold(s, self.casemap)

    def _eq(self, a, b):
        return self._key(a) == self._key(b)

    def _emit(self, type_, **kw):
        kw["type"] = type_
        try:
            self._emit_cb(kw)
        except Exception as e:                                           # a bug in a listener must not kill the connection
            print(f"[irc] listener error on {type_}: {e!r}")

    def _set_state(self, state, error="", **extra):
        self.state, self.error = state, error
        self._emit("state", state=state, error=error, **extra)

    # ------------------------------------------------------------------ public read API
    def is_ready(self):
        return self.registered and self.state == "connected"

    @property
    def online(self):
        """Registered AND the server has finished its welcome (capabilities, ISUPPORT and the MOTD are in)."""
        return self.is_ready() and self._ready_done

    def snapshot(self):
        with self._lock:
            return {"state": self.state, "error": self.error, "nick": self.nick, "account": self.account,
                    "authenticated": self.authenticated, "network": self.network or self.cfg["host"],
                    "server": self.server_name, "caps": sorted(self.caps), "casemap": self.casemap,
                    "chantypes": self.chantypes, "prefix": self.prefix_chars,
                    "clienttagdeny": self.isupport.get("CLIENTTAGDENY", ""), "echo": "echo-message" in self.caps}

    def names(self, channel, limit=3000):
        """[{nick, prefix, away, account}] for a channel, ops first."""
        with self._lock:
            c = self.chans.get(self._key(channel))
            if not c:
                return [], 0
            rank = {ch: i for i, ch in enumerate(self.prefix_chars)}
            rows = []
            for e in c.names.values():
                u = self.users.get(self._key(e["nick"]), {})
                rows.append({"nick": e["nick"], "prefix": e["prefix"], "away": bool(u.get("away")), "account": u.get("account", "")})
            rows.sort(key=lambda r: (rank.get(r["prefix"][:1], 99), r["nick"].lower()))
            return rows[:limit], len(rows)

    def topic_of(self, channel):
        with self._lock:
            c = self.chans.get(self._key(channel))
            return (c.topic, c.topic_by) if c else ("", "")

    def in_channel(self, channel):
        with self._lock:
            return self._key(channel) in self.chans

    def user_info(self, nick, channel=None):
        with self._lock:
            u = dict(self.users.get(self._key(nick), {"nick": nick}))
            if channel:
                c = self.chans.get(self._key(channel))
                e = c.names.get(self._key(nick)) if c else None
                if e:
                    u["prefix"] = e["prefix"]
            return u

    def cap_enabled(self, name):
        return name in self.caps

    def max_text_bytes(self, target):
        """How many bytes of message text fit in one line (the server adds our user@host when relaying)."""
        used = len(f"PRIVMSG {target} :".encode()) + 1 + len(self.nick) + 1 + len(self.own_userhost or ("~" + "x" * 9 + "@" + "x" * 63)) + 2
        return max(100, 512 - used - 8)

    # ------------------------------------------------------------------ public write API
    def send(self, line):
        """Queue a line (flood-controlled). Raises NotConnected unless we are registered."""
        if not self.is_ready():
            raise NotConnected("Not connected to IRC.")
        self._out.put(line)

    def send_many(self, lines):
        if not self.is_ready():
            raise NotConnected("Not connected to IRC.")
        for ln in lines:
            self._out.put(ln)

    def privmsg(self, target, text, tags=None, action=False):
        """Send text (split into several lines if needed). Returns the lines of text that were sent."""
        budget = self.max_text_bytes(target) - (9 if action else 0)
        parts = P.split_text(text, budget)
        if not parts:
            raise ValueError("Message is empty.")
        lines = []
        for i, part in enumerate(parts):
            body = f"\x01ACTION {part}\x01" if action else part
            lines.append(P.format_line("PRIVMSG", target, body, tags=tags if i == 0 else None, trailing=True))
        self.send_many(lines)
        return parts

    def notice(self, target, text):
        parts = P.split_text(text, self.max_text_bytes(target))
        self.send_many([P.format_line("NOTICE", target, p, trailing=True) for p in parts])
        return parts

    def tagmsg(self, target, tags):
        if "message-tags" not in self.caps:
            raise NotConnected("This server does not support message tags.")
        self.send(P.format_line("TAGMSG", target, tags=tags))

    def join(self, channel, key=None):
        self.send(P.format_line("JOIN", channel, *([key] if key else [])))

    def part(self, channel, reason=""):
        self.send(P.format_line("PART", channel, *([reason] if reason else []), trailing=True))

    def set_nick(self, nick):
        self.send(P.format_line("NICK", nick))

    def whois(self, nick):
        self.send(P.format_line("WHOIS", nick))

    def set_topic(self, channel, text=None):
        self.send(P.format_line("TOPIC", channel, *([text] if text is not None else []), trailing=True))

    def set_away(self, message=""):
        self.send(P.format_line("AWAY", *([message] if message else []), trailing=True))

    def raw(self, line):
        self.send(P.clean_param(line)[:480])

    def services(self, text):
        """A private command to NickServ (never logged by the manager)."""
        self.send(P.format_line("PRIVMSG", SERVICES_NICK, text, trailing=True))

    def quit(self, reason="Leaving"):
        try:
            if self.registered:
                self._send_now(P.format_line("QUIT", reason, trailing=True))
        except OSError:
            pass
        self.stop()

    def stop(self):
        self._halt.set()
        self._close_socket()

    def _close_socket(self):
        s = self._sock
        if s is not None:
            for fn in (lambda: s.shutdown(socket.SHUT_RDWR), s.close):
                try:
                    fn()
                except OSError:
                    pass

    # ------------------------------------------------------------------ thread: connect / reconnect loop
    def run(self):
        attempt = 0
        while not self._halt.is_set():
            err, was_up = "", False
            try:
                was_up = self._session()
            except (OSError, ssl.SSLError, ValueError, ConnectionError) as e:
                err = str(e) or e.__class__.__name__
            except Exception as e:                                       # never let the thread die silently
                err = f"internal error: {e!r}"
            if self.fatal:
                self._set_state("failed", self.error or err)
                return
            if self._halt.is_set():
                break
            attempt = 1 if was_up else attempt + 1
            delay = min(300, 5 * 2 ** min(attempt - 1, 6))
            self._set_state("disconnected", self.error or err or "Connection closed.", retry_in=delay)
            if self._halt.wait(delay):
                break
        self._set_state("stopped")

    def _session(self):
        """One connection, from TCP connect to disconnect. Returns True if we got as far as registering."""
        self._reset()
        self.error = ""
        self._set_state("connecting")
        sock = self._connect()
        self._sock = sock
        self._out = queue.Queue()
        writer = threading.Thread(target=self._writer, args=(sock, self._out), daemon=True, name=self.name + "-w")
        writer.start()
        up = False
        try:
            c = self.cfg
            self._send_now("CAP LS 302")
            self._send_now(P.format_line("NICK", self.nick))
            self._send_now(P.format_line("USER", c["nick"][:20], "0", "*", c.get("realname") or c["nick"]))
            buf, last_rx, pinged = b"", time.time(), False
            while not self._halt.is_set():
                try:
                    data = sock.recv(8192)
                except (socket.timeout, TimeoutError):
                    idle = time.time() - last_rx
                    if idle > 240 and not pinged:
                        pinged = True
                        self._send_now("PING :keepalive")
                    if idle > 330:
                        raise ConnectionError("Ping timeout (no data from the server).")
                    continue
                except ssl.SSLError as e:
                    if "timed out" in str(e):
                        continue
                    raise
                if not data:
                    raise ConnectionError("The server closed the connection.")
                last_rx, pinged = time.time(), False
                buf += data
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    if len(raw) > 16384:
                        continue
                    msg = P.parse(raw.decode("utf-8", "replace"))
                    if msg is not None:
                        with self._lock:
                            self._dispatch(msg)
                        up = up or self.registered
                if len(buf) > 65536:
                    buf = b""
            return up
        finally:
            self._out.put(None)
            self._close_socket()
            self._sock = None
            self.registered = False

    def _connect(self):
        c = self.cfg
        last = None
        for fam, ip in check_host(c["host"]):
            try:
                sock = socket.create_connection((ip, int(c["port"])), timeout=20)
                break
            except OSError as e:
                last = e
        else:
            raise ConnectionError(f"Could not connect to {c['host']}:{c['port']} ({last}).")
        if c.get("tls", True):
            ctx = ssl.create_default_context()
            if not c.get("tls_verify", True):
                ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
            try:
                sock = ctx.wrap_socket(sock, server_hostname=c["host"])
            except ssl.SSLCertVerificationError as e:
                sock.close()
                raise ConnectionError("The server's certificate was rejected (" + (e.verify_message or str(e)) + "). "
                                      "If you trust this server, untick 'verify certificate' in the connection settings.")
        sock.settimeout(15)
        return sock

    # ------------------------------------------------------------------ writing
    def _write(self, sock, line):
        data = line.encode("utf-8", "replace")
        if b"\r" in data or b"\n" in data or b"\0" in data:
            data = data.replace(b"\r", b" ").replace(b"\n", b" ").replace(b"\0", b"")
        with self._wlock:
            sock.sendall(data[:8700] + b"\r\n")

    def _send_now(self, line):
        sock = self._sock
        if sock is None:
            raise NotConnected("Not connected to IRC.")
        self._write(sock, line)

    def _writer(self, sock, q):
        """Sends queued lines with flood control (a burst of 5, then about 1.7 lines per second)."""
        tokens, last = 5.0, time.time()
        while True:
            try:
                item = q.get(timeout=1)
            except queue.Empty:
                if self._halt.is_set() or self._sock is not sock:
                    return
                continue
            if item is None:
                return
            if isinstance(item, tuple):                                  # ("sleep", seconds)
                time.sleep(item[1])
                continue
            now = time.time()
            tokens, last = min(5.0, tokens + (now - last) / 0.6), now
            if tokens < 1:
                time.sleep((1 - tokens) * 0.6)
                tokens = 1.0
                last = time.time()
            tokens -= 1
            try:
                self._write(sock, item)
            except OSError:
                return

    # ------------------------------------------------------------------ line handling
    def _dispatch(self, m):
        h = getattr(self, "_h_" + m.command, None)
        if h is not None:
            h(m)
        elif m.command.isdigit():
            self._numeric(m)

    def _ts(self, m):
        t = m.tags.get("time")
        return _iso(t) if t else _iso_now()

    def _remember(self, nick, userhost="", **extra):
        k = self._key(nick)
        u = self.users.get(k)
        if u is None:
            if len(self.users) > 5000:
                for old in list(self.users)[:500]:
                    del self.users[old]
            u = self.users[k] = {"nick": nick}
        u["nick"] = nick
        if userhost and "@" in userhost:
            u["user"], u["host"] = userhost.split("@", 1)
        u.update({k2: v for k2, v in extra.items() if v is not None})
        return u

    # ---- registration / capabilities
    def _h_PING(self, m):
        self._send_now(P.format_line("PONG", *(m.params[:1] or [""])))

    def _h_CAP(self, m):
        if len(m.params) < 2:
            return
        sub, text = m.params[1].upper(), m.params[-1]
        if sub == "LS":
            self._ls_buf.update(P.parse_caps(text))
            if len(m.params) > 3 and m.params[2] == "*":
                return                                                    # more lines are coming
            self.caps_avail.update(self._ls_buf)
            self._ls_buf = {}
            self._request_caps()
        elif sub == "NEW":
            self.caps_avail.update(P.parse_caps(text))
            self._request_caps()
        elif sub == "DEL":
            for n in P.parse_caps(text):
                self.caps_avail.pop(n, None)
                self.caps.discard(n)
            self._emit("features")
        elif sub == "ACK":
            for item in text.split():
                n = item.lstrip("~=").lower()
                if n.startswith("-"):
                    self.caps.discard(n[1:])
                else:
                    self.caps.add(n)
            self._pending_reqs = [r for r in self._pending_reqs if set(r) != {x.lstrip("~=-").lower() for x in text.split()}]
            self._caps_settled()
            self._emit("features")
        elif sub == "NAK":
            names = [x.lower() for x in text.split()]
            self._pending_reqs = [r for r in self._pending_reqs if set(r) != set(names)]
            if len(names) > 1:                                            # the server refused the bundle: ask one by one
                for n in names:
                    self._pending_reqs.append([n])
                    self._send_now(P.format_line("CAP", "REQ", n))
            self._caps_settled()

    def _can_sasl(self):
        if not (self.cfg.get("sasl") and self.cfg.get("password")):
            return False
        v = self.caps_avail.get("sasl")
        return v is not None and (v == "" or "PLAIN" in v.upper().split(","))

    def _request_caps(self):
        want = [c for c in WANT_CAPS if c in self.caps_avail and c not in self.caps
                and not any(c in r for r in self._pending_reqs) and (c != "sasl" or self._can_sasl())]
        for i in range(0, len(want), 12):
            chunk = want[i:i + 12]
            self._pending_reqs.append(chunk)
            self._send_now(P.format_line("CAP", "REQ", " ".join(chunk)))
        self._caps_settled()

    def _caps_settled(self):
        if not self._cap_phase or self._pending_reqs:
            return
        if "sasl" in self.caps and self._can_sasl() and not self._sasl_active and not self.authenticated:
            self._sasl_active = True
            self._send_now("AUTHENTICATE PLAIN")
        elif not self._sasl_active:
            self._cap_end()

    def _cap_end(self):
        if self._cap_phase:
            self._cap_phase = False
            self._send_now("CAP END")

    # ---- SASL
    def _h_AUTHENTICATE(self, m):
        if m.params and m.params[0] == "+" and self._sasl_active:
            c = self.cfg
            payload = P.sasl_plain_payload("", c.get("account") or c["nick"], c["password"])
            for chunk in P.sasl_chunks(payload):
                self._send_now("AUTHENTICATE " + chunk)

    def _auth_failed(self, text):
        self.fatal = True
        self.error = "Login was rejected by the server: " + text
        self._emit("auth_failed", text=self.error)
        try:
            self._send_now("QUIT :login failed")
        except (OSError, NotConnected):
            pass
        self._halt.set()
        self._close_socket()

    def _h_900(self, m):                                                  # RPL_LOGGEDIN
        if len(m.params) >= 3:
            self.account = m.params[2]
        if len(m.params) > 1 and "!" in m.params[1]:
            self.own_userhost = m.params[1].split("!", 1)[1]

    def _h_903(self, m):                                                  # SASL success
        self.authenticated, self._sasl_active = True, False
        if not self.account:
            self.account = self.cfg.get("account") or self.cfg["nick"]
        self._emit("server", text="Logged in with SASL as " + self.account + ".", numeric="903", error=False)
        self._cap_end()

    def _h_907(self, m):                                                  # already authenticated
        self.authenticated, self._sasl_active = True, False
        self._cap_end()

    def _h_902(self, m):
        self._auth_failed(m.params[-1] if m.params else "the account is locked")

    def _h_904(self, m):
        self._auth_failed(m.params[-1] if m.params else "wrong account name or password")

    def _h_905(self, m):
        self._auth_failed(m.params[-1] if m.params else "the login message was too long")

    def _h_906(self, m):
        self._auth_failed(m.params[-1] if m.params else "login aborted")

    def _h_908(self, m):                                                  # server lists mechanisms: PLAIN is not among them
        if self._sasl_active:
            self._sasl_active = False
            self._emit("server", text="The server does not accept SASL PLAIN (it offers: " + (m.params[1] if len(m.params) > 1 else "?") + ").",
                       numeric="908", error=True)
            self._cap_end()

    # ---- registration complete
    def _h_001(self, m):
        self.registered = True
        if m.params:
            self.nick = m.params[0]
        self.server_name = m.prefix or ""
        self._set_state("connected")
        self._emit("registered", nick=self.nick, server=self.server_name)
        self._emit("server", text=m.params[-1] if m.params else "Connected.", numeric="001", error=False)

    def _h_005(self, m):
        for tok in m.params[1:-1]:
            if tok.startswith("-"):
                self.isupport.pop(tok[1:].upper(), None)
                continue
            k, _, v = tok.partition("=")
            self.isupport[k.upper()] = v
        iso = self.isupport
        self.casemap = "ascii" if iso.get("CASEMAPPING") == "ascii" else "rfc1459"
        self.chantypes = iso.get("CHANTYPES", "#&") or "#&"
        self.prefix_map, self.prefix_chars = P.parse_prefix_token(iso.get("PREFIX", ""))
        parts = (iso.get("CHANMODES") or "").split(",")
        if len(parts) >= 4:
            self.chanmodes = tuple(parts[:4])
        self.network = iso.get("NETWORK", self.network)
        self._emit("features")

    def _h_376(self, m):
        self._ready()

    _h_422 = _h_376

    def _ready(self):
        """Registration and MOTD are finished: log in with NickServ if SASL was not possible, then join channels."""
        if self._ready_done:
            return
        self._ready_done = True
        c = self.cfg
        if c.get("password") and not self.authenticated and c.get("identify_fallback", True) and c.get("sasl"):
            self._out.put(P.format_line("PRIVMSG", SERVICES_NICK, f"IDENTIFY {c.get('account') or c['nick']} {c['password']}", trailing=True))
            self._out.put(("sleep", 1.5))
            self._emit("server", text="This server does not offer SASL login, so I sent your password to NickServ instead.", numeric="", error=False)
        chans = [x for x in c.get("autojoin", []) if P.valid_channel(x, self.chantypes)]
        for i in range(0, len(chans), 5):
            self._out.put(P.format_line("JOIN", ",".join(chans[i:i + 5])))
        self._emit("ready")

    # ---- nick problems
    def _h_433(self, m):                                                  # nickname in use
        if not self.registered and self._nick_tries < 4:
            self._nick_tries += 1
            self.nick = self.cfg["nick"] + "_" * self._nick_tries
            self._emit("server", text=f"The nickname {self.cfg['nick']} is already in use; trying {self.nick}.", numeric="433", error=True)
            self._send_now(P.format_line("NICK", self.nick))
        else:
            self._numeric(m)

    def _h_432(self, m):                                                  # erroneous nickname
        if not self.registered:
            self.fatal = True
            self.error = "The server does not accept that nickname."
            self._send_now("QUIT")
            self._halt.set()
            self._close_socket()
        else:
            self._numeric(m)

    # ---- messages
    def _target_buffer(self, target, sender, own, is_chan):
        if is_chan:
            return target
        return target if own else sender

    def _h_PRIVMSG(self, m, kind="user"):
        if len(m.params) < 2:
            return
        target, text = m.params[0], m.params[1]
        sender = m.nick or m.prefix
        own = self._eq(sender, self.nick)
        if own and target.lower() in SERVICE_NICKS:
            return
        probe = target.lstrip(self.prefix_chars)
        if probe[:1] in self.chantypes:
            target = probe
        is_chan = target[:1] in self.chantypes
        if m.userhost:
            self._remember(sender, m.userhost, account=m.tags.get("account", None) if "account-tag" in self.caps else None)
            if own:
                self.own_userhost = m.userhost
        if text.startswith("\x01"):
            body = text.strip("\x01")
            cmd, _, arg = body.partition(" ")
            cmd = cmd.upper()
            if cmd == "ACTION" and kind == "user":
                kind, text = "action", arg
            else:
                if kind == "user" and not own:                            # a CTCP request: answer the polite ones
                    if cmd == "VERSION":
                        self.send_notice_ctcp(sender, f"VERSION {CLIENT_NAME}")
                    elif cmd == "PING":
                        self.send_notice_ctcp(sender, f"PING {arg[:40]}")
                    self._emit("server", text=f"CTCP {cmd} from {sender}", numeric="", error=False)
                return
        text = P.strip_format(text)
        server_msg = m.is_server or not m.prefix
        if kind == "notice" and (not is_chan) or server_msg:
            buf = None                                                    # server notices and notices to me go to the status window
        else:
            buf = self._target_buffer(target, sender, own, is_chan)
        ukey = self._key(sender)
        account = m.tags.get("account") or self.users.get(ukey, {}).get("account", "")
        if account == "*":
            account = ""
        self._emit("message", kind=kind, buffer=buf, sender=sender, userhost=m.userhost, text=text, ts=self._ts(m),
                   own=own, msgid=m.tags.get("msgid", ""), account=account, is_channel=is_chan, target=target,
                   reply=m.tags.get("+draft/reply") or m.tags.get("+reply") or "")

    def _h_NOTICE(self, m):
        self._h_PRIVMSG(m, kind="notice")

    def send_notice_ctcp(self, nick, body):
        try:
            self.send(P.format_line("NOTICE", nick, f"\x01{body}\x01", trailing=True))
        except NotConnected:
            pass

    def _h_TAGMSG(self, m):
        if not m.params:
            return
        target, sender = m.params[0], m.nick
        own = self._eq(sender, self.nick)
        is_chan = target[:1] in self.chantypes
        buf = self._target_buffer(target, sender, own, is_chan)
        t = m.tags
        typing = t.get("+typing", t.get("+draft/typing"))
        if typing is not None and not own:
            self._emit("typing", buffer=buf, nick=sender, state=typing)
        react, unreact = t.get("+draft/react") or t.get("+react"), t.get("+draft/unreact") or t.get("+unreact")
        reply = t.get("+draft/reply") or t.get("+reply")
        if (react or unreact) and reply:
            self._emit("react", buffer=buf, nick=sender, msgid=reply, emoji=react or unreact, remove=bool(unreact), own=own)

    # ---- channel membership
    def _h_JOIN(self, m):
        if not m.params:
            return
        chan, nick = m.params[0], m.nick
        account, real = None, None
        if "extended-join" in self.caps and len(m.params) >= 3:
            account = "" if m.params[1] == "*" else m.params[1]
            real = m.params[2]
        self._remember(nick, m.userhost, account=account, realname=real)
        own = self._eq(nick, self.nick)
        k = self._key(chan)
        if own:
            self.own_userhost = m.userhost or self.own_userhost
            self.chans[k] = Chan(chan)
            self.chans[k].names[self._key(nick)] = {"nick": nick, "prefix": ""}
        else:
            c = self.chans.get(k)
            if c is not None:
                c.names[self._key(nick)] = {"nick": nick, "prefix": ""}
        self._emit("join", channel=chan, nick=nick, userhost=m.userhost, own=own, account=account or "", ts=self._ts(m))

    def _h_PART(self, m):
        if not m.params:
            return
        chan, nick, reason = m.params[0], m.nick, (m.params[1] if len(m.params) > 1 else "")
        own, k = self._eq(m.nick, self.nick), self._key(m.params[0])
        if own:
            self.chans.pop(k, None)
        elif k in self.chans:
            self.chans[k].names.pop(self._key(nick), None)
        self._emit("part", channel=chan, nick=nick, userhost=m.userhost, reason=P.strip_format(reason), own=own, ts=self._ts(m))

    def _h_KICK(self, m):
        if len(m.params) < 2:
            return
        chan, victim = m.params[0], m.params[1]
        reason = m.params[2] if len(m.params) > 2 else ""
        own, k = self._eq(victim, self.nick), self._key(chan)
        if own:
            self.chans.pop(k, None)
        elif k in self.chans:
            self.chans[k].names.pop(self._key(victim), None)
        self._emit("kick", channel=chan, nick=victim, by=m.nick, reason=P.strip_format(reason), own=own, ts=self._ts(m))

    def _h_QUIT(self, m):
        nick, k = m.nick, self._key(m.nick)
        reason = m.params[0] if m.params else ""
        in_chans = [c.name for c in self.chans.values() if k in c.names]
        for c in self.chans.values():
            c.names.pop(k, None)
        self.users.pop(k, None)
        self._emit("quit", nick=nick, reason=P.strip_format(reason), channels=in_chans, ts=self._ts(m))

    def _h_NICK(self, m):
        if not m.params:
            return
        old, new = m.nick, m.params[0]
        ok, nk = self._key(old), self._key(new)
        own = self._eq(old, self.nick)
        in_chans = []
        for c in self.chans.values():
            e = c.names.pop(ok, None)
            if e is not None:
                e["nick"] = new
                c.names[nk] = e
                in_chans.append(c.name)
        u = self.users.pop(ok, None)
        if u is not None:
            u["nick"] = new
            self.users[nk] = u
        if own:
            self.nick = new
        self._emit("nick", old=old, new=new, channels=in_chans, own=own, ts=self._ts(m))

    def _h_TOPIC(self, m):
        if len(m.params) < 2:
            return
        c = self.chans.get(self._key(m.params[0]))
        text = P.strip_format(m.params[1])
        if c:
            c.topic, c.topic_by = text, m.nick
        self._emit("topic", channel=m.params[0], text=text, by=m.nick, initial=False, ts=self._ts(m))

    def _h_332(self, m):                                                  # RPL_TOPIC
        if len(m.params) >= 3:
            c = self.chans.get(self._key(m.params[1]))
            text = P.strip_format(m.params[2])
            if c:
                c.topic = text
            self._emit("topic", channel=m.params[1], text=text, by="", initial=True, ts=_iso_now())

    def _h_333(self, m):                                                  # who set the topic
        if len(m.params) >= 3:
            c = self.chans.get(self._key(m.params[1]))
            if c:
                c.topic_by = m.params[2].split("!", 1)[0]

    def _h_353(self, m):                                                  # NAMES reply
        if len(m.params) < 4:
            return
        c = self.chans.get(self._key(m.params[2]))
        if c is None:
            return
        if c.pending is None:
            c.pending = {}
        for entry in m.params[3].split():
            i = 0
            while i < len(entry) and entry[i] in self.prefix_chars:
                i += 1
            prefix, rest = entry[:i], entry[i:]
            nick, _, uh = rest.partition("!")
            if not nick:
                continue
            c.pending[self._key(nick)] = {"nick": nick, "prefix": prefix}
            self._remember(nick, uh)

    def _h_366(self, m):                                                  # end of NAMES
        if len(m.params) >= 2:
            c = self.chans.get(self._key(m.params[1]))
            if c is not None and c.pending is not None:
                c.names, c.pending = c.pending, None
                self._emit("names", channel=c.name)

    def _h_MODE(self, m):
        if len(m.params) < 2 or m.params[0][:1] not in self.chantypes:
            return
        chan, modes, args = m.params[0], m.params[1], list(m.params[2:])
        c = self.chans.get(self._key(chan))
        sign, ai = "+", 0
        a_modes, b_modes, c_modes, _d = self.chanmodes
        for ch in modes:
            if ch in "+-":
                sign = ch
            elif ch in self.prefix_map:
                nick = args[ai] if ai < len(args) else ""
                ai += 1
                e = c.names.get(self._key(nick)) if c else None
                if e is not None:
                    sym = self.prefix_map[ch]
                    have = set(e["prefix"])
                    (have.add if sign == "+" else have.discard)(sym)
                    e["prefix"] = "".join(x for x in self.prefix_chars if x in have)
            elif ch in a_modes or ch in b_modes or (ch in c_modes and sign == "+"):
                ai += 1
        self._emit("mode", channel=chan, by=m.nick or m.prefix, text=" ".join([modes] + args), ts=self._ts(m))
        self._emit("names", channel=chan)

    def _h_AWAY(self, m):
        u = self._remember(m.nick, m.userhost, away=bool(m.params))
        self._emit("roster")

    def _h_ACCOUNT(self, m):
        acct = m.params[0] if m.params else ""
        self._remember(m.nick, m.userhost, account="" if acct == "*" else acct)
        self._emit("roster")

    def _h_CHGHOST(self, m):
        if len(m.params) >= 2:
            self._remember(m.nick, f"{m.params[0]}@{m.params[1]}")

    def _h_INVITE(self, m):
        if len(m.params) >= 2:
            self._emit("server", text=f"{m.nick} invited you to {m.params[1]}.", numeric="INVITE", error=False)

    def _h_ERROR(self, m):
        raise ConnectionError(m.params[0] if m.params else "The server ended the connection.")

    # ---- numerics
    def _numeric(self, m):
        n, ps = m.command, m.params
        if n in _SILENT:
            return
        if n == "372":
            self._emit("server", text=(ps[-1] if ps else "").lstrip("- "), numeric=n, error=False, motd=True)
            return
        if n == "311" and len(ps) >= 6:
            self._remember(ps[1], f"{ps[2]}@{ps[3]}", realname=ps[5])
            text = f"{ps[1]} is {ps[2]}@{ps[3]} ({ps[5]})"
        elif n == "312" and len(ps) >= 4:
            text = f"{ps[1]} is connected via {ps[2]} ({ps[3]})"
        elif n == "317" and len(ps) >= 3:
            text = f"{ps[1]} has been idle {int(ps[2]) // 60} min" if ps[2].isdigit() else " ".join(ps[1:])
        elif n == "319" and len(ps) >= 3:
            text = f"{ps[1]} is in {ps[2]}"
        elif n == "330" and len(ps) >= 3:
            self._remember(ps[1], "", account=ps[2])
            text = f"{ps[1]} is logged in as {ps[2]}"
        elif n == "301" and len(ps) >= 3:
            self._remember(ps[1], "", away=True)
            text = f"{ps[1]} is away: {ps[2]}"
        elif n == "671" and len(ps) >= 2:
            text = f"{ps[1]} is using a secure connection"
        elif n == "324" and len(ps) >= 3:
            text = f"Modes of {ps[1]}: {' '.join(ps[2:])}"
        else:
            text = " ".join(ps[1:]) if len(ps) > 1 else " ".join(ps)
        self._emit("server", text=P.strip_format(text), numeric=n, error=n.isdigit() and int(n) >= 400)
