import os, re, json, shutil, asyncio, subprocess, uuid
from pyrogram import Client, filters
from pyrogram.types import (ReplyKeyboardMarkup, InlineKeyboardMarkup,
                            InlineKeyboardButton)

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNERS = {int(x) for x in os.environ.get("OWNER_ID", "").replace(" ", "").split(",") if x.isdigit() and int(x)}  # چند آیدی با کاما؛ خالی = همه
DELOGO = os.environ.get("DELOGO", "")                   # x:y:w:h;x:y:w:h
MAX_TOTAL_MB = int(os.environ.get("MAX_TOTAL_MB", "500"))  # سقف جمع حجم صف
DISK_MB = int(os.environ.get("DISK_MB", "500"))
DISK_FACTOR = float(os.environ.get("DISK_FACTOR", "1.5"))  # ورودی + خروجی نسبت به حجم ویدیو          # حجم ولوم ریلوی
WAREHOUSE_CHAT = os.environ.get("WAREHOUSE_CHAT", "")   # اختیاری: چت/کانال انبار
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
TAG_FILE, STORE_FILE = "/tmp/tag.txt", "/tmp/warehouse.json"
AUTO_REMOVE = os.environ.get("AUTO_REMOVE", "1") == "1"  # حذف خودکار هر متن با OCR
OCR_EVERY = float(os.environ.get("OCR_EVERY", "0.5"))     # هر چند ثانیه یک بار OCR
OCR_WIDTH = int(os.environ.get("OCR_WIDTH", "1280"))
os.environ.setdefault("OMP_THREAD_LIMIT", "1")
MB = 1024 * 1024

B_SEND, B_STORE = "📤 ارسال ویدیو", "📦 انبار"
B_SETID, B_SHOWID = "🏷 تنظیم آیدی", "👁 آیدی فعلی"
B_QUEUE, B_CLEAR = "📋 صف", "🗑 پاک کردن صف"
KB = ReplyKeyboardMarkup(
    [[B_SEND, B_STORE], [B_SETID, B_SHOWID], [B_QUEUE, B_CLEAR]],
    resize_keyboard=True)

app = Client("bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, workdir="/tmp")
lock = asyncio.Lock()
queues, waiting_id = {}, set()
URL_RE = re.compile(r"https?://\S+")


# ---------- ابزارها ----------
def get_tag():
    try:
        t = open(TAG_FILE, encoding="utf-8").read().strip()
    except FileNotFoundError:
        t = ""
    return t or os.environ.get("TAG", "@mytag")


def load_store():
    try:
        return json.load(open(STORE_FILE))
    except Exception:
        return {}


def save_store(d):
    json.dump(d, open(STORE_FILE, "w"))


def run(cmd):
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def duration(path):
    out = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries",
                                   "format=duration", "-of", "csv=p=0", path])
    return float(out.decode().strip())


def url_size(url):
    try:
        out = subprocess.check_output(
            ["yt-dlp", "--skip-download", "--no-playlist", "--print",
             "%(filesize,filesize_approx)s", url], stderr=subprocess.DEVNULL, timeout=60)
        return int(float(out.decode().strip().splitlines()[0]))
    except Exception:
        return 0


def fmt(n):
    return f"{n / MB:.0f} MB"


def total(uid):
    return sum(i["size"] for i in queues.get(uid, []))


def build_vf(d):
    tag = get_tag()
    fl = []
    for box in filter(None, DELOGO.split(";")):
        x, y, w, h = box.split(":")
        fl.append(f"delogo=x={x}:y={y}:w={w}:h={h}")
    for s in [0, max(d / 2 - 2.5, 0), max(d - 5, 0)]:
        fl.append(
            f"drawtext=fontfile={FONT}:text='{tag}':fontsize=h/18:fontcolor=white:"
            f"borderw=2:bordercolor=black:x='(w-text_w)*(t-{s:.2f})/5':"
            f"y='h*0.8+20*sin(6*t)':enable='between(t,{s:.2f},{s + 5:.2f})'")
    return ",".join(fl)


ENC = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "28", "-c:a", "copy",
       "-map_metadata", "-1", "-movflags", "+faststart"]


def ocr_boxes(fr):
    import cv2, pytesseract
    from pytesseract import Output
    H, W = fr.shape[:2]
    sc = min(1.0, OCR_WIDTH / W)
    small = cv2.cvtColor(cv2.resize(fr, None, fx=sc, fy=sc), cv2.COLOR_BGR2GRAY)
    d = pytesseract.image_to_data(small, lang="eng+fas", config="--psm 11",
                                  output_type=Output.DICT)
    pad = max(6, int(W * 0.008))
    boxes = []
    for j, t in enumerate(d["text"]):
        try:
            conf = float(d["conf"][j])
        except ValueError:
            continue
        if conf >= 40 and len(t.strip()) >= 2:
            x, y = int(d["left"][j] / sc) - pad, int(d["top"][j] / sc) - pad
            w, h = int(d["width"][j] / sc) + 2 * pad, int(d["height"][j] / sc) + 2 * pad
            x0, y0 = max(x, 0), max(y, 0)
            boxes.append((x0, y0, min(x + w, W) - x0, min(y + h, H) - y0))
    return boxes


def detect(inp):
    import cv2
    cap = cv2.VideoCapture(inp)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    step = max(int(fps * OCR_EVERY), 1)
    samples, i = [], 0
    while cap.grab():
        if i % step == 0:
            ok, fr = cap.retrieve()
            samples.append(ocr_boxes(fr) if ok else [])
        i += 1
    cap.release()
    return samples, step, fps


def edit(inp, out):
    d = duration(inp)
    vf = build_vf(d)
    samples, step, fps = ([], 1, 30) if not AUTO_REMOVE else detect(inp)
    if not any(samples):  # تگی پیدا نشد یا حذف خودکار خاموش است
        return run(["ffmpeg", "-y", "-i", inp, "-vf", vf, *ENC, out])
    import cv2
    cap = cv2.VideoCapture(inp)
    ok, fr = cap.read()
    H, W = fr.shape[:2]
    p = subprocess.Popen(
        ["ffmpeg", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}",
         "-r", str(fps), "-i", "-", "-i", inp, "-map", "0:v", "-map", "1:a?",
         "-vf", vf, *ENC, "-shortest", out],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    i = 0
    while ok:
        k = i // step
        boxes = list(samples[min(k, len(samples) - 1)])
        if k + 1 < len(samples):
            boxes += samples[k + 1]  # اتحاد دو نمونه تا تگ متحرک را بپوشاند
        for (x, y, w, h) in boxes:
            if w <= 0 or h <= 0:
                continue
            m = 10
            x0, y0 = max(x - m, 0), max(y - m, 0)
            x1, y1 = min(x + w + m, W), min(y + h + m, H)
            roi = fr[y0:y1, x0:x1]
            mask = roi[:, :, 0] * 0
            mask[y - y0:y - y0 + h, x - x0:x - x0 + w] = 255
            fr[y0:y1, x0:x1] = cv2.inpaint(roi, mask, 3, cv2.INPAINT_TELEA)
        p.stdin.write(fr.tobytes())
        ok, fr = cap.read()
        i += 1
    cap.release()
    p.stdin.close()
    if p.wait() != 0:
        raise RuntimeError("ffmpeg ناموفق بود")


def allowed(_, __, m):
    return not OWNERS or (m.from_user and m.from_user.id in OWNERS)


ok = filters.create(allowed)
P = filters.private & ok


def btn(text):
    return filters.regex(f"^{re.escape(text)}$")


# ---------- صف ----------
async def add_item(msg, src, size):
    uid = msg.chat.id
    if total(uid) + size > MAX_TOTAL_MB * MB:
        return await msg.reply(
            f"❌ جمع صف می‌شود {fmt(total(uid) + size)} و از سقف {MAX_TOTAL_MB} MB بیشتر است.\n"
            f"اول «{B_SEND}» را بزن.", reply_markup=KB)
    queues.setdefault(uid, []).append({"msg": msg, "src": src, "size": size})
    n = len(queues[uid])
    unk = " (حجم لینک نامشخص)" if size == 0 else ""
    await msg.reply(f"✅ اضافه شد{unk}\nصف: {n} ویدیو | جمع حجم: {fmt(total(uid))} از {MAX_TOTAL_MB} MB",
                    reply_markup=KB)


@app.on_message(P & filters.command(["start", "menu"]))
async def start(client, msg):
    await msg.reply("ویدیو یا لینک بفرست تا به صف اضافه شود، بعد «📤 ارسال ویدیو» را بزن.",
                    reply_markup=KB)


@app.on_message(P & (filters.video | filters.document))
async def on_file(client, msg):
    f = msg.video or msg.document
    if msg.document and not (f.mime_type or "").startswith("video"):
        return await msg.reply("فقط ویدیو قبول می‌کنم.", reply_markup=KB)
    await add_item(msg, None, f.file_size or 0)


@app.on_message(P & btn(B_QUEUE))
async def show_queue(client, msg):
    uid = msg.chat.id
    q = queues.get(uid, [])
    if not q:
        return await msg.reply("صف خالی است.", reply_markup=KB)
    lines = [f"{i}. {fmt(it['size']) if it['size'] else '؟'} - {'لینک' if it['src'] else 'فایل'}"
             for i, it in enumerate(q, 1)]
    await msg.reply("\n".join(lines) + f"\n\nجمع: {fmt(total(uid))} از {MAX_TOTAL_MB} MB",
                    reply_markup=KB)


@app.on_message(P & btn(B_CLEAR))
async def clear_queue(client, msg):
    queues[msg.chat.id] = []
    await msg.reply("🗑 صف پاک شد.", reply_markup=KB)


# ---------- آیدی ----------
@app.on_message(P & btn(B_SHOWID))
async def show_id(client, msg):
    await msg.reply(f"🏷 آیدی فعلی: {get_tag()}", reply_markup=KB)


@app.on_message(P & btn(B_SETID))
async def ask_id(client, msg):
    waiting_id.add(msg.chat.id)
    await msg.reply("آیدی جدید را بفرست (مثلاً @mychannel):")


# ---------- انبار ----------
@app.on_message(P & btn(B_STORE))
async def store(client, msg):
    ids = load_store().get(str(msg.chat.id), [])
    if not ids:
        return await msg.reply("📦 انبار خالی است.", reply_markup=KB)
    await msg.reply(f"📦 {len(ids)} ویدیو در انبار:", reply_markup=KB)
    for fid in ids:
        await client.send_video(msg.chat.id, fid)
    await msg.reply("تمام شد.", reply_markup=InlineKeyboardMarkup(
        [[InlineKeyboardButton("🗑 خالی کردن انبار", callback_data="clear_store")]]))


@app.on_callback_query(filters.regex("^clear_store$"))
async def clear_store(client, cq):
    d = load_store()
    d[str(cq.message.chat.id)] = []
    save_store(d)
    await cq.answer("انبار خالی شد")
    await cq.message.edit("🗑 انبار خالی شد.")


# ---------- پردازش ----------
@app.on_message(P & btn(B_SEND))
async def send_all(client, msg):
    uid = msg.chat.id
    items = queues.get(uid, [])
    if not items:
        return await msg.reply("صف خالی است. اول ویدیو بفرست.", reply_markup=KB)
    if lock.locked():
        return await msg.reply("⏳ یک پردازش در حال انجام است.", reply_markup=KB)
    queues[uid] = []
    done = 0
    async with lock:
        for i, it in enumerate(items, 1):
            head = f"[{i}/{len(items)}] "
            st = await msg.reply(head + "⏳ دانلود...")
            name = uuid.uuid4().hex[:8]
            inp, out = f"/tmp/{name}_in.mp4", f"/tmp/{name}_out.mp4"
            try:
                need = it["size"] * DISK_FACTOR
                free = shutil.disk_usage("/tmp").free
                if need > DISK_MB * MB or need > free:
                    raise RuntimeError(
                        f"فضا کافی نیست: حجم ویدیو {fmt(it['size'])}، "
                        f"نیاز {fmt(need)}، سقف {DISK_MB} MB، آزاد {fmt(free)}")
                if it["src"]:
                    await asyncio.to_thread(run, ["yt-dlp", "-f", "best[ext=mp4]/best",
                                                  "--merge-output-format", "mp4",
                                                  "-o", inp, it["src"]])
                else:
                    await it["msg"].download(file_name=inp)
                await st.edit(head + "🧹 پاک کردن تگ‌ها و ویرایش (کند است)...")
                await asyncio.to_thread(edit, inp, out)
                os.remove(inp)
                await st.edit(head + "📤 ارسال...")
                sent = await client.send_video(uid, out, supports_streaming=True)
                d = load_store()
                d.setdefault(str(uid), []).append(sent.video.file_id)
                save_store(d)
                if WAREHOUSE_CHAT:
                    await client.send_video(WAREHOUSE_CHAT, sent.video.file_id)
                done += 1
                await st.delete()
            except Exception as e:
                await st.edit(head + f"❌ {str(e)[:150]}")
            finally:
                for f in (inp, out):
                    if os.path.exists(f):
                        os.remove(f)
    await msg.reply(f"✅ {done} از {len(items)} ویدیو آماده شد و در 📦 انبار ذخیره شد.",
                    reply_markup=KB)


# ---------- متن آزاد: آیدی یا لینک ----------
@app.on_message(P & filters.text)
async def on_text(client, msg):
    uid = msg.chat.id
    if uid in waiting_id:
        waiting_id.discard(uid)
        t = re.sub(r"['\":\\%]", "", msg.text).strip()
        open(TAG_FILE, "w", encoding="utf-8").write(t)
        return await msg.reply(f"✅ آیدی ذخیره شد: {t}", reply_markup=KB)
    for u in URL_RE.findall(msg.text):
        size = await asyncio.to_thread(url_size, u)
        await add_item(msg, u, size)


app.run()
