"""Theme / gimmick / preset catalogue and validation of user-supplied appearance settings."""
import base64
import json
import os
import re
import zlib

import config
from config import BASE_DIR, MAX_CUSTOM_CSS, MAX_CUSTOM_JS, MAX_INTRO

STATIC = os.path.join(BASE_DIR, "static")
THEMES_DIR = os.path.join(STATIC, "themes")
GIMMICK_DIR = os.path.join(STATIC, "js", "gimmicks")

LABEL_KEYS = {"room_title": 60, "roster_title": 30, "prompt": 4, "placeholder": 60, "send_label": 20,
              "live_label": 40, "lost_label": 40, "badge": 40}
FONTS = {
    "system": 'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif',
    "serif": 'Georgia, "Times New Roman", serif',
    "mono": 'ui-monospace, Menlo, Consolas, "Courier New", monospace',
    "rounded": '"Nunito", "Quicksand", "Trebuchet MS", system-ui, sans-serif',
    "hand": '"Comic Sans MS", "Segoe Print", "Bradley Hand", cursive',
    "retro": 'Verdana, Tahoma, Geneva, sans-serif',
}
VAR_KEYS = ("bg", "bg2", "panel", "text", "muted", "accent", "border", "msg_bg", "msg_own_bg", "input_bg")
DEFAULT_VARS = {"bg": "#111111", "bg2": "", "panel": "#1b1b1b", "text": "#eeeeee", "muted": "#999999",
                "accent": "#7ab8ff", "border": "#333333", "msg_bg": "#222222", "msg_own_bg": "#2a2a2a",
                "input_bg": "#151515", "font": "system", "radius": 10}
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,20}$")
CUSTOM = {"id": "custom", "name": "Custom (make your own)",
          "description": "Pick colours, font and corners yourself.", "labels": {}, "time_format": "short"}

_catalog = {}


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def load_catalog():
    """(Re)read themes/, gimmicks and presets from disk."""
    themes = {}
    if os.path.isdir(THEMES_DIR):
        for tid in sorted(os.listdir(THEMES_DIR)):
            m = _read_json(os.path.join(THEMES_DIR, tid, "theme.json"), None)
            if not m:
                continue
            m["id"] = tid
            m["has_css"] = os.path.exists(os.path.join(THEMES_DIR, tid, "theme.css"))
            m.setdefault("time_format", "short")
            themes[tid] = m
    themes["custom"] = dict(CUSTOM, has_css=False)
    gimmicks = [g for g in _read_json(os.path.join(GIMMICK_DIR, "gimmicks.json"), [])
                if os.path.exists(os.path.join(GIMMICK_DIR, g["id"] + ".js"))]
    file_presets = [p for p in _read_json(os.path.join(BASE_DIR, "presets.json"), []) if p.get("theme") in themes]
    _catalog.update(themes=themes, gimmicks=gimmicks, gimmick_ids={g["id"] for g in gimmicks},
                    file_presets=file_presets, presets=file_presets)


load_catalog()


def catalog():
    return _catalog


def preset(pid):
    return next((p for p in _catalog["presets"] if p["id"] == pid), None)


def theme_labels(theme_id):
    t = _catalog["themes"].get(theme_id) or _catalog["themes"]["classic"]
    base = _catalog["themes"]["classic"]["labels"]
    return {**base, **t.get("labels", {})}


def effective_labels(theme_id, overrides, room_name):
    labels = theme_labels(theme_id)
    labels.update({k: v for k, v in (overrides or {}).items() if k in LABEL_KEYS and v})
    return {k: str(v).replace("{room}", room_name) for k, v in labels.items()}


# ---------------------------------------------------------------- validation
def clean_color(value, allow_empty=False):
    value = (value or "").strip()
    if allow_empty and not value:
        return ""
    if not HEX.match(value):
        raise ValueError("Colours must look like #aabbcc.")
    return value.lower()


def clean_vars(raw):
    raw = raw if isinstance(raw, dict) else {}
    out = dict(DEFAULT_VARS)
    for k in VAR_KEYS:
        if k in raw:
            out[k] = clean_color(raw[k], allow_empty=(k == "bg2"))
    if raw.get("font") in FONTS:
        out["font"] = raw["font"]
    if "radius" in raw:
        try:
            out["radius"] = max(0, min(30, int(raw["radius"])))
        except (TypeError, ValueError):
            raise ValueError("Corner radius must be a number from 0 to 30.")
    return out


def vars_to_style(v):
    """Inline style for <body> when the custom theme is active. Every value is validated above."""
    v = clean_vars(v)
    parts = [f"--bg:{v['bg']}", f"--panel:{v['panel']}", f"--chat-bg:{v['panel']}", f"--text:{v['text']}",
             f"--muted:{v['muted']}", f"--accent:{v['accent']}", f"--border:{v['border']}",
             f"--msg-bg:{v['msg_bg']}", f"--msg-own-bg:{v['msg_own_bg']}", f"--input-bg:{v['input_bg']}",
             f"--font:{FONTS[v['font']]}", f"--radius:{v['radius']}px", f"--msg-radius:{v['radius']}px"]
    if v["bg2"]:
        parts.append(f"background:linear-gradient(135deg,{v['bg']},{v['bg2']}) fixed")
    return ";".join(parts)


def clean_labels(raw):
    raw = raw if isinstance(raw, dict) else {}
    out = {}
    for k, limit in LABEL_KEYS.items():
        val = raw.get(k)
        if isinstance(val, str) and val.strip():
            out[k] = val.strip()[:limit]
    return out


def clean_gimmicks(raw):
    if not isinstance(raw, list):
        raise ValueError("Gimmicks must be a list.")
    seen = []
    for g in raw:
        if g in _catalog["gimmick_ids"] and g not in seen:
            seen.append(g)
    return seen


def clean_settings(data):
    """Validate the appearance part of a settings payload. Returns a dict of column -> value
    containing only the keys that were supplied. Raises ValueError with a friendly message."""
    out = {}
    if "theme" in data:
        if data["theme"] not in _catalog["themes"]:
            raise ValueError("Unknown theme.")
        out["theme"] = data["theme"]
    if "gimmicks" in data:
        out["gimmicks"] = json.dumps(clean_gimmicks(data["gimmicks"]))
    if "theme_vars" in data:
        out["theme_vars"] = json.dumps(clean_vars(data["theme_vars"]))
    if "labels" in data:
        out["labels"] = json.dumps(clean_labels(data["labels"]))
    if "name_color" in data:
        out["name_color"] = clean_color(data["name_color"], allow_empty=True)
    if "status_text" in data:
        out["status_text"] = str(data["status_text"] or "").strip()[:40]
    for key, limit, label in (("custom_css", MAX_CUSTOM_CSS, "Custom CSS"), ("custom_js", MAX_CUSTOM_JS, "Custom JS"),
                              ("custom_intro", MAX_INTRO, "Intro")):
        if key in data:
            val = str(data[key] or "")
            if len(val) > limit:
                raise ValueError(f"{label} is too long (max {limit} characters).")
            out[key] = val
    for key in ("use_custom_css", "use_custom_js", "show_name_colors"):
        if key in data:
            out[key] = 1 if data[key] else 0
    if "intro_mode" in data:
        if data["intro_mode"] not in ("once", "always", "never"):
            raise ValueError("Unknown intro mode.")
        out["intro_mode"] = data["intro_mode"]
    if "bio" in data:
        out["bio"] = " ".join(str(data["bio"] or "").split())[:config.MAX_BIO]
    if "msg_effect" in data:
        if data["msg_effect"] not in config.MSG_EFFECTS:
            raise ValueError("Unknown message effect.")
        out["msg_effect"] = data["msg_effect"]
    for key in ("show_effects", "follow_events"):
        if key in data:
            out[key] = 1 if data[key] else 0
    if "schedule" in data:
        out["schedule"] = json.dumps(clean_schedule(data["schedule"]))
    return out


def clean_display_name(name):
    name = " ".join(str(name or "").split())
    if not 1 <= len(name) <= 24:
        raise ValueError("Display name must be 1-24 characters.")
    if any(ord(c) < 32 for c in name):
        raise ValueError("Display name contains invalid characters.")
    return name


def clean_username(name):
    name = str(name or "").strip()
    if not USERNAME_RE.match(name):
        raise ValueError("Username must be 3-20 characters: letters, numbers, _ . -")
    return name


# ---------------------------------------------------------------- presets the admin can edit
PRESET_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,23}$")


def file_presets():
    return _catalog["file_presets"]


def clean_preset(raw):
    """Validate one preset coming from the admin panel."""
    if not isinstance(raw, dict):
        raise ValueError("Bad preset.")
    pid = str(raw.get("id") or "").strip().lower()
    if not PRESET_ID_RE.match(pid):
        raise ValueError("Preset ids are 2-24 characters: lowercase letters, numbers, - and _.")
    name = " ".join(str(raw.get("name") or "").split())[:30]
    if not name:
        raise ValueError("Every preset needs a name.")
    theme = raw.get("theme")
    if theme not in _catalog["themes"]:
        raise ValueError(f"Preset '{name}': unknown theme.")
    return {"id": pid, "name": name, "description": " ".join(str(raw.get("description") or "").split())[:120],
            "theme": theme, "gimmicks": clean_gimmicks(raw.get("gimmicks") or []),
            "name_color": clean_color(raw.get("name_color"), allow_empty=True)}


def clean_presets(raw):
    if not isinstance(raw, list) or not raw:
        raise ValueError("Keep at least one preset.")
    if len(raw) > 40:
        raise ValueError("Too many presets (max 40).")
    out, seen = [], set()
    for p in raw:
        c = clean_preset(p)
        if c["id"] in seen:
            raise ValueError(f"Two presets share the id '{c['id']}'.")
        seen.add(c["id"])
        out.append(c)
    return out


def apply_presets(stored_json):
    """Use the admin's saved presets (a JSON string from the database) or, if there are none, presets.json."""
    chosen = None
    if stored_json:
        try:
            chosen = clean_presets(json.loads(stored_json))
        except (ValueError, TypeError):
            chosen = None
    _catalog["presets"] = chosen if chosen is not None else _catalog["file_presets"]


# ---------------------------------------------------------------- scheduled looks
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _mins(hhmm):
    return int(hhmm[:2]) * 60 + int(hhmm[3:])


def clean_schedule(raw):
    """[{preset, from, to}] -> validated list. 'from' to 'to' may cross midnight (e.g. 00:00-05:00 or 22:00-06:00)."""
    if not isinstance(raw, list):
        raise ValueError("Schedule must be a list.")
    if len(raw) > config.MAX_SCHEDULE_RULES:
        raise ValueError(f"At most {config.MAX_SCHEDULE_RULES} scheduled looks.")
    out = []
    for r in raw:
        if not isinstance(r, dict) or not preset(r.get("preset")):
            raise ValueError("A scheduled look uses a preset that does not exist (any more).")
        a, b = str(r.get("from") or ""), str(r.get("to") or "")
        if not (TIME_RE.match(a) and TIME_RE.match(b)) or a == b:
            raise ValueError("Scheduled looks need two different times like 00:00 and 05:00.")
        out.append({"preset": r["preset"], "from": a, "to": b})
    return out


def active_rule(rules, minutes_now):
    """Index of the first rule whose window contains the time (minutes since midnight), else None."""
    for i, r in enumerate(rules or []):
        try:
            a, b = _mins(r["from"]), _mins(r["to"])
        except (KeyError, ValueError, TypeError):
            continue
        inside = a <= minutes_now < b if a < b else (minutes_now >= a or minutes_now < b)
        if inside and preset(r.get("preset")):
            return i
    return None


# ---------------------------------------------------------------- sharing a theme as one code
CODE_PREFIX = "ROOMTHEME1:"


def make_bundle(data, include_js=False):
    """Build the shareable bundle from a settings-like dict (what the settings page sends). Everything is validated."""
    theme = data.get("theme")
    if theme not in _catalog["themes"]:
        raise ValueError("Unknown theme.")
    b = {"v": 1, "name": " ".join(str(data.get("name") or "").split())[:config.MAX_GALLERY_TITLE],
         "theme": theme, "gimmicks": clean_gimmicks(data.get("gimmicks") or []),
         "vars": clean_vars(data.get("theme_vars")), "labels": clean_labels(data.get("labels")),
         "intro": str(data.get("custom_intro") or "")[:MAX_INTRO], "css": str(data.get("custom_css") or "")}
    if len(b["css"]) > MAX_CUSTOM_CSS:
        raise ValueError(f"Custom CSS is too long (max {MAX_CUSTOM_CSS} characters).")
    if include_js:
        js = str(data.get("custom_js") or "")
        if len(js) > MAX_CUSTOM_JS:
            raise ValueError(f"Custom JS is too long (max {MAX_CUSTOM_JS} characters).")
        b["js"] = js
    return b


def encode_bundle(bundle):
    raw = zlib.compress(json.dumps(bundle, separators=(",", ":"), ensure_ascii=False).encode("utf-8"), 9)
    return CODE_PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_bundle(code):
    """Parse and RE-VALIDATE a pasted code. Never trusts what is inside it."""
    code = "".join(str(code or "").split())
    if not code.startswith(CODE_PREFIX):
        raise ValueError("That does not look like a theme code (it should start with " + CODE_PREFIX + ").")
    if len(code) > config.MAX_SHARED_CODE:
        raise ValueError("That theme code is too big.")
    body = code[len(CODE_PREFIX):]
    try:
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        d = zlib.decompressobj()
        text = d.decompress(raw, 400000)                      # cap the inflated size (zip-bomb guard)
        if d.unconsumed_tail:
            raise ValueError("too big")
        data = json.loads(text.decode("utf-8"))
    except Exception:
        raise ValueError("That theme code is damaged or incomplete.")
    if not isinstance(data, dict) or data.get("v") != 1:
        raise ValueError("That theme code is from a different version.")
    legacy = {"theme_vars": data.get("vars"), "custom_intro": data.get("intro"), "custom_css": data.get("css"),
              "custom_js": data.get("js")}
    return make_bundle({**data, **legacy}, include_js="js" in data)
