import json
import os
import re
import asyncio
import random
from html import escape
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from telegram.error import NetworkError

TOKEN = os.getenv("TOKEN")
TEXTS_FILE = "saved_texts.json"
POSTS_FILE = "my_posts.json"
TOPICS_FILE = "topics.json"
ADMINS_FILE = "admins.json"
LINK_REGEX = re.compile(r'(https?://\S+|t\.me/\S+)', re.IGNORECASE)

OWNER_ID = 8361990555

_HTML_TAG_RE = re.compile(r'(<[^>]+>)')

def preserve_spaces(text):
    parts = _HTML_TAG_RE.split(text)
    result = []
    for part in parts:
        if part and part.startswith("<"):
            result.append(part)
        else:
            result.append(part.replace(" ", "&nbsp;"))
    return "".join(result)

# ==================================================
# Admin System
# ==================================================

def load_json(path, default):
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(default, f, ensure_ascii=False, indent=2)
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

ADMINS = load_json(ADMINS_FILE, {})

ALL_PERMISSIONS = [
    "ready",
    "list",
    "add_text",
    "my_posts",
    "manage_topics",
    "manage_admins"
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

# ==================================================
# Keyboards
# ==================================================

def get_main_keyboard(user_id: int):
    buttons = [
        ["🎯 آماده‌سازی", "📋 لیست متن‌ها"],
        ["➕ افزودن متن", "📁 پست‌های من"],
        ["❓ راهنما"]
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

ACTIVE_KEY = "p1"

def save_texts():
    save_json(TEXTS_FILE, ALL_TEXTS)
    save_json(TOPICS_FILE, TOPICS)

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

# ==================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not can_use_bot(uid):
        await update.message.reply_text("⛔ دسترسی نداری!")
        return
    await update.message.reply_text(
        "👋 سلام!\n\n"
        "🎯 آماده‌سازی → یه قالب انتخاب کن، بعد فقط لینک بده\n"
        "📋 لیست متن‌ها → مدیریت قالب‌ها + متن‌های رندوم\n"
        "➕ افزودن متن → ساخت قالب جدید\n"
        "📁 پست‌های من → همه پست‌ها\n\n"
        "💡 نکته: وقتی قالبی رو با 🎯 آماده‌سازی انتخاب کنی،\n"
        "هر لینکی بدی همونجوری پست می‌سازه!",
        reply_markup=get_main_keyboard(uid)
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
        "👆 انتخاب کن، بعد فقط لینک بده!",
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

# ==================================================
# Admin Management Commands
# ==================================================

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

# ==================================================
async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global ACTIVE_KEY
    q = update.callback_query
    await q.answer()
    data = q.data
    uid = update.effective_user.id
    if not can_use_bot(uid):
        await q.edit_message_text("⛔ دسترسی نداری!")
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
            t = ALL_TEXTS[k]
            lw = t.get("linked_word", "")
            footer = t.get("footer", "")
            rh = t.get("random_headers", [])
            bq = t.get("blockquote", False)
            rh_count = len(rh)
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
            msg += "\n✅ حالا فقط لینک بده، من پست می‌سازم!"
            await q.edit_message_text(msg)
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="⬇️ لینک رو بده یا بنویس تمام:",
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
            header = TOPICS[topic_name]
            link_text = base.get("link_text", "download")
            linked_word = base.get("linked_word", "")
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
        await q.edit_message_text(
            "➕ افزودن موضوع جدید:\n\n"
            "یه اسم برای موضوع بذار:\n"
            "(مثلاً: وطنی چهره دار، کاسپلی)\n\n"
            "برای لغو بنویس: لغو"
        )
    elif data == "topic_del_menu":
        if not has_permission(uid, "manage_topics"):
            await q.edit_message_text("⛔ اجازه مدیریت موضوعات رو نداری!")
            return
        if not TOPICS:
            await q.edit_message_text("📭 هیچ موضوعی نیست!")
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
        idx = int(data[3:])
        topic_names = sorted(TOPICS.keys())
        if idx < 0 or idx >= len(topic_names):
            await q.answer("❌ خطا!")
            return
        name = topic_names[idx]
        del TOPICS[name]
        save_texts()
        await q.edit_message_text(f"🗑 موضوع {name} حذف شد!")
    elif data == "topic_back":
        url = context.user_data.get("pending_url", "")
        if not url:
            await q.edit_message_text("❌ خطا!")
            return
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
        for p in ALL_PERMISSIONS:
            mark = "✅" if p in perms else "❌"
            row.append(InlineKeyboardButton(f"{mark} {p}", callback_data=f"adm_perm:{target_id}:{p}"))
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
    buttons.append([InlineKeyboardButton(f"{'❌ خاموش' if fa else '✅ روشن'} دسترسی کامل", callback_data=f"adm_full:{target_id}")])
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data="admin_back")])
    await q.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(buttons))

# ==================================================
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
            await update.message.reply_text(
                f"✅ موضوع {name} {'جایگزین شد' if existed else 'اضافه شد'}!\n\n"
                f"📝 {text}\n\n"
                "لینک رو بده یا بنویس تمام:",
                reply_markup=MULTI_POST_KEYBOARD
            )
            context.user_data.pop("state", None)
            context.user_data.pop("topic_name", None)
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
            "🎲 حالا از 📋 لیست متن‌ها → {text} → ➕ افزودن متن رندوم\n"
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
    if text == "❓ راهنما":
        await start(update, context)
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
            await update.message.reply_text("⛔ اجازه ساخت پست رو نداری!", reply_markup=get_main_keyboard(uid))
            return
        context.user_data["pending_url"] = url
        topic_names = sorted(TOPICS.keys())
        buttons = []
        for idx, name in enumerate(topic_names):
            buttons.append([InlineKeyboardButton(f"{name}", callback_data=f"ts:{idx}")])
        buttons.append([
            InlineKeyboardButton("➕ افزودن موضوع", callback_data="topic_add"),
            InlineKeyboardButton("🗑 حذف موضوع", callback_data="topic_del_menu")
        ])
        await update.message.reply_text(
            f"🔗 لینک دریافت شد!\n\n"
            "🎬 حالا موضوع رو انتخاب کن:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
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

# ==================================================
def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
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
