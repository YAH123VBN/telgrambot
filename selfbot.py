import json
import os
import re
import asyncio
import random
from html import escape
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from telegram.error import NetworkError

TOKEN = "8743722675:AAGkZAdXX7ufh3kWTvcIrsikXdkVbhsrXXA"
TEXTS_FILE = "saved_texts.json"
POSTS_FILE = "my_posts.json"
TOPICS_FILE = "topics.json"
LINK_REGEX = re.compile(r'(https?://\S+|t\.me/\S+)', re.IGNORECASE)

# ── regex برای پیدا کردن تگ‌های HTML ──
_HTML_TAG_RE = re.compile(r'(<[^>]+>)')

def preserve_spaces(text):
    """spaceهای معمولی رو توی متن (نه داخل تگ HTML) به &nbsp; تبدیل می‌کنه"""
    parts = _HTML_TAG_RE.split(text)
    result = []
    for part in parts:
        if part and part.startswith('<'):
            result.append(part)
        else:
            result.append(part.replace(' ', '&nbsp;'))
    return ''.join(parts)

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["🎯 آماده‌سازی", "📋 لیست متن‌ها"],
        ["➕ افزودن متن", "📁 پست‌های من"],
        ["❓ راهنما"]
    ],
    resize_keyboard=True
)

MULTI_POST_KEYBOARD = ReplyKeyboardMarkup(
    [["✅ تمام", "❌ لغو"]],
    resize_keyboard=True
)

# ── دیکشنری سراسری برای قفل کاربران ──
_USER_LOCKS = {}

def load_json(path, default):
    if not os.path.exists(path):
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(default, f, ensure_ascii=False, indent=2)
        return default
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)

def save_json(path, data):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

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

# ═══════════════════════════════════════════════════
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 سلام!\n\n"
        "🎯 آماده‌سازی → یه قالب انتخاب کن، بعد فقط لینک بده\n"
        "📋 لیست متن‌ها → مدیریت قالب‌ها + متن‌های رندوم\n"
        "➕ افزودن متن → ساخت قالب جدید\n"
        "📁 پست‌های من → همه پست‌ها\n\n"
        "💡 نکته: وقتی قالبی رو با 🎯 آماده‌سازی انتخاب کنی،\n"
        "هر لینکی بدی همونجوری پست می‌سازه!",
        reply_markup=MAIN_KEYBOARD
    )

async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text or ""
    m = re.match(r'/add\s+(\S+)\s+(.*)', raw, re.DOTALL)
    if not m:
        await update.message.reply_text(
            "❌ /add نام | متن-پایین | کلمه-لینک\n\n"
            "مثال:\n/add p2 DownLoad مشاهده 📥 | مشاهده",
            reply_markup=MAIN_KEYBOARD
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
    await update.message.reply_text(msg, reply_markup=MAIN_KEYBOARD)

async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ALL_TEXTS:
        text = "📭 خالی!"
        if update.message:
            await update.message.reply_text(text, reply_markup=MAIN_KEYBOARD)
        else:
            await update.callback_query.edit_message_text(text, reply_markup=MAIN_KEYBOARD)
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
    if not ALL_TEXTS:
        await update.message.reply_text("📭 اول یه قالب بساز!", reply_markup=MAIN_KEYBOARD)
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
    user_id = update.effective_user.id
    posts = get_posts(user_id)
    total = len(posts)
    if not posts:
        await update.message.reply_text("📭 هنوز پستی نساختی!", reply_markup=MAIN_KEYBOARD)
        return
    await update.message.reply_text(f"📁 در حال ارسال {total} پست...", reply_markup=MAIN_KEYBOARD)
    sent = 0
    failed = 0
    for post in posts:
        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=post["result"],
                parse_mode='HTML',
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
    await update.message.reply_text(msg, reply_markup=MAIN_KEYBOARD)

# ═══════════════════════════════════════════════════
async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global ACTIVE_KEY
    q = update.callback_query
    await q.answer()
    data = q.data

    if data.startswith("use:"):
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
        k = data[3:]
        if k in ALL_TEXTS:
            old = ALL_TEXTS[k].get("blockquote", False)
            ALL_TEXTS[k]["blockquote"] = not old
            save_texts()
            try:
                await q.answer(f"نقل‌قول {'روشن' if not old else 'خاموش'} شد!")
            except Exception:
                pass
            # ⬇️ دوباره منوی use رو بساز بدون صدا زدن callback
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
        k = data[6:]
        context.user_data["rh_template"] = k
        context.user_data["state"] = "waiting_random_header"
        await q.edit_message_text(
            f"🎲 متن رندوم جدید برای {k}:\n\n"
            f"متنی که می‌خوای بالای پست بیاد (تصادفی) رو بفرست:\n\n"
            f"(برای لغو بنویس: لغو)"
        )

    elif data.startswith("rhlist:"):
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

    # ── انتخاب موضوع ──
    elif data.startswith("ts:"):
        uid = update.effective_user.id

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
                # ⬇️ فقط کلمه لینک‌شده رو نقل‌قول کن
                link_anchor = f"<a href=\"{url}\">{escape(linked_word)}</a>"
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
                await q.edit_message_text(result, parse_mode='HTML', disable_web_page_preview=True)
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

    # ── افزودن موضوع جدید ──
    elif data == "topic_add":
        context.user_data["state"] = "waiting_topic_name"
        await q.edit_message_text(
            "➕ افزودن موضوع جدید:\n\n"
            "یه اسم برای موضوع بذار:\n"
            "(مثلاً: وطنی چهره دار، کاسپلی)\n\n"
            "برای لغو بنویس: لغو"
        )

    # ── منوی حذف موضوع ──
    elif data == "topic_del_menu":
        if not TOPICS:
            await q.edit_message_text("📭 هیچ موضوعی نیست!")
            return
        topic_names = sorted(TOPICS.keys())
        buttons = []
        for i, name in enumerate(topic_names):
            buttons.append([InlineKeyboardButton(f"🗑 {name}", callback_data=f"td:{i}")])
        buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data="topic_back")])
        await q.edit_message_text("🗑 کدوم موضوع رو می‌خوای حذف کنی؟", reply_markup=InlineKeyboardMarkup(buttons))

    # ── حذف موضوع ──
    elif data.startswith("td:"):
        idx = int(data[3:])
        topic_names = sorted(TOPICS.keys())
        if idx < 0 or idx >= len(topic_names):
            await q.answer("❌ خطا!")
            return
        name = topic_names[idx]
        del TOPICS[name]
        save_texts()
        await q.edit_message_text(f"🗑 موضوع {name} حذف شد!")

    # ── برگشت به لیست موضوعات ──
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
            f"🎬 حالا موضوع رو انتخاب کن:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

# ═══════════════════════════════════════════════════
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global ACTIVE_KEY
    text = update.message.text or ""
    state = context.user_data.get("state")

    # خروج از چندپستی
    if text in ["تمام", "✅ تمام"] and context.user_data.get("ready_mode"):
        context.user_data.pop("ready_mode", None)
        context.user_data.pop("pending_url", None)
        await update.message.reply_text("✅ آماده‌سازی تمام شد.", reply_markup=MAIN_KEYBOARD)
        return

    if text in ["لغو", "❌ لغو"]:
        context.user_data.clear()
        await update.message.reply_text("❌ لغو شد.", reply_markup=MAIN_KEYBOARD)
        return

    if state == "waiting_random_header":
        k = context.user_data.get("rh_template")
        if not k or k not in ALL_TEXTS:
            await update.message.reply_text("❌ خطا! دوباره شروع کن.", reply_markup=MAIN_KEYBOARD)
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
            reply_markup=MAIN_KEYBOARD
        )
        context.user_data.clear()
        return

    if state == "waiting_topic_name":
        context.user_data["topic_name"] = text
        context.user_data["state"] = "waiting_topic_text"
        await update.message.reply_text(
            f"✅ اسم موضوع: {text}\n\n"
            f"حالا متن کامل موضوع رو بفرست:\n"
            f"(این متن بالای پست میاد)\n\n"
            f"برای لغو بنویس: لغو"
        )
        return

    if state == "waiting_topic_text":
        name = context.user_data.get("topic_name", "")
        if not name:
            await update.message.reply_text("❌ خطا! دوباره شروع کن.", reply_markup=MAIN_KEYBOARD)
            context.user_data.clear()
            return
        existed = name in TOPICS
        TOPICS[name] = text
        save_texts()
        if context.user_data.get("ready_mode"):
            await update.message.reply_text(
                f"✅ موضوع {name} {'جایگزین شد' if existed else 'اضافه شد'}!\n\n"
                f"📝 {text}\n\n"
                f"لینک رو بده یا بنویس تمام:",
                reply_markup=MULTI_POST_KEYBOARD
            )
            context.user_data.pop("state", None)
            context.user_data.pop("topic_name", None)
        else:
            await update.message.reply_text(
                f"✅ موضوع {name} {'جایگزین شد' if existed else 'اضافه شد'}!\n\n"
                f"📝 {text}\n\n"
                f"حالا از 🎯 آماده‌سازی می‌تونی استفاده کنی.",
                reply_markup=MAIN_KEYBOARD
            )
            context.user_data.clear()
        return

    if state == "waiting_link_text":
        context.user_data["temp_link_text"] = text
        context.user_data["state"] = "waiting_linked_word"
        await update.message.reply_text(
            f"✅ متن پایین دریافت شد.\n\n"
            f"حالا بگو کدوم کلمه لینک بشه:\n\n{text}\n\n"
            f"(برای لغو بنویس: لغو)"
        )
        return

    if state == "waiting_linked_word":
        link_text = context.user_data.get("temp_link_text", "")
        if text not in link_text:
            await update.message.reply_text(
                f"❌ کلمه {text} توی متن پیدا نشد!\n\n"
                f"متن: {link_text}\n\n"
                f"دوباره بگو:\n(برای لغو بنویس: لغو)"
            )
            return
        context.user_data["temp_linked_word"] = text
        context.user_data["state"] = "waiting_name"
        await update.message.reply_text(
            f"✅ کلمه لینک‌شده: {text}\n\n"
            f"حالا یه اسم برای این قالب بذار:\n"
            f"(مثلاً: p2, پک-جدید)\n\n"
            f"(برای لغو بنویس: لغو)"
        )
        return

    if state == "waiting_name":
        if text in ALL_TEXTS:
            await update.message.reply_text(
                f"❌ اسم {text} قبلاً هست! یه اسم دیگه:\n"
                f"(برای لغو بنویس: لغو)"
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
            f"🎲 حالا از 📋 لیست متن‌ها → {text} → ➕ افزودن متن رندوم\n"
            f"متن‌های بالای پست رو اضافه کن.",
            reply_markup=MAIN_KEYBOARD
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
        await update.message.reply_text("❓ لینک ندیدم!", reply_markup=MAIN_KEYBOARD)
        return
    url = matches[0]

    # ── آماده‌سازی فعال → لیست موضوعات ──
    if context.user_data.get("ready_mode"):
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
            f"🎬 حالا موضوع رو انتخاب کن:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    # ── حالت عادی → مستقیم بساز ──
    template = ALL_TEXTS.get(ACTIVE_KEY)
    if not template:
        await update.message.reply_text(
            "📭 اول یه قالب انتخاب کن!\n\n"
            "🎯 آماده‌سازی رو بزن و یه قالب انتخاب کن.",
            reply_markup=MAIN_KEYBOARD
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
        # ⬇️ فقط کلمه لینک‌شده رو نقل‌قول کن
        link_anchor = f"<a href=\"{url}\">{escape(linked_word)}</a>"
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
        await update.message.reply_text(result, parse_mode='HTML', disable_web_page_preview=True)
    except NetworkError:
        await update.message.reply_text(
            "⚠️ پست ساخته شد ولی به خاطر مشکل اینترنت نتونستم بفرستم.",
            reply_markup=MAIN_KEYBOARD
        )

# ═══════════════════════════════════════════════════
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
