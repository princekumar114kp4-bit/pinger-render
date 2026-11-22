"""Keep-alive pinger with a Telegram control panel (premium look).

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
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import requests
from flask import Flask, Response

MAX_URLS = 20
ADD_WINDOW = 120  # seconds the bot waits for a URL after "Add URL"
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
    """Message text plus entities, for sendMessage or editMessageText (no parse_mode)."""
    out = _Out()
    _emit(out, template, raw)
    return {"text": out.text, "entities": out.entities}


def plain(template, *raw):
    """Popup text (answerCallbackQuery takes no entities): normal emoji only."""
    return rich(template, *raw)["text"]


def btn(name, text, data, style):
    """Inline button with a colour style. The premium icon is used when its ID is set,
    otherwise the normal emoji goes into the label."""
    button = {"text": text, "callback_data": data, "style": style}
    if name:
        if PREMIUM_EMOJI.get(name):
            button["icon_custom_emoji_id"] = PREMIUM_EMOJI[name]
        else:
            button["text"] = f"{FALLBACK_EMOJI[name]} {text}"
    return button


def utf16_slice(text, offset, length):
    data = text.encode("utf-16-le")
    return data[offset * 2:(offset + length) * 2].decode("utf-16-le", errors="ignore")


def message_text(msg):
    """(text, entities) of a message, or of its caption when there is no text."""
    if "text" in msg:
        return msg.get("text") or "", msg.get("entities") or []
    return msg.get("caption") or "", msg.get("caption_entities") or []


def parse_cmd(text):
    word, _, arg = text.partition(" ")
    return word.split("@")[0].lower(), arg.strip()


# ---- Pinger and state ---------------------------------------------------------------

def valid_url(value):
    if len(value) > 500 or any(c.isspace() for c in value):
        return False
    return value.startswith(("http://", "https://")) and bool(urlparse(value).netloc)


def short(text, limit=60):
    return text if len(text) <= limit else text[: limit - 3] + "..."


def is_ok(code):
    return code is not None and code.isdigit() and int(code) < 500


def fmt_uptime(seconds):
    hours, rem = divmod(int(seconds), 3600)
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h {rem // 60}m" if days else f"{hours}h {rem // 60}m"


def seed_minutes():
    try:
        value = int(os.environ.get("PING_MINUTES", "10").strip())
    except ValueError:
        return 10
    return min(value, 60) if value > 0 else 10


def seed_urls():
    parts = dict.fromkeys(p.strip() for p in os.environ.get("PING_URLS", "").split(","))
    return [u for u in parts if valid_url(u)][:MAX_URLS]


def own_url():
    own = os.environ.get("RENDER_EXTERNAL_URL", "").strip()
    return own if valid_url(own) else ""


def ping_one(url):
    host = urlparse(url).hostname or "unknown-host"
    try:
        code = str(requests.get(url, timeout=20).status_code)
    except Exception:
        code = "down"
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
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            log("state file unreadable, starting over")
        data = {"urls": seed_urls(), "minutes": seed_minutes(), "paused": False,
                "last_at": None, "summary": "", "results": {}}
        self.write(data)
        return data

    def write(self, data):
        """Atomic write: temp file first, then replace."""
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, self.path)

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
            if key.isdigit() and 1 <= int(key) <= len(urls):
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
        with ThreadPoolExecutor(max_workers=5) as pool:
            codes = list(pool.map(ping_one, urls))
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
                                for row in markup["inline_keyboard"] for b in row)


def without_premium(msg, markup):
    """The same message with premium icons removed, used when Telegram refuses them."""
    plain_msg = {"text": msg["text"],
                 "entities": [e for e in msg["entities"] if e["type"] != "custom_emoji"]}
    if not markup:
        return plain_msg, markup
    rows = [[{k: v for k, v in b.items() if k != "icon_custom_emoji_id"} for b in row]
            for row in markup["inline_keyboard"]]
    return plain_msg, {"inline_keyboard": rows}


class Telegram:
    def __init__(self, token):
        self.base = f"{API_BASE}/bot{token}/"

    def call(self, method, **params):
        try:
            data = requests.post(self.base + method, json=params, timeout=40).json()
        except Exception:
            raise ApiError(f"{method} failed") from None  # hides the URL, which holds the token
        if data.get("ok"):
            return data.get("result")
        if data.get("error_code") == 429:
            raise RetryAfter(int((data.get("parameters") or {}).get("retry_after", 5)))
        raise ApiError(data.get("description", "unknown error"))

    def deliver(self, method, msg, markup=None, **where):
        try:
            self.call(method, **where, **body(msg, markup))
        except ApiError as e:
            if "message is not modified" in str(e):
                return
            if str(e).endswith(" failed") or not has_premium(msg, markup):
                raise
            # Telegram refused a premium ID: send the same message with normal emoji instead.
            log("premium look refused, sent plain look")
            plain_msg, plain_markup = without_premium(msg, markup)
            self.call(method, **where, **body(plain_msg, plain_markup))

    def send(self, chat_id, msg, markup=None):
        self.deliver("sendMessage", msg, markup, chat_id=chat_id)

    def edit(self, chat_id, message_id, msg, markup):
        self.deliver("editMessageText", msg, markup, chat_id=chat_id, message_id=message_id)

    def answer(self, callback_id, text=""):
        try:
            self.call("answerCallbackQuery", callback_query_id=callback_id, text=text[:200])
        except ApiError as e:
            log(f"answer failed: {e}")


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
        toggle = btn("play", "Resume", "toggle", GO)
    else:
        toggle = btn("pause", "Pause", "toggle", STOP)
    return {"inline_keyboard": [
        [btn("ping", "Ping now", "ping", INFO), toggle],
        [btn("minus", "-1", "int:-1", INFO), btn("clock", f"{snap['minutes']} min", "noop", INFO),
         btn("plus", "+1", "int:1", INFO)],
        [btn("list", "List URLs", "list", INFO), btn("add", "Add URL", "add", GO)],
        [btn("remove", "Remove", "rm", STOP), btn("refresh", "Refresh", "refresh", INFO)],
        [btn("close", "Close", "close", STOP)],
    ]}


def sub_kb(snap=None, cancel=False):
    """Back button, Cancel button, or one Remove button per URL (when snap is given)."""
    if cancel:
        return {"inline_keyboard": [[btn("cancel", "Cancel", "cancel", STOP)]]}
    rows = []
    if snap:
        rows = [[btn("remove", f"{i}. {short(u, 40)}", f"rmi:{i}", STOP)]
                for i, u in enumerate(snap["urls"], 1)]
    return {"inline_keyboard": rows + [[btn("back", "Back", "home", INFO)]]}


class Panel:
    def __init__(self, tg, store):
        self.tg = tg
        self.store = store
        raw = os.environ.get("OWNER_ID", "").strip()
        self.owner = int(raw) if raw.isdigit() else None
        if raw and self.owner is None:
            log("OWNER_ID must be numeric, ignoring it")
        self.pending = {}  # chat_id -> (expires_at, panel message id or None)

    def handle(self, update):
        if "callback_query" in update:
            self.on_callback(update["callback_query"])
        elif "message" in update:
            self.on_message(update["message"])

    def on_message(self, msg):
        raw_text, ents = message_text(msg)  # entities refer to raw_text, so slice that one
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
        if not cmd.startswith("/") and chat_id in self.pending:
            return self.take_url(chat_id, text)
        handler = getattr(self, "cmd_" + cmd[1:], None) if cmd.startswith("/") else None
        if handler:
            handler(chat_id, arg)

    def on_callback(self, cq):
        if self.owner is None:
            return
        if (cq.get("from") or {}).get("id") != self.owner:
            return self.tg.answer(cq["id"], plain("{e:lock} Ye bot private hai."))
        msg = cq.get("message")
        if not msg:
            return self.tg.answer(cq["id"], plain("{e:info} Ye panel purana hai. /start likho."))
        chat_id, mid = msg["chat"]["id"], msg["message_id"]
        toast, view = self.act(cq.get("data", ""), chat_id, mid)
        self.tg.answer(cq["id"], toast)
        self.show(chat_id, mid, view)

    def act(self, data, chat_id, mid):
        """Runs one panel button. Returns (popup text, view to show)."""
        name, _, arg = data.partition(":")
        store = self.store
        if name == "ping":
            store.force.set()
            return plain("{e:ping} Ping shuru ho gayi"), "main"
        if name == "toggle":
            if store.toggle():
                return plain("{e:pause} Paused"), "main"
            return plain("{e:play} Running"), "main"
        if name == "int":
            return plain("{e:clock} Interval {0} min", store.bump(int(arg))), "main"
        if name == "list":
            return "", "list"
        if name == "rm":
            return "", "remove"
        if name == "rmi":
            gone = store.remove(arg)
            if gone:
                return plain("{e:remove} Hata diya"), "remove"
            return plain("{e:warn} Ye ab list me nahi hai"), "remove"
        if name == "add":
            self.pending[chat_id] = (time.time() + ADD_WINDOW, mid)
            return plain("{e:add} URL bhejo"), "add"
        if name == "cancel":
            self.pending.pop(chat_id, None)
            return plain("{e:cancel} Cancel ho gaya"), "main"
        if name == "close":
            return plain("{e:close} Panel band"), "closed"
        if name == "refresh":
            return plain("{e:refresh} Updated"), "main"
        return "", "main"

    def show(self, chat_id, mid, view):
        """Edits the panel message in place into the given view."""
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
            markup = sub_kb(cancel=True)
        elif view == "closed":
            msg, markup = rich("{e:close} Panel band hai. /start likho to phir khul jayega."), {"inline_keyboard": []}
        else:
            msg, markup = card_text(snap), main_kb(snap)
        self.tg.edit(chat_id, mid, msg, markup)

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
        expires, mid = self.pending.pop(chat_id)
        if time.time() > expires:
            return
        self.tg.send(chat_id, self.add_url(text))
        if mid:
            self.show(chat_id, mid, "main")

    def cmd_start(self, chat_id, arg):
        snap = self.store.snapshot()
        self.tg.send(chat_id, card_text(snap), main_kb(snap))

    def cmd_status(self, chat_id, arg):
        self.tg.send(chat_id, card_text(self.store.snapshot()))

    def cmd_list(self, chat_id, arg):
        self.tg.send(chat_id, list_text(self.store.snapshot()))

    def cmd_add(self, chat_id, arg):
        if arg:
            return self.tg.send(chat_id, self.add_url(arg))
        self.pending[chat_id] = (time.time() + ADD_WINDOW, None)
        self.tg.send(chat_id, rich("{e:globe} Ab URL bhejo (http:// ya https://). /cancel se rok sakte ho."))

    def cmd_remove(self, chat_id, arg):
        if not arg:
            return self.tg.send(chat_id, rich("{e:info} Aise likho: /remove 2 ya /remove https://..."))
        gone = self.store.remove(arg)
        if gone:
            return self.tg.send(chat_id, rich("{e:remove} Hata diya: {0}", gone))
        self.tg.send(chat_id, rich("{e:fail} Ye number ya URL list me nahi mila."))

    def cmd_interval(self, chat_id, arg):
        if not (arg.isdigit() and 1 <= int(arg) <= 60):
            return self.tg.send(chat_id, rich("{e:warn} Interval 1 se 60 minute ke beech do. Jaise: /interval 5"))
        self.store.set_minutes(int(arg))
        self.tg.send(chat_id, rich("{e:clock} Interval ab {0} minute hai.", int(arg)))

    def cmd_pingnow(self, chat_id, arg):
        self.store.force.set()
        self.tg.send(chat_id, rich("{e:ping} Ping shuru ho gayi. Result ke liye /status likho."))

    def cmd_pause(self, chat_id, arg):
        if self.store.set_paused(True):
            return self.tg.send(chat_id, rich("{e:pause} Pause ho gaya."))
        self.tg.send(chat_id, rich("{e:info} Ye pehle se pause hai."))

    def cmd_resume(self, chat_id, arg):
        if self.store.set_paused(False):
            return self.tg.send(chat_id, rich("{e:play} Resume ho gaya."))
        self.tg.send(chat_id, rich("{e:info} Ye pehle se chal raha hai."))

    def cmd_cancel(self, chat_id, arg):
        self.pending.pop(chat_id, None)
        self.tg.send(chat_id, rich("{e:cancel} Cancel ho gaya."))


def poll_loop(panel):
    try:
        panel.tg.call("deleteWebhook")
    except Exception as e:
        log(f"deleteWebhook skipped: {type(e).__name__}")
    offset = 0
    while True:
        try:
            updates = panel.tg.call("getUpdates", offset=offset, timeout=25,
                                    allowed_updates=["message", "callback_query"])
        except RetryAfter as e:
            time.sleep(e.seconds)
            continue
        except Exception as e:
            log(f"telegram poll error: {e}")
            time.sleep(5)
            continue
        for update in updates or []:
            try:
                offset = update["update_id"] + 1
                panel.handle(update)
            except RetryAfter as e:
                time.sleep(e.seconds)
            except Exception as e:
                log(f"telegram update error: {type(e).__name__}")


@app.route("/")
@app.route("/health")
def health():
    return Response("ok", status=200, mimetype="text/plain")


def main():
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
    port = int(os.environ.get("PORT", "10000") or 10000)
    log(f"Serving on port {port}")
    app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
