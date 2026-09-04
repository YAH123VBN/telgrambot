import json
import os
import re
import asyncio
import random
import zipfile
import threading
import secrets
from urllib.parse import urlparse, parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime
from html import escape
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from telegram.error import NetworkError

# ═══════════════════════════════════════════════════
# Persistent data directory
# On Railway, use the mounted Volume so JSON data survives redeploys.
# Locally, keep data next to the .py file.
# ═══════════════════════════════════════════════════
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
if not DATA_DIR:
    DATA_DIR = "/data" if os.environ.get("RAILWAY_ENVIRONMENT") else SCRIPT_DIR
os.makedirs(DATA_DIR, exist_ok=True)

def data_path(filename):
    """Return the persistent data-file path."""
    return os.path.join(DATA_DIR, filename)

TOKEN = os.environ.get("BOT_TOKEN", "").strip()
TEXTS_FILE = data_path("saved_texts.json")
POSTS_FILE = data_path("my_posts.json")
TOPICS_FILE = data_path("topics.json")
LINK_REGISTRY_FILE = data_path("link_registry.json")
TOPIC_VARIANTS_FILE = data_path("topic_variants.json")
TEMPLATE_GROUPS_FILE = data_path("template_groups.json")
SECTION_SETTINGS_FILE = data_path("section_settings.json")
TITLE_BANK_FILE = data_path("title_bank.json")
LINK_BRIDGE_KEY_FILE = data_path("link_bridge_key.txt")
ADMINS_FILE = data_path("admins.json")
LINK_REGEX = re.compile(r'(https?://\S+|t\.me/\S+)', re.IGNORECASE)

OWNER_ID = 8361990555

# Link-bridge API settings. The second bot will use this API to register
# which topic belongs to each generated link. Set LINK_BRIDGE_KEY on Railway
# for a fixed shared secret; otherwise one is generated and persisted locally.
LINK_BRIDGE_HOST = os.environ.get("LINK_BRIDGE_HOST", "0.0.0.0")
LINK_BRIDGE_PORT = int(os.environ.get("PORT", os.environ.get("LINK_BRIDGE_PORT", "8080")))
LINK_BRIDGE_KEY = os.environ.get("LINK_BRIDGE_KEY", "").strip()
if not LINK_BRIDGE_KEY:
    if os.path.exists(LINK_BRIDGE_KEY_FILE):
        try:
            LINK_BRIDGE_KEY = open(LINK_BRIDGE_KEY_FILE, "r", encoding="utf-8").read().strip()
        except Exception:
            LINK_BRIDGE_KEY = ""
    if not LINK_BRIDGE_KEY:
        LINK_BRIDGE_KEY = secrets.token_urlsafe(32)
        with open(LINK_BRIDGE_KEY_FILE, "w", encoding="utf-8") as _f:
            _f.write(LINK_BRIDGE_KEY)

_LINK_REGISTRY_LOCK = threading.Lock()

_HTML_TAG_RE = re.compile(r'(<[^>]+>)')

def preserve_spaces(text):
    return text

# ═══════════════════════════════════════════════════
# Safe JSON loader - NEVER overwrites existing files
# ═══════════════════════════════════════════════════

def load_json(path, default):
    if not os.path.exists(path):
        print(f"[INFO] Creating new file: {path}")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(default, f, ensure_ascii=False, indent=2)
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data is None:
            return default
        file_size = os.path.getsize(path)
        print(f"[INFO] Loaded {file_size} bytes from: {os.path.basename(path)}")
        return data
    except (json.JSONDecodeError, Exception) as e:
        print(f"[WARNING] Could not load {path}: {e}")
        return default

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"[SAVE] {os.path.basename(path)} saved ({os.path.getsize(path)} bytes)")

ADMINS = load_json(ADMINS_FILE, {})

ALL_PERMISSIONS = [
    ("ready", "🎯 آماده‌سازی / ساخت پست"),
    ("list", "📋 لیست متن‌ها / قالب‌ها"),
    ("add_text", "➕ افزودن متن / قالب جدید"),
    ("my_posts", "📁 پست‌های من"),
    ("manage_topics", "🎬 مدیریت موضوعات"),
    ("manage_templates", "🧩 مدیریت قالب‌ها"),
    ("manage_admins", "👮‍♂️ مدیریت ادمین‌ها")
]

def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID

def has_permission(user_id: int, perm: str) -> bool:
    if is_owner(user_id):
        return True
    uid = str(user_id)
    if uid not in ADMINS:
        return False
    admin = ADMINS[uid]
    if admin.get("full_access", False):
        return True
    return perm in admin.get("permissions", [])

def can_use_bot(user_id: int) -> bool:
    if is_owner(user_id):
        return True
    return str(user_id) in ADMINS

# ═══════════════════════════════════════════════════
# Keyboards
# ═══════════════════════════════════════════════════

def get_main_keyboard(user_id: int):
    # منوی اصلی مرتب و دو ستونه
    buttons = [
        ["🤖 دستیار", "🎯 آماده‌سازی"],
        ["⚡ پست سریع", "📋 لیست متن‌ها"],
        ["🧩 مدیریت قالب‌ها", "➕ افزودن متن"],
        ["📁 نهایی", "📁 پست‌های من"],
    ]
    if is_owner(user_id):
        buttons.append(["👮‍♂️ مدیریت ادمین‌ها"])
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)

# Every label that appears on any persistent reply-keyboard. Used so that
# tapping a menu button always navigates, even while the bot is mid-way
# through collecting free-text input for something else (e.g. bulk titles
# for "تنظیم عنوان همه" or bank items for "بانک کلمات") — otherwise the
# button's own label text gets swallowed as if it were that input, which is
# how a literal "📁 نهایی" line previously ended up stored as a title.
MAIN_MENU_BUTTON_TEXTS = {
    "🤖 دستیار", "🎯 آماده‌سازی", "📋 لیست متن‌ها", "📁 نهایی", "➕ افزودن متن",
    "⚡ پست سریع", "🧩 مدیریت قالب‌ها", "📁 پست‌های من", "👮‍♂️ مدیریت ادمین‌ها",
    "➕ افزودن ادمین", "🗑 برکناری ادمین", "⚙️ مدیریت دسترسی‌ها",
    "📋 لیست ادمین‌ها", "🔙 بازگشت به منوی اصلی",
}

MULTI_POST_KEYBOARD = ReplyKeyboardMarkup(
    [["✅ تمام", "❌ لغو"]],
    resize_keyboard=True
)

ADMIN_MANAGE_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["➕ افزودن ادمین", "🗑 برکناری ادمین"],
        ["⚙️ مدیریت دسترسی‌ها", "📋 لیست ادمین‌ها"],
        ["🔙 بازگشت به منوی اصلی"]
    ],
    resize_keyboard=True
)

_USER_LOCKS = {}

ALL_TEXTS = load_json(TEXTS_FILE, {
    "p1": {
        "header": "",
        "link_text": "DownLoad مشـ.ـاهده 📥",
        "linked_word": "مشـ.ـاهده",
        "footer": "@CondomClub ♥️",
        "random_headers": ["🤩پک #کمیاب کم.سن🍑"],
        "blockquote": False
    }
})

TOPICS = load_json(TOPICS_FILE, {
    "وطنی چهره دار": "🥵پک# چهره دار وطنی عجب کص💔",
    "کم سن وطنی": "🍑پک# کم سن وطنی",
    "کاسپلی آمریکایی": "🇺🇸 10 عدد فیلم (کاسپلی)"
})

LINK_REGISTRY = load_json(LINK_REGISTRY_FILE, {})
TOPIC_VARIANTS = load_json(TOPIC_VARIANTS_FILE, {})
# Maps "category|subcategory" -> template keys.
TEMPLATE_GROUPS = load_json(TEMPLATE_GROUPS_FILE, {})
SECTION_SETTINGS = load_json(SECTION_SETTINGS_FILE, {})
# Per-section, per-template title banks. Each bank is independent.
TITLE_BANKS = load_json(TITLE_BANK_FILE, {})

ACTIVE_KEY = "p1"

# In-memory state for the "نمایش پست" / "ایست" flow in My Posts.
# Keyed by (user_id, scope) -> bool / int. Not persisted to disk on purpose:
# it's a per-session viewing cursor, not user data.
_POST_SEND_ACTIVE = {}
_POST_SEND_CURSOR = {}

def save_texts():
    save_json(TEXTS_FILE, ALL_TEXTS)
    save_json(TOPICS_FILE, TOPICS)


def save_template_groups():
    save_json(TEMPLATE_GROUPS_FILE, TEMPLATE_GROUPS)

def save_section_settings():
    save_json(SECTION_SETTINGS_FILE, SECTION_SETTINGS)

def section_enabled(g):
    return bool(SECTION_SETTINGS.get(g, {}).get("quick_enabled", False))

def set_section_enabled(g, enabled):
    SECTION_SETTINGS.setdefault(g, {})["quick_enabled"] = bool(enabled)
    save_section_settings()

def get_section_last_quote(g):
    return str(SECTION_SETTINGS.get(g, {}).get("last_quote_text", "") or "")

def set_section_last_quote(g, text):
    SECTION_SETTINGS.setdefault(g, {})["last_quote_text"] = text
    save_section_settings()

def section_title(g):
    category, subcategory = (g.split("|", 1) + [""])[:2]
    return f"{category} / {subcategory}" if subcategory else category


def _title_bank_entry(g, k):
    """Return the persistent title-bank entry for one section/template."""
    section = TITLE_BANKS.setdefault(g, {})
    entry = section.setdefault(k, {"items": [], "cursor": 0, "first_used": False})
    if not isinstance(entry, dict):
        entry = {"items": [], "cursor": 0, "first_used": False}
        section[k] = entry
    items = entry.get("items", [])
    if not isinstance(items, list):
        items = []
    entry["items"] = [str(x).strip() for x in items if str(x).strip()]
    try:
        entry["cursor"] = max(0, int(entry.get("cursor", 0)))
    except (TypeError, ValueError):
        entry["cursor"] = 0
    entry["first_used"] = bool(entry.get("first_used", False))
    # "order" is the current reading order (a permutation of indices into
    # "items"). The first pass through a bank always reads items 0..N-1 in
    # their stored order; once that pass is exhausted, choose_post_title()
    # reshuffles this into a new pattern instead of repeating the same
    # sequence or stopping. Any time the item count changes (items added),
    # this is invalid and gets rebuilt fresh (sequential) automatically.
    order = entry.get("order")
    n = len(entry["items"])
    if not isinstance(order, list) or sorted(order) != list(range(n)):
        entry["order"] = list(range(n))
    return entry


def save_title_banks():
    save_json(TITLE_BANK_FILE, TITLE_BANKS)


def add_title_bank_items(g, k, items):
    """Append new bank titles without resetting already-consumed titles."""
    entry = _title_bank_entry(g, k)
    had_items = bool(entry.get("items"))
    entry["items"].extend([str(x).strip() for x in items if str(x).strip()])
    # A newly-created bank starts at the first bank item on the second use.
    if not had_items and entry["items"]:
        entry["cursor"] = 0
        entry["first_used"] = False
    save_title_banks()
    return len(items)


def choose_post_title(template_key):
    """
    Title selection for real posts:
      1) first use of a template in a section -> current 'تنظیم عنوان همه' title
      2) later uses -> next item from that template's bank, in the bank's
         stored order (item #1, #2, #3, ...), without repetition
      3) once every item in the bank has been used once (the bank is
         "exhausted") -> instead of stopping or looping back to the exact
         same order, build a new pattern by reshuffling those same bank
         items into a fresh (non-sequential) order and start reading that.
         Every time that shuffled round is finished, it is reshuffled again
         (never twice in a row identical), so repeated rounds never look
         like the same repeating sequence, even though the underlying set
         of texts stays whatever is currently in the bank.
      4) if the user later adds more items to the bank, they are folded in
         automatically (see _title_bank_entry) and used going forward.

    Returns None when the template is not assigned to a section, so old
    behavior outside sections remains untouched.
    """
    g = find_group_for_key(template_key)
    if not g:
        return None
    base_title = str(ALL_TEXTS.get(template_key, {}).get("title", "") or "").strip()
    entry = _title_bank_entry(g, template_key)

    if not entry["first_used"]:
        entry["first_used"] = True
        save_title_banks()
        return base_title

    items = entry.get("items", [])
    if not items:
        # No bank items exist for this template at all.
        return ""

    cursor = int(entry.get("cursor", 0))
    order = entry.get("order") or list(range(len(items)))

    if cursor >= len(order):
        # A full pass (sequential the first time, shuffled every time after)
        # just finished. Build a new pattern out of the same bank items
        # instead of repeating the same order or stopping. Also make sure
        # the very first item of the new pattern isn't the same text as the
        # very last item just used, so two posts in a row never repeat.
        last_idx = order[-1] if order else None
        new_order = list(range(len(items)))
        if len(new_order) > 1:
            prev_order = order
            for _ in range(12):
                random.shuffle(new_order)
                if new_order != prev_order and new_order[0] != last_idx:
                    break
        entry["order"] = new_order
        order = new_order
        cursor = 0

    idx = order[cursor] if 0 <= cursor < len(order) and order[cursor] < len(items) else 0
    selected = items[idx]
    entry["cursor"] = cursor + 1
    save_title_banks()
    return selected


def reset_title_bank_progress(g):
    """Reset bank progress (not the stored items) for every template in a
    section, so the next quick-post round starts again from 'تنظیم عنوان
    همه' instead of continuing mid-way through each template's bank.

    Also resets the quick-post round-robin position for this section back
    to 0, so the very next link goes to the first template in the section
    (not wherever the round-robin had gotten to) — otherwise the title
    reset above would be right but the template order would still be
    mid-cycle.
    """
    section = TITLE_BANKS.get(g)
    reset_count = 0
    if isinstance(section, dict):
        for k, entry in section.items():
            if not isinstance(entry, dict):
                continue
            entry["cursor"] = 0
            entry["first_used"] = False
            entry["order"] = list(range(len(entry.get("items", []) or [])))
            reset_count += 1
        save_title_banks()

    cursor_state = TOPIC_VARIANTS.setdefault("__quick_cursor__", {})
    cursor_state[g] = 0
    save_json(TOPIC_VARIANTS_FILE, TOPIC_VARIANTS)

    return reset_count


def reset_bank_cursor_only(g):
    """Reset ONLY each template's bank position (which bank item comes
    next) back to item #1, for every template in a section — independent
    of reset_title_bank_progress(). Does NOT touch 'first_used' and does
    NOT touch the quick-post round-robin template order; a template that
    is currently mid-bank will just start re-reading its own bank from
    the top the next time its turn comes, still using bank titles (not
    falling back to 'تنظیم عنوان همه')."""
    section = TITLE_BANKS.get(g)
    reset_count = 0
    if isinstance(section, dict):
        for k, entry in section.items():
            if not isinstance(entry, dict):
                continue
            entry["cursor"] = 0
            entry["order"] = list(range(len(entry.get("items", []) or [])))
            reset_count += 1
        save_title_banks()
    return reset_count


def find_group_for_key(k):
    """Return the section/group key that a template belongs to, or None."""
    for g, keys in TEMPLATE_GROUPS.items():
        if k in keys:
            return g
    return None


def group_key(category, subcategory):
    return f"{str(category).strip()}|{str(subcategory).strip()}"


def get_group_templates(category, subcategory=""):
    """Return templates for a category. Exact subgroup is supported, but
    category-only templates are the default so the second bot can stay simple."""
    category = str(category or "").strip()
    subcategory = str(subcategory or "").strip()
    exact = TEMPLATE_GROUPS.get(group_key(category, subcategory), []) if subcategory else []
    category_only = TEMPLATE_GROUPS.get(group_key(category, ""), [])
    keys = exact or category_only
    if not keys:
        # Backward compatibility with older category|subcategory groups.
        keys = [k for g, vals in TEMPLATE_GROUPS.items()
                if str(g).split("|", 1)[0].strip() == category
                for k in vals]
    return [k for k in keys if k in ALL_TEXTS]


def choose_template_for_link(mapping):
    """Choose templates in saved order, cycling within the matching category.
    This makes 1..50 (or any number) deterministic and never asks the user
    to choose a template for each link."""
    if not mapping:
        return ACTIVE_KEY
    category = mapping.get("category") or mapping.get("topic_name") or ""
    subcategory = mapping.get("subcategory") or ""
    candidates = get_group_templates(category, subcategory)
    if not candidates:
        return ACTIVE_KEY

    state = TOPIC_VARIANTS.setdefault("__template_cursor__", {})
    key = group_key(category, subcategory) if subcategory else group_key(category, "")
    cursor = int(state.get(key, 0))
    selected = candidates[cursor % len(candidates)]
    state[key] = cursor + 1
    save_json(TOPIC_VARIANTS_FILE, TOPIC_VARIANTS)
    return selected


def normalize_link(url):
    """Normalize a Telegram/generated link so the bridge lookup is stable."""
    url = (url or "").strip()
    url = url.rstrip(".,!?؛،")
    if url.startswith("t.me/"):
        url = "https://" + url
    return url


def register_link_mapping(url, topic_name=None, topic_key=None, category=None, subcategory=None, label=None, source="bridge"):
    """Persist a link -> topic mapping sent by the second bot."""
    url = normalize_link(url)
    if not url:
        raise ValueError("لینک خالی است")
    record = {
        "url": url,
        "topic_name": topic_name or "",
        "topic_key": topic_key or "",
        "category": category or topic_name or "",
        "subcategory": subcategory or "",
        "label": label or "",
        "source": source,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    with _LINK_REGISTRY_LOCK:
        LINK_REGISTRY[url] = record
        save_json(LINK_REGISTRY_FILE, LINK_REGISTRY)
    return record


def get_link_mapping(url):
    url = normalize_link(url)
    with _LINK_REGISTRY_LOCK:
        return LINK_REGISTRY.get(url)


def choose_topic_header(topic_name):
    """Choose a non-repeating variant for a topic when variants exist."""
    variants = TOPIC_VARIANTS.get(topic_name, [])
    if not isinstance(variants, list):
        variants = []
    variants = [str(x).strip() for x in variants if str(x).strip()]
    if not variants:
        return TOPICS.get(topic_name, "")
    state = TOPIC_VARIANTS.setdefault("__state__", {})
    history = state.setdefault(topic_name, [])
    available = [v for v in variants if v not in history[-10:]] or variants[:]
    selected = random.choice(available)
    history.append(selected)
    state[topic_name] = history[-50:]
    save_json(TOPIC_VARIANTS_FILE, TOPIC_VARIANTS)
    return selected


class LinkBridgeHandler(BaseHTTPRequestHandler):
    """Small stdlib HTTP API used by the separate link-classifier bot."""
    def _send(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        return self.headers.get("X-Link-Bridge-Key", "") == LINK_BRIDGE_KEY

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._send(200, {"ok": True, "service": "main-bot-link-bridge"})
            return
        if parsed.path == "/link-info":
            if not self._authorized():
                self._send(401, {"ok": False, "error": "unauthorized"})
                return
            url = parse_qs(parsed.query).get("url", [""])[0]
            record = get_link_mapping(url)
            if record is None:
                self._send(404, {"ok": False, "found": False})
            else:
                self._send(200, {"ok": True, "found": True, "record": record})
            return
        self._send(404, {"ok": False, "error": "not_found"})

    def do_POST(self):
        if self.path != "/register-link":
            self._send(404, {"ok": False, "error": "not_found"})
            return
        if not self._authorized():
            self._send(401, {"ok": False, "error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            record = register_link_mapping(
                data.get("url", ""),
                topic_name=data.get("topic_name"),
                topic_key=data.get("topic_key"),
                category=data.get("category"),
                subcategory=data.get("subcategory"),
                label=data.get("label"),
                source=data.get("source", "bridge"),
            )
            self._send(200, {"ok": True, "record": record})
        except Exception as e:
            self._send(400, {"ok": False, "error": str(e)})

    def log_message(self, fmt, *args):
        print(f"[LINK BRIDGE] {self.address_string()} - {fmt % args}")


def start_link_bridge_server():
    server = ThreadingHTTPServer((LINK_BRIDGE_HOST, LINK_BRIDGE_PORT), LinkBridgeHandler)
    thread = threading.Thread(target=server.serve_forever, name="link-bridge", daemon=True)
    thread.start()
    print(f"🔗 Link bridge API listening on {LINK_BRIDGE_HOST}:{LINK_BRIDGE_PORT}")
    print(f"🔐 Link bridge key file: {LINK_BRIDGE_KEY_FILE}")
    return server

def save_admins():
    save_json(ADMINS_FILE, ADMINS)

def add_post(user_id, header, link_text, linked_word, url, result_text, section=None):
    posts = load_json(POSTS_FILE, {})
    uid = str(user_id)
    if uid not in posts:
        posts[uid] = []
    posts[uid].insert(0, {
        "id": secrets.token_hex(4),
        "header": header,
        "link_text": link_text,
        "linked_word": linked_word,
        "url": url,
        "result": result_text,
        "section": section or ""
    })
    save_json(POSTS_FILE, posts)

def get_posts(user_id):
    """Returns the user's posts, lazily assigning an id to any legacy post
    that doesn't have one yet (needed for single-post deletion)."""
    posts = load_json(POSTS_FILE, {})
    uid = str(user_id)
    lst = posts.get(uid, [])
    changed = False
    for p in lst:
        if not p.get("id"):
            p["id"] = secrets.token_hex(4)
            changed = True
    if changed:
        posts[uid] = lst
        save_json(POSTS_FILE, posts)
    return lst

def delete_post_by_id(user_id, post_id):
    posts = load_json(POSTS_FILE, {})
    uid = str(user_id)
    lst = posts.get(uid, [])
    new_lst = [p for p in lst if p.get("id") != post_id]
    removed = len(lst) - len(new_lst)
    posts[uid] = new_lst
    save_json(POSTS_FILE, posts)
    return removed

def delete_posts_where(user_id, predicate):
    posts = load_json(POSTS_FILE, {})
    uid = str(user_id)
    lst = posts.get(uid, [])
    remaining = [p for p in lst if not predicate(p)]
    removed = len(lst) - len(remaining)
    posts[uid] = remaining
    save_json(POSTS_FILE, posts)
    return removed

def _scope_title(scope):
    if scope == "__ALL__":
        return "📬 همه پست‌ها"
    if scope == "__OTHER__":
        return "📦 سایر"
    return "📁 " + section_title(scope)

def _posts_for_scope(uid, scope):
    posts = get_posts(uid)
    if scope == "__ALL__":
        return posts
    if scope == "__OTHER__":
        return [p for p in posts if not p.get("section")]
    return [p for p in posts if p.get("section") == scope]

def _scope_predicate(scope):
    if scope == "__ALL__":
        return lambda p: True
    if scope == "__OTHER__":
        return lambda p: not p.get("section")
    return lambda p: p.get("section") == scope

def _myposts_section_menu(uid, scope):
    posts = _posts_for_scope(uid, scope)
    cursor = _POST_SEND_CURSOR.get((uid, scope), 0)
    if cursor > len(posts):
        cursor = 0
    text = f"{_scope_title(scope)}\n\n📦 {len(posts)} پست"
    buttons = []
    if posts:
        if 0 < cursor < len(posts):
            text += f"\n▶️ تا پست {cursor} از {len(posts)} قبلاً نمایش داده شده."
            buttons.append([InlineKeyboardButton("🖨 ادامه نمایش", callback_data=f"myposts_show:{scope}")])
            buttons.append([InlineKeyboardButton("🔁 نمایش از اول", callback_data=f"myposts_showreset:{scope}")])
        else:
            if cursor >= len(posts) and cursor > 0:
                text += "\n✅ همه پست‌های این بخش نمایش داده شدن."
            show_cb = f"myposts_showreset:{scope}" if cursor >= len(posts) else f"myposts_show:{scope}"
            buttons.append([InlineKeyboardButton("🖨 نمایش پست", callback_data=show_cb)])
        buttons.append([InlineKeyboardButton("🗑 حذف پست", callback_data=f"myposts_delpick:{scope}")])
        buttons.append([InlineKeyboardButton("🗑 حذف همه", callback_data=f"myposts_delall:{scope}")])
    else:
        text += "\n📭 هنوز پستی در این بخش نیست."
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data="myposts_back")])
    return text, InlineKeyboardMarkup(buttons)

def _myposts_main_menu(uid):
    posts = get_posts(uid)
    groups = _final_groups()
    counts = {}
    other = 0
    for post in posts:
        g = post.get("section") or ""
        if g:
            counts[g] = counts.get(g, 0) + 1
        else:
            other += 1
    buttons = [[InlineKeyboardButton(f"📬 همه پست‌ها — {len(posts)}", callback_data="myposts_all")]]
    sec_buttons = [InlineKeyboardButton(f"📁 {section_title(g)} — {counts.get(g, 0)} پست", callback_data=f"myposts_sec:{g}") for g in groups]
    for i in range(0, len(sec_buttons), 2):
        buttons.append(sec_buttons[i:i + 2])
    if other:
        buttons.append([InlineKeyboardButton(f"📦 سایر — {other}", callback_data="myposts_other")])
    text = "📁 پست‌های من\n\nهمون پوشه‌های «نهایی» اینجا هم هست؛ یکی رو انتخاب کن."
    empty = not posts and not groups
    return text, InlineKeyboardMarkup(buttons), empty

async def _run_show_posts(bot, chat_id, uid, scope):
    posts = _posts_for_scope(uid, scope)
    idx = _POST_SEND_CURSOR.get((uid, scope), 0)
    if idx >= len(posts):
        idx = 0
    while idx < len(posts):
        if not _POST_SEND_ACTIVE.get((uid, scope)):
            break
        post = posts[idx]
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=post["result"],
                parse_mode="HTML",
                disable_web_page_preview=True
            )
        except Exception:
            pass
        idx += 1
        _POST_SEND_CURSOR[(uid, scope)] = idx
        await asyncio.sleep(0.2)
    _POST_SEND_ACTIVE[(uid, scope)] = False
    if idx >= len(posts):
        status = f"✅ همه {len(posts)} پست نمایش داده شد."
    else:
        status = f"⏹ متوقف شد. {idx} از {len(posts)} پست نمایش داده شد.\nبا «نمایش پست» از همینجا ادامه بده."
    text2, markup2 = _myposts_section_menu(uid, scope)
    try:
        await bot.send_message(chat_id=chat_id, text=status + "\n\n" + text2, reply_markup=markup2)
    except Exception:
        pass


def build_post_result(url, template_key, topic_name=None, title_override=None):
    """Build a post. The ONLY source of the post title is ALL_TEXTS[key]["title"]."""
    base = ALL_TEXTS.get(template_key, {})
    if not base:
        raise ValueError("قالب پیدا نشد.")

    link_text = str(base.get("link_text", "download") or "")
    linked_word = str(base.get("linked_word", "") or "")
    footer = str(base.get("footer", "") or "")
    bq = base.get("blockquote", False)
    quote_text = str(base.get("quote_text", "") or "")
    if quote_text and quote_text not in link_text:
        quote_text = ""

    # A real post may supply a title from the section's title-bank flow.
    # If no override is supplied, preserve the existing template title exactly.
    header = (str(title_override).strip() if title_override is not None
              else str(base.get("title", "") or "").strip())

    # link_text is used exactly as stored — "تنظیم عنوان همه" already keeps
    # it clean (no leftover title line, no lost blank-line spacing) at the
    # moment the title is set, so nothing here should second-guess it.

    if linked_word and linked_word not in link_text:
        linked_word = ""

    if linked_word and linked_word in link_text:
        parts = link_text.split(linked_word, 1)
        link_anchor = f'<a href="{url}">{escape(linked_word)}</a>'
        before = escape(parts[0])
        after = escape(parts[1])
        if bq and quote_text and quote_text.strip() == linked_word.strip():
            # The quoted phrase IS the linked word itself: wrap the whole link.
            link_anchor = f"<blockquote>{link_anchor}</blockquote>"
        elif bq and quote_text:
            esc_q = escape(quote_text)
            if esc_q in before:
                before = before.replace(esc_q, f"<blockquote>{esc_q}</blockquote>", 1)
            elif esc_q in after:
                after = after.replace(esc_q, f"<blockquote>{esc_q}</blockquote>", 1)
            else:
                # The chosen phrase overlaps the link boundary — fall back to
                # quoting the whole link so nothing silently gets dropped.
                link_anchor = f"<blockquote>{link_anchor}</blockquote>"
        elif bq and not quote_text:
            # Old templates with no specific phrase chosen: keep legacy behavior.
            link_anchor = f"<blockquote>{link_anchor}</blockquote>"
        link_part = f"{before}{link_anchor}{after}"
    else:
        link_part = f'<a href="{url}">{escape(link_text)}</a>'
        if bq:
            link_part = f"<blockquote>{link_part}</blockquote>"

    parts_list = []
    if header:
        parts_list.append(f"<b>{escape(header)}</b>")
    parts_list.append(link_part)
    if footer:
        parts_list.append(escape(footer))

    # Never force extra blank lines here: each template's own link_text/footer
    # already carries whatever spacing it needs. Join with a single newline
    # and let that stored spacing (or lack of it) come through untouched.
    result = preserve_spaces("\n".join(parts_list))
    return header, link_text, linked_word, result

# ═══════════════════════════════════════════════════
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not can_use_bot(uid):
        await update.message.reply_text("⛔ دسترسی نداری!")
        return
    await update.message.reply_text(
        "👋 سلام!\n\n"
        "🎯 آماده‌سازی → قالب رو انتخاب کن، بعد با موضوع یا بدون موضوع رو مشخص کن\n"
        "📋 لیست متن‌ها → مدیریت قالب‌ها + متن‌های رندوم\n"
        "🧩 مدیریت قالب‌ها → مدیریت بخش‌ها و قالب‌ها\n"
        "➕ افزودن متن → ساخت قالب جدید\n"
        "📁 پست‌های من → همه پست‌ها\n"
        "🤖 دستیار → اجرای دستورهای ساده با متن فارسی\n\n"
        "💡 نکته: بعد از انتخاب قالب، می‌تونی پست رو با موضوع یا بدون موضوع بسازی.\n"
        "در حالت بدون موضوع، بعد از لینک مستقیم پست ساخته می‌شه.",
        reply_markup=get_main_keyboard(uid)
    )

async def backup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Create and send a manual backup of the bot's JSON data. Owner only."""
    uid = update.effective_user.id
    if not is_owner(uid):
        await update.message.reply_text("⛔ فقط مالک ربات می‌تونه بکاپ بگیره!")
        return

    files = [ADMINS_FILE, TEXTS_FILE, TOPICS_FILE, POSTS_FILE, LINK_REGISTRY_FILE, TOPIC_VARIANTS_FILE, TEMPLATE_GROUPS_FILE, TITLE_BANK_FILE, LINK_BRIDGE_KEY_FILE]
    existing_files = [path for path in files if os.path.isfile(path)]

    if not existing_files:
        await update.message.reply_text("❌ هیچ فایل دیتایی برای بکاپ وجود نداره!")
        return

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    backup_file = os.path.join(DATA_DIR, f"bot_backup_{timestamp}.zip")

    try:
        with zipfile.ZipFile(backup_file, "w", zipfile.ZIP_DEFLATED) as zipf:
            for file_path in existing_files:
                zipf.write(file_path, arcname=os.path.basename(file_path))

        with open(backup_file, "rb") as backup_stream:
            await update.message.reply_document(
                document=backup_stream,
                filename=os.path.basename(backup_file),
                caption=f"📦 بکاپ آماده شد!\n\n🕐 {timestamp}"
            )
    except Exception as e:
        print(f"[BACKUP ERROR] {e}")
        await update.message.reply_text("❌ گرفتن بکاپ با خطا مواجه شد. لاگ Railway رو بررسی کن.")
    finally:
        # The backup is manual and temporary; it is deleted after being sent.
        try:
            if os.path.exists(backup_file):
                os.remove(backup_file)
        except Exception as e:
            print(f"[BACKUP CLEANUP ERROR] {e}")



async def template_manager_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not can_use_bot(uid):
        await update.message.reply_text("⛔ دسترسی نداری!")
        return
    if not has_permission(uid, "manage_templates"):
        await update.message.reply_text("⛔ اجازه مدیریت قالب‌ها رو نداری!", reply_markup=get_main_keyboard(uid))
        return

    groups = []
    for g, keys in sorted(TEMPLATE_GROUPS.items()):
        valid = [k for k in keys if k in ALL_TEXTS]
        if valid:
            category, subcategory = g.split("|", 1)
            groups.append(f"• {category} → {subcategory}: {len(valid)} قالب")

    text = "🧩 مدیریت قالب‌ها\n\n"
    text += "➕ افزودن قالب → یک قالب جدید بساز و دسته‌بندی کن.\n"
    text += "📋 گروه‌ها → ببین هر دسته چند قالب دارد.\n\n"
    text += ("📦 گروه‌های فعلی:\n" + "\n".join(groups)) if groups else "📭 هنوز قالب دسته‌بندی‌شده‌ای نداری."

    buttons = [
        [InlineKeyboardButton("➕ افزودن قالب", callback_data="tmpl_add")],
        [InlineKeyboardButton("📋 گروه‌های قالب", callback_data="tmpl_groups")],
    ]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons))

async def template_groups_callback(q):
    lines = []
    for g, keys in sorted(TEMPLATE_GROUPS.items()):
        valid = [k for k in keys if k in ALL_TEXTS]
        if not valid:
            continue
        category, subcategory = g.split("|", 1)
        lines.append(f"• {category} → {subcategory} ({len(valid)} قالب)")
        for k in valid[:10]:
            lines.append(f"   └ {k}")
    if not lines:
        lines = ["📭 هنوز گروهی ساخته نشده."]
    await q.edit_message_text(
        "📋 گروه‌های قالب:\n\n" + "\n".join(lines),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ افزودن قالب", callback_data="tmpl_add")],
            [InlineKeyboardButton("🔙 بستن", callback_data="tmpl_close")]
        ])
    )

async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not can_use_bot(uid):
        await update.message.reply_text("⛔ دسترسی نداری!")
        return
    if not has_permission(uid, "add_text"):
        await update.message.reply_text("⛔ اجازه افزودن متن رو نداری!", reply_markup=get_main_keyboard(uid))
        return
    raw = update.message.text or ""
    m = re.match(r'/add\s+(\S+)\s+(.*)', raw, re.DOTALL)
    if not m:
        await update.message.reply_text(
            "❌ /add نام | متن-پایین | کلمه-لینک\n\n"
            "مثال:\n/add p2 DownLoad مشاهده 📥 | مشاهده",
            reply_markup=get_main_keyboard(uid)
        )
        return
    key, rest = m.group(1), m.group(2)
    parts = rest.split("|", 2)
    if len(parts) == 3:
        header, link_text, linked_word = [p.strip() for p in parts]
    elif len(parts) == 2:
        header, link_text = [p.strip() for p in parts]
        linked_word = ""
    else:
        header = ""
        link_text = rest.strip()
        linked_word = ""
    rh = ALL_TEXTS.get(key, {}).get("random_headers", []) if key in ALL_TEXTS else []
    if header and header not in rh:
        rh.insert(0, header)
    old_footer = ALL_TEXTS.get(key, {}).get("footer", "")
    old_bq = ALL_TEXTS.get(key, {}).get("blockquote", False)
    ALL_TEXTS[key] = {
        "header": "",
        "link_text": link_text,
        "linked_word": linked_word,
        "footer": old_footer,
        "random_headers": rh,
        "blockquote": old_bq
    }
    save_texts()
    msg = f"✅ {key} ذخیره شد!\n\n"
    msg += f"🔗 {link_text}\n\n"
    if linked_word:
        msg += f"🔵 لینک‌شده: {linked_word}\n\n"
    if old_footer:
        msg += f"📌 فوتر: {old_footer}\n\n"
    msg += f"💬 نقل‌قول: {'✅ روشن' if old_bq else '❌ خاموش'}\n\n"
    if rh:
        msg += f"🎲 متن‌های رندوم ({len(rh)} تا):\n" + "\n".join([f"• {h}" for h in rh[:5]])
    await update.message.reply_text(msg, reply_markup=get_main_keyboard(uid))

async def final_menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Final folder manager: open, add, and delete folders."""
    uid = update.effective_user.id
    if not can_use_bot(uid):
        await update.message.reply_text("⛔ دسترسی نداری!")
        return
    if not has_permission(uid, "list"):
        await update.message.reply_text("⛔ اجازه مدیریت پوشه‌ها رو نداری!", reply_markup=get_main_keyboard(uid))
        return
    groups = sorted(TEMPLATE_GROUPS.keys())
    buttons = [
        [InlineKeyboardButton("➕ افزودن پوشه", callback_data="final_add")],
        [InlineKeyboardButton("🗑 حذف پوشه", callback_data="final_del_menu")],
    ]
    for idx, g in enumerate(groups):
        buttons.append([InlineKeyboardButton(f"📁 {section_title(g)}", callback_data=f"final_open:{idx}")])
    await update.message.reply_text(
        "📁 نهایی\n\n"
        "از اینجا پوشه‌ها را مدیریت کن.\n"
        "➕ افزودن پوشه = ساخت پوشه جدید\n"
        "🗑 حذف پوشه = حذف خود پوشه (قالب‌ها حذف نمی‌شوند)\n\n"
        f"📦 تعداد پوشه‌ها: {len(groups)}",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

def _final_groups():
    return sorted(TEMPLATE_GROUPS.keys())

async def _show_final_menu(q):
    groups = _final_groups()
    buttons = [
        [InlineKeyboardButton("➕ افزودن پوشه", callback_data="final_add")],
        [InlineKeyboardButton("🗑 حذف پوشه", callback_data="final_del_menu")],
    ]
    folder_buttons = [InlineKeyboardButton(f"📁 {section_title(g)}", callback_data=f"final_open:{idx}") for idx, g in enumerate(groups)]
    for i in range(0, len(folder_buttons), 2):
        buttons.append(folder_buttons[i:i + 2])
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data="final_close")])
    await q.edit_message_text("📁 نهایی\n\nپوشه موردنظر را انتخاب کن یا پوشه جدید بساز.", reply_markup=InlineKeyboardMarkup(buttons))


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not can_use_bot(uid):
        await update.message.reply_text("⛔ دسترسی نداری!")
        return
    if not has_permission(uid, "list"):
        await update.message.reply_text("⛔ اجازه دیدن لیست متن‌ها رو نداری!", reply_markup=get_main_keyboard(uid))
        return

    groups = []
    for g, keys in TEMPLATE_GROUPS.items():
        valid = [k for k in keys if k in ALL_TEXTS]
        if valid:
            groups.append((g, valid))

    buttons = [[InlineKeyboardButton("➕ ساخت بخش جدید", callback_data="section_add")]]
    section_buttons = []
    for g, keys in sorted(groups, key=lambda x: section_title(x[0])):
        mark = "⚡" if section_enabled(g) else "⛔"
        section_buttons.append(InlineKeyboardButton(f"📁 {section_title(g)} — {len(keys)} قالب {mark}", callback_data=f"section:{g}"))
    for i in range(0, len(section_buttons), 2):
        buttons.append(section_buttons[i:i + 2])

    text = (
        "📋 لیست متن‌ها / قالب‌ها\n\n"
        "📁 هر بخش قالب‌های خودش را دارد.\n"
        "⚡ = فعال برای پست سریع\n"
        "⛔ = غیرفعال برای پست سریع\n\n"
        "یک بخش را انتخاب کن یا بخش جدید بساز."
    )
    if update.message:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons))
    else:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))


async def cleanup_ungrouped_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global ACTIVE_KEY
    uid = update.effective_user.id
    if not can_use_bot(uid):
        await update.message.reply_text("⛔ دسترسی نداری!")
        return
    if not has_permission(uid, "list"):
        await update.message.reply_text("⛔ اجازه نداری!")
        return
    ungrouped = [k for k in list(ALL_TEXTS.keys()) if not any(k in vals for vals in TEMPLATE_GROUPS.values())]
    for k in ungrouped:
        ALL_TEXTS.pop(k, None)
    save_texts()
    if ACTIVE_KEY not in ALL_TEXTS:
        ACTIVE_KEY = next(iter(ALL_TEXTS), "")
    await update.message.reply_text(f"✅ {len(ungrouped)} قالب بدون‌بخش برای همیشه حذف شدند.")


async def ready_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not can_use_bot(uid):
        await update.message.reply_text("⛔ دسترسی نداری!")
        return
    if not has_permission(uid, "ready"):
        await update.message.reply_text("⛔ اجازه آماده‌سازی رو نداری!", reply_markup=get_main_keyboard(uid))
        return
    if not ALL_TEXTS:
        await update.message.reply_text("📭 اول یه قالب بساز!", reply_markup=get_main_keyboard(uid))
        return
    buttons = []
    for k in sorted(ALL_TEXTS.keys()):
        mark = "✅ " if k == ACTIVE_KEY else ""
        lt = ALL_TEXTS[k].get("link_text", "")[:20]
        rh_count = len(ALL_TEXTS[k].get("random_headers", []))
        buttons.append([InlineKeyboardButton(f"{mark}{k} — {lt} (🎲{rh_count})", callback_data=f"ready:{k}")])
    await update.message.reply_text(
        "🎯 کدوم قالب رو می‌خوای آماده کنی؟\n\n"
        "👆 انتخاب کن، بعد مشخص می‌کنی با موضوع یا بدون موضوع.",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def quick_post_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not can_use_bot(uid) or not has_permission(uid, "ready"):
        await update.message.reply_text("⛔ اجازه ساخت پست سریع رو نداری!", reply_markup=get_main_keyboard(uid))
        return
    groups=[]
    for g, keys in sorted(TEMPLATE_GROUPS.items()):
        valid=[k for k in keys if k in ALL_TEXTS]
        if valid and section_enabled(g):
            groups.append((g, valid))
    if not groups:
        await update.message.reply_text(
            "📭 هیچ بخشی برای پست سریع فعال نیست.\n\n"
            "از 📋 لیست متن‌ها وارد بخش شو و ⚡ پست سریع آن بخش را روشن کن.",
            reply_markup=get_main_keyboard(uid)
        )
        return
    buttons=[[InlineKeyboardButton(f"📁 {section_title(g)} — {len(keys)} قالب", callback_data=f"quickselect:{g}")] for g,keys in groups]
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data="quick_close")])
    await update.message.reply_text(
        "⚡ پست سریع\n\n"
        "بخش موردنظر را انتخاب کن.\n"
        "بعد از انتخاب، فقط لینک‌ها را بفرست یا پیام‌های ربات دوم را Forward کن.\n"
        "قالب‌های بخش‌های دیگر وارد این صف نمی‌شوند.",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def my_posts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not can_use_bot(uid):
        await update.message.reply_text("⛔ دسترسی نداری!")
        return
    if not has_permission(uid, "my_posts"):
        await update.message.reply_text("⛔ اجازه دیدن پست‌ها رو نداری!", reply_markup=get_main_keyboard(uid))
        return
    text, markup, empty = _myposts_main_menu(uid)
    if empty:
        await update.message.reply_text("📭 هنوز پستی نساختی!", reply_markup=get_main_keyboard(uid))
        return
    await update.message.reply_text(text, reply_markup=markup)

# ═══════════════════════════════════════════════════
# Admin Management Commands
# ═══════════════════════════════════════════════════

async def admin_menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_owner(uid):
        await update.message.reply_text("⛔ فقط مالک ربات می‌تونه ادمین‌ها رو مدیریت کنه!", reply_markup=get_main_keyboard(uid))
        return
    await update.message.reply_text(
        "👮‍♂️ پنل مدیریت ادمین‌ها:\n\n"
        "➕ افزودن ادمین → یه نفر رو ادمین کن\n"
        "🗑 برکناری ادمین → ادمین رو حذف کن\n"
        "⚙️ مدیریت دسترسی‌ها → دسترسی هر ادمین رو تنظیم کن\n"
        "📋 لیست ادمین‌ها → ببین کی ادمینه\n\n"
        "💡 نکته: مالک (تو) همیشه به همه چی دسترسی داری!",
        reply_markup=ADMIN_MANAGE_KEYBOARD
    )

async def admin_list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_owner(uid):
        return
    if not ADMINS:
        await update.message.reply_text("📭 هیچ ادمینی ثبت نشده!", reply_markup=ADMIN_MANAGE_KEYBOARD)
        return
    lines = []
    for aid, data in ADMINS.items():
        fa = "✅ کامل" if data.get("full_access") else "❌ محدود"
        perms = ", ".join(data.get("permissions", [])) if not data.get("full_access") else "همه"
        lines.append(f"👤 {aid}\n   دسترسی: {fa}\n   بخش‌ها: {perms}")
    await update.message.reply_text("📋 لیست ادمین‌ها:\n\n" + "\n\n".join(lines), reply_markup=ADMIN_MANAGE_KEYBOARD)

async def use_template_view(q, k, extra_note=None):
    """Render the template-detail view (used after selecting/updating a template)."""
    t = ALL_TEXTS.get(k)
    if not t:
        await q.edit_message_text("❌ قالب پیدا نشد!")
        return
    lw = t.get("linked_word", "")
    footer = t.get("footer", "")
    bq = t.get("blockquote", False)
    qt = t.get("quote_text", "")
    msg = f"✅ {k} فعال شد!\n\n🔗 {t.get('link_text', '')}"
    if lw:
        msg += f"\n🔵 لینک‌شده: {lw}"
    if footer:
        msg += f"\n📌 فوتر: {footer}"
    msg += f"\n💬 نقل‌قول: {'✅ روشن' if bq else '❌ خاموش'}"
    if bq and qt:
        msg += f"\n💬 متن نقل‌قول: {qt}"
    if extra_note:
        msg += f"\n\n{extra_note}"
    buttons = [
        [InlineKeyboardButton("✏️ ویرایش قالب", callback_data=f"edit:{k}"),
         InlineKeyboardButton("📄 کپی قالب", callback_data=f"copy:{k}")],
        [InlineKeyboardButton("👀 پیش‌نمایش", callback_data=f"preview:{k}")],
        [InlineKeyboardButton("🗑️ حذف قالب", callback_data=f"del_confirm:{k}")],
        [InlineKeyboardButton("➕ افزودن متن رندوم", callback_data=f"rhadd:{k}")],
        [InlineKeyboardButton("📋 لیست متن‌های رندوم", callback_data=f"rhlist:{k}")],
    ]
    if bq:
        buttons.append([InlineKeyboardButton("❌ خاموش کردن نقل‌قول", callback_data=f"bq_off:{k}")])
        buttons.append([InlineKeyboardButton("🌐 روشن کردن نقل‌قول برای همه این بخش", callback_data=f"bqall:{k}")])
    elif qt:
        buttons.append([InlineKeyboardButton(f"✅ روشن کردن نقل‌قول ({qt})", callback_data=f"bq_on_quick:{k}")])
        buttons.append([InlineKeyboardButton("✏️ نقل‌قول با متن جدید", callback_data=f"bq_ask:{k}")])
    else:
        buttons.append([InlineKeyboardButton("✅ روشن کردن نقل‌قول", callback_data=f"bq_ask:{k}")])
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data="back_list")])
    await q.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(buttons))


# ═══════════════════════════════════════════════════
async def show_section(q, g):
    keys = [k for k in TEMPLATE_GROUPS.get(g, []) if k in ALL_TEXTS]
    title = "📁 " + section_title(g)
    enabled = section_enabled(g)
    buttons=[]
    if keys:
        buttons.append([InlineKeyboardButton("🏷️ بانک کلمات", callback_data=f"titlebank:{g}")])
        buttons.append([InlineKeyboardButton("🏷️ تنظیم عنوان همه", callback_data=f"titles_all:{g}")])
        buttons.append([InlineKeyboardButton("👁️ مشاهده عنوان‌های تنظیم‌شده", callback_data=f"titles_all_view:{g}")])
    else:
        buttons.append([InlineKeyboardButton("📭 این بخش فعلاً خالی است", callback_data="noop")])
    buttons.append([InlineKeyboardButton(f"{'⛔ خاموش کردن' if enabled else '⚡ روشن کردن'} پست سریع این بخش", callback_data=f"quicktoggle:{g}")])
    buttons.append([InlineKeyboardButton("➕ افزودن متن به این بخش", callback_data=f"section_add_text:{g}")])
    buttons.append([InlineKeyboardButton("📦 افزودن قالب موجود به این بخش", callback_data=f"section_add_template:{g}")])
    quote_status = ""
    if keys:
        bq_count = sum(1 for k in keys if ALL_TEXTS.get(k, {}).get("blockquote"))
        quote_status = f"\n💬 نقل‌قول: {bq_count} از {len(keys)} قالب روشن"
        last_word = get_section_last_quote(g)
        if last_word:
            quote_status += f"\n💬 آخرین کلمه: {last_word}"
            buttons.append([InlineKeyboardButton(f"✅ روشن کردن نقل‌قول برای همه ({last_word})", callback_data=f"section_bq_on_quick:{g}")])
            buttons.append([InlineKeyboardButton("✏️ نقل‌قول همه با کلمه جدید", callback_data=f"section_bq_on:{g}")])
        else:
            buttons.append([InlineKeyboardButton("✅ روشن کردن نقل‌قول برای همه بخش", callback_data=f"section_bq_on:{g}")])
        buttons.append([InlineKeyboardButton("❌ خاموش کردن نقل‌قول برای همه بخش", callback_data=f"section_bq_off:{g}")])
    for k in keys:
        buttons.append([InlineKeyboardButton(f"📝 {k}", callback_data=f"use:{k}")])
        buttons.append([InlineKeyboardButton(f"🗑 حذف {k}", callback_data=f"del:{k}")])
    buttons.append([InlineKeyboardButton("🗑 حذف همه قالب‌ها", callback_data=f"section_delall_templates:{g}")])
    buttons.append([InlineKeyboardButton("🗑 حذف بخش", callback_data=f"section_del:{g}")])
    buttons.append([InlineKeyboardButton("🔙 لیست بخش‌ها", callback_data="back_list")])
    await q.edit_message_text(title + f"\n\n📦 {len(keys)} قالب" + quote_status, reply_markup=InlineKeyboardMarkup(buttons))

async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global ACTIVE_KEY
    q = update.callback_query
    await q.answer()
    data = q.data
    uid = update.effective_user.id

    if data == "quick_close":
        await q.edit_message_text("⚡ پست سریع بسته شد.", reply_markup=None)
        return

    if data == "noop":
        await q.answer("این بخش هنوز قالبی ندارد.", show_alert=False)
        return

    if data == "final_close":
        await q.edit_message_text("📁 منوی نهایی بسته شد.", reply_markup=None)
        return

    if data == "final_add":
        if not has_permission(uid, "list"):
            await q.edit_message_text("⛔ اجازه نداری!")
            return
        context.user_data.clear()
        context.user_data["state"] = "section_add_name"
        context.user_data["return_to_final"] = True
        await q.edit_message_text("➕ افزودن پوشه\n\nاسم پوشه را بفرست.\nمثلاً: وطنی\n\nبرای لغو: لغو")
        return

    if data == "final_del_menu":
        if not has_permission(uid, "list"):
            await q.edit_message_text("⛔ اجازه نداری!")
            return
        groups = _final_groups()
        if not groups:
            await q.edit_message_text("📭 هنوز هیچ پوشه‌ای ساخته نشده.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➕ افزودن پوشه", callback_data="final_add")],[InlineKeyboardButton("🔙 بازگشت", callback_data="final_close")]]))
            return
        buttons = [[InlineKeyboardButton(f"🗑 {section_title(g)}", callback_data=f"final_del:{idx}")] for idx, g in enumerate(groups)]
        buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data="final_close")])
        await q.edit_message_text("🗑 حذف پوشه\n\nپوشه‌ای را که می‌خواهی حذف شود انتخاب کن:", reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data.startswith("final_open:"):
        if not has_permission(uid, "list"):
            await q.edit_message_text("⛔ اجازه نداری!")
            return
        try:
            idx = int(data.split(":", 1)[1]); g = _final_groups()[idx]
        except (ValueError, IndexError):
            await q.edit_message_text("❌ این پوشه پیدا نشد."); return
        await show_section(q, g)
        return

    if data.startswith("final_del:"):
        if not has_permission(uid, "list"):
            await q.edit_message_text("⛔ اجازه نداری!"); return
        try:
            idx = int(data.split(":", 1)[1]); g = _final_groups()[idx]
        except (ValueError, IndexError):
            await q.edit_message_text("❌ این پوشه پیدا نشد."); return
        context.user_data["final_delete_group"] = g
        await q.edit_message_text(f"⚠️ حذف پوشه «{section_title(g)}»؟\n\nاین کار خود قالب‌های داخل این پوشه را هم برای همیشه حذف می‌کند.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ بله، حذف کن", callback_data="final_del_yes")],[InlineKeyboardButton("❌ لغو", callback_data="final_del_menu")]]))
        return

    if data == "final_del_yes":
        if not has_permission(uid, "list"):
            await q.edit_message_text("⛔ اجازه نداری!"); return
        g = context.user_data.pop("final_delete_group", None)
        if not g or g not in TEMPLATE_GROUPS:
            await q.edit_message_text("❌ این پوشه دیگر وجود ندارد."); return
        keys=[k for k in TEMPLATE_GROUPS.get(g,[]) if k in ALL_TEXTS]
        for k in keys:
            ALL_TEXTS.pop(k, None)
        del TEMPLATE_GROUPS[g]; SECTION_SETTINGS.pop(g, None)
        save_template_groups(); save_section_settings(); save_texts()
        if ACTIVE_KEY not in ALL_TEXTS:
            ACTIVE_KEY = next(iter(ALL_TEXTS), "")
        await q.edit_message_text(f"✅ پوشه «{section_title(g)}» و {len(keys)} قالب داخل آن برای همیشه حذف شدند.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📁 برگشت به نهایی", callback_data="final_back")]]))
        return

    if data == "final_back":
        if not has_permission(uid, "list"):
            await q.edit_message_text("⛔ اجازه نداری!"); return
        await _show_final_menu(q)
        return

    if data.startswith("quickselect:"):
        if not has_permission(uid, "ready"):
            await q.edit_message_text("⛔ اجازه نداری!")
            return
        g=data[len("quickselect:"):]
        if not section_enabled(g):
            await q.edit_message_text("⛔ این بخش برای پست سریع خاموش است.")
            return
        keys=[k for k in TEMPLATE_GROUPS.get(g,[]) if k in ALL_TEXTS]
        if not keys:
            await q.edit_message_text("📭 این بخش قالبی ندارد.")
            return
        context.user_data["quick_mode"] = True
        context.user_data["quick_group"] = g
        buttons = [
            [InlineKeyboardButton("🔄 ریست (برگشت به تنظیم عنوان همه)", callback_data=f"quickreset:{g}")],
            [InlineKeyboardButton("🏦 ریست بانک (شروع بانک از اول)", callback_data=f"quickresetbank:{g}")],
        ]
        await q.edit_message_text(
            f"⚡ پست سریع فعال شد: {section_title(g)}\n\n"
            f"📦 {len(keys)} قالب در صف این بخش است.\n"
            "حالا فقط لینک‌ها را بفرست یا پیام‌های ربات دوم را Forward کن.\n"
            "هر لینک = یک پست.\n\n"
            "اگر اشتباهی پیش اومد و می‌خوای دوباره از عنوان‌های «تنظیم عنوان همه» شروع کنی (نه از وسط بانک کلمات)، روی ریست بزن.\n"
            "اگر فقط می‌خوای بانک کلمات هر قالب از متن شماره ۱ خودش دوباره شروع بشه (بدون برگشتن به تنظیم عنوان همه و بدون تغییر نوبت قالب‌ها)، روی ریست بانک بزن.\n\n"
            "برای خروج: لغو",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    if data.startswith("quickreset:"):
        if not has_permission(uid, "ready"):
            await q.edit_message_text("⛔ اجازه نداری!")
            return
        g = data[len("quickreset:"):]
        if not section_enabled(g):
            await q.edit_message_text("⛔ این بخش برای پست سریع خاموش است.")
            return
        keys = [k for k in TEMPLATE_GROUPS.get(g, []) if k in ALL_TEXTS]
        if not keys:
            await q.edit_message_text("📭 این بخش قالبی ندارد.")
            return
        reset_title_bank_progress(g)
        try:
            await q.answer("✅ ریست شد — دور بعدی از تنظیم عنوان همه شروع می‌شه.", show_alert=True)
        except Exception:
            pass
        context.user_data["quick_mode"] = True
        context.user_data["quick_group"] = g
        buttons = [
            [InlineKeyboardButton("🔄 ریست (برگشت به تنظیم عنوان همه)", callback_data=f"quickreset:{g}")],
            [InlineKeyboardButton("🏦 ریست بانک (شروع بانک از اول)", callback_data=f"quickresetbank:{g}")],
        ]
        await q.edit_message_text(
            f"✅ ریست شد: {section_title(g)}\n\n"
            "همه‌ی قالب‌های این بخش دوباره از عنوان «تنظیم عنوان همه» شروع می‌کنن و نوبت قالب‌ها هم از قالب اول شروع می‌شه؛ بانک کلمات هر قالب دست‌نخورده مونده (فقط از اول دوباره خونده می‌شه، وقتی نوبتش برسه).\n\n"
            f"📦 {len(keys)} قالب در صف این بخش است.\n"
            "حالا فقط لینک‌ها را بفرست یا پیام‌های ربات دوم را Forward کن.\n"
            "هر لینک = یک پست.\n\n"
            "برای خروج: لغو",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    if data.startswith("quickresetbank:"):
        if not has_permission(uid, "ready"):
            await q.edit_message_text("⛔ اجازه نداری!")
            return
        g = data[len("quickresetbank:"):]
        if not section_enabled(g):
            await q.edit_message_text("⛔ این بخش برای پست سریع خاموش است.")
            return
        keys = [k for k in TEMPLATE_GROUPS.get(g, []) if k in ALL_TEXTS]
        if not keys:
            await q.edit_message_text("📭 این بخش قالبی ندارد.")
            return
        reset_bank_cursor_only(g)
        try:
            await q.answer("✅ بانک ریست شد — هر قالب دوباره از متن شماره ۱ بانک خودش شروع می‌کنه.", show_alert=True)
        except Exception:
            pass
        context.user_data["quick_mode"] = True
        context.user_data["quick_group"] = g
        buttons = [
            [InlineKeyboardButton("🔄 ریست (برگشت به تنظیم عنوان همه)", callback_data=f"quickreset:{g}")],
            [InlineKeyboardButton("🏦 ریست بانک (شروع بانک از اول)", callback_data=f"quickresetbank:{g}")],
        ]
        await q.edit_message_text(
            f"✅ بانک ریست شد: {section_title(g)}\n\n"
            "نوبت قالب‌ها و «اولین‌بار از تنظیم عنوان همه» بودنشون دست‌نخورده مونده؛ فقط بانک هر قالب از متن شماره ۱ خودش دوباره شروع می‌شه.\n\n"
            f"📦 {len(keys)} قالب در صف این بخش است.\n"
            "حالا فقط لینک‌ها را بفرست یا پیام‌های ربات دوم را Forward کن.\n"
            "هر لینک = یک پست.\n\n"
            "برای خروج: لغو",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    if data == "section_add":
        if not has_permission(uid, "list"):
            await q.edit_message_text("⛔ اجازه نداری!")
            return
        context.user_data.clear()
        context.user_data["state"] = "section_add_name"
        await q.edit_message_text("📁 ساخت بخش جدید\n\nاسم بخش را بفرست.\nمثلاً: وطنی\n\nبرای لغو: لغو")
        return

    if data.startswith("section:"):
        if not has_permission(uid, "list"):
            await q.edit_message_text("⛔ اجازه نداری!")
            return
        await show_section(q, data[8:])
        return

    if data.startswith("titlebank:"):
        if not has_permission(uid, "list"):
            await q.edit_message_text("⛔ اجازه نداری!")
            return
        g = data[len("titlebank:"):]
        keys = [k for k in TEMPLATE_GROUPS.get(g, []) if k in ALL_TEXTS]
        if not keys:
            await q.edit_message_text("📭 این بخش قالبی ندارد.")
            return
        buttons = []
        for k in keys:
            buttons.append([InlineKeyboardButton(k, callback_data=f"tbk:{g}:{k}")])
        buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data=f"section:{g}")])
        await q.edit_message_text(
            f"🏷️ بانک کلمات — {section_title(g)}\n\n"
            "قالب موردنظر را انتخاب کن:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    if data.startswith("tbk:"):
        if not has_permission(uid, "list"):
            await q.edit_message_text("⛔ اجازه نداری!")
            return
        parts = data.split(":", 2)
        if len(parts) != 3:
            await q.edit_message_text("❌ خطا در انتخاب قالب.")
            return
        _, g, k = parts
        keys = [x for x in TEMPLATE_GROUPS.get(g, []) if x in ALL_TEXTS]
        if k not in keys:
            await q.edit_message_text("❌ قالب پیدا نشد یا دیگر در این بخش نیست.")
            return
        context.user_data.clear()
        context.user_data["state"] = "title_bank_add"
        context.user_data["title_bank_group"] = g
        context.user_data["title_bank_template"] = k
        entry = _title_bank_entry(g, k)
        count = len(entry.get("items", []))
        await q.edit_message_text(
            f"🏷️ بانک کلمات — {k}\n\n"
            "متن‌هایی که برای این قسمت نیاز دارید را در یک پیام بفرست؛ هر خط = یک عنوان.\n\n"
            "مثال:\n"
            "سیاه\n"
            "گلی\n"
            "فلان\n\n"
            f"📦 تعداد متن‌های بانک: {count}\n\n"
            "برای لغو: لغو"
        )
        return

    if data.startswith("titles_all:"):
        if not has_permission(uid, "list"):
            await q.edit_message_text("⛔ اجازه نداری!")
            return
        g = data[len("titles_all:"):]
        keys = [k for k in TEMPLATE_GROUPS.get(g, []) if k in ALL_TEXTS]
        if not keys:
            await q.edit_message_text("📭 این بخش قالبی ندارد.")
            return
        context.user_data.clear()
        context.user_data["state"] = "titles_all"
        context.user_data["titles_group"] = g
        context.user_data["titles_keys"] = keys
        msg = (
            f"🏷️ تنظیم عنوان همه — {section_title(g)}\n\n"
            "عنوان‌های جدید را به ترتیب در یک پیام بفرست، هر خط = عنوان یک قالب.\n"
            "لازم نیست برای همه‌ی قالب‌ها بنویسی؛ هر چند خط بفرستی، فقط همون‌قدر قالب اول عوض می‌شن، بقیه دست‌نخورده می‌مونن.\n\n"
            "مثال:\n"
            "سیاه\n"
            "گلی\n"
            "فلان\n\n"
            f"📦 این بخش {len(keys)} قالب دارد.\n\n"
            "برای لغو: لغو"
        )
        await q.edit_message_text(msg)
        return

    if data.startswith("titles_all_view:"):
        if not has_permission(uid, "list"):
            await q.edit_message_text("⛔ اجازه نداری!")
            return
        g = data[len("titles_all_view:"):]
        keys = [k for k in TEMPLATE_GROUPS.get(g, []) if k in ALL_TEXTS]
        if not keys:
            await q.edit_message_text("📭 این بخش قالبی ندارد.")
            return
        entries = []
        for k in keys:
            title = str(ALL_TEXTS[k].get("title", "") or "").strip()
            entries.append(f"📝 {k}:\n{title if title else '— (بدون عنوان) —'}")

        header = f"👁️ عنوان‌های تنظیم‌شده — {section_title(g)}\n"
        buttons = [
            [InlineKeyboardButton("✏️ ویرایش عنوان‌ها", callback_data=f"titles_all_edit:{g}")],
            [InlineKeyboardButton("🗑️ حذف عنوان یک قالب", callback_data=f"titles_all_delone:{g}")],
            [InlineKeyboardButton("🗑️🗑️ حذف همه عنوان‌ها", callback_data=f"titles_all_delall_confirm:{g}")],
            [InlineKeyboardButton("🔙 بازگشت", callback_data=f"section:{g}")],
        ]

        # Telegram caps messages at 4096 chars; split into multiple messages
        # if the section has enough templates to exceed that.
        chunks, current = [], header
        for entry in entries:
            candidate = current + "\n\n" + entry if current else entry
            if len(candidate) > 3500:
                chunks.append(current)
                current = entry
            else:
                current = candidate
        if current:
            chunks.append(current)

        if len(chunks) == 1:
            await q.edit_message_text(chunks[0], reply_markup=InlineKeyboardMarkup(buttons))
        else:
            await q.edit_message_text(chunks[0])
            for extra in chunks[1:-1]:
                await q.message.reply_text(extra)
            await q.message.reply_text(chunks[-1], reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data.startswith("titles_all_edit:"):
        if not has_permission(uid, "list"):
            await q.edit_message_text("⛔ اجازه نداری!")
            return
        g = data[len("titles_all_edit:"):]
        keys = [k for k in TEMPLATE_GROUPS.get(g, []) if k in ALL_TEXTS]
        if not keys:
            await q.edit_message_text("📭 این بخش قالبی ندارد.")
            return
        context.user_data.clear()
        context.user_data["state"] = "titles_all_edit"
        context.user_data["titles_edit_group"] = g
        context.user_data["titles_edit_keys"] = keys

        listing = []
        for i, k in enumerate(keys, start=1):
            title = str(ALL_TEXTS[k].get("title", "") or "").strip()
            listing.append(f"{i}) {k}: {title if title else '— (بدون عنوان) —'}")

        header = (
            f"✏️ ویرایش عنوان‌ها — {section_title(g)}\n\n"
            "لیست فعلی به ترتیب پایین اومده. حالا همین تعداد خط بفرست (به همون ترتیب):\n"
            "• هر خط = عنوان جدید همون شماره.\n"
            "• هر خط رو خالی بفرستی (فقط Enter رد کنی)، همون قالب دست‌نخورده می‌مونه — فرقی نداره خط چندم باشه.\n\n"
        )
        chunks, current = [], header
        for line in listing:
            candidate = current + line + "\n" if current else line + "\n"
            if len(candidate) > 3500:
                chunks.append(current)
                current = line + "\n"
            else:
                current = candidate
        if current:
            chunks.append(current)

        await q.edit_message_text(chunks[0])
        for extra in chunks[1:]:
            await q.message.reply_text(extra)
        await q.message.reply_text("برای لغو: لغو")
        return

    if data.startswith("titles_all_delone:"):
        if not has_permission(uid, "list"):
            await q.edit_message_text("⛔ اجازه نداری!")
            return
        g = data[len("titles_all_delone:"):]
        keys = [k for k in TEMPLATE_GROUPS.get(g, []) if k in ALL_TEXTS]
        if not keys:
            await q.edit_message_text("📭 این بخش قالبی ندارد.")
            return
        buttons = []
        for k in keys:
            title = str(ALL_TEXTS[k].get("title", "") or "").strip()
            label = f"{k} ({title[:15]})" if title else f"{k} (بدون عنوان)"
            buttons.append([InlineKeyboardButton(label, callback_data=f"titles_all_delone_do:{g}:{k}")])
        buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data=f"titles_all_view:{g}")])
        await q.edit_message_text(
            f"🗑️ حذف عنوان یک قالب — {section_title(g)}\n\nقالب موردنظر را انتخاب کن:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    if data.startswith("titles_all_delone_do:"):
        if not has_permission(uid, "list"):
            await q.edit_message_text("⛔ اجازه نداری!")
            return
        parts = data.split(":", 2)
        if len(parts) != 3:
            await q.edit_message_text("❌ خطا در انتخاب قالب.")
            return
        _, g, k = parts
        item = ALL_TEXTS.get(k)
        if not item:
            await q.edit_message_text("❌ قالب پیدا نشد.")
            return
        item["title"] = ""
        item["header"] = ""
        item["random_headers"] = []
        save_texts()
        buttons = [[InlineKeyboardButton("🔙 بازگشت به لیست", callback_data=f"titles_all_view:{g}")]]
        await q.edit_message_text(
            f"✅ عنوان قالب «{k}» پاک شد.",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    if data.startswith("titles_all_delall_confirm:"):
        if not has_permission(uid, "list"):
            await q.edit_message_text("⛔ اجازه نداری!")
            return
        g = data[len("titles_all_delall_confirm:"):]
        keys = [k for k in TEMPLATE_GROUPS.get(g, []) if k in ALL_TEXTS]
        if not keys:
            await q.edit_message_text("📭 این بخش قالبی ندارد.")
            return
        await q.edit_message_text(
            f"⚠️ مطمئنی می‌خوای عنوان همه‌ی {len(keys)} قالب «{section_title(g)}» پاک بشه؟\n\n"
            "این کار قابل برگشت نیست.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🗑️🗑️ بله، همه رو پاک کن", callback_data=f"titles_all_delall_yes:{g}")],
                [InlineKeyboardButton("❌ لغو", callback_data=f"titles_all_view:{g}")]
            ])
        )
        return

    if data.startswith("titles_all_delall_yes:"):
        if not has_permission(uid, "list"):
            await q.edit_message_text("⛔ اجازه نداری!")
            return
        g = data[len("titles_all_delall_yes:"):]
        keys = [k for k in TEMPLATE_GROUPS.get(g, []) if k in ALL_TEXTS]
        if not keys:
            await q.edit_message_text("📭 این بخش قالبی ندارد.")
            return
        for k in keys:
            item = ALL_TEXTS.get(k)
            if not item:
                continue
            item["title"] = ""
            item["header"] = ""
            item["random_headers"] = []
        save_texts()
        buttons = [[InlineKeyboardButton("🔙 بازگشت به لیست", callback_data=f"titles_all_view:{g}")]]
        await q.edit_message_text(
            f"✅ عنوان همه‌ی {len(keys)} قالب «{section_title(g)}» پاک شد.",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    if data.startswith("quicktoggle:"):
        if not has_permission(uid, "list"):
            await q.edit_message_text("⛔ اجازه نداری!")
            return
        g=data[len("quicktoggle:"):]
        new_state=not section_enabled(g)
        set_section_enabled(g,new_state)
        await show_section(q,g)
        return

    if data.startswith("section_bq_on_quick:"):
        if not has_permission(uid, "list"):
            await q.edit_message_text("⛔ اجازه نداری!")
            return
        g = data[len("section_bq_on_quick:"):]
        keys = [k for k in TEMPLATE_GROUPS.get(g, []) if k in ALL_TEXTS]
        quote_text = get_section_last_quote(g)
        if not keys or not quote_text:
            await q.answer("کلمه‌ای ذخیره نشده، اول یک بار با «کلمه جدید» انتخابش کن.", show_alert=True)
            return
        changed, skipped = 0, 0
        for k in keys:
            body = str(ALL_TEXTS[k].get("link_text", "") or "")
            if quote_text in body:
                ALL_TEXTS[k]["quote_text"] = quote_text
                ALL_TEXTS[k]["blockquote"] = True
                changed += 1
            else:
                skipped += 1
        save_texts()
        try:
            await q.answer(f"نقل‌قول «{quote_text}» برای {changed} قالب روشن شد!")
        except Exception:
            pass
        await show_section(q, g)
        return

    if data.startswith("section_bq_on:"):
        if not has_permission(uid, "list"):
            await q.edit_message_text("⛔ اجازه نداری!")
            return
        g = data[len("section_bq_on:"):]
        keys = [k for k in TEMPLATE_GROUPS.get(g, []) if k in ALL_TEXTS]
        if not keys:
            await q.edit_message_text("📭 این بخش قالبی ندارد.")
            return
        context.user_data.clear()
        context.user_data["state"] = "waiting_section_quote_text"
        context.user_data["quote_section_group"] = g
        await q.edit_message_text(
            f"💬 نقل‌قول برای همه — {section_title(g)}\n\n"
            "کدام کلمه/متن نقل‌قول بشه؟ عین همون کلمه یا عبارت را بفرست.\n"
            "این کلمه در هر قالبی از این بخش که وجودش را داشته باشد، نقل‌قول می‌شود؛ قالب‌هایی که این کلمه را ندارند دست‌نخورده می‌مانند.\n"
            "این کلمه ذخیره می‌شه تا دفعه‌های بعد دیگه لازم نباشه دوباره بپرسم.\n\n"
            f"📦 این بخش {len(keys)} قالب دارد.\n\n"
            "برای لغو: لغو"
        )
        return

    if data.startswith("section_bq_off:"):
        if not has_permission(uid, "list"):
            await q.edit_message_text("⛔ اجازه نداری!")
            return
        g = data[len("section_bq_off:"):]
        keys = [k for k in TEMPLATE_GROUPS.get(g, []) if k in ALL_TEXTS]
        for k in keys:
            ALL_TEXTS[k]["blockquote"] = False
        save_texts()
        try:
            await q.answer(f"نقل‌قول برای {len(keys)} قالب خاموش شد!")
        except Exception:
            pass
        await show_section(q, g)
        return

    if data.startswith("section_del:"):
        if not has_permission(uid, "list"):
            await q.edit_message_text("⛔ اجازه نداری!")
            return
        g=data[len("section_del:"):]
        keys=[k for k in TEMPLATE_GROUPS.get(g,[]) if k in ALL_TEXTS]
        context.user_data.clear(); context.user_data["state"]="section_delete_confirm"; context.user_data["delete_group"]=g
        msg = (
            f"🗑 حذف بخش «{section_title(g)}»؟\n\n"
            "⚠️ این کار خود قالب‌های داخل این بخش را هم برای همیشه حذف می‌کند.\n\n"
            f"تعداد قالب: {len(keys)}"
        )
        await q.edit_message_text(msg, reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ بله، حذف بخش", callback_data="section_del_yes")],
            [InlineKeyboardButton("❌ لغو", callback_data=f"section:{g}")]
        ]))
        return

    if data == "section_del_yes":
        if not has_permission(uid, "list"):
            await q.edit_message_text("⛔ اجازه نداری!")
            return
        g=context.user_data.get("delete_group")
        keys=[k for k in TEMPLATE_GROUPS.get(g,[]) if k in ALL_TEXTS]
        for k in keys:
            ALL_TEXTS.pop(k, None)
        if g in TEMPLATE_GROUPS:
            del TEMPLATE_GROUPS[g]
            save_template_groups()
        SECTION_SETTINGS.pop(g,None); save_section_settings()
        save_texts()
        if ACTIVE_KEY not in ALL_TEXTS:
            ACTIVE_KEY = next(iter(ALL_TEXTS), "")
        context.user_data.clear()
        await q.edit_message_text(f"✅ بخش و {len(keys)} قالب داخل آن برای همیشه حذف شدند.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 لیست بخش‌ها", callback_data="back_list")]]))
        return

    if data.startswith("section_delall_templates:"):
        if not has_permission(uid, "list"):
            await q.edit_message_text("⛔ اجازه نداری!")
            return
        g = data[len("section_delall_templates:"):]
        if g not in TEMPLATE_GROUPS:
            await q.edit_message_text("❌ این پوشه دیگر وجود ندارد.")
            return
        keys = [k for k in TEMPLATE_GROUPS.get(g, []) if k in ALL_TEXTS]
        if not keys:
            await q.edit_message_text("📭 این بخش قالبی ندارد.")
            return
        context.user_data.clear()
        context.user_data["state"] = "section_delall_templates_confirm"
        context.user_data["delete_group"] = g
        msg = (
            f"🗑 حذف همه‌ی قالب‌های «{section_title(g)}»؟\n\n"
            "⚠️ خود قالب‌ها برای همیشه حذف می‌شن (بانک کلمات و تنظیمات هرکدوم هم از بین می‌ره).\n"
            "خود بخش/پوشه باقی می‌مونه، فقط خالی می‌شه.\n\n"
            f"تعداد قالب: {len(keys)}"
        )
        await q.edit_message_text(msg, reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ بله، همه قالب‌ها را حذف کن", callback_data="section_delall_templates_yes")],
            [InlineKeyboardButton("❌ لغو", callback_data=f"section:{g}")]
        ]))
        return

    if data == "section_delall_templates_yes":
        if not has_permission(uid, "list"):
            await q.edit_message_text("⛔ اجازه نداری!")
            return
        g = context.user_data.get("delete_group")
        if not g or g not in TEMPLATE_GROUPS:
            context.user_data.clear()
            await q.edit_message_text("❌ این پوشه دیگر وجود ندارد.")
            return
        keys = [k for k in TEMPLATE_GROUPS.get(g, []) if k in ALL_TEXTS]
        for k in keys:
            ALL_TEXTS.pop(k, None)
        TEMPLATE_GROUPS[g] = []
        save_template_groups()
        save_texts()
        TITLE_BANKS.pop(g, None)
        save_title_banks()
        cursor_state = TOPIC_VARIANTS.setdefault("__quick_cursor__", {})
        cursor_state.pop(g, None)
        save_json(TOPIC_VARIANTS_FILE, TOPIC_VARIANTS)
        if ACTIVE_KEY not in ALL_TEXTS:
            ACTIVE_KEY = next(iter(ALL_TEXTS), "")
        context.user_data.clear()
        await q.edit_message_text(
            f"✅ {len(keys)} قالب داخل «{section_title(g)}» برای همیشه حذف شدند.\n📁 خود بخش خالی باقی موند.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📁 بازگشت به بخش", callback_data=f"section:{g}")]])
        )
        return

    if data.startswith("section_add_text:"):
        if not has_permission(uid, "manage_templates"):
            await q.edit_message_text("⛔ اجازه ساخت قالب نداری!")
            return
        g = data[len("section_add_text:"):]
        if g not in TEMPLATE_GROUPS:
            await q.edit_message_text("❌ این بخش پیدا نشد!")
            return
        context.user_data.clear()
        context.user_data["state"] = "section_tmpl_wait_link_text"
        context.user_data["section_target"] = g
        await q.edit_message_text(
            f"➕ افزودن متن به بخش «{section_title(g)}»\n\n"
            "📝 متن قالب را بفرست.\n\n"
            "مثال:\n"
            "Download pack مشـ.ـاهده ✅\n\n"
            "بعد مشخص می‌کنیم کدام کلمه لینک شود.\n\n"
            "برای لغو: لغو"
        )
        return

    if data.startswith("section_add_template:"):
        if not has_permission(uid, "list"):
            await q.edit_message_text("⛔ اجازه نداری!")
            return
        g=data[len("section_add_template:"):]
        ungrouped=[k for k in ALL_TEXTS if not any(k in vals for vals in TEMPLATE_GROUPS.values())]
        if not ungrouped:
            await q.edit_message_text("📭 قالب بدون بخش نداریم.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data=f"section:{g}")]]))
            return
        buttons=[[InlineKeyboardButton(k, callback_data=f"move:{k}:{g}")] for k in sorted(ungrouped)]
        buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data=f"section:{g}")])
        await q.edit_message_text("📦 قالب‌هایی که هنوز بخش ندارند را انتخاب کن:", reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data.startswith("move:"):
        if not has_permission(uid, "list"):
            await q.edit_message_text("⛔ اجازه نداری!")
            return
        _,k,g=data.split(":",2)
        for vals in TEMPLATE_GROUPS.values():
            if k in vals: vals.remove(k)
        TEMPLATE_GROUPS.setdefault(g,[]).append(k); save_template_groups()
        await show_section(q,g)
        return
    if not can_use_bot(uid):
        await q.edit_message_text("⛔ دسترسی نداری!")
        return


    if data == "tmpl_close":
        await q.edit_message_text("🧩 مدیریت قالب بسته شد.", reply_markup=None)
        return

    if data == "tmpl_groups":
        if not has_permission(uid, "manage_templates"):
            await q.edit_message_text("⛔ اجازه نداری!")
            return
        await template_groups_callback(q)
        return

    if data == "tmpl_add":
        if not has_permission(uid, "manage_templates"):
            await q.edit_message_text("⛔ اجازه نداری!")
            return
        context.user_data.clear()
        context.user_data["state"] = "tmpl_wait_category"
        await q.edit_message_text(
            "🧩 ساخت قالب جدید\n\n"
            "نوع اصلی را بفرست:\n"
            "مثال: وطنی\n"
            "یا: خارجی\n\n"
            "برای لغو: لغو"
        )
        return

    if data.startswith("adm_edit:"):
        if not is_owner(uid):
            await q.edit_message_text("⛔ فقط مالک!")
            return
        target_id = data[9:]
        if target_id not in ADMINS:
            await q.edit_message_text("❌ این ادمین پیدا نشد!")
            return
        await rebuild_admin_perms_inline(q, target_id)
        return

    if data.startswith("use:"):
        if not has_permission(uid, "list"):
            await q.edit_message_text("⛔ اجازه نداری!")
            return
        k = data[4:]
        if k in ALL_TEXTS:
            ACTIVE_KEY = k
            await use_template_view(q, k)
    elif data.startswith("edit:"):
        if not has_permission(uid, "list"):
            await q.edit_message_text("⛔ اجازه نداری!")
            return
        k = data[5:]
        if k not in ALL_TEXTS:
            await q.edit_message_text("❌ قالب پیدا نشد!")
            return
        context.user_data.clear()
        context.user_data["state"] = "edit_template_text"
        context.user_data["edit_template_key"] = k
        current = ALL_TEXTS[k].get("link_text", "")
        await q.edit_message_text(
            f"✏️ ویرایش قالب «{k}»\n\n"
            "متن فعلی را کامل می‌بینی. هرجایش را خواستی عوض کن و نسخه جدید را بفرست.\n\n"
            f"{current}\n\n"
            "اگر همان کلمه لینک‌شده قبلی داخل متن بماند، خودکار حفظ می‌شود.\n"
            "برای لغو: لغو"
        )
    elif data.startswith("copy:"):
        if not has_permission(uid, "list"):
            await q.edit_message_text("⛔ اجازه نداری!")
            return
        k = data[5:]
        if k not in ALL_TEXTS:
            await q.edit_message_text("❌ قالب پیدا نشد!")
            return
        context.user_data.clear()
        context.user_data["state"] = "copy_template_count"
        context.user_data["copy_template_key"] = k
        await q.edit_message_text(f"📄 کپی از «{k}»\n\n🔢 چند کپی می‌خواهی؟\nمثلاً: 50\n\nبرای لغو: لغو")
    elif data.startswith("preview:"):
        if not has_permission(uid, "list"):
            await q.edit_message_text("⛔ اجازه نداری!")
            return
        k = data[8:]
        if k not in ALL_TEXTS:
            await q.edit_message_text("❌ قالب پیدا نشد!")
            return
        try:
            _, _, _, result = build_post_result(
                "https://t.me/example", k, topic_name=None
            )
            await q.edit_message_text(
                "👀 پیش‌نمایش قالب «%s»:\n\n%s" % (k, result),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data=f"use:{k}")]])
            )
        except Exception as e:
            await q.edit_message_text(f"❌ خطا در پیش‌نمایش: {e}")
    elif data.startswith("bq_on_quick:"):
        if not has_permission(uid, "list"):
            await q.edit_message_text("⛔ اجازه نداری!")
            return
        k = data[len("bq_on_quick:"):]
        if k not in ALL_TEXTS:
            await q.edit_message_text("❌ قالب پیدا نشد!")
            return
        qt = str(ALL_TEXTS[k].get("quote_text", "") or "")
        body = str(ALL_TEXTS[k].get("link_text", "") or "")
        if not qt or qt not in body:
            await q.answer("این متن دیگه داخل قالب نیست، یک متن جدید انتخاب کن.", show_alert=True)
            return
        ALL_TEXTS[k]["blockquote"] = True
        save_texts()
        try:
            await q.answer("نقل‌قول روشن شد!")
        except Exception:
            pass
        await use_template_view(q, k)
        return

    elif data.startswith("bq_ask:"):
        if not has_permission(uid, "list"):
            await q.edit_message_text("⛔ اجازه نداری!")
            return
        k = data[len("bq_ask:"):]
        if k not in ALL_TEXTS:
            await q.edit_message_text("❌ قالب پیدا نشد!")
            return
        context.user_data.clear()
        context.user_data["state"] = "waiting_quote_text"
        context.user_data["quote_template_key"] = k
        current = ALL_TEXTS[k].get("link_text", "")
        await q.edit_message_text(
            f"💬 نقل‌قول برای «{k}»\n\n"
            "کدام متن نقل‌قول بشه؟ عین همون تکه از متن را کپی و بفرست.\n\n"
            f"متن فعلی قالب:\n{current}\n\n"
            "برای لغو: لغو"
        )
        return

    elif data.startswith("bq_off:"):
        if not has_permission(uid, "list"):
            await q.edit_message_text("⛔ اجازه نداری!")
            return
        k = data[len("bq_off:"):]
        if k in ALL_TEXTS:
            ALL_TEXTS[k]["blockquote"] = False
            save_texts()
            try:
                await q.answer("نقل‌قول خاموش شد!")
            except Exception:
                pass
            await use_template_view(q, k)
        return

    elif data.startswith("bqall:"):
        if not has_permission(uid, "list"):
            await q.edit_message_text("⛔ اجازه نداری!")
            return
        k = data[len("bqall:"):]
        if k not in ALL_TEXTS:
            await q.edit_message_text("❌ قالب پیدا نشد!")
            return
        quote_text = str(ALL_TEXTS[k].get("quote_text", "") or "")
        if not ALL_TEXTS[k].get("blockquote") or not quote_text:
            await q.answer("اول برای همین قالب نقل‌قول را روشن کن.", show_alert=True)
            return
        g = find_group_for_key(k)
        if not g:
            await q.answer("این قالب داخل هیچ بخشی نیست.", show_alert=True)
            return
        keys = [x for x in TEMPLATE_GROUPS.get(g, []) if x in ALL_TEXTS]
        changed, skipped = 0, 0
        for x in keys:
            body = str(ALL_TEXTS[x].get("link_text", "") or "")
            if quote_text in body:
                ALL_TEXTS[x]["quote_text"] = quote_text
                ALL_TEXTS[x]["blockquote"] = True
                changed += 1
            else:
                skipped += 1
        save_texts()
        try:
            await q.answer(f"برای {changed} قالب این بخش روشن شد.")
        except Exception:
            pass
        await use_template_view(q, k, extra_note=f"🌐 نقل‌قول برای {changed} قالب بخش «{section_title(g)}» روشن شد"
                                                  + (f" ({skipped} قالب چون این متن را نداشتند، دست‌نخورده ماندند)." if skipped else "."))
        return
    elif data.startswith("ready:"):
        if not has_permission(uid, "ready"):
            await q.edit_message_text("⛔ اجازه نداری!")
            return
        k = data[6:]
        if k in ALL_TEXTS:
            ACTIVE_KEY = k
            context.user_data["ready_mode"] = True
            context.user_data.pop("pending_url", None)
            context.user_data.pop("selected_topic", None)
            context.user_data.pop("post_mode", None)

            t = ALL_TEXTS[k]
            lw = t.get("linked_word", "")
            footer = t.get("footer", "")
            rh_count = len(t.get("random_headers", []))
            bq = t.get("blockquote", False)

            msg = (
                f"🎯 قالب {k} آماده‌ست!\n\n"
                f"🔗 {t.get('link_text', '')}\n"
            )
            if lw:
                msg += f"🔵 لینک‌شده: {lw}\n"
            if footer:
                msg += f"📌 فوتر: {footer}\n"
            msg += f"💬 نقل‌قول: {'✅ روشن' if bq else '❌ خاموش'}\n"
            if rh_count > 0:
                msg += f"🎲 {rh_count} متن رندوم\n"

            msg += "\n🎯 پست رو با موضوع می‌خوای یا بدون موضوع؟"
            buttons = [
                [InlineKeyboardButton("🎬 با موضوع", callback_data="postmode:topic")],
                [InlineKeyboardButton("🚫 بدون موضوع", callback_data="postmode:no_topic")]
            ]
            await q.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(buttons))

    elif data == "postmode:topic":
        if not has_permission(uid, "ready"):
            await q.edit_message_text("⛔ اجازه نداری!")
            return
        if not context.user_data.get("ready_mode"):
            await q.edit_message_text("❌ اول از 🎯 آماده‌سازی یک قالب انتخاب کن.")
            return

        context.user_data["post_mode"] = "topic"
        context.user_data.pop("selected_topic", None)
        topic_names = sorted(TOPICS.keys())

        if not topic_names:
            await q.edit_message_text(
                "📭 هنوز هیچ موضوعی ساخته نشده.\n\nاول یک موضوع بساز، بعد لینک رو بده.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("➕ افزودن موضوع", callback_data="topic_add")]
                ])
            )
            return

        buttons = [
            [InlineKeyboardButton(name, callback_data=f"topicselect:{idx}")]
            for idx, name in enumerate(topic_names)
        ]
        buttons.append([
            InlineKeyboardButton("➕ افزودن موضوع", callback_data="topic_add"),
            InlineKeyboardButton("🗑 حذف موضوع", callback_data="topic_del_menu")
        ])
        await q.edit_message_text(
            "🎬 موضوع رو انتخاب کن:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

    elif data == "postmode:no_topic":
        if not has_permission(uid, "ready"):
            await q.edit_message_text("⛔ اجازه نداری!")
            return
        if not context.user_data.get("ready_mode"):
            await q.edit_message_text("❌ اول از 🎯 آماده‌سازی یک قالب انتخاب کن.")
            return

        context.user_data["post_mode"] = "no_topic"
        context.user_data.pop("selected_topic", None)
        await q.edit_message_text("🚫 بدون موضوع انتخاب شد.")
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="⬇️ حالا لینک رو بده یا بنویس تمام:",
            reply_markup=MULTI_POST_KEYBOARD
        )

    elif data.startswith("topicselect:"):
        if not has_permission(uid, "ready"):
            await q.edit_message_text("⛔ اجازه نداری!")
            return
        if not context.user_data.get("ready_mode"):
            await q.edit_message_text("❌ اول از 🎯 آماده‌سازی یک قالب انتخاب کن.")
            return

        try:
            idx = int(data.split(":", 1)[1])
        except (TypeError, ValueError):
            await q.edit_message_text("❌ موضوع نامعتبره.")
            return

        topic_names = sorted(TOPICS.keys())
        if idx < 0 or idx >= len(topic_names):
            await q.edit_message_text("❌ این موضوع پیدا نشد.")
            return

        topic_name = topic_names[idx]
        context.user_data["post_mode"] = "topic"
        context.user_data["selected_topic"] = topic_name

        await q.edit_message_text(f"🎬 موضوع «{topic_name}» انتخاب شد.")
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="⬇️ حالا لینک رو بده یا بنویس تمام:",
            reply_markup=MULTI_POST_KEYBOARD
        )

    elif data.startswith("ts:"):
        # Backward-compatible handler for old topic buttons.
        if not has_permission(uid, "ready"):
            await q.edit_message_text("⛔ اجازه نداری!")
            return
        try:
            idx = int(data[3:])
        except (TypeError, ValueError):
            await q.edit_message_text("❌ موضوع نامعتبره.")
            return

        topic_names = sorted(TOPICS.keys())
        if idx < 0 or idx >= len(topic_names):
            await q.edit_message_text("❌ موضوع پیدا نشد.")
            return

        topic_name = topic_names[idx]
        context.user_data["post_mode"] = "topic"
        context.user_data["selected_topic"] = topic_name
        await q.edit_message_text(f"🎬 موضوع «{topic_name}» انتخاب شد.")
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="⬇️ حالا لینک رو بده یا بنویس تمام:",
            reply_markup=MULTI_POST_KEYBOARD
        )

    elif data.startswith("del_confirm:"):
        if not has_permission(uid, "list"):
            await q.edit_message_text("⛔ اجازه نداری!")
            return
        k = data[len("del_confirm:"):]
        if k not in ALL_TEXTS:
            await q.edit_message_text("❌ قالب پیدا نشد!")
            return
        await q.edit_message_text(
            f"⚠️ مطمئنی می‌خواهی قالب «{k}» را حذف کنی؟\n\n"
            "این کار خود قالب را حذف می‌کند و از بخش و صف پست سریع هم خارجش می‌کند.\n"
            "این عملیات قابل برگشت نیست.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🗑️ بله، حذفش کن", callback_data=f"del_yes:{k}")],
                [InlineKeyboardButton("❌ لغو", callback_data=f"use:{k}")]
            ])
        )
    elif data.startswith("del_yes:"):
        if not has_permission(uid, "list"):
            await q.edit_message_text("⛔ اجازه نداری!")
            return
        k = data[len("del_yes:"):]
        if k not in ALL_TEXTS:
            await q.edit_message_text("❌ قالب پیدا نشد یا قبلاً حذف شده است.")
            return
        del ALL_TEXTS[k]
        for g in list(TEMPLATE_GROUPS.keys()):
            TEMPLATE_GROUPS[g] = [x for x in TEMPLATE_GROUPS[g] if x != k]
            if not TEMPLATE_GROUPS[g]:
                TEMPLATE_GROUPS.pop(g, None)
                SECTION_SETTINGS.pop(g, None)
        save_texts()
        save_template_groups()
        save_section_settings()
        if ACTIVE_KEY == k:
            ACTIVE_KEY = next(iter(ALL_TEXTS), "")
        await q.edit_message_text(
            f"🗑️ قالب «{k}» با موفقیت حذف شد.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 لیست بخش‌ها", callback_data="back_list")]])
        )
    elif data.startswith("del:"):
        if not has_permission(uid, "list"):
            await q.edit_message_text("⛔ اجازه نداری!")
            return
        k = data[4:]
        if k in ALL_TEXTS:
            del ALL_TEXTS[k]
            for g in list(TEMPLATE_GROUPS.keys()):
                TEMPLATE_GROUPS[g] = [x for x in TEMPLATE_GROUPS[g] if x != k]
                if not TEMPLATE_GROUPS[g]:
                    TEMPLATE_GROUPS.pop(g, None)
                    SECTION_SETTINGS.pop(g, None)
            save_texts(); save_template_groups(); save_section_settings()
            if ACTIVE_KEY == k and ALL_TEXTS:
                ACTIVE_KEY = list(ALL_TEXTS.keys())[0]
            await q.edit_message_text(f"🗑 قالب «{k}» حذف شد!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 لیست بخش‌ها", callback_data="back_list")]]))
    elif data == "back_list":
        await list_cmd(update, context)
    elif data == "myposts_all":
        if not has_permission(uid, "my_posts"):
            await q.edit_message_text("⛔ اجازه نداری!")
            return
        text2, markup2 = _myposts_section_menu(uid, "__ALL__")
        await q.edit_message_text(text2, reply_markup=markup2)
    elif data == "myposts_other":
        if not has_permission(uid, "my_posts"):
            await q.edit_message_text("⛔ اجازه نداری!")
            return
        text2, markup2 = _myposts_section_menu(uid, "__OTHER__")
        await q.edit_message_text(text2, reply_markup=markup2)
    elif data.startswith("myposts_sec:"):
        if not has_permission(uid, "my_posts"):
            await q.edit_message_text("⛔ اجازه نداری!")
            return
        g = data[len("myposts_sec:"):]
        text2, markup2 = _myposts_section_menu(uid, g)
        await q.edit_message_text(text2, reply_markup=markup2)
    elif data.startswith("myposts_sec_open:"):
        if not has_permission(uid, "my_posts"):
            await q.edit_message_text("⛔ اجازه نداری!")
            return
        scope = data[len("myposts_sec_open:"):]
        text2, markup2 = _myposts_section_menu(uid, scope)
        await q.edit_message_text(text2, reply_markup=markup2)
    elif data == "myposts_back":
        if not has_permission(uid, "my_posts"):
            await q.edit_message_text("⛔ اجازه نداری!")
            return
        text2, markup2, empty = _myposts_main_menu(uid)
        if empty:
            await q.edit_message_text("📭 هنوز پستی نساختی!")
            return
        await q.edit_message_text(text2, reply_markup=markup2)
    elif data.startswith("myposts_show:") or data.startswith("myposts_showreset:"):
        if not has_permission(uid, "my_posts"):
            await q.edit_message_text("⛔ اجازه نداری!")
            return
        reset = data.startswith("myposts_showreset:")
        scope = data[len("myposts_showreset:"):] if reset else data[len("myposts_show:"):]
        posts = _posts_for_scope(uid, scope)
        if not posts:
            await q.edit_message_text("📭 پستی در این بخش نیست.")
            return
        if reset or _POST_SEND_CURSOR.get((uid, scope), 0) >= len(posts):
            _POST_SEND_CURSOR[(uid, scope)] = 0
        _POST_SEND_ACTIVE[(uid, scope)] = True
        await q.edit_message_text(
            f"{_scope_title(scope)}\n\n🚀 در حال ارسال...",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⏹ ایست", callback_data=f"myposts_stop:{scope}")]])
        )
        context.application.create_task(_run_show_posts(context.bot, update.effective_chat.id, uid, scope))
    elif data.startswith("myposts_stop:"):
        scope = data[len("myposts_stop:"):]
        _POST_SEND_ACTIVE[(uid, scope)] = False
    elif data.startswith("myposts_delpick:"):
        if not has_permission(uid, "my_posts"):
            await q.edit_message_text("⛔ اجازه نداری!")
            return
        scope = data[len("myposts_delpick:"):]
        posts = _posts_for_scope(uid, scope)
        if not posts:
            await q.edit_message_text("📭 پستی در این بخش نیست.")
            return
        shown = posts[:40]
        buttons = []
        for p in shown:
            label = (p.get("header") or p.get("link_text") or "پست").strip() or "پست"
            buttons.append([InlineKeyboardButton(f"🗑 {label[:28]}", callback_data=f"myposts_delone:{scope}:{p.get('id','')}")])
        note = f"\n\n(فقط {len(shown)} تای اول نشون داده شده)" if len(posts) > len(shown) else ""
        buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data=f"myposts_sec_open:{scope}")])
        await q.edit_message_text(f"{_scope_title(scope)}\n\nکدوم پست حذف بشه؟{note}", reply_markup=InlineKeyboardMarkup(buttons))
    elif data.startswith("myposts_delone:"):
        if not has_permission(uid, "my_posts"):
            await q.edit_message_text("⛔ اجازه نداری!")
            return
        rest = data[len("myposts_delone:"):]
        scope, _, post_id = rest.partition(":")
        removed = delete_post_by_id(uid, post_id)
        _POST_SEND_CURSOR.pop((uid, scope), None)
        msg = "✅ پست حذف شد." if removed else "❌ پست پیدا نشد (شاید قبلاً حذف شده)."
        text2, markup2 = _myposts_section_menu(uid, scope)
        await q.edit_message_text(msg + "\n\n" + text2, reply_markup=markup2)
    elif data.startswith("myposts_delall_yes:"):
        if not has_permission(uid, "my_posts"):
            await q.edit_message_text("⛔ اجازه نداری!")
            return
        scope = data[len("myposts_delall_yes:"):]
        removed = delete_posts_where(uid, _scope_predicate(scope))
        _POST_SEND_CURSOR.pop((uid, scope), None)
        _POST_SEND_ACTIVE.pop((uid, scope), None)
        text2, markup2 = _myposts_section_menu(uid, scope)
        await q.edit_message_text(f"✅ {removed} پست حذف شد.\n\n" + text2, reply_markup=markup2)
    elif data.startswith("myposts_delall:"):
        if not has_permission(uid, "my_posts"):
            await q.edit_message_text("⛔ اجازه نداری!")
            return
        scope = data[len("myposts_delall:"):]
        posts = _posts_for_scope(uid, scope)
        await q.edit_message_text(
            f"⚠️ حذف همه‌ی {len(posts)} پست «{_scope_title(scope)}»؟\nاین کار برگشت‌ناپذیره.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ بله، حذف کن", callback_data=f"myposts_delall_yes:{scope}")],
                [InlineKeyboardButton("❌ لغو", callback_data=f"myposts_sec_open:{scope}")]
            ])
        )
    elif data.startswith("rhadd:"):
        if not has_permission(uid, "list"):
            await q.edit_message_text("⛔ اجازه نداری!")
            return
        k = data[6:]
        context.user_data["rh_template"] = k
        context.user_data["state"] = "waiting_random_header"
        await q.edit_message_text(
            f"🎲 متن رندوم جدید برای {k}:\n\n"
            "متنی که می‌خوای بالای پست بیاد (تصادفی) رو بفرست:\n\n"
            "(برای لغو بنویس: لغو)"
        )
    elif data.startswith("rhlist:"):
        if not has_permission(uid, "list"):
            await q.edit_message_text("⛔ اجازه نداری!")
            return
        k = data[7:]
        rh = ALL_TEXTS.get(k, {}).get("random_headers", [])
        if not rh:
            await q.edit_message_text("📭 هیچ متن رندومی نیست!")
            return
        lines = [f"{i+1}. {h}" for i, h in enumerate(rh)]
        buttons = []
        for i in range(len(rh)):
            buttons.append([InlineKeyboardButton(f"🗑 حذف {i+1}", callback_data=f"rhdel:{k}:{i}")])
        buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data=f"use:{k}")])
        await q.edit_message_text(
            f"🎲 متن‌های رندوم {k}:\n\n" + "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    elif data.startswith("rhdel:"):
        if not has_permission(uid, "list"):
            await q.edit_message_text("⛔ اجازه نداری!")
            return
        parts = data.split(":")
        k = parts[1]
        idx = int(parts[2])
        rh = ALL_TEXTS.get(k, {}).get("random_headers", [])
        if 0 <= idx < len(rh):
            removed = rh.pop(idx)
            ALL_TEXTS[k]["random_headers"] = rh
            save_texts()
            await q.edit_message_text(f"🗑 حذف شد:\n{removed}")
        else:
            await q.answer("❌ خطا!")
    elif data.startswith("ts:"):
        if not has_permission(uid, "ready"):
            await q.edit_message_text("⛔ اجازه نداری!")
            return
        if _USER_LOCKS.get(uid):
            await q.answer("⏳ قبلاً پردازش شد!")
            return
        _USER_LOCKS[uid] = True
        try:
            await q.edit_message_text("⏳ در حال ساخت پست...", reply_markup=None)
            url = context.user_data.pop("pending_url", "")
            if not url:
                await q.edit_message_text("❌ خطا! لینک منقضی شده.")
                return
            idx = int(data[3:])
            topic_names = sorted(TOPICS.keys())
            if idx < 0 or idx >= len(topic_names):
                await q.edit_message_text("❌ خطا! موضوع پیدا نشد.")
                return
            topic_name = topic_names[idx]
            title_override = choose_post_title(ACTIVE_KEY)
            header, link_text, linked_word, result = build_post_result(
                url, ACTIVE_KEY, topic_name=topic_name, title_override=title_override
            )
            try:
                await q.edit_message_text(result, parse_mode="HTML", disable_web_page_preview=True)
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="✅ پست ساخته شد!\n\n📝 لینک بعدی رو بده یا بنویس تمام:",
                    reply_markup=MULTI_POST_KEYBOARD
                )
            except NetworkError:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="⚠️ پست ساخته شد ولی به خاطر مشکل اینترنت نتونستم بفرستم.",
                    reply_markup=MULTI_POST_KEYBOARD
                )
        except Exception as e:
            await q.edit_message_text(f"❌ خطا: {str(e)}")
        finally:
            _USER_LOCKS[uid] = False
    elif data == "topic_add":
        if not has_permission(uid, "manage_topics"):
            await q.edit_message_text("⛔ اجازه مدیریت موضوعات رو نداری!")
            return
        context.user_data["state"] = "waiting_topic_name"
        context.user_data.pop("topic_name", None)
        await q.edit_message_text(
            "➕ افزودن موضوع جدید:\n\n"
            "یه اسم برای موضوع بذار:\n"
            "(مثلاً: وطنی چهره دار، کاسپلی)\n\n"
            "برای لغو بنویس: لغو"
        )
        return
    elif data == "topic_del_menu":
        if not has_permission(uid, "manage_topics"):
            await q.edit_message_text("⛔ اجازه مدیریت موضوعات رو نداری!")
            return
        if not TOPICS:
            await q.edit_message_text(
                "📭 هیچ موضوعی نیست!",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("➕ افزودن موضوع", callback_data="topic_add")],
                    [InlineKeyboardButton("🔙 بازگشت", callback_data="topic_back")]
                ])
            )
            return
        topic_names = sorted(TOPICS.keys())
        buttons = []
        for i, name in enumerate(topic_names):
            buttons.append([InlineKeyboardButton(f"🗑 {name}", callback_data=f"td:{i}")])
        buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data="topic_back")])
        await q.edit_message_text("🗑 کدوم موضوع رو می‌خوای حذف کنی؟", reply_markup=InlineKeyboardMarkup(buttons))
    elif data.startswith("td:"):
        if not has_permission(uid, "manage_topics"):
            await q.edit_message_text("⛔ اجازه نداری!")
            return
        try:
            idx = int(data[3:])
        except (TypeError, ValueError):
            await q.answer("❌ خطا!")
            return
        topic_names = sorted(TOPICS.keys())
        if idx < 0 or idx >= len(topic_names):
            await q.answer("❌ این موضوع دیگر وجود ندارد!")
            return
        name = topic_names[idx]
        del TOPICS[name]
        save_texts()
        topic_names = sorted(TOPICS.keys())
        buttons = [[InlineKeyboardButton(topic, callback_data=f"ts:{i}")] for i, topic in enumerate(topic_names)]
        buttons.append([
            InlineKeyboardButton("➕ افزودن موضوع", callback_data="topic_add"),
            InlineKeyboardButton("🗑 حذف موضوع", callback_data="topic_del_menu")
        ])
        await q.edit_message_text(
            f"🗑 موضوع «{name}» حذف شد!\n\n🎬 حالا موضوع رو انتخاب کن:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    elif data == "topic_back":
        url = context.user_data.get("pending_url", "")
        topic_names = sorted(TOPICS.keys())
        buttons = []
        for idx, name in enumerate(topic_names):
            buttons.append([InlineKeyboardButton(f"{name}", callback_data=f"ts:{idx}")])
        buttons.append([
            InlineKeyboardButton("➕ افزودن موضوع", callback_data="topic_add"),
            InlineKeyboardButton("🗑 حذف موضوع", callback_data="topic_del_menu")
        ])
        await q.edit_message_text(
            f"🔗 لینک دریافت شد!\n\n"
            "🎬 حالا موضوع رو انتخاب کن:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    elif data.startswith("adm_perm:"):
        if not is_owner(uid):
            await q.edit_message_text("⛔ فقط مالک!")
            return
        parts = data.split(":")
        target_id = parts[1]
        perm = parts[2]
        if target_id not in ADMINS:
            await q.answer("❌ ادمین پیدا نشد!")
            return
        perms = set(ADMINS[target_id].get("permissions", []))
        if perm in perms:
            perms.discard(perm)
        else:
            perms.add(perm)
        ADMINS[target_id]["permissions"] = sorted(list(perms))
        save_admins()
        await q.answer("✅ تغییر کرد!")
        await rebuild_admin_perms_inline(q, target_id)
    elif data.startswith("adm_full:"):
        if not is_owner(uid):
            await q.edit_message_text("⛔ فقط مالک!")
            return
        target_id = data.split(":")[1]
        if target_id not in ADMINS:
            await q.answer("❌ ادمین پیدا نشد!")
            return
        ADMINS[target_id]["full_access"] = not ADMINS[target_id].get("full_access", False)
        save_admins()
        await q.answer("✅ تغییر کرد!")
        await rebuild_admin_perms_inline(q, target_id)
    elif data == "admin_back":
        if not is_owner(uid):
            return
        await q.edit_message_text("👮‍♂️ بازگشت به پنل ادمین.")

async def rebuild_admin_perms_inline(q, target_id):
    admin = ADMINS.get(target_id, {})
    fa = admin.get("full_access", False)
    perms = admin.get("permissions", [])
    msg = f"⚙️ مدیریت دسترسی ادمین: {target_id}\n\n"
    msg += f"🌟 دسترسی کامل: {'✅ روشن' if fa else '❌ خاموش'}\n\n"
    msg += "🔹 دسترسی‌های تکی:"
    buttons = []
    if not fa:
        row = []
        for p, label in ALL_PERMISSIONS:
            mark = "✅" if p in perms else "❌"
            row.append(InlineKeyboardButton(f"{mark} {label}", callback_data=f"adm_perm:{target_id}:{p}"))
            if len(row) == 1:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
    buttons.append([InlineKeyboardButton(f"{'❌ خاموش' if fa else '✅ روشن'} دسترسی کامل", callback_data=f"adm_full:{target_id}")])
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data="admin_back")])
    await q.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(buttons))

# ═══════════════════════════════════════════════════
# 🤖 دستیار فرمان داخلی (بدون AI / بدون اینترنت)
# ═══════════════════════════════════════════════════
_PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")
_ASSISTANT_NUM_WORDS = {"صفر":0,"یک":1,"یکی":1,"دو":2,"سه":3,"چهار":4,"پنج":5,"شش":6,"هفت":7,"هشت":8,"نه":9,"ده":10,"یازده":11,"دوازده":12,"سیزده":13,"چهارده":14,"پانزده":15,"شانزده":16,"هفده":17,"هجده":18,"نوزده":19,"بیست":20,"سی":30,"چهل":40,"پنجاه":50,"صد":100}

def _assistant_norm(value):
    value=str(value or "").translate(_PERSIAN_DIGITS).replace("ي","ی").replace("ى","ی").replace("ك","ک").replace("ۀ","ه").replace("ة","ه")
    value=re.sub(r"[\u200c\u200d]", " ", value)
    return re.sub(r"\s+", " ", value).strip()

def _assistant_number(value):
    value=_assistant_norm(value)
    if value.isdigit(): return int(value)
    if value in _ASSISTANT_NUM_WORDS: return _ASSISTANT_NUM_WORDS[value]
    parts=[x for x in value.split() if x!="و"]
    if parts and all(x in _ASSISTANT_NUM_WORDS for x in parts):
        total=0
        for x in parts:
            n=_ASSISTANT_NUM_WORDS[x]
            total=max(100,total*100) if n==100 else total+n
        return total
    return None

def _assistant_clean_name(value):
    value=str(value or "").strip(" \t\n،,:؛.!؟")
    value=re.sub(r"^(?:به نام|با نام|اسم|نام|به اسم)\s+", "", value, flags=re.I)
    return value.strip(" \t\n،,:؛.!؟")

def _assistant_section_key(name):
    name=_assistant_clean_name(name)
    return name if "|" in name else group_key(name, "")

def _assistant_template_names(section_name,count,raw_names=""):
    count=max(1,min(int(count),500)); raw_names=_assistant_norm(raw_names)
    if raw_names:
        m=re.search(r"(.+?)\s*(\d+)\s*تا\s*(\d+)\s*$",raw_names)
        if m and int(m.group(3))>=int(m.group(2)) and int(m.group(3))-int(m.group(2))+1==count:
            prefix=m.group(1).strip(" _-"); a,b=int(m.group(2)),int(m.group(3)); w=max(len(m.group(2)),len(m.group(3)))
            return [f"{prefix}{i:0{w}d}" for i in range(a,b+1)]
        explicit=[x.strip() for x in re.split(r"[,،;؛]",raw_names) if x.strip()]
        if len(explicit)==count: return explicit
        if len(explicit)==1: raw_names=explicit[0]
    base=_assistant_clean_name(raw_names) if raw_names else _assistant_clean_name(section_name)
    base=base or "template"
    return [f"{base}_{i:02d}" for i in range(1,count+1)]

def _assistant_bank_items(payload):
    payload=re.sub(r"^(?:متن(?:های)?|آیتم(?:ها)?|عنوان(?:ها)?)\s*[:：]\s*", "", payload.strip())
    return [x.strip(" \t\n،,؛;") for x in re.split(r"\n+|\s*\|\s*|\s*،\s*|\s*؛\s*",payload) if x.strip()]

def _assistant_parse(text):
    n=_assistant_norm(text); low=n.lower()
    if not n or not any(w in low for w in ("بساز","درست کن","ایجاد کن","اضافه کن","فعال کن","روشن کن","خاموش کن","غیرفعال کن","ریست","بازنشانی","داخل بانک","بانک")): return None
    actions=[]
    sec=re.search(r"(?:یک\s+)?(?:بخش|پوشه)\s+(?:به\s+)?(?:اسم|نام|به\s+نام)?\s*[:：]?\s*[«\"']?([^«»\"'،,؛;]+?)[»\"']?\s*(?=بساز|درست کن|ایجاد کن|،|$)",n,re.I)
    if sec:
        name=_assistant_clean_name(sec.group(1))
        if name: actions.append({"type":"create_section","name":name})
    tm=re.search(r"(?:([0-9]+|[آ-ی]+)\s*)?(?:تا\s*)?(?:قالب|قالبها|قالب ها)\s+(?:برای|داخل|تو|در)\s+[«\"']?([^«»\"'،,؛;]+?)[»\"']?(?=\s*(?:بساز|درست کن|ایجاد کن|با|،|$))",n,re.I)
    if not tm: tm=re.search(r"(?:برای|داخل|تو|در)\s+[«\"']?([^«»\"'،,؛;]+?)[»\"']?\s+(?:([0-9]+|[آ-ی]+)\s*)?(?:تا\s*)?(?:قالب|قالبها|قالب ها)",n,re.I)
    if tm:
        if _assistant_number(tm.group(1)) is not None: count=_assistant_number(tm.group(1)); section=tm.group(2)
        else: section=tm.group(1); count=_assistant_number(tm.group(2))
        count=count or 1; section=_assistant_clean_name(section); names=""
        nm=re.search(r"(?:قالب(?:ها| ها)?)\s*(?:با|به)\s*(?:این\s*)?(?:نام|اسم)\s*[:：]?\s*(.+?)(?=\s*(?:و\s+پست|و\s+بانک|پست سریع|داخل بانک|$))",n,re.I)
        if nm: names=nm.group(1).strip()
        actions.append({"type":"create_templates","section":section,"count":count,"names":names})
    qm=re.search(r"پست\s*سریع\s*(?:بخش|برای|در|داخل)?\s*[«\"']?([^«»\"'،,؛;]+?)[»\"']?\s*(?:را\s*)?(فعال|روشن|خاموش|غیرفعال)",n,re.I)
    if qm: actions.append({"type":"quick","section":_assistant_clean_name(qm.group(1)),"enabled":qm.group(2) in ("فعال","روشن")})
    rm=re.search(r"(?:ریست|بازنشانی)\s+(?:بانک|بانک\s+عنوان|بانک\s+قالب)\s*(?:بخش|برای|در|داخل)?\s*[«\"']?([^«»\"'،,؛;]+?)[»\"']?\s*$",n,re.I)
    if rm: actions.append({"type":"reset_bank","section":_assistant_clean_name(rm.group(1))})
    bm=re.search(r"(?:داخل\s+بانک|بانک)\s+(?:هر|همه)?\s*(?:قالب(?:ها|های| ها| های)?\s*)?(?:بخش|برای|در)?\s*[«\"']?([^:：\n]+?)[»\"']?\s*[:：]\s*(.+)$",n,re.I|re.S)
    if bm:
        items=_assistant_bank_items(bm.group(2)); section=_assistant_clean_name(bm.group(1))
        if section and items: actions.append({"type":"add_bank","section":section,"items":items})
    return actions or None

async def _assistant_execute(update,context,actions):
    uid=update.effective_user.id
    if not has_permission(uid,"manage_templates"):
        await update.message.reply_text("⛔ برای استفاده از دستیار، دسترسی مدیریت قالب‌ها لازم است."); return True
    report=[]
    for a in actions:
        typ=a["type"]
        if typ=="create_section":
            if not has_permission(uid,"list"): report.append("⛔ اجازه ساخت بخش نداری."); continue
            name=a["name"]; g=_assistant_section_key(name)
            if g in TEMPLATE_GROUPS: report.append(f"ℹ️ بخش «{name}» از قبل وجود دارد.")
            else:
                TEMPLATE_GROUPS[g]=[]; SECTION_SETTINGS[g]={"quick_enabled":False}; save_template_groups(); save_section_settings(); report.append(f"✅ بخش «{name}» ساخته شد.")
        elif typ=="create_templates":
            section=a["section"]; g=_assistant_section_key(section)
            if g not in TEMPLATE_GROUPS: TEMPLATE_GROUPS[g]=[]; SECTION_SETTINGS[g]={"quick_enabled":False}
            made=[]
            for name in _assistant_template_names(section,a["count"],a.get("names","")):
                if name in ALL_TEXTS: continue
                ALL_TEXTS[name]={"header":"","link_text":"download","linked_word":"","footer":"","random_headers":[],"blockquote":False,"title":""}
                TEMPLATE_GROUPS[g].append(name); _title_bank_entry(g,name); made.append(name)
            save_texts(); save_template_groups(); save_title_banks(); report.append(f"📦 {len(made)} قالب برای «{section}» ساخته شد.")
        elif typ=="add_bank":
            section=a["section"]; g=_assistant_section_key(section); keys=[k for k in TEMPLATE_GROUPS.get(g,[]) if k in ALL_TEXTS]
            if not keys: report.append(f"❌ در بخش «{section}» قالبی پیدا نشد."); continue
            for k in keys: add_title_bank_items(g,k,a["items"])
            save_title_banks(); report.append(f"🏦 {len(a['items'])} متن به بانک {len(keys)} قالب بخش «{section}» اضافه شد.")
        elif typ=="quick":
            g=_assistant_section_key(a["section"])
            if g not in TEMPLATE_GROUPS: report.append(f"❌ بخش «{a['section']}» پیدا نشد."); continue
            set_section_enabled(g,a["enabled"]); report.append(f"⚡ پست سریع «{a['section']}» {'روشن' if a['enabled'] else 'خاموش'} شد.")
        elif typ=="reset_bank":
            g=_assistant_section_key(a["section"])
            if g not in TEMPLATE_GROUPS: report.append(f"❌ بخش «{a['section']}» پیدا نشد."); continue
            report.append(f"🔄 بانک بخش «{a['section']}» ریست شد ({reset_title_bank_progress(g)} قالب).")
    context.user_data.pop("state",None)
    await update.message.reply_text("🤖 انجام شد.\n\n"+"\n".join(report),reply_markup=get_main_keyboard(uid)); return True


async def assistant_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not can_use_bot(uid):
        await update.message.reply_text("⛔ دسترسی نداری!")
        return
    if not has_permission(uid, "manage_templates"):
        await update.message.reply_text(
            "⛔ برای استفاده از دستیار، دسترسی مدیریت قالب‌ها لازم است.",
            reply_markup=get_main_keyboard(uid)
        )
        return
    context.user_data.clear()
    context.user_data["assistant_mode"] = True
    await update.message.reply_text(
        "🤖 دستیار آماده است!\n\n"
        "خیلی ساده بهش بگو چی می‌خوای انجام بده. مثلاً:\n"
        "• یه بخش به اسم وطنی بساز\n"
        "• ۲۰ تا قالب برای وطنی بساز\n"
        "• داخل بانک وطنی: متن اول، متن دوم، متن سوم\n"
        "• پست سریع وطنی رو روشن کن\n\n"
        "💡 چند کار رو هم می‌تونی در یک پیام بنویسی.\n"
        "برای خروج، یکی از دکمه‌های منو رو بزن.",
        reply_markup=get_main_keyboard(uid)
    )

# ═══════════════════════════════════════════════════
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global ACTIVE_KEY
    text = update.message.text or ""
    if text == "🤖 دستیار":
        await assistant_cmd(update, context)
        return
    state = context.user_data.get("state")
    uid = update.effective_user.id
    if not can_use_bot(uid):
        await update.message.reply_text("⛔ دسترسی نداری!")
        return
    if not state or context.user_data.get("assistant_mode"):
        assistant_actions = _assistant_parse(text)
        if assistant_actions:
            if await _assistant_execute(update, context, assistant_actions):
                context.user_data.pop("assistant_mode", None)
                return
        if context.user_data.get("assistant_mode") and text not in MAIN_MENU_BUTTON_TEXTS:
            await update.message.reply_text(
                "🤖 این دستور رو نفهمیدم.\n\n"
                "مثال: «۲۰ تا قالب برای وطنی بساز» یا «یه بخش به اسم وطنی بساز»"
            )
            return
    if text in ["تمام", "✅ تمام"] and context.user_data.get("ready_mode"):
        context.user_data.pop("ready_mode", None)
        context.user_data.pop("pending_url", None)
        await update.message.reply_text("✅ آماده‌سازی تمام شد.", reply_markup=get_main_keyboard(uid))
        return
    if text in ["لغو", "❌ لغو"]:
        context.user_data.clear()
        await update.message.reply_text("❌ لغو شد.", reply_markup=get_main_keyboard(uid))
        return
    if state and text in MAIN_MENU_BUTTON_TEXTS:
        # A menu button was tapped while some other input was being
        # collected (e.g. bulk bank titles). Drop that pending state so the
        # button below is handled as real navigation instead of being
        # swallowed as a line of bank/title text.
        context.user_data.pop("state", None)
        state = None
    if text == "👮‍♂️ مدیریت ادمین‌ها":
        await admin_menu_cmd(update, context)
        return
    if text == "📋 لیست ادمین‌ها":
        await admin_list_cmd(update, context)
        return
    if text == "➕ افزودن ادمین":
        if not is_owner(uid):
            await update.message.reply_text("⛔ فقط مالک!", reply_markup=get_main_keyboard(uid))
            return
        context.user_data["state"] = "waiting_new_admin_id"
        await update.message.reply_text(
            "➕ افزودن ادمین جدید:\n\n"
            "آیدی عددی (User ID) شخص رو بفرست:\n"
            "(مثلاً: 123456789)\n\n"
            "برای لغو بنویس: لغو",
            reply_markup=ADMIN_MANAGE_KEYBOARD
        )
        return
    if text == "🗑 برکناری ادمین":
        if not is_owner(uid):
            await update.message.reply_text("⛔ فقط مالک!", reply_markup=get_main_keyboard(uid))
            return
        if not ADMINS:
            await update.message.reply_text("📭 هیچ ادمینی برای برکناری نیست!", reply_markup=ADMIN_MANAGE_KEYBOARD)
            return
        context.user_data["state"] = "waiting_remove_admin_id"
        lines = "\n".join([f"• {aid}" for aid in ADMINS.keys()])
        await update.message.reply_text(
            f"🗑 آیدی عددی ادمینی که می‌خوای برکنار کنی رو بفرست:\n\n{lines}\n\n"
            "برای لغو بنویس: لغو",
            reply_markup=ADMIN_MANAGE_KEYBOARD
        )
        return
    if text == "⚙️ مدیریت دسترسی‌ها":
        if not is_owner(uid):
            await update.message.reply_text("⛔ فقط مالک!", reply_markup=get_main_keyboard(uid))
            return
        if not ADMINS:
            await update.message.reply_text("📭 اول یه ادمین اضافه کن!", reply_markup=ADMIN_MANAGE_KEYBOARD)
            return
        buttons = []
        for aid in sorted(ADMINS.keys()):
            fa = "🌟" if ADMINS[aid].get("full_access") else "👤"
            buttons.append([InlineKeyboardButton(f"{fa} {aid}", callback_data=f"adm_edit:{aid}")])
        await update.message.reply_text(
            "⚙️ کدوم ادمین رو می‌خوای مدیریت کنی؟",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return
    if text == "🔙 بازگشت به منوی اصلی":
        await update.message.reply_text("🔙 برگشتیم!", reply_markup=get_main_keyboard(uid))
        return
    if state == "waiting_new_admin_id":
        if not text.isdigit():
            await update.message.reply_text("❌ آیدی باید فقط عدد باشه! دوباره بفرست:\n(برای لغو: لغو)", reply_markup=ADMIN_MANAGE_KEYBOARD)
            return
        new_id = text
        if new_id == str(OWNER_ID):
            await update.message.reply_text("😂 خودت که مالکی! نیازی به ادمین کردن خودت نیست.", reply_markup=ADMIN_MANAGE_KEYBOARD)
            context.user_data.pop("state", None)
            return
        if new_id in ADMINS:
            await update.message.reply_text("⚠️ این کاربر قبلاً ادمین بود!", reply_markup=ADMIN_MANAGE_KEYBOARD)
            context.user_data.pop("state", None)
            return
        ADMINS[new_id] = {"full_access": False, "permissions": []}
        save_admins()
        context.user_data.pop("state", None)
        await update.message.reply_text(
            f"✅ ادمین {new_id} اضافه شد!\n\n"
            "حالا از ⚙️ مدیریت دسترسی‌ها می‌تونی دسترسی‌هاش رو تنظیم کنی.",
            reply_markup=ADMIN_MANAGE_KEYBOARD
        )
        return
    if state == "waiting_remove_admin_id":
        if not text.isdigit():
            await update.message.reply_text("❌ آیدی باید عدد باشه! دوباره:\n(برای لغو: لغو)", reply_markup=ADMIN_MANAGE_KEYBOARD)
            return
        rem_id = text
        if rem_id not in ADMINS:
            await update.message.reply_text("❌ این آیدی توی لیست ادمین‌ها نیست!", reply_markup=ADMIN_MANAGE_KEYBOARD)
            context.user_data.pop("state", None)
            return
        del ADMINS[rem_id]
        save_admins()
        context.user_data.pop("state", None)
        await update.message.reply_text(f"🗑 ادمین {rem_id} برکنار شد!", reply_markup=ADMIN_MANAGE_KEYBOARD)
        return

    if state == "tmpl_wait_category":
        if not has_permission(uid, "manage_templates"):
            context.user_data.clear()
            await update.message.reply_text("⛔ اجازه نداری!", reply_markup=get_main_keyboard(uid))
            return
        if text in {"لغو", "❌ لغو"}:
            context.user_data.clear()
            await update.message.reply_text("❌ لغو شد.", reply_markup=get_main_keyboard(uid))
            return
        context.user_data["tmpl_category"] = text.strip()
        context.user_data["state"] = "tmpl_wait_subcategory"
        await update.message.reply_text(
            f"✅ نوع اصلی: {text.strip()}\n\n"
            "حالا زیرنوع/مدل را بفرست.\n"
            "مثال: فوتبال\n"
            "یا: فیلم\n\n"
            "برای لغو: لغو"
        )
        return

    if state == "tmpl_wait_subcategory":
        if not has_permission(uid, "manage_templates"):
            context.user_data.clear()
            await update.message.reply_text("⛔ اجازه نداری!", reply_markup=get_main_keyboard(uid))
            return
        if text in {"لغو", "❌ لغو"}:
            context.user_data.clear()
            await update.message.reply_text("❌ لغو شد.", reply_markup=get_main_keyboard(uid))
            return
        context.user_data["tmpl_subcategory"] = text.strip()
        context.user_data["state"] = "tmpl_wait_link_text"
        await update.message.reply_text(
            "📝 حالا متن پایین قالب را بفرست.\n\n"
            "مثال:\n"
            "Download pack مشـ.ـاهده ✅\n\n"
            "کلمه‌ای که باید لینک شود را بعداً می‌پرسم."
        )
        return

    if state == "section_tmpl_wait_link_text":
        if not has_permission(uid, "manage_templates"):
            context.user_data.clear()
            await update.message.reply_text("⛔ اجازه نداری!", reply_markup=get_main_keyboard(uid))
            return
        if text in {"لغو", "❌ لغو"}:
            context.user_data.clear()
            await update.message.reply_text("❌ لغو شد.", reply_markup=get_main_keyboard(uid))
            return
        context.user_data["section_tmpl_link_text"] = text
        context.user_data["state"] = "section_tmpl_wait_linked_word"
        await update.message.reply_text(
            "🔗 کدام کلمه/عبارت از متن باید لینک شود؟\n\n"
            f"{text}\n\n"
            "اگر می‌خواهی کل متن لینک شود، بنویس: همه\n"
            "برای لغو: لغو"
        )
        return

    if state == "section_tmpl_wait_linked_word":
        if not has_permission(uid, "manage_templates"):
            context.user_data.clear()
            await update.message.reply_text("⛔ اجازه نداری!", reply_markup=get_main_keyboard(uid))
            return
        if text in {"لغو", "❌ لغو"}:
            context.user_data.clear()
            await update.message.reply_text("❌ لغو شد.", reply_markup=get_main_keyboard(uid))
            return
        link_text = context.user_data.get("section_tmpl_link_text", "")
        linked_word = "" if text.strip() == "همه" else text.strip()
        if linked_word and linked_word not in link_text:
            await update.message.reply_text("❌ این عبارت داخل متن نیست. دوباره بفرست.")
            return
        context.user_data["section_tmpl_linked_word"] = linked_word
        context.user_data["state"] = "section_tmpl_wait_name"
        await update.message.reply_text(
            "🏷 حالا یک اسم یکتا برای قالب بفرست.\n"
            "مثال: وطنی-01\n\n"
            "برای لغو: لغو"
        )
        return

    if state == "section_tmpl_wait_name":
        if not has_permission(uid, "manage_templates"):
            context.user_data.clear()
            await update.message.reply_text("⛔ اجازه نداری!", reply_markup=get_main_keyboard(uid))
            return
        if text in {"لغو", "❌ لغو"}:
            context.user_data.clear()
            await update.message.reply_text("❌ لغو شد.", reply_markup=get_main_keyboard(uid))
            return
        name = text.strip()
        if not name or name in ALL_TEXTS:
            await update.message.reply_text("❌ این اسم خالی است یا قبلاً استفاده شده. اسم دیگری بفرست.")
            return
        g = context.user_data.get("section_target")
        if not g or g not in TEMPLATE_GROUPS:
            context.user_data.clear()
            await update.message.reply_text("❌ بخش پیدا نشد. دوباره وارد بخش شو.", reply_markup=get_main_keyboard(uid))
            return
        link_text = context.user_data.get("section_tmpl_link_text", "")
        linked_word = context.user_data.get("section_tmpl_linked_word", "")
        ALL_TEXTS[name] = {
            "header": "",
            "link_text": link_text,
            "linked_word": linked_word,
            "footer": "",
            "random_headers": [],
            "blockquote": False,
            "title": ""
        }
        TEMPLATE_GROUPS.setdefault(g, []).append(name)
        save_texts()
        save_template_groups()
        context.user_data.clear()
        await update.message.reply_text(
            f"✅ قالب «{name}» داخل بخش «{section_title(g)}» ساخته شد.\n\n"
            "📁 برای مدیریت قالب‌های این بخش از 📋 لیست متن‌ها وارد همان بخش شو.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📁 باز کردن بخش", callback_data=f"section:{g}")],
                [InlineKeyboardButton("📋 لیست بخش‌ها", callback_data="back_list")]
            ])
        )
        return

    if state == "tmpl_wait_link_text":
        if not has_permission(uid, "manage_templates"):
            context.user_data.clear()
            await update.message.reply_text("⛔ اجازه نداری!", reply_markup=get_main_keyboard(uid))
            return
        context.user_data["tmpl_link_text"] = text
        context.user_data["state"] = "tmpl_wait_linked_word"
        await update.message.reply_text(
            "🔗 کدام کلمه/عبارت از متن باید لینک شود؟\n\n"
            f"{text}\n\n"
            "اگر می‌خواهی کل متن لینک شود، بنویس: همه"
        )
        return

    if state == "tmpl_wait_linked_word":
        if not has_permission(uid, "manage_templates"):
            context.user_data.clear()
            await update.message.reply_text("⛔ اجازه نداری!", reply_markup=get_main_keyboard(uid))
            return
        link_text = context.user_data.get("tmpl_link_text", "")
        linked_word = "" if text.strip() == "همه" else text.strip()
        if linked_word and linked_word not in link_text:
            await update.message.reply_text("❌ این عبارت داخل متن نیست. دوباره بفرست.")
            return
        context.user_data["tmpl_linked_word"] = linked_word
        context.user_data["state"] = "tmpl_wait_name"
        await update.message.reply_text(
            "🏷 حالا یک اسم یکتا برای قالب بفرست.\n"
            "مثال: v_fut_01"
        )
        return

    if state == "tmpl_wait_name":
        if not has_permission(uid, "manage_templates"):
            context.user_data.clear()
            await update.message.reply_text("⛔ اجازه نداری!", reply_markup=get_main_keyboard(uid))
            return
        name = text.strip()
        if not name or name in ALL_TEXTS:
            await update.message.reply_text("❌ این اسم خالی است یا قبلاً استفاده شده. یک اسم دیگر بفرست.")
            return

        category = context.user_data.get("tmpl_category", "").strip()
        subcategory = context.user_data.get("tmpl_subcategory", "").strip()
        link_text = context.user_data.get("tmpl_link_text", "")
        linked_word = context.user_data.get("tmpl_linked_word", "")

        ALL_TEXTS[name] = {
            "header": "",
            "link_text": link_text,
            "linked_word": linked_word,
            "footer": "",
            "random_headers": [],
            "blockquote": False,
            "title": ""
        }
        g = group_key(category, subcategory)
        TEMPLATE_GROUPS.setdefault(g, [])
        if name not in TEMPLATE_GROUPS[g]:
            TEMPLATE_GROUPS[g].append(name)

        save_texts()
        save_template_groups()
        context.user_data.clear()

        await update.message.reply_text(
            f"✅ قالب ساخته شد!\n\n"
            f"🏷 نام: {name}\n"
            f"🇮🇷/🌍 نوع: {category}\n"
            f"🎯 مدل: {subcategory}\n"
            f"🔗 متن: {link_text}\n\n"
            "از این به بعد اگر لینک با همین نوع/مدل از ربات دوم بیاید، "
            "ربات اصلی فقط از همین گروه قالب انتخاب می‌کند.",
            reply_markup=get_main_keyboard(uid)
        )
        return

    if state == "edit_template_text":
        if not has_permission(uid, "list"):
            context.user_data.clear()
            await update.message.reply_text("⛔ اجازه نداری!", reply_markup=get_main_keyboard(uid))
            return
        if text in {"لغو", "❌ لغو"}:
            context.user_data.clear()
            await update.message.reply_text("❌ لغو شد.", reply_markup=get_main_keyboard(uid))
            return
        k = context.user_data.get("edit_template_key")
        if not k or k not in ALL_TEXTS:
            context.user_data.clear()
            await update.message.reply_text("❌ قالب پیدا نشد.", reply_markup=get_main_keyboard(uid))
            return
        old_word = ALL_TEXTS[k].get("linked_word", "")
        ALL_TEXTS[k]["link_text"] = text
        ALL_TEXTS[k]["linked_word"] = old_word if old_word and old_word in text else ""
        save_texts()
        context.user_data.clear()
        await update.message.reply_text(
            f"✅ قالب «{k}» ویرایش شد.\n\n"
            f"{text}\n\n"
            "🔗 کلمه لینک‌شده: " + (ALL_TEXTS[k].get("linked_word") or "کل متن"),
            reply_markup=get_main_keyboard(uid)
        )
        return

    if state == "copy_template_count":
        if not has_permission(uid, "list"):
            context.user_data.clear(); await update.message.reply_text("⛔ اجازه نداری!", reply_markup=get_main_keyboard(uid)); return
        if text in {"لغو", "❌ لغو"}:
            context.user_data.clear(); await update.message.reply_text("❌ لغو شد.", reply_markup=get_main_keyboard(uid)); return
        raw_count = text.strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))
        if not raw_count.isdigit():
            await update.message.reply_text("❌ فقط تعداد را به عدد بفرست. مثال: 50"); return
        count = int(raw_count)
        if not 1 <= count <= 1000:
            await update.message.reply_text("❌ تعداد باید بین 1 تا 1000 باشد."); return
        context.user_data["copy_template_count"] = count
        context.user_data["state"] = "copy_template_prefix"
        await update.message.reply_text(f"✅ تعداد {count} کپی ثبت شد.\n\n🏷 اسم پایه را بفرست.\nمثلاً: test\nنتیجه: test 1 ، test 2 ، ...\n\nبرای لغو: لغو")
        return

    if state == "copy_template_prefix":
        if not has_permission(uid, "list"):
            context.user_data.clear(); await update.message.reply_text("⛔ اجازه نداری!", reply_markup=get_main_keyboard(uid)); return
        if text in {"لغو", "❌ لغو"}:
            context.user_data.clear(); await update.message.reply_text("❌ لغو شد.", reply_markup=get_main_keyboard(uid)); return
        prefix = text.strip(); source = context.user_data.get("copy_template_key"); count = int(context.user_data.get("copy_template_count", 0))
        if not prefix or not source or source not in ALL_TEXTS or not count:
            context.user_data.clear(); await update.message.reply_text("❌ اطلاعات کپی نامعتبر است.", reply_markup=get_main_keyboard(uid)); return
        import copy as _copy
        names = [f"{prefix} {i}" for i in range(1, count + 1)]
        conflicts = [n for n in names if n in ALL_TEXTS]
        if conflicts:
            await update.message.reply_text(
                "❌ بعضی از نام‌های تولیدشده از قبل وجود دارند.\n"
                "یک اسم پایه دیگر بفرست."
            )
            return

        # Copy the selected template in one fast batch and keep all its data intact.
        source_copy = _copy.deepcopy(ALL_TEXTS[source])
        matching_groups = [g for g, keys in TEMPLATE_GROUPS.items() if source in keys]

        for name in names:
            ALL_TEXTS[name] = _copy.deepcopy(source_copy)

        # Put all generated copies immediately after the original in the same section.
        for g in matching_groups:
            keys = TEMPLATE_GROUPS[g]
            pos = keys.index(source) + 1
            keys[pos:pos] = names

        save_texts()
        save_template_groups()
        context.user_data.clear()

        await update.message.reply_text(
            f"⚡ {count} کپی در یک مرحله ساخته شد!\n\n"
            f"📄 اصلی: {source}\n"
            f"🏷 نام‌ها: {prefix} 1 تا {prefix} {count}",
            reply_markup=get_main_keyboard(uid)
        )
        return

    if state == "waiting_section_quote_text":
        if not has_permission(uid, "list"):
            context.user_data.clear(); await update.message.reply_text("⛔ اجازه نداری!", reply_markup=get_main_keyboard(uid)); return
        if text in {"لغو", "❌ لغو"}:
            context.user_data.clear(); await update.message.reply_text("❌ لغو شد.", reply_markup=get_main_keyboard(uid)); return
        g = context.user_data.get("quote_section_group")
        keys = [k for k in TEMPLATE_GROUPS.get(g, []) if k in ALL_TEXTS]
        if not g or not keys:
            context.user_data.clear()
            await update.message.reply_text("❌ بخش پیدا نشد.", reply_markup=get_main_keyboard(uid))
            return
        quote_text = text.strip()
        changed, skipped = 0, 0
        for k in keys:
            body = str(ALL_TEXTS[k].get("link_text", "") or "")
            if quote_text and quote_text in body:
                ALL_TEXTS[k]["quote_text"] = quote_text
                ALL_TEXTS[k]["blockquote"] = True
                changed += 1
            else:
                skipped += 1
        save_texts()
        if quote_text:
            set_section_last_quote(g, quote_text)
        context.user_data.clear()
        msg = f"✅ نقل‌قول برای {changed} قالب بخش «{section_title(g)}» روشن شد."
        if skipped:
            msg += f"\n📌 {skipped} قالب چون این متن را نداشتند، دست‌نخورده ماندند."
        await update.message.reply_text(
            msg,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 بازگشت به بخش", callback_data=f"section:{g}")]
            ])
        )
        return

    if state == "waiting_quote_text":
        if not has_permission(uid, "list"):
            context.user_data.clear(); await update.message.reply_text("⛔ اجازه نداری!", reply_markup=get_main_keyboard(uid)); return
        if text in {"لغو", "❌ لغو"}:
            context.user_data.clear(); await update.message.reply_text("❌ لغو شد.", reply_markup=get_main_keyboard(uid)); return
        k = context.user_data.get("quote_template_key")
        item = ALL_TEXTS.get(k)
        if not item:
            context.user_data.clear()
            await update.message.reply_text("❌ قالب پیدا نشد.", reply_markup=get_main_keyboard(uid))
            return
        body = str(item.get("link_text", "") or "")
        quote_text = text.strip()
        if not quote_text or quote_text not in body:
            await update.message.reply_text("❌ این متن داخل قالب پیدا نشد. عین همون تکه از متن را کپی و دوباره بفرست.\n\nبرای لغو: لغو")
            return
        item["quote_text"] = quote_text
        item["blockquote"] = True
        save_texts()
        context.user_data.clear()
        await update.message.reply_text(
            f"✅ نقل‌قول برای «{k}» روشن شد.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 بازگشت به قالب", callback_data=f"use:{k}")]
            ])
        )
        return

    if state == "title_bank_add":
        if not has_permission(uid, "list"):
            context.user_data.clear(); await update.message.reply_text("⛔ اجازه نداری!", reply_markup=get_main_keyboard(uid)); return
        if text in {"لغو", "❌ لغو"}:
            g = context.user_data.get("title_bank_group")
            context.user_data.clear()
            await update.message.reply_text(
                "❌ لغو شد.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت به بخش", callback_data=f"section:{g}")]]) if g else get_main_keyboard(uid)
            )
            return
        g = context.user_data.get("title_bank_group")
        k = context.user_data.get("title_bank_template")
        if not g or not k or k not in ALL_TEXTS or k not in TEMPLATE_GROUPS.get(g, []):
            context.user_data.clear()
            await update.message.reply_text("❌ قالب پیدا نشد.", reply_markup=get_main_keyboard(uid))
            return
        items = [line.strip() for line in text.splitlines() if line.strip()]
        if not items:
            await update.message.reply_text("❌ حداقل یک متن در یک خط بفرست.")
            return
        add_title_bank_items(g, k, items)
        entry = _title_bank_entry(g, k)
        total = len(entry.get("items", []))
        context.user_data.clear()
        await update.message.reply_text(
            f"✅ {len(items)} متن به بانک «{k}» اضافه شد.\n📦 مجموع بانک این قالب: {total}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت به بانک", callback_data=f"titlebank:{g}")]])
        )
        return

    if state == "titles_all":
        if not has_permission(uid, "list"):
            context.user_data.clear(); await update.message.reply_text("⛔ اجازه نداری!", reply_markup=get_main_keyboard(uid)); return
        if text in {"لغو", "❌ لغو"}:
            context.user_data.clear(); await update.message.reply_text("❌ لغو شد.", reply_markup=get_main_keyboard(uid)); return
        g = context.user_data.get("titles_group")
        keys = context.user_data.get("titles_keys", [])
        titles = [line.strip() for line in text.splitlines() if line.strip()]
        changed = min(len(titles), len(keys))
        for i in range(changed):
            key = keys[i]
            item = ALL_TEXTS.get(key)
            if not item:
                continue
            new_title = titles[i]

            # A template's body (link_text) may or may not have its own
            # leading title-like line. Detect that shape directly instead of
            # guessing from linked_word or a previously-stored title — that
            # old approach deleted real content any time the linked phrase
            # happened to appear in the body, even with no old title at all.
            #
            # Shape 1: "TitleLine\n\nRest of the body..." — the first line
            # is non-blank and is immediately followed by a blank line. That
            # first line is acting as the title; drop only that one line,
            # keeping the blank line (and everything else) exactly as-is.
            # Shape 2: anything else — no separable title line exists in the
            # body (the title, if any, lives only in the "title" field), so
            # link_text is left completely untouched.
            link_text = str(item.get("link_text", "") or "")
            lines = link_text.splitlines(keepends=True)
            if len(lines) >= 2 and lines[0].strip() and not lines[1].strip():
                del lines[0]
                item["link_text"] = "".join(lines)

            item["title"] = new_title
            item["header"] = ""
            item["random_headers"] = []
        save_texts(); context.user_data.clear()
        await update.message.reply_text(
            f"✅ عنوان {changed} قالب تغییر کرد.\n📌 بقیه‌ی قالب‌های این بخش دست‌نخورده ماندند.",
            reply_markup=get_main_keyboard(uid)
        )
        return

    if state == "titles_all_edit":
        if not has_permission(uid, "list"):
            context.user_data.clear(); await update.message.reply_text("⛔ اجازه نداری!", reply_markup=get_main_keyboard(uid)); return
        if text in {"لغو", "❌ لغو"}:
            context.user_data.clear(); await update.message.reply_text("❌ لغو شد.", reply_markup=get_main_keyboard(uid)); return
        g = context.user_data.get("titles_edit_group")
        keys = context.user_data.get("titles_edit_keys", [])
        # Preserve blank lines exactly by position — a blank line means
        # "leave this template's title untouched", no matter which
        # position (1st, 21st, last...) it falls on.
        lines = text.splitlines()
        changed = 0
        skipped = 0
        for i, key in enumerate(keys):
            line = lines[i].strip() if i < len(lines) else ""
            if not line:
                skipped += 1
                continue
            item = ALL_TEXTS.get(key)
            if not item:
                continue
            new_title = line

            # Same title/link_text shape-detection as bulk "تنظیم عنوان همه":
            # only drop a leading title-like line from link_text if the body
            # actually has that shape; otherwise leave link_text untouched.
            link_text = str(item.get("link_text", "") or "")
            body_lines = link_text.splitlines(keepends=True)
            if len(body_lines) >= 2 and body_lines[0].strip() and not body_lines[1].strip():
                del body_lines[0]
                item["link_text"] = "".join(body_lines)

            item["title"] = new_title
            item["header"] = ""
            item["random_headers"] = []
            changed += 1
        save_texts(); context.user_data.clear()
        await update.message.reply_text(
            f"✅ عنوان {changed} قالب تغییر کرد.\n"
            f"📌 {skipped} قالب چون خط خالی بود، دست‌نخورده موند.",
            reply_markup=get_main_keyboard(uid)
        )
        return

    if state == "section_add_name":
        if not has_permission(uid, "list"):
            context.user_data.clear(); await update.message.reply_text("⛔ اجازه نداری!", reply_markup=get_main_keyboard(uid)); return
        if text in {"لغو", "❌ لغو"}:
            context.user_data.clear(); await update.message.reply_text("❌ لغو شد.", reply_markup=get_main_keyboard(uid)); return
        name=text.strip()
        if not name:
            await update.message.reply_text("❌ اسم بخش نمی‌تواند خالی باشد."); return
        g=group_key(name, "")
        if g in TEMPLATE_GROUPS:
            await update.message.reply_text("⚠️ این بخش از قبل وجود دارد. اسم دیگری بفرست."); return
        TEMPLATE_GROUPS[g]=[]; SECTION_SETTINGS[g]={"quick_enabled":False}; save_template_groups(); save_section_settings()
        return_to_final = bool(context.user_data.get("return_to_final"))
        context.user_data.clear()
        if return_to_final:
            await update.message.reply_text(
                f"✅ پوشه «{name}» ساخته شد.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📁 ورود به پوشه", callback_data=f"section:{g}")],
                    [InlineKeyboardButton("📁 برگشت به نهایی", callback_data="final_back")]
                ])
            )
        else:
            await update.message.reply_text(
                f"✅ بخش «{name}» ساخته شد.\n\n"
                "حالا می‌تونی مستقیم داخل همین بخش متن/قالب اضافه کنی.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📁 ورود به بخش", callback_data=f"section:{g}")],
                    [InlineKeyboardButton("📋 لیست بخش‌ها", callback_data="back_list")]
                ])
            )
        return

    if state == "waiting_random_header":
        if not has_permission(uid, "list"):
            await update.message.reply_text("⛔ اجازه نداری!", reply_markup=get_main_keyboard(uid))
            context.user_data.clear()
            return
        k = context.user_data.get("rh_template")
        if not k or k not in ALL_TEXTS:
            await update.message.reply_text("❌ خطا! دوباره شروع کن.", reply_markup=get_main_keyboard(uid))
            context.user_data.clear()
            return
        if "random_headers" not in ALL_TEXTS[k]:
            ALL_TEXTS[k]["random_headers"] = []
        ALL_TEXTS[k]["random_headers"].append(text)
        save_texts()
        rh_count = len(ALL_TEXTS[k]["random_headers"])
        await update.message.reply_text(
            f"✅ اضافه شد!\n\n"
            f"🎲 الان {rh_count} تا متن رندوم برای {k} داری.",
            reply_markup=get_main_keyboard(uid)
        )
        context.user_data.clear()
        return
    if state == "waiting_topic_name":
        if not has_permission(uid, "manage_topics"):
            await update.message.reply_text("⛔ اجازه نداری!", reply_markup=get_main_keyboard(uid))
            context.user_data.clear()
            return
        context.user_data["topic_name"] = text
        context.user_data["state"] = "waiting_topic_text"
        await update.message.reply_text(
            f"✅ اسم موضوع: {text}\n\n"
            "حالا متن کامل موضوع رو بفرست:\n"
            "(این متن بالای پست میاد)\n\n"
            "برای لغو بنویس: لغو"
        )
        return
    if state == "waiting_topic_text":
        if not has_permission(uid, "manage_topics"):
            await update.message.reply_text("⛔ اجازه نداری!", reply_markup=get_main_keyboard(uid))
            context.user_data.clear()
            return
        name = context.user_data.get("topic_name", "")
        if not name:
            await update.message.reply_text("❌ خطا! دوباره شروع کن.", reply_markup=get_main_keyboard(uid))
            context.user_data.clear()
            return
        existed = name in TOPICS
        TOPICS[name] = text
        save_texts()
        if context.user_data.get("ready_mode"):
            # If a topic is added during the "با موضوع" flow,
            # use the newly created/updated topic immediately.
            context.user_data["post_mode"] = "topic"
            context.user_data["selected_topic"] = name
            context.user_data.pop("state", None)
            context.user_data.pop("topic_name", None)
            await update.message.reply_text(
                f"✅ موضوع {name} {'جایگزین شد' if existed else 'اضافه شد'}!\n\n"
                f"📝 {text}\n\n"
                "⬇️ حالا لینک رو بده یا بنویس تمام:",
                reply_markup=MULTI_POST_KEYBOARD
            )
        else:
            await update.message.reply_text(
                f"✅ موضوع {name} {'جایگزین شد' if existed else 'اضافه شد'}!\n\n"
                f"📝 {text}\n\n"
                "حالا از 🎯 آماده‌سازی می‌تونی استفاده کنی.",
                reply_markup=get_main_keyboard(uid)
            )
            context.user_data.clear()
        return
    if state == "waiting_link_text":
        if not has_permission(uid, "add_text"):
            await update.message.reply_text("⛔ اجازه نداری!", reply_markup=get_main_keyboard(uid))
            context.user_data.clear()
            return
        context.user_data["temp_link_text"] = text
        context.user_data["state"] = "waiting_linked_word"
        await update.message.reply_text(
            f"✅ متن پایین دریافت شد.\n\n"
            "حالا بگو کدوم کلمه لینک بشه:\n\n"
            f"{text}\n\n"
            "(برای لغو بنویس: لغو)"
        )
        return
    if state == "waiting_linked_word":
        if not has_permission(uid, "add_text"):
            await update.message.reply_text("⛔ اجازه نداری!", reply_markup=get_main_keyboard(uid))
            context.user_data.clear()
            return
        link_text = context.user_data.get("temp_link_text", "")
        if text not in link_text:
            await update.message.reply_text(
                f"❌ کلمه {text} توی متن پیدا نشد!\n\n"
                f"متن: {link_text}\n\n"
                "دوباره بگو:\n(برای لغو بنویس: لغو)"
            )
            return
        context.user_data["temp_linked_word"] = text
        context.user_data["state"] = "waiting_name"
        await update.message.reply_text(
            f"✅ کلمه لینک‌شده: {text}\n\n"
            "حالا یه اسم برای این قالب بذار:\n"
            "(مثلاً: p2, پک-جدید)\n\n"
            "(برای لغو بنویس: لغو)"
        )
        return
    if state == "waiting_name":
        if not has_permission(uid, "add_text"):
            await update.message.reply_text("⛔ اجازه نداری!", reply_markup=get_main_keyboard(uid))
            context.user_data.clear()
            return
        if text in ALL_TEXTS:
            await update.message.reply_text(
                f"❌ اسم {text} قبلاً هست! یه اسم دیگه:\n"
                "(برای لغو بنویس: لغو)"
            )
            return
        link_text = context.user_data.get("temp_link_text", "")
        linked_word = context.user_data.get("temp_linked_word", "")
        ALL_TEXTS[text] = {
            "header": "",
            "link_text": link_text,
            "linked_word": linked_word,
            "footer": "",
            "random_headers": [],
            "blockquote": False,
            "title": ""
        }
        save_texts()
        await update.message.reply_text(
            f"✅ قالب {text} ساخته شد!\n\n"
            f"🔗 {link_text}\n\n"
            "🎲 حالا از 📋 لیست متن‌ها → قالب «{text}» → ➕ افزودن متن رندوم\n"
            "متن‌های بالای پست رو اضافه کن.",
            reply_markup=get_main_keyboard(uid)
        )
        context.user_data.clear()
        return
    if text == "🎯 آماده‌سازی":
        await ready_cmd(update, context)
        return
    if text == "📋 لیست متن‌ها":
        await list_cmd(update, context)
        return
    if text == "⚡ پست سریع":
        await quick_post_cmd(update, context)
        return
    if text == "🧩 مدیریت قالب‌ها":
        await template_manager_cmd(update, context)
        return
    if text == "➕ افزودن متن":
        if not has_permission(uid, "add_text"):
            await update.message.reply_text("⛔ اجازه افزودن متن رو نداری!", reply_markup=get_main_keyboard(uid))
            return
        context.user_data["state"] = "waiting_link_text"
        await update.message.reply_text(
            "📝 ساخت قالب جدید:\n\n"
            "متن پایین رو بفرست:\n\n"
            "مثال:\nDownLoad مشـ.ـاهده 📥\n\n"
            "برای لغو بنویس: لغو"
        )
        return
    if text == "📁 نهایی":
        await final_menu_cmd(update, context)
        return

    if text == "📁 پست‌های من":
        await my_posts_cmd(update, context)
        return
    matches = LINK_REGEX.findall(text)
    if not matches:
        if context.user_data.get("ready_mode"):
            await update.message.reply_text(
                "❓ لینک ندیدم! لینک بده یا بنویس تمام",
                reply_markup=MULTI_POST_KEYBOARD
            )
            return
        await update.message.reply_text("❓ لینک ندیدم!", reply_markup=get_main_keyboard(uid))
        return
    url = matches[0]

    # =====================================================
    # QUICK MODE: use only the selected section's templates.
    # =====================================================
    if context.user_data.get("quick_mode"):
        group = context.user_data.get("quick_group", "")
        if not group or not section_enabled(group):
            context.user_data.pop("quick_mode", None)
            context.user_data.pop("quick_group", None)
            await update.message.reply_text("⛔ بخش پست سریع خاموش شده است.", reply_markup=get_main_keyboard(uid))
            return
        keys=[k for k in TEMPLATE_GROUPS.get(group,[]) if k in ALL_TEXTS]
        if not keys:
            context.user_data.pop("quick_mode", None)
            context.user_data.pop("quick_group", None)
            await update.message.reply_text("📭 این بخش دیگر قالبی ندارد.", reply_markup=get_main_keyboard(uid))
            return
        urls=[normalize_link(x) for x in LINK_REGEX.findall(text or "")]
        urls=list(dict.fromkeys(urls))
        if urls:
            cursor_state=TOPIC_VARIANTS.setdefault("__quick_cursor__", {})
            cursor=int(cursor_state.get(group,0))
            results=[]
            for url in urls:
                key=keys[cursor % len(keys)]
                cursor += 1
                try:
                    title_override = choose_post_title(key)
                    header, link_text, linked_word, result=build_post_result(
                        url, key, topic_name=None, title_override=title_override
                    )
                    add_post(uid, header, link_text, linked_word, url, result, section=group)
                    results.append(result)
                except Exception as e:
                    results.append(f"❌ خطا برای {url}: {e}")
            cursor_state[group]=cursor
            save_json(TOPIC_VARIANTS_FILE, TOPIC_VARIANTS)
            for result in results:
                await update.message.reply_text(result, parse_mode="HTML", disable_web_page_preview=True)
            return

    # =====================================================
    # AUTO MODE: if the link was classified by the second bot,
    # build the post immediately. No template/topic selection
    # is required in the main bot.
    # =====================================================
    mapped = get_link_mapping(url)
    if mapped:
        if not has_permission(uid, "ready"):
            await update.message.reply_text(
                "⛔ اجازه ساخت پست رو نداری!",
                reply_markup=get_main_keyboard(uid)
            )
            return

        if _USER_LOCKS.get(uid):
            await update.message.reply_text("⏳ قبلاً در حال پردازش یک لینک هستی.")
            return

        selected_template_key = choose_template_for_link(mapped)
        if selected_template_key not in ALL_TEXTS:
            await update.message.reply_text(
                "❌ برای این نوع و مدل هنوز قالبی ثبت نشده.\n\n"
                f"نوع: {mapped.get('category') or '—'}\n"
                f"مدل: {mapped.get('subcategory') or '—'}",
                reply_markup=get_main_keyboard(uid)
            )
            return

        _USER_LOCKS[uid] = True
        try:
            title_override = choose_post_title(selected_template_key)
            # A section title-bank is authoritative for section templates,
            # including an intentionally empty title after its finite bank is exhausted.
            if title_override is not None:
                topic_name = None
            else:
                custom_title = str(ALL_TEXTS.get(selected_template_key, {}).get("title", "") or "").strip()
                topic_name = None if custom_title else (mapped.get("topic_name") or mapped.get("category") or None)
            header, link_text, linked_word, result = build_post_result(
                url,
                selected_template_key,
                topic_name=topic_name,
                title_override=title_override
            )
            add_post(uid, header, link_text, linked_word, url, result, section=find_group_for_key(selected_template_key))

            await update.message.reply_text(
                result,
                parse_mode="HTML",
                disable_web_page_preview=True
            )
            await update.message.reply_text(
                "✅ پست ساخته شد!\n\n🔗 لینک بعدی رو بفرست.",
                reply_markup=get_main_keyboard(uid)
            )
        except NetworkError:
            await update.message.reply_text(
                "⚠️ مشکل شبکه پیش آمد. دوباره امتحان کن.",
                reply_markup=get_main_keyboard(uid)
            )
        except Exception as e:
            await update.message.reply_text(
                f"❌ خطا: {str(e)}",
                reply_markup=get_main_keyboard(uid)
            )
        finally:
            _USER_LOCKS[uid] = False
        return

    # No bridge record: keep the old preparation flow as a fallback.
    if context.user_data.get("ready_mode"):
        if not has_permission(uid, "ready"):
            await update.message.reply_text(
                "⛔ اجازه ساخت پست رو نداری!",
                reply_markup=get_main_keyboard(uid)
            )
            return

        post_mode = context.user_data.get("post_mode")
        if post_mode not in {"topic", "no_topic"}:
            await update.message.reply_text(
                "🎯 این لینک هنوز از ربات دوم دسته‌بندی نشده.\n\n"
                "اگر می‌خواهی خودکار ساخته شود، اول لینک را در ربات دوم ثبت کن.",
                reply_markup=get_main_keyboard(uid)
            )
            return

        topic_name = context.user_data.get("selected_topic") if post_mode == "topic" else None
        selected_template_key = ACTIVE_KEY
        if post_mode == "no_topic":
            selected_template_key = ACTIVE_KEY

        title_override = choose_post_title(selected_template_key)
        if title_override is not None:
            topic_name = None
        else:
            custom_title = str(ALL_TEXTS.get(selected_template_key, {}).get("title", "") or "").strip()
            if custom_title:
                topic_name = None

        try:
            header, link_text, linked_word, result = build_post_result(
                url, selected_template_key, topic_name=topic_name, title_override=title_override
            )
            add_post(uid, header, link_text, linked_word, url, result, section=find_group_for_key(selected_template_key))
            await update.message.reply_text(result, parse_mode="HTML", disable_web_page_preview=True)
            await update.message.reply_text("✅ پست ساخته شد!", reply_markup=get_main_keyboard(uid))
        except Exception as e:
            await update.message.reply_text(f"❌ خطا: {str(e)}", reply_markup=get_main_keyboard(uid))
        return

    await update.message.reply_text(
        "⚠️ این لینک هنوز توسط ربات دوم دسته‌بندی نشده.\n\n"
        "اول لینک را در ربات دوم ثبت کن، نوع و مدل را انتخاب کن؛ بعد همین لینک را اینجا بفرست.",
        reply_markup=get_main_keyboard(uid)
    )
    return

    # If this URL was registered by the second bot, automatically choose
    # the matching category template. Otherwise keep the old active-template flow.
    mapping = get_link_mapping(url)
    auto_key = choose_template_for_link(mapping) if mapping else ACTIVE_KEY
    template = ALL_TEXTS.get(auto_key)
    if not template:
        await update.message.reply_text(
            "📭 قالب مناسب پیدا نشد. اول برای این موضوع قالب بساز.",
            reply_markup=get_main_keyboard(uid)
        )
        return
    link_text = template.get("link_text", "download")
    linked_word = template.get("linked_word", "")
    footer = template.get("footer", "")
    rh = template.get("random_headers", [])
    bq = template.get("blockquote", False)
    if rh:
        header = random.choice(rh)
    else:
        header = ""
    if linked_word and linked_word in link_text:
        parts = link_text.split(linked_word, 1)
        link_anchor = f'<a href="{url}">{escape(linked_word)}</a>'
        if bq:
            link_anchor = f"<blockquote>{link_anchor}</blockquote>"
        link_part = f"{escape(parts[0])}{link_anchor}{escape(parts[1])}"
    else:
        link_part = f'<a href="{url}">{escape(link_text)}</a>'
        if bq:
            link_part = f"<blockquote>{link_part}</blockquote>"
    parts_list = []
    if header:
        parts_list.append(f"<b>{escape(header)}</b>")
    parts_list.append(link_part)
    if footer:
        parts_list.append(escape(footer))
    result = "\n".join(parts_list)
    result = preserve_spaces(result)
    add_post(update.effective_user.id, header, link_text, linked_word, url, result)
    try:
        await update.message.reply_text(result, parse_mode="HTML", disable_web_page_preview=True)
    except NetworkError:
        await update.message.reply_text(
            "⚠️ پست ساخته شد ولی به خاطر مشکل اینترنت نتونستم بفرستم.",
            reply_markup=get_main_keyboard(uid)
        )

# ═══════════════════════════════════════════════════
async def post_init(app):
    # Show these commands in Telegram's Menu button.
    await app.bot.set_my_commands([
        ("start", "استارت ربات"),
        ("assistant", "باز کردن دستیار فرمان"),
        ("backup", "گرفتن بکاپ از اطلاعات ربات"),
    ])


def main():
    if not TOKEN:
        raise RuntimeError("❌ BOT_TOKEN در Railway تنظیم نشده است.")
    print(f"📁 Data folder: {DATA_DIR}")
    print(f"📄 Texts file: {TEXTS_FILE} ({os.path.getsize(TEXTS_FILE) if os.path.exists(TEXTS_FILE) else 'new'})")
    print(f"📄 Topics file: {TOPICS_FILE} ({os.path.getsize(TOPICS_FILE) if os.path.exists(TOPICS_FILE) else 'new'})")
    print(f"📄 Admins file: {ADMINS_FILE} ({os.path.getsize(ADMINS_FILE) if os.path.exists(ADMINS_FILE) else 'new'})")
    print(f"📄 Posts file: {POSTS_FILE} ({os.path.getsize(POSTS_FILE) if os.path.exists(POSTS_FILE) else 'new'})")
    print(f"📄 Link registry: {LINK_REGISTRY_FILE} ({os.path.getsize(LINK_REGISTRY_FILE) if os.path.exists(LINK_REGISTRY_FILE) else 'new'})")
    print(f"📄 Template groups: {TEMPLATE_GROUPS_FILE} ({os.path.getsize(TEMPLATE_GROUPS_FILE) if os.path.exists(TEMPLATE_GROUPS_FILE) else 'new'})")
    print(f"📄 Section settings: {SECTION_SETTINGS_FILE} ({os.path.getsize(SECTION_SETTINGS_FILE) if os.path.exists(SECTION_SETTINGS_FILE) else 'new'})")
    print(f"📄 Title bank: {TITLE_BANK_FILE} ({os.path.getsize(TITLE_BANK_FILE) if os.path.exists(TITLE_BANK_FILE) else 'new'})")
    print(f"🔑 Link bridge key: {LINK_BRIDGE_KEY}")
    start_link_bridge_server()
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("assistant", assistant_cmd))
    app.add_handler(CommandHandler("backup", backup_cmd))
    app.add_handler(CommandHandler("add", add_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("cleanup_ungrouped", cleanup_ungrouped_cmd))
    app.add_handler(CommandHandler("ready", ready_cmd))
    app.add_handler(CommandHandler("myposts", my_posts_cmd))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    print("🤖 استارت شد...")
    app.run_polling()

if __name__ == "__main__":
    main()
