"""Small, dependency-free helpers for the IRC wire protocol (RFC 1459/2812 + IRCv3 message tags).

Nothing in here touches the network or the database, so it is easy to test.
"""
import base64
import re

# ------------------------------------------------------------------ tags
_TAG_ESC = {":": ";", "s": " ", "\\": "\\", "r": "\r", "n": "\n"}
_TAG_ESC_REV = {";": "\\:", " ": "\\s", "\\": "\\\\", "\r": "\\r", "\n": "\\n"}


def unescape_tag(value):
    out, i = [], 0
    while i < len(value):
        c = value[i]
        if c == "\\":
            i += 1
            if i < len(value):
                out.append(_TAG_ESC.get(value[i], value[i]))
        else:
            out.append(c)
        i += 1
    return "".join(out)


def escape_tag(value):
    return "".join(_TAG_ESC_REV.get(c, c) for c in str(value))


class Msg:
    """One parsed IRC line."""
    __slots__ = ("tags", "prefix", "command", "params")

    def __init__(self, tags, prefix, command, params):
        self.tags, self.prefix, self.command, self.params = tags, prefix, command, params

    @property
    def nick(self):
        return self.prefix.split("!", 1)[0].split("@", 1)[0] if self.prefix else ""

    @property
    def userhost(self):
        """'user@host' part of the prefix ('' for servers and bare nicks)."""
        return self.prefix.split("!", 1)[1] if self.prefix and "!" in self.prefix else ""

    @property
    def is_server(self):
        return bool(self.prefix) and "!" not in self.prefix and "@" not in self.prefix and "." in self.prefix

    def __repr__(self):
        return f"Msg({self.tags!r}, {self.prefix!r}, {self.command!r}, {self.params!r})"


def parse(line):
    """Parse one line (without CRLF). Returns None for blank lines."""
    line = line.strip("\r\n")
    if not line.strip():
        return None
    tags, prefix = {}, ""
    if line.startswith("@"):
        raw, _, line = line[1:].partition(" ")
        for part in raw.split(";"):
            if not part:
                continue
            k, _, v = part.partition("=")
            tags[k] = unescape_tag(v)
        line = line.lstrip(" ")
    if line.startswith(":"):
        prefix, _, line = line[1:].partition(" ")
        line = line.lstrip(" ")
    params = []
    while line:
        if line.startswith(":"):
            params.append(line[1:])
            break
        word, _, line = line.partition(" ")
        if word:
            params.append(word)
        line = line.lstrip(" ")
    if not params:
        return None
    return Msg(tags, prefix, params[0].upper(), params[1:])


def clean_param(value):
    """No CR, LF or NUL may ever reach the wire (that would let text inject extra commands)."""
    return str(value).replace("\r", "").replace("\n", " ").replace("\0", "")


def format_line(command, *params, tags=None, trailing=False):
    """Build one outgoing line (no CRLF). The last parameter may contain spaces; trailing=True always writes it as
    ':text' (the usual form for messages)."""
    parts = []
    if tags:
        parts.append("@" + ";".join(k if v in (None, "") else f"{k}={escape_tag(v)}" for k, v in tags.items()))
    parts.append(clean_param(command))
    params = [clean_param(p) for p in params]
    for i, p in enumerate(params):
        last = i == len(params) - 1
        if last and (trailing or p == "" or " " in p or p.startswith(":")):
            parts.append(":" + p)
        else:
            if " " in p or p == "" or p.startswith(":"):
                raise ValueError("only the last IRC parameter may contain spaces")
            parts.append(p)
    return " ".join(parts)


# ------------------------------------------------------------------ text
_FORMAT_RE = re.compile(r"\x03(?:\d{1,2}(?:,\d{1,2})?)?|\x04(?:[0-9A-Fa-f]{6}(?:,[0-9A-Fa-f]{6})?)?|[\x02\x0f\x11\x16\x1d\x1e\x1f]")


def strip_format(text):
    """Remove mIRC colour/bold/italic codes (we show plain text) and other control characters."""
    text = _FORMAT_RE.sub("", text)
    return "".join(c for c in text if c == "\t" or ord(c) >= 32)


def split_text(text, max_bytes):
    """Split text into IRC-sized lines: one line per newline, long lines cut at spaces, never inside a character."""
    out = []
    for raw in str(text).replace("\r", "").split("\n"):
        line = raw.replace("\0", "").strip()
        while line:
            enc = line.encode("utf-8")
            if len(enc) <= max_bytes:
                out.append(line)
                break
            cut = max_bytes
            while cut > 0 and (enc[cut] & 0xC0) == 0x80:          # do not cut in the middle of a UTF-8 character
                cut -= 1
            head = enc[:cut].decode("utf-8", "ignore")
            sp = head.rfind(" ")
            if sp > len(head) // 2:
                head = head[:sp]
            out.append(head.rstrip())
            line = line[len(head):].lstrip()
    return [x for x in out if x]


def casefold(text, mapping="rfc1459"):
    """IRC case-insensitive comparison key. ('[' and '{' are the same letter on classic servers.)"""
    t = str(text).lower()
    if mapping == "ascii":
        return t
    t = t.translate(str.maketrans("[]\\~", "{}|^"))
    return t


# ------------------------------------------------------------------ SASL
def sasl_plain_payload(authzid, authcid, password):
    return base64.b64encode(f"{authzid}\0{authcid}\0{password}".encode("utf-8")).decode("ascii")


def sasl_chunks(payload, size=400):
    """AUTHENTICATE data goes out in 400-byte pieces; a final '+' marks the end when the last piece is exactly 400."""
    if not payload:
        return ["+"]
    chunks = [payload[i:i + size] for i in range(0, len(payload), size)]
    if len(chunks[-1]) == size:
        chunks.append("+")
    return chunks


# ------------------------------------------------------------------ capabilities / ISUPPORT
def parse_caps(text):
    """'sasl=PLAIN,EXTERNAL server-time' -> {'sasl': 'PLAIN,EXTERNAL', 'server-time': ''}"""
    out = {}
    for item in text.split():
        name, _, value = item.partition("=")
        name = name.lstrip("-~=")
        if name:
            out[name.lower()] = value
    return out


def client_tag_allowed(clienttagdeny, tag):
    """Does the server's CLIENTTAGDENY token allow us to send the client-only tag +<tag>?
    No token at all means nothing is denied."""
    if not clienttagdeny:
        return True
    star, deny, allow = False, set(), set()
    for e in clienttagdeny.split(","):
        e = e.strip().lstrip("+").lower()
        if e == "*":
            star = True
        elif e.startswith("-"):
            allow.add(e[1:].lstrip("+"))
        elif e:
            deny.add(e)
    t = tag.lower()
    if t in allow:
        return True
    if t in deny:
        return False
    return not star


def pick_client_tag(clienttagdeny, names):
    """The first tag name from `names` that the server lets us send, or None."""
    for n in names:
        if client_tag_allowed(clienttagdeny, n):
            return n
    return None


def parse_prefix_token(value):
    """PREFIX=(qaohv)~&@%+ -> ({'q':'~',...}, '~&@%+') ; falls back to the classic (ov)@+"""
    m = re.match(r"^\((\w*)\)(\S*)$", value or "")
    if not m or len(m.group(1)) != len(m.group(2)):
        return {"o": "@", "v": "+"}, "@+"
    return dict(zip(m.group(1), m.group(2))), m.group(2)


def parse_server(text, default_port=6697):
    """Accepts 'irc.libera.chat', 'irc.libera.chat:6667', 'ircs://host:6697', 'irc://host' -> (host, port, tls)."""
    s = (text or "").strip()
    tls = None
    m = re.match(r"^(ircs?|ircs?\+tls)://", s, re.I)
    if m:
        tls = m.group(1).lower() != "irc"
        s = s[m.end():]
    s = s.split("/", 1)[0]
    host, port = s, None
    if s.startswith("["):                                         # [::1]:6667
        h, _, rest = s[1:].partition("]")
        host, port = h, rest[1:] if rest.startswith(":") else None
    elif s.count(":") == 1:
        host, _, p = s.partition(":")
        port = p
    host = host.strip().lower()
    if not re.match(r"^[a-z0-9]([a-z0-9.\-]*[a-z0-9])?$|^[0-9a-f:]+$", host or "!") or len(host) > 253:
        raise ValueError("That does not look like a server address (try irc.libera.chat).")
    try:
        port_n = int(port) if port else (6667 if tls is False else default_port)
    except ValueError:
        raise ValueError("The port must be a number.")
    if not 1 <= port_n <= 65535:
        raise ValueError("The port must be between 1 and 65535.")
    if tls is None:
        tls = port_n != 6667
    return host, port_n, bool(tls)


NICK_RE = re.compile(r"^[A-Za-z\[\]\\`_^{|}][A-Za-z0-9\[\]\\`_^{|}\-]{0,29}$")
CHAN_RE = re.compile(r"^[#&+!][^\s,\x07\0]{1,49}$")


def valid_nick(n):
    return bool(NICK_RE.match(n or ""))


def valid_channel(c, chantypes="#&"):
    return bool(CHAN_RE.match(c or "")) and c[0] in (chantypes or "#&")
