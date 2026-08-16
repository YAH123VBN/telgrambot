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

TOKEN = os.getenv("TOKEN")
TEXTS_FILE = data_path("saved_texts.json")
POSTS_FILE = data_path("my_posts.json")
TOPICS_FILE = data_path("topics.json")
LINK_REGISTRY_FILE = data_path("link_registry.json")
TOPIC_VARIANTS_FILE = data_path("topic_variants.json")
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
        ["➕ افزودن متن", "📁 پست‌های من"],
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

ACTIVE_KEY = "p1"

def save_texts():
    save_json(TEXTS_FILE, ALL_TEXTS)
    save_json(TOPICS_FILE, TOPICS)


def normalize_link(url):
    """Normalize a Telegram/generated link so the bridge lookup is stable."""
    url = (url or "").strip()
    url = url.rstrip(".,!?؛،")
    if url.startswith("t.me/"):
        url = "https://" + url
    return url


def register_link_mapping(url, topic_name=None, topic_key=None, label=None, source="bridge"):
    """Persist a link -> topic mapping sent by the second bot."""
    url = normalize_link(url)
    if not url:
        raise ValueError("لینک خالی است")
    record = {
        "url": url,
        "topic_name": topic_name or "",
        "topic_key": topic_key or "",
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

    if topic_name is not None:
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

    files = [ADMINS_FILE, TEXTS_FILE, TOPICS_FILE, POSTS_FILE, LINK_REGISTRY_FILE, TOPIC_VARIANTS_FILE, LINK_BRIDGE_KEY_FILE]
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

async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not can_use_bot(uid):
        await update.message.reply_text("⛔ دسترسی نداری!")
        return
    if not has_permission(uid, "list"):
        await update.message.reply_text("⛔ اجازه دیدن لیست متن‌ها رو نداری!", reply_markup=get_main_keyboard(uid))
        return
    if not ALL_TEXTS:
        text = "📭 خالی!"
        if update.message:
            await update.message.reply_text(text, reply_markup=get_main_keyboard(uid))
        else:
            await update.callback_query.edit_message_text(text, reply_markup=get_main_keyboard(uid))
        return
    buttons = []
    for k in sorted(ALL_TEXTS.keys()):
        mark = "✅ " if k == ACTIVE_KEY else ""
        lt = ALL_TEXTS[k].get("link_text", "")[:15]
        rh_count = len(ALL_TEXTS[k].get("random_headers", []))
        buttons.append([InlineKeyboardButton(f"{mark}{k} — {lt} (🎲{rh_count})", callback_data=f"use:{k}")])
    del_row = [InlineKeyboardButton(f"🗑 {k}", callback_data=f"del:{k}") for k in sorted(ALL_TEXTS.keys())]
    for i in range(0, len(del_row), 3):
        buttons.append(del_row[i:i+3])
    text = "📋 انتخاب کن:"
    markup = InlineKeyboardMarkup(buttons)
    if update.message:
        await update.message.reply_text(text, reply_markup=markup)
    else:
        await update.callback_query.edit_message_text(text, reply_markup=markup)

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
async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global ACTIVE_KEY
    q = update.callback_query
    await q.answer()
    data = q.data
    uid = update.effective_user.id
    if not can_use_bot(uid):
        await q.edit_message_text("⛔ دسترسی نداری!")
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
                [InlineKeyboardButton("➕ افزودن متن رندوم", callback_data=f"rhadd:{k}")],
                [InlineKeyboardButton("📋 لیست متن‌های رندوم", callback_data=f"rhlist:{k}")],
                [InlineKeyboardButton(f"{'❌ خاموش' if bq else '✅ روشن'} نقل‌قول", callback_data=f"bq:{k}")],
                [InlineKeyboardButton("🔙 بازگشت", callback_data="back_list")]
            ]
            await q.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(buttons))
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

    elif data.startswith("del:"):
        if not has_permission(uid, "list"):
            await q.edit_message_text("⛔ اجازه نداری!")
            return
        k = data[4:]
        if k in ALL_TEXTS:
            del ALL_TEXTS[k]
            save_texts()
            if ACTIVE_KEY == k and ALL_TEXTS:
                ACTIVE_KEY = list(ALL_TEXTS.keys())[0]
            await q.edit_message_text(f"🗑 {k} حذف شد!")
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
            "blockquote": False
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
                "🎯 اول مشخص کن پست با موضوع باشه یا بدون موضوع.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🎬 با موضوع", callback_data="postmode:topic")],
                    [InlineKeyboardButton("🚫 بدون موضوع", callback_data="postmode:no_topic")]
                ])
            )
            return

        topic_name = context.user_data.get("selected_topic") if post_mode == "topic" else None
        if post_mode == "topic" and not topic_name:
            await update.message.reply_text("🎬 اول یک موضوع انتخاب کن.")
            return

        # In no-topic mode, automatically recover the topic previously registered
        # by the separate classifier bot. No extra marker is required in the URL.
        if post_mode == "no_topic":
            mapped = get_link_mapping(url)
            if mapped and mapped.get("topic_name"):
                topic_name = mapped.get("topic_name")

        if _USER_LOCKS.get(uid):
            await update.message.reply_text("⏳ قبلاً در حال پردازش یک لینک هستی.")
            return

        _USER_LOCKS[uid] = True
        try:
            header, link_text, linked_word, result = build_post_result(
                url,
                ACTIVE_KEY,
                topic_name=topic_name
            )
            add_post(uid, header, link_text, linked_word, url, result)

            await update.message.reply_text(
                result,
                parse_mode="HTML",
                disable_web_page_preview=True
            )
            await update.message.reply_text(
                "✅ پست ساخته شد!\n\n📝 لینک بعدی رو بده یا بنویس تمام:",
                reply_markup=MULTI_POST_KEYBOARD
            )
        except NetworkError:
            await update.message.reply_text(
                "⚠️ پست ساخته شد ولی به خاطر مشکل اینترنت نتونستم بفرستم.",
                reply_markup=MULTI_POST_KEYBOARD
            )
        except Exception as e:
            await update.message.reply_text(
                f"❌ خطا: {str(e)}",
                reply_markup=MULTI_POST_KEYBOARD
            )
        finally:
            _USER_LOCKS[uid] = False
        return

    template = ALL_TEXTS.get(ACTIVE_KEY)
    if not template:
        await update.message.reply_text(
            "📭 اول یه قالب انتخاب کن!\n\n"
            "🎯 آماده‌سازی رو بزن و یه قالب انتخاب کن.",
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
    print(f"📁 Data folder: {DATA_DIR}")
    print(f"📄 Texts file: {TEXTS_FILE} ({os.path.getsize(TEXTS_FILE) if os.path.exists(TEXTS_FILE) else 'new'})")
    print(f"📄 Topics file: {TOPICS_FILE} ({os.path.getsize(TOPICS_FILE) if os.path.exists(TOPICS_FILE) else 'new'})")
    print(f"📄 Admins file: {ADMINS_FILE} ({os.path.getsize(ADMINS_FILE) if os.path.exists(ADMINS_FILE) else 'new'})")
    print(f"📄 Posts file: {POSTS_FILE} ({os.path.getsize(POSTS_FILE) if os.path.exists(POSTS_FILE) else 'new'})")
    print(f"📄 Link registry: {LINK_REGISTRY_FILE} ({os.path.getsize(LINK_REGISTRY_FILE) if os.path.exists(LINK_REGISTRY_FILE) else 'new'})")
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
