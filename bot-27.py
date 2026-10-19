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
THUMB_FILE = "/tmp/thumb.jpg"
STATIC_TAG = os.environ.get("STATIC_TAG", "1") == "1"      # تگ ثابت کوچک در تمام مدت ویدیو
STATIC_POS = os.environ.get("STATIC_POS", "tr")            # tr tl br bl
AUTO_REMOVE = os.environ.get("AUTO_REMOVE", "1") == "1"  # حذف خودکار هر متن با OCR
OCR_EVERY = float(os.environ.get("OCR_EVERY", "0.5"))     # هر چند ثانیه یک بار OCR
OCR_WIDTH = int(os.environ.get("OCR_WIDTH", "960"))
OCR_LANG = os.environ.get("OCR_LANG", "eng+fas")
OCR_MODES = os.environ.get("OCR_MODES", "white").split(",")  # white و/یا gray
PROMO_WORDS = [w.strip().lower() for w in os.environ.get(
    "PROMO_WORDS", "kos365,تلگرام,چنل,کانال,telegram").split(",") if w.strip()]
CUT_OUTRO = os.environ.get("CUT_OUTRO", "1") == "1"            # حذف کارت تبلیغاتی آخر ویدیو
OUTRO_MAX_SEC = float(os.environ.get("OUTRO_MAX_SEC", "12"))   # حداکثر ثانیه‌ای که از آخر بریده می‌شود
MAX_SAMPLES = int(os.environ.get("MAX_SAMPLES", "60"))  # حداکثر دفعات OCR برای هر ویدیو
os.environ.setdefault("OMP_THREAD_LIMIT", "1")
MB = 1024 * 1024

B_SEND, B_STORE = "📤 ارسال ویدیو", "📦 انبار"
B_SETID, B_SHOWID = "🏷 تنظیم آیدی", "👁 آیدی فعلی"
B_QUEUE, B_CLEAR = "📋 صف", "🗑 پاک کردن صف"
B_THUMB = "🖼 تامبنیل"
KB = ReplyKeyboardMarkup(
    [[B_SEND, B_STORE], [B_SETID, B_SHOWID], [B_THUMB, B_QUEUE], [B_CLEAR]],
    resize_keyboard=True)

app = Client("bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, workdir="/tmp")
lock = asyncio.Lock()
queues, waiting_id, waiting_thumb = {}, set(), set()
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


def probe(path):
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height:format=duration", "-of", "csv=p=0", path]).decode()
    lines = out.strip().splitlines()
    w, h = [int(v) for v in lines[0].split(",")[:2]]
    return w, h, int(float(lines[1]))


def auto_thumb(video, dst):
    try:
        run(["ffmpeg", "-y", "-ss", "1", "-i", video, "-frames:v", "1",
             "-vf", "scale=320:-2", dst])
        return dst if os.path.exists(dst) else None
    except Exception:
        return None


def save_thumb(src):
    from PIL import Image
    img = Image.open(src).convert("RGB")
    img.thumbnail((320, 320))
    for q in (90, 80, 70, 55):
        img.save(THUMB_FILE, "JPEG", quality=q)
        if os.path.getsize(THUMB_FILE) < 190 * 1024:
            break


def build_vf(d):
    tag = get_tag()
    fl = []
    for box in filter(None, DELOGO.split(";")):
        x, y, w, h = box.split(":")
        fl.append(f"delogo=x={x}:y={y}:w={w}:h={h}")
    if STATIC_TAG:
        pos = {"tr": "x='w-text_w-w*0.03':y='h*0.04'", "tl": "x='w*0.03':y='h*0.04'",
               "br": "x='w-text_w-w*0.03':y='h*0.9'", "bl": "x='w*0.03':y='h*0.9'"}.get(
            STATIC_POS, "x='w-text_w-w*0.03':y='h*0.04'")
        fl.append(f"drawtext=fontfile={FONT}:text='{tag}':fontsize=h/40:"
                  f"fontcolor=white@0.75:borderw=1:bordercolor=black@0.7:{pos}")
    starts = []
    for c in [0, max(d / 2 - 2.5, 0), max(d - 5, 0)]:
        if not starts or c >= starts[-1] + 5.5:  # در ویدیوی کوتاه تگ‌ها روی هم نیفتند
            starts.append(c)
    for s in starts:
        fl.append(
            f"drawtext=fontfile={FONT}:text='{tag}':fontsize=h/18:fontcolor=white:"
            f"borderw=2:bordercolor=black:x='(w-text_w)*(t-{s:.2f})/5':"
            f"y='h*0.8+20*sin(6*t)':enable='between(t,{s:.2f},{s + 5:.2f})'")
    return ",".join(fl)


ENC = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "28", "-c:a", "copy",
       "-map_metadata", "-1", "-movflags", "+faststart"]


def is_promo(t):
    t = t.lower()
    return "@" in t or "t.me" in t or any(w in t for w in PROMO_WORDS)


def ocr_words(fr):
    import cv2, numpy as np, pytesseract
    from pytesseract import Output
    H, W = fr.shape[:2]
    sc = min(1.0, OCR_WIDTH / W)
    small = cv2.resize(fr, None, fx=sc, fy=sc)
    imgs = []
    if "white" in OCR_MODES:  # تگ‌ها معمولاً سفیدند: فقط پیکسل‌های سفید را نگه می‌داریم
        hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
        white = (hsv[:, :, 1] < 70) & (hsv[:, :, 2] > 190)
        imgs.append(255 - white.astype(np.uint8) * 255)
    if "gray" in OCR_MODES:
        imgs.append(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY))
    words = []
    for img in imgs:
        d = pytesseract.image_to_data(img, lang=OCR_LANG, config="--psm 11",
                                      output_type=Output.DICT)
        for j, t in enumerate(d["text"]):
            t = t.strip()
            try:
                conf = float(d["conf"][j])
            except ValueError:
                continue
            if conf >= 60 and len(t) >= 2 and sum(c.isalnum() for c in t) >= 2:
                words.append((int(d["left"][j] / sc), int(d["top"][j] / sc),
                              int(d["width"][j] / sc), int(d["height"][j] / sc), t))
    return words


def outro_like(words, H):
    promo = [w for w in words if is_promo(w[4])]
    if not promo:
        return False
    hashes = sum(1 for w in words if w[4].startswith("#"))
    return hashes >= 2 or any(w[3] >= 0.04 * H for w in promo)


def find_outro(inp, fps, n):
    """اگر ته ویدیو کارت تبلیغاتی باشد، شماره فریمی که باید از آنجا برید برمی‌گرداند."""
    import cv2
    cap = cv2.VideoCapture(inp)

    def at(idx):
        cap.set(cv2.CAP_PROP_POS_FRAMES, max(idx, 0))
        ok, fr = cap.read()
        return fr if ok else None

    last = n - max(int(fps * 0.4), 3)
    fr = at(last)
    if fr is None or not outro_like(ocr_words(fr), fr.shape[0]):
        cap.release()
        return n
    cut, step = last, max(int(fps * 0.5), 1)
    limit = max(n - int(fps * OUTRO_MAX_SEC), n // 2)
    idx = last - step
    while idx > limit:
        fr = at(idx)
        if fr is None or not outro_like(ocr_words(fr), fr.shape[0]):
            break
        cut = idx
        idx -= step
    cap.release()
    return max(cut - int(fps * 0.15), 1)


def cluster(rects):
    rs = [list(r) for r in rects]
    again = True
    while again:
        again = False
        for i in range(len(rs)):
            for j in range(i + 1, len(rs)):
                a, b = rs[i], rs[j]
                g = int(max(a[3], b[3]) * 0.8)
                if (a[0] - g <= b[0] + b[2] and b[0] - g <= a[0] + a[2] and
                        a[1] - g <= b[1] + b[3] and b[1] - g <= a[1] + a[3]):
                    x0, y0 = min(a[0], b[0]), min(a[1], b[1])
                    x1 = max(a[0] + a[2], b[0] + b[2])
                    y1 = max(a[1] + a[3], b[1] + b[3])
                    rs[i] = [x0, y0, x1 - x0, y1 - y0]
                    rs.pop(j)
                    again = True
                    break
            if again:
                break
    return [tuple(r) for r in rs]


def banner_box(fr, r):
    """مستطیل پس‌زمینه بنر تبلیغاتی دور متن و رنگ غالبش را پیدا می‌کند."""
    import cv2, numpy as np
    H, W = fr.shape[:2]
    x, y, w, h = r
    pad = max(4, int(h * 0.15))
    tx0, ty0, tx1, ty1 = max(x - pad, 0), max(y - pad, 0), min(x + w + pad, W), min(y + h + pad, H)
    px = fr[ty0:ty1, tx0:tx1].reshape(-1, 3)
    q = (px // 32).astype(int)
    keys = q[:, 0] * 64 + q[:, 1] * 8 + q[:, 2]
    mode = np.bincount(keys).argmax()
    col = px[keys == mode].mean(axis=0)
    fb = (max(x - pad, 0), max(y - pad, 0), min(x + w + pad, W), min(y + h + pad, H))
    mx, my = int(w * 0.4), int(h * 0.8)
    ex0, ey0, ex1, ey1 = max(x - mx, 0), max(y - my, 0), min(x + w + mx, W), min(y + h + my, H)
    roi = fr[ey0:ey1, ex0:ex1].astype(int)
    m = (np.abs(roi - col).max(axis=2) <= 45).astype(np.uint8)
    k = max(3, h // 4) | 1
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (k, k)))
    n, lab, st, _ = cv2.connectedComponentsWithStats(m)
    reg = lab[y - ey0:y - ey0 + h, x - ex0:x - ex0 + w]
    cnt = np.bincount(reg.ravel(), minlength=n)
    cnt[0] = 0
    x0, y0, x1, y1 = fb
    if cnt.max() > 0:
        li = int(cnt.argmax())
        bx, by, bw, bh = [int(v) for v in st[li][:4]]
        if bw * bh <= 6 * w * h:
            x0, y0 = min(bx + ex0, x), min(by + ey0, y)
            x1, y1 = max(bx + ex0 + bw, x + w), max(by + ey0 + bh, y + h)
    x0, y0, x1, y1 = max(x0 - 2, 0), max(y0 - 2, 0), min(x1 + 2, W), min(y1 + 2, H)
    return (x0, y0, x1 - x0, y1 - y0), tuple(int(c) for c in col)


def draw_banner(fr, rect, col, text):
    import cv2, numpy as np
    from PIL import Image, ImageDraw, ImageFont
    x, y, w, h = rect
    if w <= 4 or h <= 4:
        return
    fr[y:y + h, x:x + w] = col
    img = Image.fromarray(cv2.cvtColor(fr[y:y + h, x:x + w], cv2.COLOR_BGR2RGB))
    dr = ImageDraw.Draw(img)
    size = max(int(h * 0.7), 8)
    while True:
        f = ImageFont.truetype(FONT, size)
        bb = dr.textbbox((0, 0), text, font=f, stroke_width=2)
        if size <= 8 or (bb[2] - bb[0] <= w * 0.9 and bb[3] - bb[1] <= h * 0.8):
            break
        size -= 2
    dr.text((w / 2, h / 2), text, font=f, fill="white", anchor="mm",
            stroke_width=2, stroke_fill="black")
    fr[y:y + h, x:x + w] = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


def detect(inp, end):
    import cv2
    cap = cv2.VideoCapture(inp)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    step = max(int(fps * OCR_EVERY), 1)
    n = min(int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0) or end, end)
    step = max(step, -(-n // MAX_SAMPLES))  # سقف تعداد OCR برای هر ویدیو
    samples, i = [], 0
    while i < end and cap.grab():
        if i % step == 0:
            ok, fr = cap.retrieve()
            if ok:
                words = ocr_words(fr)
                prom = [w[:4] for w in words if is_promo(w[4])]
                samples.append({
                    "plain": [w[:4] for w in words if not is_promo(w[4])],
                    "banners": [banner_box(fr, r) for r in cluster(prom)]})
            else:
                samples.append({"plain": [], "banners": []})
        i += 1
    cap.release()
    return samples, step, fps


def banners_at(samples, k, i, step):
    a = samples[min(k, len(samples) - 1)]["banners"]
    b = samples[k + 1]["banners"] if k + 1 < len(samples) else []
    t = (i - k * step) / step
    if a and b and len(a) == len(b):
        res = []
        for (ra, ca), (rb, cb) in zip(sorted(a), sorted(b)):
            res.append((tuple(int(ra[j] + (rb[j] - ra[j]) * t) for j in range(4)), ca))
        return res
    if a and not b:
        return a if t < 0.5 else []
    if b and not a:
        return b if t >= 0.5 else []
    return a if t < 0.5 else b


def edit(inp, out):
    import cv2
    cap = cv2.VideoCapture(inp)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    cap.release()
    dur = duration(inp)
    if n <= 0:
        n = int(dur * fps)
    end = find_outro(inp, fps, n) if (AUTO_REMOVE and CUT_OUTRO) else n
    d = min(end / fps, dur)
    vf = build_vf(d)
    tlim = ["-t", f"{d:.3f}"]
    samples, step = ([], 1) if not AUTO_REMOVE else detect(inp, end)[:2]
    if not any(s["plain"] or s["banners"] for s in samples):
        return run(["ffmpeg", "-y", "-i", inp, "-vf", vf, *tlim, *ENC, out])
    tag = get_tag()
    cap = cv2.VideoCapture(inp)
    ok, fr = cap.read()
    H, W = fr.shape[:2]
    p = subprocess.Popen(
        ["ffmpeg", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}",
         "-r", str(fps), "-i", "-", "-i", inp, "-map", "0:v", "-map", "1:a?",
         "-vf", vf, *tlim, *ENC, "-shortest", out],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pad = max(6, int(W * 0.008))
    i = 0
    while ok and i < end:
        k = min(i // step, len(samples) - 1)
        boxes = list(samples[k]["plain"])
        if k + 1 < len(samples):
            boxes += samples[k + 1]["plain"]  # اتحاد دو نمونه برای پوشش تگ متحرک
        for (x, y, w, h) in boxes:
            x0, y0 = max(x - pad, 0), max(y - pad, 0)
            x1, y1 = min(x + w + pad, W), min(y + h + pad, H)
            if x1 - x0 <= 0 or y1 - y0 <= 0:
                continue
            m = 10
            rx0, ry0, rx1, ry1 = max(x0 - m, 0), max(y0 - m, 0), min(x1 + m, W), min(y1 + m, H)
            roi = fr[ry0:ry1, rx0:rx1]
            mask = roi[:, :, 0] * 0
            mask[y0 - ry0:y1 - ry0, x0 - rx0:x1 - rx0] = 255
            fr[ry0:ry1, rx0:rx1] = cv2.inpaint(roi, mask, 3, cv2.INPAINT_TELEA)
        for rect, col in banners_at(samples, k, i, step):
            draw_banner(fr, rect, col, tag)  # جای بنر تبلیغاتی، بنر با آیدی تو
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


def _wt(_, __, m):
    return m.chat.id in waiting_thumb


wt = filters.create(_wt)


@app.on_message(P & btn(B_THUMB))
async def thumb_menu(client, msg):
    if os.path.exists(THUMB_FILE):
        await client.send_photo(
            msg.chat.id, THUMB_FILE, caption="🖼 تامبنیل فعلی",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 تغییر", callback_data="thumb_set"),
                InlineKeyboardButton("🗑 حذف", callback_data="thumb_del")]]))
    else:
        waiting_thumb.add(msg.chat.id)
        await msg.reply("تامبنیلی ذخیره نشده. عکس را از گالری بفرست:")


@app.on_callback_query(filters.regex("^thumb_(set|del)$"))
async def thumb_cb(client, cq):
    uid = cq.message.chat.id
    if cq.data == "thumb_set":
        waiting_thumb.add(uid)
        await cq.answer()
        await client.send_message(uid, "عکس جدید را بفرست:")
    else:
        if os.path.exists(THUMB_FILE):
            os.remove(THUMB_FILE)
        await cq.answer("حذف شد")
        await cq.message.delete()


@app.on_message(P & wt & (filters.photo | filters.document))
async def on_thumb(client, msg):
    raw = f"/tmp/{uuid.uuid4().hex[:8]}_thumb_raw"
    try:
        await msg.download(file_name=raw)
        await asyncio.to_thread(save_thumb, raw)
        waiting_thumb.discard(msg.chat.id)
        await msg.reply("✅ تامبنیل ذخیره شد و روی همه ویدیوها گذاشته می‌شود.", reply_markup=KB)
    except Exception as e:
        await msg.reply(f"❌ عکس معتبر نبود: {str(e)[:100]}", reply_markup=KB)
    finally:
        if os.path.exists(raw):
            os.remove(raw)


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
                vw, vh, vd = await asyncio.to_thread(probe, out)
                thumb = THUMB_FILE if os.path.exists(THUMB_FILE) else \
                    await asyncio.to_thread(auto_thumb, out, f"/tmp/{name}_t.jpg")
                sent = await client.send_video(
                    uid, out, supports_streaming=True, width=vw, height=vh,
                    duration=vd, thumb=thumb)
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
                for f in (inp, out, f"/tmp/{name}_t.jpg"):
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


if __name__ == "__main__":
    app.run()
