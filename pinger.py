"""Keep-alive pinger with a Telegram control panel (premium look).

The panel uses reply keyboard buttons (above the chat box), not buttons under the message.

Env: BOT_TOKEN, OWNER_ID (your numeric Telegram id), PING_URLS and PING_MINUTES (seeds,
used on first start only), DATA_DIR (default /tmp/pinger-data), PORT (default 10000),
RENDER_EXTERNAL_URL (set by Render, pinged automatically), TELEGRAM_API_BASE (optional).
"""

import copy
import datetime as dt
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from urllib.parse import urlparse

import requests
from flask import Flask, Response

MAX_URLS = 20
ADD_WINDOW = 120  # seconds the bot waits for a URL after "Add URL"
ROUND_CAP = 45  # seconds: a ping round never waits longer than this for one URL
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
API_BASE = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")

# ---- Premium look -----------------------------------------------------------------
# Paste premium custom emoji IDs between the quotes. Get an ID by sending a premium emoji
# to the bot (it replies with the ID). Empty "" means the normal emoji is shown.
# The bot owner needs Telegram Premium for the IDs to show.
PREMIUM_EMOJI = {
    "panel": "", "ok": "", "warn": "", "fail": "", "info": "", "lock": "", "cancel": "",
    "pause": "", "play": "", "ping": "", "add": "", "remove": "", "list": "", "timer": "",
    "clock": "", "uptime": "", "link": "", "close": "", "back": "", "refresh": "",
    "globe": "", "star": "", "bolt": "", "shield": "", "chart": "", "crown": "",
    "minus": "", "plus": "",
}
# Normal emoji: used when the ID above is empty, and what non-Premium users see.
FALLBACK_EMOJI = {
    "panel": "🛰", "ok": "✅", "warn": "⚠️", "fail": "❌", "info": "ℹ️", "lock": "🔒",
    "cancel": "🚫", "pause": "⏸", "play": "▶️", "ping": "⚡", "add": "➕", "remove": "🗑",
    "list": "📋", "timer": "⏱", "clock": "🕒", "uptime": "⏳", "link": "🔗", "close": "✖️",
    "back": "⬅️", "refresh": "🔄", "globe": "🌐", "star": "⭐", "bolt": "🔥", "shield": "🛡",
    "chart": "📊", "crown": "👑", "minus": "➖", "plus": "➕",
}
# Button colours supported by the Bot API: "success" green, "danger" red, "primary" blue.
GO, STOP, INFO = "success", "danger", "primary"

app = Flask(__name__)
logging.getLogger("werkzeug").setLevel(logging.ERROR)
# Library warnings can print the full request address, which holds the bot token.
logging.getLogger("urllib3").setLevel(logging.CRITICAL)


def log(msg):
    print(msg, flush=True)


class ApiError(Exception):
    pass


class RetryAfter(Exception):
    def __init__(self, seconds):
        super().__init__("flood wait")
        self.seconds = seconds


# ---- Rich text: text plus entities -------------------------------------------------
# Template syntax for rich(): {e:name} premium emoji (normal emoji if no ID),
# {0}, {1}... raw values that are never parsed (use these for URLs and other user text),
# **bold** and `code`.
_TOKEN = re.compile(r"\{e:(\w+)\}|\{(\d+)\}|\*\*(.+?)\*\*|`([^`\n]+)`")


def u16(text):
    """Telegram entity offsets and lengths count UTF-16 code units."""
    return len(text.encode("utf-16-le")) // 2


class _Out:
    def __init__(self):
        self.text = ""
        self.size = 0  # UTF-16 length so far
        self.entities = []

    def add(self, s):
        self.text += s
        self.size += u16(s)


def _emit(out, template, raw):
    pos = 0
    for m in _TOKEN.finditer(template):
        out.add(template[pos:m.start()])
        pos = m.end()
        if m.group(1) is not None:
            name = m.group(1)
            fallback = FALLBACK_EMOJI[name]
            start = out.size
            out.add(fallback)
            if PREMIUM_EMOJI.get(name):
                out.entities.append({"type": "custom_emoji", "offset": start,
                                     "length": u16(fallback),
                                     "custom_emoji_id": PREMIUM_EMOJI[name]})
        elif m.group(2) is not None:
            out.add(str(raw[int(m.group(2))]))
        else:
            kind, inner = ("bold", m.group(3)) if m.group(3) is not None else ("code", m.group(4))
            start = out.size
            _emit(out, inner, raw)
            out.entities.append({"type": kind, "offset": start, "length": out.size - start})
    out.add(template[pos:])


def rich(template, *raw):
    """Message text plus entities, for sendMessage (no parse_mode)."""
    out = _Out()
    _emit(out, template, raw)
    return {"text": out.text, "entities": out.entities}


def join_rich(first, second):
    """Two rich messages one after the other, with a blank line between them."""
    gap = "\n\n"
    shift = u16(first["text"] + gap)
    return {"text": first["text"] + gap + second["text"],
            "entities": first["entities"]
            + [dict(e, offset=e["offset"] + shift) for e in second["entities"]]}


def rbtn(name, text, style):
    """Reply keyboard button with a colour style. The premium icon goes in icon_custom_emoji_id
    when its ID is set, otherwise the normal emoji becomes part of the label text."""
    button = {"text": text, "style": style}
    if name:
        if PREMIUM_EMOJI.get(name):
            button["icon_custom_emoji_id"] = PREMIUM_EMOJI[name]
        else:
            button["text"] = f"{FALLBACK_EMOJI[name]} {text}"
    return button


def reply_kb(rows):
    """Reply keyboard: sits above the chat box and stays until a new keyboard is sent."""
    return {"keyboard": rows, "resize_keyboard": True}


def utf16_slice(text, offset, length):
    data = text.encode("utf-16-le")
    return data[offset * 2:(offset + length) * 2].decode("utf-16-le", errors="ignore")


def message_text(msg):
    """(text, entities) of a message, or of its caption when there is no text."""
    if "text" in msg:
        return msg.get("text") or "", msg.get("entities") or []
    return msg.get("caption") or "", msg.get("caption_entities") or []


def parse_cmd(text):
    parts = text.split(maxsplit=1)  # any whitespace ends the command word, e.g. a newline
    if not parts:
        return "", ""
    word = parts[0]
    arg = parts[1].strip() if len(parts) > 1 else ""
    return word.split("@")[0].lower(), arg


# ---- Button labels: what a tap sends back as text ----------------------------------
# Each fixed button: (icon name, label, action). Telegram sends the label text back as a
# normal message when the button is tapped, so the handler matches that text exactly.
FIXED_BUTTONS = [
    ("ping", "Ping now", "ping"),
    ("pause", "Pause", "toggle"),
    ("play", "Resume", "toggle"),
    ("minus", "-1", "int:-1"),
    ("plus", "+1", "int:1"),
    ("list", "List URLs", "list"),
    ("add", "Add URL", "add"),
    ("remove", "Remove", "rm"),
    ("refresh", "Refresh", "refresh"),
    ("close", "Close", "close"),
    ("cancel", "Cancel", "cancel"),
    ("back", "Back", "home"),
]
# Both spellings are accepted (with the normal emoji and without it), because a button can
# lose its emoji when Telegram refuses a premium icon.
LABEL_ACTION = {}
for _name, _text, _action in FIXED_BUTTONS:
    LABEL_ACTION[_text] = _action
    LABEL_ACTION[f"{FALLBACK_EMOJI[_name]} {_text}"] = _action
# The interval button shows the current minutes, e.g. "5 min".
INTERVAL_LABEL = re.compile(r"^(?:%s )?\d+ min$" % re.escape(FALLBACK_EMOJI["clock"]))
# Remove buttons read "1. https://...": the number is the position in the list.
REMOVE_LABEL = re.compile(r"^\d+\. ")


def remove_label(i, url):
    return f"{i}. {short(url, 40)}"


def url_for_label(text, urls):
    """The URL whose remove button has this exact label, or None."""
    for i, url in enumerate(urls, 1):
        if remove_label(i, url) == text:
            return url
    return None


# ---- Pinger and state ---------------------------------------------------------------

def valid_url(value):
    if not isinstance(value, str) or not value or len(value) > 500:
        return False
    if any(c.isspace() or ord(c) < 32 for c in value):
        return False
    if not value.lower().startswith(("http://", "https://")):
        return False
    try:
        return bool(urlparse(value).netloc)
    except ValueError:  # malformed address such as an unclosed "[" in the host
        return False


def short(text, limit=60):
    return text if len(text) <= limit else text[: limit - 3] + "..."


def is_ok(code):
    return code is not None and code.isdigit() and int(code) < 500


def fmt_uptime(seconds):
    hours, rem = divmod(int(seconds), 3600)
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h {rem // 60}m" if days else f"{hours}h {rem // 60}m"


def seed_minutes():
    """PING_MINUTES from the environment. Anything that is not 1 to 60 means the default, 10."""
    raw = os.environ.get("PING_MINUTES", "").strip()
    if raw.isdecimal() and 1 <= int(raw) <= 60:
        return int(raw)
    return 10


def seed_urls():
    """PING_URLS split on commas or spaces. Empty items and bad URLs are skipped."""
    parts = dict.fromkeys(p for p in re.split(r"[\s,]+", os.environ.get("PING_URLS", "")) if p)
    return [u for u in parts if valid_url(u)][:MAX_URLS]


def own_url():
    own = os.environ.get("RENDER_EXTERNAL_URL", "").strip()
    return own if valid_url(own) else ""


def ping_one(url):
    try:
        host = urlparse(url).hostname or "unknown-host"
    except ValueError:
        host = "unknown-host"
    code = "down"
    try:
        # stream=True: only the status line is needed, the body is not downloaded
        resp = requests.get(url, timeout=20, stream=True)
        code = str(resp.status_code)
        resp.close()
    except Exception:
        code = "down"  # error text is not logged: it can contain the full URL
    log(f"ping {code} {host}")  # host only, never the full URL
    return code


class Store:
    """Persisted state lives in self.d, guarded by self.lock."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.force = threading.Event()
        self.started = time.time()
        self.next_due = 0.0
        self.d = self.load()

    def load(self):
        data = None
        try:
            with open(self.path, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data = loaded
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            log("state file unreadable, starting over")
        if data is None:  # first start, or the file is unreadable: start from the seeds
            data = self.clean({})
            self.write(data)
            return data
        return self.clean(data)

    def clean(self, data):
        """Fills in missing keys and drops values of the wrong type (an old or hand-edited file)."""
        urls = data.get("urls")
        if isinstance(urls, list):
            urls = list(dict.fromkeys(u for u in urls if valid_url(u)))[:MAX_URLS]
        else:
            urls = seed_urls()
        minutes = data.get("minutes")
        if not (isinstance(minutes, int) and not isinstance(minutes, bool) and 1 <= minutes <= 60):
            minutes = seed_minutes()
        last_at = data.get("last_at")
        if not isinstance(last_at, (int, float)) or isinstance(last_at, bool):
            last_at = None
        results = data.get("results") if isinstance(data.get("results"), dict) else {}
        summary = data.get("summary") if isinstance(data.get("summary"), str) else ""
        return {"urls": urls, "minutes": minutes, "paused": data.get("paused") is True,
                "last_at": last_at, "summary": summary, "results": results}

    def write(self, data):
        """Atomic write: temp file first, flushed to disk, then renamed over the real file.
        A crash in the middle leaves the old file intact. A failed save is logged, not raised."""
        tmp = self.path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except (OSError, TypeError, ValueError):
            log("state not saved this time")

    def snapshot(self):
        with self.lock:
            snap = copy.deepcopy(self.d)
        snap["uptime"] = time.time() - self.started
        return snap

    def add(self, url):
        """Returns "" when the URL was added, otherwise "dup" or "full"."""
        with self.lock:
            if url in self.d["urls"]:
                return "dup"
            if len(self.d["urls"]) >= MAX_URLS:
                return "full"
            self.d["urls"].append(url)
            self.write(self.d)
            return ""

    def remove(self, key):
        """key is a 1-based number or an exact URL. Returns the removed URL or None."""
        with self.lock:
            urls = self.d["urls"]
            if key.isdecimal() and 1 <= int(key) <= len(urls):
                url = urls.pop(int(key) - 1)
            elif key in urls:
                url = urls.pop(urls.index(key))
            else:
                return None
            self.d["results"].pop(url, None)
            self.write(self.d)
            return url

    def set_minutes(self, minutes):
        with self.lock:
            self.d["minutes"] = minutes
            if self.d["last_at"]:
                self.next_due = self.d["last_at"] + minutes * 60
            self.write(self.d)
            return minutes

    def bump(self, delta):
        with self.lock:
            return self.set_minutes(min(60, max(1, self.d["minutes"] + delta)))

    def set_paused(self, value):
        """Returns True when the state actually changed. Resuming makes a ping due now."""
        with self.lock:
            if self.d["paused"] == value:
                return False
            self.d["paused"] = value
            if not value:
                self.next_due = 0.0
            self.write(self.d)
            return True

    def toggle(self):
        with self.lock:
            self.set_paused(not self.d["paused"])
            return self.d["paused"]

    def is_due(self):
        with self.lock:
            return not self.d["paused"] and time.time() >= self.next_due

    def run_round(self):
        with self.lock:
            urls = list(self.d["urls"])
        own = own_url()
        if own and own not in urls:
            urls.append(own)
        codes = ["down"] * len(urls)
        if urls:
            # All URLs run at once. A URL that hangs past the cap is counted as down,
            # so one slow site cannot hold up the whole round.
            pool = ThreadPoolExecutor(max_workers=len(urls))
            futures = [pool.submit(ping_one, u) for u in urls]
            done, _ = wait(futures, timeout=ROUND_CAP)
            codes = [f.result() if f in done else "down" for f in futures]
            pool.shutdown(wait=False, cancel_futures=True)
        oks = sum(1 for c in codes if is_ok(c))
        with self.lock:
            self.d["results"] = dict(zip(urls, codes))
            self.d["last_at"] = time.time()
            self.d["summary"] = f"{oks} ok, {len(codes) - oks} failed" if codes else "nothing to ping"
            self.next_due = time.time() + self.d["minutes"] * 60
            self.write(self.d)


def ping_loop(store):
    time.sleep(5)
    while True:
        try:
            if store.force.is_set() or store.is_due():
                store.force.clear()
                store.run_round()
        except Exception as e:
            log(f"ping round error: {type(e).__name__}")
            with store.lock:  # wait a full interval before trying again, no tight retry loop
                store.next_due = time.time() + store.d["minutes"] * 60
        store.force.wait(1)


# ---- Telegram ------------------------------------------------------------------------

def body(msg, markup=None):
    data = {"text": msg["text"], "link_preview_options": {"is_disabled": True}}
    if msg["entities"]:
        data["entities"] = msg["entities"]
    if markup is not None:
        data["reply_markup"] = markup
    return data


def has_premium(msg, markup):
    if any(e["type"] == "custom_emoji" for e in msg["entities"]):
        return True
    return bool(markup) and any("icon_custom_emoji_id" in b
                                for row in markup.get("keyboard", []) for b in row)


def without_premium(msg, markup):
    """The same message with premium icons removed, used when Telegram refuses them."""
    plain_msg = {"text": msg["text"],
                 "entities": [e for e in msg["entities"] if e["type"] != "custom_emoji"]}
    if not markup or "keyboard" not in markup:
        return plain_msg, markup
    rows = [[{k: v for k, v in b.items() if k != "icon_custom_emoji_id"} for b in row]
            for row in markup["keyboard"]]
    return plain_msg, reply_kb(rows)


class Telegram:
    def __init__(self, token):
        self.base = f"{API_BASE}/bot{token}/"

    def call(self, method, **params):
        try:
            data = requests.post(self.base + method, json=params, timeout=40).json()
        except Exception:
            raise ApiError(f"{method} failed") from None  # hides the URL, which holds the token
        if not isinstance(data, dict):
            raise ApiError(f"{method} failed")
        if data.get("ok"):
            return data.get("result")
        if data.get("error_code") == 429:
            try:
                wait_for = int((data.get("parameters") or {}).get("retry_after", 5))
            except (TypeError, ValueError):
                wait_for = 5
            raise RetryAfter(min(max(wait_for, 1), 120))
        raise ApiError(str(data.get("description", "unknown error"))[:200])

    def send(self, chat_id, msg, markup=None):
        """Sends one message. Returns Telegram's result (it holds the new message_id)."""
        try:
            return self.call("sendMessage", chat_id=chat_id, **body(msg, markup))
        except ApiError as e:
            if str(e).endswith(" failed") or not has_premium(msg, markup):
                raise
            # Telegram refused a premium ID: send the same message with normal emoji instead.
            log("premium look refused, sent plain look")
            plain_msg, plain_markup = without_premium(msg, markup)
            return self.call("sendMessage", chat_id=chat_id, **body(plain_msg, plain_markup))

    def delete(self, chat_id, message_id):
        """Tidy-up only: if the delete fails, the old panel just stays in the chat."""
        try:
            self.call("deleteMessage", chat_id=chat_id, message_id=message_id)
        except Exception:
            pass


# ---- Panel texts and keyboards -------------------------------------------------------

def card_text(snap):
    count = str(len(snap["urls"])) + (" (+1 own URL)" if own_url() else "")
    last = snap["last_at"]
    last_txt = dt.datetime.fromtimestamp(last, IST).strftime("%d %b, %H:%M IST") if last else "abhi tak nahi"
    state = "Paused" if snap["paused"] else "Running"
    status_emoji = "pause" if snap["paused"] else "play"
    return rich("\n".join([
        "{e:panel} **Keep-Alive Panel**",
        "━━━━━━━━━━━━━━━",
        "{e:link} URLs: **{0}**",
        "{e:clock} Interval: **{1} min**",
        "{e:%s} Status: **{2}**" % status_emoji,
        "{e:timer} Last ping: **{3}**",
        "{e:chart} Result: **{4}**",
        "{e:uptime} Uptime: **{5}**",
    ]), count, snap["minutes"], state, last_txt, snap["summary"] or "none yet",
        fmt_uptime(snap["uptime"]))


def list_text(snap):
    urls = snap["urls"]
    if not urls:
        return rich("{e:list} **List khali hai**\nAdd URL dabao, ya /add <url> likho.")
    lines = ["{e:list} **Your URLs (%d/%d)**" % (len(urls), MAX_URLS)]
    raw = []
    for i, url in enumerate(urls, 1):
        code = snap["results"].get(url)
        mark = "·" if code is None else ("{e:ok}" if is_ok(code) else "{e:fail}")
        raw.append(short(url))
        lines.append(f"{i}. {{{len(raw) - 1}}} {mark}")
    return rich("\n".join(lines), *raw)


def main_kb(snap):
    if snap["paused"]:
        toggle = rbtn("play", "Resume", GO)
    else:
        toggle = rbtn("pause", "Pause", STOP)
    return reply_kb([
        [rbtn("ping", "Ping now", INFO), toggle],
        [rbtn("minus", "-1", INFO), rbtn("clock", f"{snap['minutes']} min", INFO),
         rbtn("plus", "+1", INFO)],
        [rbtn("list", "List URLs", INFO), rbtn("add", "Add URL", GO)],
        [rbtn("remove", "Remove", STOP), rbtn("refresh", "Refresh", INFO)],
        [rbtn("close", "Close", STOP)],
    ])


def sub_kb(snap=None):
    """Back button, plus one Remove button per URL when snap is given."""
    rows = []
    if snap:
        rows = [[rbtn(None, remove_label(i, u), STOP)] for i, u in enumerate(snap["urls"], 1)]
    return reply_kb(rows + [[rbtn("back", "Back", INFO)]])


def cancel_kb():
    return reply_kb([[rbtn("cancel", "Cancel", STOP)]])


class Panel:
    def __init__(self, tg, store):
        self.tg = tg
        self.store = store
        raw = os.environ.get("OWNER_ID", "").strip()
        self.owner = int(raw) if raw.isdecimal() else None
        if raw and self.owner is None:
            log("OWNER_ID must be numeric, ignoring it")
        self.pending = {}    # chat_id -> time when the bot stops waiting for a URL
        self.panel_mid = {}  # chat_id -> message id of the last panel, removed after the next one

    def handle(self, update):
        if "message" in update:
            self.on_message(update["message"])

    def on_message(self, msg):
        raw_text, _ = message_text(msg)
        text = raw_text.strip()
        chat_id = msg["chat"]["id"]
        user_id = (msg.get("from") or {}).get("id")
        if self.owner is None:
            if parse_cmd(text)[0] == "/start":
                self.tg.send(chat_id, rich(
                    "{e:shield} Aapka Telegram ID: `{0}`\n"
                    "Render me OWNER_ID naam ki env variable me ye ID daal, "
                    "service restart karo, phir /start likho.", user_id))
            return
        if user_id != self.owner:
            if text:
                self.tg.send(chat_id, rich("{e:lock} Ye bot private hai."))
            return
        if not text:
            return
        cmd, arg = parse_cmd(text)
        if cmd.startswith("/"):
            self.pending.pop(chat_id, None)  # any command ends a waiting Add URL
            handler = getattr(self, "cmd_" + cmd[1:], None)
            if handler:
                handler(chat_id, arg)
            return
        if self.on_label(chat_id, text):
            return
        if chat_id in self.pending:
            self.take_url(chat_id, text)

    def on_label(self, chat_id, text):
        """Runs the action of a reply button the owner tapped. False when the text is no button."""
        action = LABEL_ACTION.get(text)
        if action is None and INTERVAL_LABEL.match(text):
            action = "noop"  # the interval button only shows the value
        if action is None:
            url = url_for_label(text, self.store.snapshot()["urls"])
            if url is None:
                if REMOVE_LABEL.match(text):
                    # an old remove button: its number has moved or the URL is gone
                    self.pending.pop(chat_id, None)
                    self.show(chat_id, "remove", rich("{e:warn} Ye ab list me nahi hai. Naya list dekh lo."))
                    return True
                return False
            action = "rmu:" + url
        self.act(action, chat_id)
        return True

    def act(self, data, chat_id):
        """Runs one panel button, then sends the updated panel."""
        name, _, arg = data.partition(":")
        store = self.store
        note, view = None, "main"
        if name != "add":
            self.pending.pop(chat_id, None)  # any other button ends a waiting Add URL
        if name == "ping":
            store.force.set()
            note = rich("{e:ping} Ping shuru ho gayi")
        elif name == "toggle":
            if store.toggle():
                note = rich("{e:pause} Paused")
            else:
                note = rich("{e:play} Running")
        elif name == "int":
            note = rich("{e:clock} Interval {0} min", store.bump(int(arg)))
        elif name == "list":
            view = "list"
        elif name == "rm":
            view = "remove"
        elif name == "rmu":
            gone = store.remove(arg)
            if gone:
                note = rich("{e:remove} Hata diya")
            else:
                note = rich("{e:warn} Ye ab list me nahi hai")
            view = "remove"
        elif name == "add":
            self.pending[chat_id] = time.time() + ADD_WINDOW
            view = "add"
        elif name == "cancel":
            self.pending.pop(chat_id, None)
            note = rich("{e:cancel} Cancel ho gaya")
        elif name == "close":
            view = "closed"
        elif name == "refresh":
            note = rich("{e:refresh} Updated")
        self.show(chat_id, view, note)

    def show(self, chat_id, view, note=None):
        """Sends the panel for a view as one new message with its keyboard, then removes the old one."""
        snap = self.store.snapshot()
        if view == "list":
            msg, markup = list_text(snap), sub_kb()
        elif view == "remove":
            if snap["urls"]:
                msg = rich("{e:remove} Kaunsa URL hatana hai? Button dabao.")
            else:
                msg = rich("{e:list} List khali hai, hatane ko kuch nahi.")
            markup = sub_kb(snap)
        elif view == "add":
            msg = rich("{e:globe} Ab mujhe URL bhejo (http:// ya https:// se shuru). "
                       "2 minute tak wait karunga.")
            markup = cancel_kb()
        elif view == "closed":
            msg = rich("{e:close} Panel band hai. /start likho to phir khul jayega.")
            markup = {"remove_keyboard": True}
        else:
            msg, markup = card_text(snap), main_kb(snap)
        if note is not None:
            msg = join_rich(note, msg)
        sent = self.tg.send(chat_id, msg, markup)
        old = self.panel_mid.get(chat_id)
        self.panel_mid[chat_id] = (sent or {}).get("message_id")
        if old:
            self.tg.delete(chat_id, old)

    def add_url(self, text):
        url = text.strip()
        if not valid_url(url):
            return rich("{e:warn} Ye URL sahi nahi hai. http:// ya https:// se shuru hona chahiye.")
        err = self.store.add(url)
        if err == "dup":
            return rich("{e:info} Ye URL pehle se list me hai.")
        if err == "full":
            return rich("{e:warn} Limit poori hai ({0} URLs). Pehle kuch remove karo.", MAX_URLS)
        return rich("{e:ok} Add ho gaya: {0}", url)

    def take_url(self, chat_id, text):
        expires = self.pending.pop(chat_id)
        if time.time() > expires:
            return self.show(chat_id, "main",
                             rich("{e:info} Wait time nikal gaya. Phir se Add URL dabao."))
        self.show(chat_id, "main", self.add_url(text))

    def cmd_start(self, chat_id, arg):
        self.show(chat_id, "main")

    def cmd_status(self, chat_id, arg):
        self.tg.send(chat_id, card_text(self.store.snapshot()))

    def cmd_list(self, chat_id, arg):
        self.tg.send(chat_id, list_text(self.store.snapshot()))

    def cmd_add(self, chat_id, arg):
        if arg:
            return self.show(chat_id, "main", self.add_url(arg))
        self.pending[chat_id] = time.time() + ADD_WINDOW
        self.tg.send(chat_id, rich("{e:globe} Ab URL bhejo (http:// ya https://). /cancel se rok sakte ho."))

    def cmd_remove(self, chat_id, arg):
        if not arg:
            return self.tg.send(chat_id, rich("{e:info} Aise likho: /remove 2 ya /remove https://..."))
        gone = self.store.remove(arg)
        if gone:
            return self.show(chat_id, "main", rich("{e:remove} Hata diya: {0}", gone))
        self.show(chat_id, "main", rich("{e:fail} Ye number ya URL list me nahi mila."))

    def cmd_interval(self, chat_id, arg):
        if not (arg.isdecimal() and 1 <= int(arg) <= 60):
            return self.tg.send(chat_id, rich("{e:warn} Interval 1 se 60 minute ke beech do. Jaise: /interval 5"))
        self.store.set_minutes(int(arg))
        self.show(chat_id, "main", rich("{e:clock} Interval ab {0} minute hai.", int(arg)))

    def cmd_pingnow(self, chat_id, arg):
        self.store.force.set()
        self.show(chat_id, "main", rich("{e:ping} Ping shuru ho gayi. Result ke liye /status likho."))

    def cmd_pause(self, chat_id, arg):
        if self.store.set_paused(True):
            return self.show(chat_id, "main", rich("{e:pause} Pause ho gaya."))
        self.show(chat_id, "main", rich("{e:info} Ye pehle se pause hai."))

    def cmd_resume(self, chat_id, arg):
        if self.store.set_paused(False):
            return self.show(chat_id, "main", rich("{e:play} Resume ho gaya."))
        self.show(chat_id, "main", rich("{e:info} Ye pehle se chal raha hai."))

    def cmd_cancel(self, chat_id, arg):
        self.pending.pop(chat_id, None)
        self.show(chat_id, "main", rich("{e:cancel} Cancel ho gaya."))


def poll_loop(panel):
    webhook_cleared = False
    offset = 0  # next update to ask for; moves past an update only after it is handled
    while True:
        try:
            if not webhook_cleared:
                panel.tg.call("deleteWebhook")
                webhook_cleared = True
            updates = panel.tg.call("getUpdates", offset=offset, timeout=25,
                                    allowed_updates=["message"])
        except RetryAfter as e:
            time.sleep(e.seconds)
            continue
        except Exception as e:
            log(f"telegram poll error: {type(e).__name__}")
            time.sleep(5)
            continue
        for update in updates if isinstance(updates, list) else []:
            try:
                panel.handle(update)
            except RetryAfter as e:
                # Telegram asked us to slow down: keep this update and the rest for the next
                # getUpdates, after the wait. Nothing is lost.
                time.sleep(e.seconds)
                break
            except Exception as e:
                log(f"telegram update error: {type(e).__name__}")  # dropped, so no retry loop
            uid = update.get("update_id") if isinstance(update, dict) else None
            if isinstance(uid, int):
                offset = max(offset, uid + 1)


@app.route("/")
@app.route("/health")
def health():
    return Response("ok", status=200, mimetype="text/plain")


def web_port():
    raw = os.environ.get("PORT", "").strip()
    return int(raw) if raw.isdecimal() and 0 < int(raw) < 65536 else 10000


def main():
    # The web server starts first, so Render sees the port open before anything else runs.
    port = web_port()
    web = threading.Thread(target=app.run,
                           kwargs={"host": "0.0.0.0", "port": port, "use_reloader": False},
                           daemon=True)
    web.start()
    log(f"Serving on port {port}")
    store = Store(os.path.join(os.environ.get("DATA_DIR", "/tmp/pinger-data"), "state.json"))
    threading.Thread(target=ping_loop, args=(store,), daemon=True).start()
    token = os.environ.get("BOT_TOKEN", "").strip()
    if token:
        panel = Panel(Telegram(token), store)
        if panel.owner is None:
            log("OWNER_ID not set: the bot only answers /start with your ID")
        threading.Thread(target=poll_loop, args=(panel,), daemon=True).start()
    else:
        log("BOT_TOKEN not set: web server only, no Telegram panel")
    web.join()  # the process lives as long as the web server; if it stops, the process exits


if __name__ == "__main__":
    main()
