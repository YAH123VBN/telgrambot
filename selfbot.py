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
    buttons = [
        ["🎯 آماده‌سازی", "📋 لیست متن‌ها"],
        ["📁 نهایی"],
        ["➕ افزودن متن"],
        ["⚡ پست سریع"],
        ["📁 پست‌های من"],
    ]
    if is_owner(user_id):
        buttons.append(["👮‍♂️ مدیریت ادمین‌ها"])
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)

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

ACTIVE_KEY = "p1"

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

def section_title(g):
    category, subcategory = (g.split("|", 1) + [""])[:2]
    return f"{category} / {subcategory}" if subcategory else category


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

def add_post(user_id, header, link_text, linked_word, url, result_text):
    posts = load_json(POSTS_FILE, {})
    uid = str(user_id)
    if uid not in posts:
        posts[uid] = []
    posts[uid].insert(0, {
        "header": header,
        "link_text": link_text,
        "linked_word": linked_word,
        "url": url,
        "result": result_text
    })
    save_json(POSTS_FILE, posts)

def get_posts(user_id):
    posts = load_json(POSTS_FILE, {})
    return posts.get(str(user_id), [])


def build_post_result(url, template_key, topic_name=None):
    """Build a post using the selected template, with or without a topic."""
    base = ALL_TEXTS.get(template_key, {})
    if not base:
        raise ValueError("قالب پیدا نشد.")

    link_text = base.get("link_text", "download")
    linked_word = base.get("linked_word", "")
    footer = base.get("footer", "")
    bq = base.get("blockquote", False)

    # A manually assigned template title must be the final title.
    # This is especially important when a topic is also selected: the topic
    # header must not remain above/beside the new title, otherwise the old
    # topic text and the new title can both appear in the generated post.
    fixed_title = str(base.get("title", "") or "").strip()

    if fixed_title:
        header = fixed_title
        # If a template still contains its previous title inside link_text,
        # remove that embedded header. Keep the actual link text intact.
        # Prefer the linked_word as the anchor point; this also handles
        # templates created from forwarded posts where the old title was
        # accidentally stored together with the download text.
        if "\n" in link_text:
            if linked_word and linked_word in link_text:
                before, after = link_text.split(linked_word, 1)
                before_lines = [x.strip() for x in before.splitlines() if x.strip()]
                if before_lines:
                    link_text = linked_word + after
            elif topic_name is not None:
                link_text = link_text.split("\n", 1)[1].lstrip("\n")
    elif topic_name is not None:
        header = choose_topic_header(topic_name)
        if "\n" in link_text:
            link_text = link_text.split("\n", 1)[1].lstrip("\n")
    else:
        rh = base.get("random_headers", [])
        header = random.choice(rh) if rh else ""

    if linked_word and linked_word not in link_text:
        linked_word = ""

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
        "➕ افزودن متن → ساخت قالب جدید\n"
        "📁 پست‌های من → همه پست‌ها\n\n"
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

    files = [ADMINS_FILE, TEXTS_FILE, TOPICS_FILE, POSTS_FILE, LINK_REGISTRY_FILE, TOPIC_VARIANTS_FILE, TEMPLATE_GROUPS_FILE, LINK_BRIDGE_KEY_FILE]
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
    for idx, g in enumerate(groups):
        buttons.append([InlineKeyboardButton(f"📁 {section_title(g)}", callback_data=f"final_open:{idx}")])
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
    for g, keys in sorted(groups, key=lambda x: x[0]):
        mark = "⚡" if section_enabled(g) else "⛔"
        buttons.append([InlineKeyboardButton(f"📁 {section_title(g)} — {len(keys)} قالب {mark}", callback_data=f"section:{g}")])

    ungrouped = [k for k in sorted(ALL_TEXTS) if not any(k in vals for vals in TEMPLATE_GROUPS.values())]
    if ungrouped:
        buttons.append([InlineKeyboardButton(f"📦 بدون بخش — {len(ungrouped)} قالب", callback_data="section:__ungrouped__")])

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
    posts = get_posts(uid)
    total = len(posts)
    if not posts:
        await update.message.reply_text("📭 هنوز پستی نساختی!", reply_markup=get_main_keyboard(uid))
        return
    await update.message.reply_text(f"📁 در حال ارسال {total} پست...", reply_markup=get_main_keyboard(uid))
    sent = 0
    failed = 0
    for post in posts:
        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=post["result"],
                parse_mode="HTML",
                disable_web_page_preview=True
            )
            sent += 1
            await asyncio.sleep(0.1)
        except Exception:
            failed += 1
            continue
    msg = f"✅ {sent} پست ارسال شد."
    if failed > 0:
        msg += f"\n⚠️ {failed} پست ارسال نشد."
    await update.message.reply_text(msg, reply_markup=get_main_keyboard(uid))

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

# ═══════════════════════════════════════════════════
async def show_section(q, g):
    if g == "__ungrouped__":
        keys = [k for k in sorted(ALL_TEXTS) if not any(k in vals for vals in TEMPLATE_GROUPS.values())]
        title = "📦 بدون بخش"
        enabled = False
    else:
        keys = [k for k in TEMPLATE_GROUPS.get(g, []) if k in ALL_TEXTS]
        title = "📁 " + section_title(g)
        enabled = section_enabled(g)
    buttons=[]
    buttons.append([InlineKeyboardButton("🏷️ تنظیم عنوان همه", callback_data=f"titles:{g}")])
    if g != "__ungrouped__" and not keys:
        buttons.append([InlineKeyboardButton("📭 این بخش فعلاً خالی است", callback_data="noop")])
    if g != "__ungrouped__":
        buttons.append([InlineKeyboardButton(f"{'⛔ خاموش کردن' if enabled else '⚡ روشن کردن'} پست سریع این بخش", callback_data=f"quicktoggle:{g}")])
    if g != "__ungrouped__":
        buttons.append([InlineKeyboardButton("➕ افزودن متن به این بخش", callback_data=f"section_add_text:{g}")])
        buttons.append([InlineKeyboardButton("📦 افزودن قالب موجود به این بخش", callback_data=f"section_add_template:{g}")])
    else:
        buttons.append([InlineKeyboardButton("➕ افزودن متن", callback_data="tmpl_add")])
    for k in keys:
        buttons.append([InlineKeyboardButton(f"📝 {k}", callback_data=f"use:{k}")])
        buttons.append([InlineKeyboardButton(f"🗑 حذف {k}", callback_data=f"del:{k}")])
    buttons.append([InlineKeyboardButton("🗑 حذف بخش", callback_data=f"section_del:{g}")]) if g != "__ungrouped__" else None
    buttons.append([InlineKeyboardButton("🔙 لیست بخش‌ها", callback_data="back_list")])
    await q.edit_message_text(title + f"\n\n📦 {len(keys)} قالب", reply_markup=InlineKeyboardMarkup(buttons))

async def titles_all_start(q, context, g):
    keys = [k for k in TEMPLATE_GROUPS.get(g, []) if k in ALL_TEXTS]
    context.user_data.clear()
    context.user_data["state"] = "titles_all"
    context.user_data["titles_group"] = g
    context.user_data["titles_keys"] = keys
    msg = (
        f"🏷️ تنظیم عنوان همه — {section_title(g)}\n\n"
        "عنوان‌های جدید را به ترتیب در یک پیام بفرست.\n"
        "هر خط = عنوان یک قالب.\n\n"
        f"📦 این بخش {len(keys)} قالب دارد.\n"
        "اگر ۶ خط بفرستی فقط عنوان ۶ قالب اول تغییر می‌کند؛ بقیه دست‌نخورده می‌مانند.\n\n"
        "برای لغو: لغو"
    )
    await q.edit_message_text(msg)

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
        await q.edit_message_text(f"⚠️ حذف پوشه «{section_title(g)}»؟\n\nخود قالب‌ها حذف نمی‌شوند و فقط پوشه و ارتباط قالب‌ها با آن حذف می‌شود.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ بله، حذف کن", callback_data="final_del_yes")],[InlineKeyboardButton("❌ لغو", callback_data="final_del_menu")]]))
        return

    if data == "final_del_yes":
        if not has_permission(uid, "list"):
            await q.edit_message_text("⛔ اجازه نداری!"); return
        g = context.user_data.pop("final_delete_group", None)
        if not g or g not in TEMPLATE_GROUPS:
            await q.edit_message_text("❌ این پوشه دیگر وجود ندارد."); return
        del TEMPLATE_GROUPS[g]; SECTION_SETTINGS.pop(g, None)
        save_template_groups(); save_section_settings()
        await q.edit_message_text(f"✅ پوشه «{section_title(g)}» حذف شد.\nقالب‌های داخل آن حذف نشدند و به «بدون بخش» منتقل شدند.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📁 برگشت به نهایی", callback_data="final_back")]]))
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
        await q.edit_message_text(
            f"⚡ پست سریع فعال شد: {section_title(g)}\n\n"
            f"📦 {len(keys)} قالب در صف این بخش است.\n"
            "حالا فقط لینک‌ها را بفرست یا پیام‌های ربات دوم را Forward کن.\n"
            "هر لینک = یک پست.\n\n"
            "برای خروج: لغو"
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

    if data.startswith("titles:"):
        if not has_permission(uid, "list"):
            await q.edit_message_text("⛔ اجازه نداری!")
            return
        g = data[7:]
        if g == "__ungrouped__":
            await q.edit_message_text("❌ بخش‌بندی نشده‌ها قابل تنظیم عنوان گروهی نیستند. اول قالب‌ها را داخل یک بخش قرار بده.")
            return
        await titles_all_start(q, context, g)
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

    if data.startswith("section_del:"):
        if not has_permission(uid, "list"):
            await q.edit_message_text("⛔ اجازه نداری!")
            return
        g=data[len("section_del:"):]
        keys=[k for k in TEMPLATE_GROUPS.get(g,[]) if k in ALL_TEXTS]
        context.user_data.clear(); context.user_data["state"]="section_delete_confirm"; context.user_data["delete_group"]=g
        msg = (
            f"🗑 حذف بخش «{section_title(g)}»؟\n\n"
            "این کار خود قالب‌ها را حذف نمی‌کند؛ فقط بخش و ارتباطشان را حذف می‌کند.\n\n"
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
        if g in TEMPLATE_GROUPS:
            del TEMPLATE_GROUPS[g]
            save_template_groups()
        SECTION_SETTINGS.pop(g,None); save_section_settings()
        context.user_data.clear()
        await q.edit_message_text("✅ بخش حذف شد. قالب‌ها به «بدون بخش» منتقل شدند.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 لیست بخش‌ها", callback_data="back_list")]]))
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
            t = ALL_TEXTS[k]
            lw = t.get("linked_word", "")
            footer = t.get("footer", "")
            rh = t.get("random_headers", [])
            bq = t.get("blockquote", False)
            msg = f"✅ {k} فعال شد!\n\n🔗 {t.get('link_text', '')}"
            if lw:
                msg += f"\n🔵 لینک‌شده: {lw}"
            if footer:
                msg += f"\n📌 فوتر: {footer}"
            msg += f"\n💬 نقل‌قول: {'✅ روشن' if bq else '❌ خاموش'}"
            buttons = [
                [InlineKeyboardButton("✏️ ویرایش قالب", callback_data=f"edit:{k}"),
                 InlineKeyboardButton("📄 کپی قالب", callback_data=f"copy:{k}")],
                [InlineKeyboardButton("👀 پیش‌نمایش", callback_data=f"preview:{k}")],
                [InlineKeyboardButton("🗑️ حذف قالب", callback_data=f"del_confirm:{k}")],
                [InlineKeyboardButton("➕ افزودن متن رندوم", callback_data=f"rhadd:{k}")],
                [InlineKeyboardButton("📋 لیست متن‌های رندوم", callback_data=f"rhlist:{k}")],
                [InlineKeyboardButton(f"{'❌ خاموش' if bq else '✅ روشن'} نقل‌قول", callback_data=f"bq:{k}")],
                [InlineKeyboardButton("🔙 بازگشت", callback_data="back_list")]
            ]
            await q.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(buttons))
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
    elif data.startswith("bq:"):
        if not has_permission(uid, "list"):
            await q.edit_message_text("⛔ اجازه نداری!")
            return
        k = data[3:]
        if k in ALL_TEXTS:
            old = ALL_TEXTS[k].get("blockquote", False)
            ALL_TEXTS[k]["blockquote"] = not old
            save_texts()
            try:
                await q.answer(f"نقل‌قول {'روشن' if not old else 'خاموش'} شد!")
            except Exception:
                pass
            t = ALL_TEXTS[k]
            lw = t.get("linked_word", "")
            footer = t.get("footer", "")
            rh = t.get("random_headers", [])
            bq = t.get("blockquote", False)
            msg = f"✅ {k} فعال شد!\n\n🔗 {t.get('link_text', '')}"
            if lw:
                msg += f"\n🔵 لینک‌شده: {lw}"
            if footer:
                msg += f"\n📌 فوتر: {footer}"
            msg += f"\n💬 نقل‌قول: {'✅ روشن' if bq else '❌ خاموش'}"
            buttons = [
                [InlineKeyboardButton("➕ افزودن متن رندوم", callback_data=f"rhadd:{k}")],
                [InlineKeyboardButton("📋 لیست متن‌های رندوم", callback_data=f"rhlist:{k}")],
                [InlineKeyboardButton(f"{'❌ خاموش' if bq else '✅ روشن'} نقل‌قول", callback_data=f"bq:{k}")],
                [InlineKeyboardButton("🔙 بازگشت", callback_data="back_list")]
            ]
            await q.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(buttons))
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
            base = ALL_TEXTS.get(ACTIVE_KEY, {})

            # وقتی موضوع انتخاب می‌شود:
            # 1) متن موضوع، جای متن بالای پست قرار می‌گیرد.
            # 2) اولین خط link_text که متن قدیمیِ موضوع/عنوان است حذف می‌شود.
            # 3) بقیه متن لینک و فوتر دقیقاً حفظ می‌شوند.
            #
            # اگر link_text فقط یک خط داشته باشد، چیزی حذف نمی‌کنیم
            # تا «Download pack» و امثال آن از بین نرود.
            header = choose_topic_header(topic_name)
            link_text = base.get("link_text", "download")
            if "\n" in link_text:
                link_text = link_text.split("\n", 1)[1].lstrip("\n")
            linked_word = base.get("linked_word", "")
            if linked_word and linked_word not in link_text:
                linked_word = ""
            footer = base.get("footer", "")
            bq = base.get("blockquote", False)
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
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global ACTIVE_KEY
    text = update.message.text or ""
    state = context.user_data.get("state")
    uid = update.effective_user.id
    if not can_use_bot(uid):
        await update.message.reply_text("⛔ دسترسی نداری!")
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

    if state == "titles_all":
        if not has_permission(uid, "list"):
            context.user_data.clear(); await update.message.reply_text("⛔ اجازه نداری!", reply_markup=get_main_keyboard(uid)); return
        if text in {"لغو", "❌ لغو"}:
            context.user_data.clear(); await update.message.reply_text("❌ لغو شد.", reply_markup=get_main_keyboard(uid)); return
        g=context.user_data.get("titles_group")
        keys=context.user_data.get("titles_keys", [])
        titles=[line.strip() for line in text.splitlines() if line.strip()]
        changed=min(len(titles),len(keys))
        for i in range(changed):
            key = keys[i]
            item = ALL_TEXTS[key]
            new_title = titles[i]
            old_title = str(item.get("title", "") or "").strip()

            # The title is a replacement, not an extra line. If an older
            # title was accidentally stored inside link_text, remove it now.
            link_text = str(item.get("link_text", "") or "")
            if old_title and link_text:
                chunks = link_text.splitlines()
                while chunks and not chunks[0].strip():
                    chunks.pop(0)
                if chunks and chunks[0].strip() == old_title:
                    chunks.pop(0)
                    while chunks and not chunks[0].strip():
                        chunks.pop(0)
                    item["link_text"] = "\n".join(chunks)

            # IMPORTANT: this is a TOTAL replacement of the rendered title.
            # The new line becomes the only title source for this template.
            # Do not let an old header/random header/topic survive.
            item["title"] = new_title
            item["header"] = new_title
            item["random_headers"] = []
        save_texts(); context.user_data.clear()
        await update.message.reply_text(f"✅ {changed} عنوان تغییر کرد.\n📌 بقیه قالب‌ها دست‌نخورده ماندند.", reply_markup=get_main_keyboard(uid))
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
                    header, link_text, linked_word, result=build_post_result(url, key, topic_name=None)
                    add_post(uid, header, link_text, linked_word, url, result)
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
            # If this template has a manually assigned title, that title is
            # authoritative. Do NOT pass the automatic topic/category header
            # into the builder, otherwise the old topic header can appear
            # together with the custom title (or override it in older logic).
            custom_title = str(ALL_TEXTS.get(selected_template_key, {}).get("title", "") or "").strip()
            topic_name = None if custom_title else (mapped.get("topic_name") or mapped.get("category") or None)
            header, link_text, linked_word, result = build_post_result(
                url,
                selected_template_key,
                topic_name=topic_name
            )
            add_post(uid, header, link_text, linked_word, url, result)

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

        # A custom title replaces the topic title completely.
        custom_title = str(ALL_TEXTS.get(selected_template_key, {}).get("title", "") or "").strip()
        if custom_title:
            topic_name = None

        try:
            header, link_text, linked_word, result = build_post_result(
                url, selected_template_key, topic_name=topic_name
            )
            add_post(uid, header, link_text, linked_word, url, result)
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
    print(f"🔑 Link bridge key: {LINK_BRIDGE_KEY}")
    start_link_bridge_server()
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("backup", backup_cmd))
    app.add_handler(CommandHandler("add", add_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("ready", ready_cmd))
    app.add_handler(CommandHandler("myposts", my_posts_cmd))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    print("🤖 استارت شد...")
    app.run_polling()

if __name__ == "__main__":
    main()
