import os, re, sys, json, time, wave, base64, asyncio, subprocess, urllib.request, urllib.parse
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from google import genai
from google.genai import types
import yt_dlp

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
DB_FILE = "users.json"
SESSIONS, ACTIVE_TASKS = {}, {}

# Gemini native TTS voice names. Set GEMINI_TTS_MODELS to override model order.
VOICES = ["Zephyr", "Kore", "Puck", "Charon", "Aoede", "Fenrir", "Leda", "Orus"]
VOICE_MAP = {name: name for name in VOICES}
TTS_MODELS = [x.strip().removeprefix("models/") for x in os.getenv(
    "GEMINI_TTS_MODELS", "gemini-2.5-flash-preview-tts,gemini-2.0-flash-preview-tts"
).split(",") if x.strip()]
VIDEO_SPEED = 0.9
VOICE_SPEED = 1.18
CHANNELS = [("@moviezone336", "https://t.me/moviezone336", "📢 Channel 1 Join ရန်"), ("@color6777", "https://t.me/color6777", "📢 Channel 2 Join ရန်"), ("@sigyi89", "https://t.me/sigyi89", "📢 Channel 3 Join ရန်")]
# Keep the default path on Flash models. Pro models commonly have zero free-tier
# quota and can make an otherwise healthy bot fail before the fallback is tried.
MODELS = [x.strip().removeprefix("models/") for x in os.getenv(
    "GEMINI_MODELS", "gemini-2.5-flash,gemini-2.0-flash"
).split(",") if x.strip() and "flash" in x.lower()]
MODELS = MODELS or ["gemini-2.5-flash", "gemini-2.0-flash"]

def try_run(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except Exception:
        return None

def get_db():
    return (try_run(lambda: json.load(open(DB_FILE))) or {}) if os.path.exists(DB_FILE) else {}

def get_u(uid):
    db, k = get_db(), str(uid)
    d = db.get(k, {})
    keys = d.get("keys", []) or ([d["key"]] if d.get("key") else [])
    v = d.get("v", "Zephyr") if d.get("v") in VOICES else "Zephyr"
    if k not in db or "keys" not in d or d.get("v") not in VOICES:
        db[k] = {"keys": keys, "k_idx": d.get("k_idx", 0), "v": v, "f": d.get("f", True), "layout": d.get("layout", "frame"), "blur": d.get("blur", "auto"), "logo": d.get("logo", True), "verified": d.get("verified", False)}
        json.dump(db, open(DB_FILE, "w"))
    return db[k]

def set_u(uid, field, val):
    db, k = get_db(), str(uid)
    if k not in db:
        get_u(uid)
        db = get_db()
    db[k][field] = val
    json.dump(db, open(DB_FILE, "w"))

def cancel_user_task(uid):
    t = ACTIVE_TASKS.pop(uid, None)
    if t and not t.done():
        t.cancel()
    for f in os.listdir("."):
        if str(uid) in f and not f.startswith("logo_"):
            try_run(os.remove, f)
    SESSIONS.pop(uid, None)

def extract_keys(text):
    raw = re.split(r"[\s,;]+", text.strip())
    return [x.strip() for x in raw if len(x.strip()) >= 20 and not x.startswith(("http", "/")) and (x.startswith(("AIza", "AQ.")) or re.match(r"^[A-Za-z0-9_\-\.]{25,}$", x.strip()))]

def add_user_keys(uid, new_keys):
    cur = list(get_u(uid).get("keys", []))
    added = sum(1 for nk in new_keys if nk not in cur and not cur.append(nk))
    set_u(uid, "keys", cur)
    return added, len(cur)

async def check_joined(bot, uid):
    for ch_id, _, _ in CHANNELS:
        try:
            m = await bot.get_chat_member(chat_id=ch_id, user_id=uid)
            if m.status in ["left", "kicked"]:
                return False
        except Exception:
            continue
    return True

def join_kb():
    rows = [[InlineKeyboardButton(lbl, url=url)] for _, url, lbl in CHANNELS]
    rows.append([InlineKeyboardButton("✅ Join ပြီးပါပြီ (Verify လုပ်မည်)", callback_data="verify|check")])
    return InlineKeyboardMarkup(rows)

def join_text():
    return "🔒 Bot ကို အသုံးပြုရန် အောက်ပါ Channel (၃) ခုကို အရင် Join ပေးပါ -\n\n1️⃣ https://t.me/moviezone336\n2️⃣ https://t.me/color6777\n3️⃣ https://t.me/sigyi89\n\n✅ (၃) ခုလုံး Join ပြီးပါက အောက်ပါ Verify ခလုတ်ကို နှိပ်ပါ -"

def main_kb():
    return ReplyKeyboardMarkup([[KeyboardButton("🎬 Dashboard"), KeyboardButton("🔑 API Key")], [KeyboardButton("🎙️ Voice ရွေးရန်"), KeyboardButton("🖼️ Logo ပုံထည့်ရန်")], [KeyboardButton("/start")]], resize_keyboard=True)

def cancel_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🛑 ရပ်တန့်မည် / ပြန်စမည် (/start)", callback_data="r|cancel")]])

def key_menu_ui(u):
    keys = u.get("keys", [])
    lines = [f"{i+1}. {k[:6]}••••{k[-4:]}" for i, k in enumerate(keys[:10])]
    k_info = f"✅ သိမ်းထားသော Key ({len(keys)}) ခု -\n" + "\n".join(lines) if keys else "❌ API Key မထည့်ရသေးပါ။"
    txt = f"🔑 GEMINI API KEY ROTATION စနစ်\n━━━━━━━━━━━━━━\n{k_info}\n\n💡 Key တစ်ခုချင်းဖြစ်စေ၊ အများကြီးကို Comma (,) သို့မဟုတ် တစ်ကြောင်းချင်းစီ ခြားပြီးဖြစ်စေ ပို့နိုင်ပါသည်။\n⚠️ Bot အလုပ်မလုပ်ပါက /start ကို နှိပ်ပါ။"
    kb = [[InlineKeyboardButton("🌐 Google AI Studio မှ Key ယူရန်", url="https://aistudio.google.com/app/apikey")]]
    if keys:
        kb.append([InlineKeyboardButton("🗑️ Key အားလုံး ဖျက်မည်", callback_data="m|clrkeys")])
    kb.append([InlineKeyboardButton("🔙 Dashboard သို့ ပြန်သွားရန်", callback_data="m|dash")])
    return txt, InlineKeyboardMarkup(kb)

def voice_menu_ui(u):
    txt = "🎙️ Gemini Native TTS အသံရွေးချယ်ရန်\nအသံအမြန်နှုန်း: 1.18x | Video အမြန်နှုန်း: 0.9x\nရွေးထားသောအသံ: " + str(u.get("v", "Zephyr")) + "\n━━━━━━━━━━━━━━"
    rows = []
    for i in range(0, len(VOICES), 2):
        rows.append([InlineKeyboardButton(f"🎙️ {v}" + (" ✅" if u.get("v", "Zephyr") == v else ""), callback_data=f"v|{v}") for v in VOICES[i:i+2]])
    rows.append([InlineKeyboardButton("🔙 Dashboard သို့ ပြန်သွားရန်", callback_data="m|dash")])
    return txt, InlineKeyboardMarkup(rows)

def dash_ui(u, uid):
    k_cnt = len(u.get("keys", []))
    kt = f"✅ ({k_cnt}) ခု ချိတ်ပြီး" if k_cnt > 0 else "❌ မရှိသေးပါ"
    v_lbl = f"{u.get('v', 'Zephyr')} (1.18x)"
    lay = "📱 Blur Frame" if u["layout"] == "frame" else "📲 Full Screen"
    bm = {"auto": "🎯 Auto-Detect", "low": "အောက်ခြေ", "mid": "အလယ်အောက်", "high": "အလယ်", "off": "OFF ❌"}
    b_txt = bm.get(u["blur"], "🎯 Auto-Detect")
    lg_t = ("ON ✅" if u["logo"] else "OFF ❌") if os.path.exists(f"logo_{uid}.jpg") else "ပုံမထည့်ရသေး"
    fl_t = "ON ✅" if u["f"] else "OFF ❌"
    kb = [[InlineKeyboardButton(f"🔑 API Keys: {kt}", callback_data="m|key")], [InlineKeyboardButton(f"🎙️ Voice: {v_lbl}", callback_data="m|vmenu"), InlineKeyboardButton(f"🖼️ {lay}", callback_data="m|lay")], [InlineKeyboardButton(f"🌫️ Blur: {b_txt}", callback_data="m|blur"), InlineKeyboardButton(f"🏷️ Logo: {lg_t}", callback_data="m|logo")], [InlineKeyboardButton(f"🪞 Flip + Filter + Crop: {fl_t}", callback_data="m|f")]]
    txt = f"⚡ SIGYI AI STUDIO PRO (ULTRA HD) ⚡\n━━━━━━━━━━━━━━\n• 🔑 API Keys: {kt} (Auto-Rotation ✅)\n• 🤖 Model: API Key နှင့် ကိုက်ညီသော Gemini Model (Auto)\n• 🎙️ Voice: {v_lbl} | 📺 Ultra HD\n• 🖼️ {lay} | 🌫️ Blur: {b_txt}\n• 🎯 Scene-Locked Frame Sync: Active ✅\n━━━━━━━━━━━━━━\n📥 TikTok / YouTube / Rednote Link သို့မဟုတ် Video ပို့ပါ!\n💡 (Bot အလုပ်မလုပ်ပါက /start ကို နှိပ်ပါ)"
    return txt, InlineKeyboardMarkup(kb)

def is_valid_video(p):
    if not os.path.exists(p) or os.path.getsize(p) < 10000:
        return False
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_type", "-of", "csv=p=0", p], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return "video" in r.stdout.lower()

def is_valid_audio(p):
    if not os.path.exists(p) or os.path.getsize(p) < 600:
        return False
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_type", "-of", "csv=p=0", p], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return "audio" in r.stdout.lower()

def save_stream_url(dl_url, vpath, ua, ref=""):
    try:
        try_run(os.remove, vpath)
        hdrs = {"User-Agent": ua, **({"Referer": ref} if ref else {})}
        with urllib.request.urlopen(urllib.request.Request(dl_url, headers=hdrs), timeout=25) as r, open(vpath, "wb") as f:
            while chunk := r.read(65536):
                f.write(chunk)
        if is_valid_video(vpath):
            return True
    except Exception:
        pass
    try_run(os.remove, vpath)
    return False

def find_mp4(obj):
    if isinstance(obj, str) and obj.startswith("http") and not any(b in obj.lower() for b in [".mp3", ".m4a", ".jpg", ".png", ".webp", ".jpeg"]):
        return obj if any(x in obj.lower() for x in [".mp4", "video", "play", "stream", "xhscdn", "googlevideo", "tikcdn", "aweme"]) else None
    if isinstance(obj, dict):
        for k in ["play", "hdplay", "noWatermark", "video", "url", "download", "mp4", "url_list", "data", "result"]:
            if k in obj and (r := find_mp4(obj[k])):
                return r
        for v in obj.values():
            if r := find_mp4(v):
                return r
    if isinstance(obj, list):
        for item in obj:
            if r := find_mp4(item):
                return r
    return None

def yt_dlp_download(url, vpath, ua):
    """Primary downloader for TikTok and YouTube, with bounded retries.
    Third-party web APIs are fallback-only because they frequently rate-limit.
    """
    base = vpath[:-4] + "_ydl" if vpath.lower().endswith(".mp4") else vpath + "_ydl"
    configs = [{}]
    low = url.lower()
    if "tiktok.com" in low:
        configs = [
            {"extractor_args": {"tiktok": {"app_name": "musically"}}},
            {"extractor_args": {"tiktok": {"app_name": "trill"}}},
            {},
        ]
    elif "youtu" in low:
        configs = [
            {"extractor_args": {"youtube": {"player_client": ["android_vr"]}}},
            {"extractor_args": {"youtube": {"player_client": ["ios"]}}},
            {},
        ]
    for i, extra in enumerate(configs):
        outbase = f"{base}{i}"
        for old in [x for x in os.listdir(".") if x.startswith(outbase)]:
            try_run(os.remove, old)
        opts = {
            "format": "bv*[ext=mp4][height<=720]+ba[ext=m4a]/b[ext=mp4][height<=720]/b[ext=mp4]/b",
            "merge_output_format": "mp4",
            "outtmpl": f"{outbase}.%(ext)s",
            "quiet": True, "no_warnings": True, "noplaylist": True,
            "nocheckcertificate": True, "retries": 3, "fragment_retries": 3,
            "extractor_retries": 3, "socket_timeout": 25,
            "concurrent_fragment_downloads": 1, "force_ipv4": True,
            "http_headers": {"User-Agent": ua, "Referer": "https://www.tiktok.com/"},
            **extra,
        }
        try_run(lambda: yt_dlp.YoutubeDL(opts).download([url]))
        candidates = [x for x in os.listdir(".") if x.startswith(outbase + ".") and is_valid_video(x)]
        if candidates:
            try_run(os.remove, vpath)
            os.rename(sorted(candidates)[0], vpath)
            for extra_file in candidates[1:]:
                try_run(os.remove, extra_file)
            return True
    return False

def dl_vid(url, vpath):
    url = url.strip().rstrip("，。、！!?,.;:)>\"'")
    ua_m = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
    ua_d = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    try_run(os.remove, vpath)
    # Use the maintained extractor before custom APIs for the platforms it
    # supports. This also follows vt.tiktok.com and youtu.be redirects.
    if any(host in url.lower() for host in ["tiktok.com", "youtu.be", "youtube.com"]):
        if yt_dlp_download(url, vpath, ua_d):
            return
    if "tiktok.com" in url:
        # Resolve vt.tiktok.com short URLs with yt-dlp first. Third-party JSON
        # download APIs are frequently rate-limited or return stale URLs, while
        # yt-dlp follows the redirect and validates the media itself.
        tt_base = vpath.replace(".mp4", "_tt")
        tt_configs = [
            {"extractor_args": {"tiktok": {"app_name": "musically"}}},
            {"extractor_args": {"tiktok": {"app_name": "trill"}}},
            {},
        ]
        for ti, extra in enumerate(tt_configs):
            for old in [x for x in os.listdir(".") if x.startswith(tt_base)]:
                try_run(os.remove, old)
            opts_tt = {
                "format": "bv*[ext=mp4][height<=720]+ba[ext=m4a]/b[ext=mp4][height<=720]/b[ext=mp4]/b",
                "merge_output_format": "mp4",
                "outtmpl": f"{tt_base}{ti}.%(ext)s",
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
                "nocheckcertificate": True,
                "retries": 3,
                "fragment_retries": 3,
                "socket_timeout": 25,
                "concurrent_fragment_downloads": 1,
                "force_ipv4": True,
                "http_headers": {"User-Agent": ua_m, "Referer": "https://www.tiktok.com/"},
                **extra,
            }
            try_run(lambda: yt_dlp.YoutubeDL(opts_tt).download([url]))
            for candidate in [x for x in os.listdir(".") if x.startswith(f"{tt_base}{ti}.") and is_valid_video(x)]:
                try_run(os.remove, vpath)
                os.rename(candidate, vpath)
                return
        api1 = f"https://www.tikwm.com/api/?url={urllib.parse.quote(url)}"
        d1 = (try_run(lambda: json.loads(urllib.request.urlopen(urllib.request.Request(api1, headers={"User-Agent": ua_m}), timeout=15).read().decode())) or {}).get("data", {})
        for fk in ["play", "wmplay", "hdplay"]:
            pu = d1.get(fk)
            if pu:
                pu = "https://www.tikwm.com" + pu if pu.startswith("/") else pu
                if save_stream_url(pu, vpath, ua_m, "https://www.tikwm.com/") or (try_run(lambda: urllib.request.urlretrieve(pu, vpath)) and is_valid_video(vpath)):
                    return
        d2 = (try_run(lambda: json.loads(urllib.request.urlopen(urllib.request.Request(f"https://api.tiklydown.eu.org/api/download?url={urllib.parse.quote(url)}", headers={"User-Agent": ua_d}), timeout=15).read().decode())) or {}).get("video", {})
        if (pu2 := (d2.get("noWatermark") or d2.get("watermark"))) and save_stream_url(pu2, vpath, ua_d):
            return
        d3 = (try_run(lambda: json.loads(urllib.request.urlopen(urllib.request.Request("https://lovetik.com/api/ajax/search", data=urllib.parse.urlencode({"query": url}).encode(), headers={"User-Agent": ua_d, "Origin": "https://lovetik.com"}), timeout=15).read().decode())) or {}).get("links", [])
        for lk in d3:
            if (pu3 := lk.get("a")) and "mp3" not in (lk.get("t") or "").lower() and save_stream_url(pu3, vpath, ua_d, "https://lovetik.com/"):
                return
    if "xhslink.com" in url or "xiaohongshu.com" in url:
        for ua in [ua_d, ua_m]:
            req_x = urllib.request.Request(url, headers={"User-Agent": ua, "Cookie": "webBuild=4.36.0; xsecappid=xhs-pc-web;"})
            html = try_run(lambda: urllib.request.urlopen(req_x, timeout=15).read().decode("utf-8", "ignore")) or ""
            mk, mog, mm = re.search(r'"originVideoKey"\s*:\s*"([^"]+)"', html), re.search(r'<meta\s+(?:name|property)="og:video"\s+content="([^"]+)"', html), re.search(r'"masterUrl"\s*:\s*"([^"]+)"', html)
            mcdn = re.search(r'(https?://sns-video-[a-z0-9\-]+\.xhscdn\.com/[^"\'\s\\]+)', html)
            cands = ([f"https://sns-video-bd.xhscdn.com/{mk.group(1).replace(chr(92)+'u002F', '/')}", f"https://sns-video-hw.xhscdn.com/{mk.group(1).replace(chr(92)+'u002F', '/')}"] if mk else []) + [m.group(1) for m in [mog, mm, mcdn] if m]
            for vu in cands:
                vu = vu.replace("\\u002F", "/").replace("&amp;", "&")
                if save_stream_url(vu if vu.startswith("http") else "https:" + vu, vpath, ua, "https://www.xiaohongshu.com/"):
                    return
    ym = re.search(r"(?:shorts/|v=|youtu\.be/|embed/)([A-Za-z0-9_-]{11})", url)
    if ym:
        vid_id = ym.group(1)
        clean_yt = f"https://www.youtube.com/watch?v={vid_id}"
        for fmt in ["720", "480", "360"]:
            tid = (try_run(lambda: json.loads(urllib.request.urlopen(urllib.request.Request(f"https://p.oceansaver.in/ajax/download.php?format={fmt}&url={urllib.parse.quote(clean_yt)}", headers={"User-Agent": ua_d}), timeout=12).read().decode())) or {}).get("id")
            for _ in range(10 if tid else 0):
                time.sleep(2)
                dl_u = (try_run(lambda: json.loads(urllib.request.urlopen(urllib.request.Request(f"https://p.oceansaver.in/ajax/progress.php?id={tid}", headers={"User-Agent": ua_d}), timeout=10).read().decode())) or {}).get("download_url")
                if dl_u and save_stream_url(dl_u, vpath, ua_d):
                    return
        tb = vpath.replace(".mp4", "_yt")
        for ycfg in [{"player_client": ["android_vr"], "player_skip": ["webpage", "configs"]}, {"player_client": ["ios"], "player_skip": ["webpage", "configs"]}, {"player_client": ["tv_embedded"], "player_skip": ["webpage", "configs"]}, {"player_client": ["android_vr", "tv_downgraded"]}]:
            for f in [x for x in os.listdir(".") if x.startswith(tb)]:
                try_run(os.remove, f)
            opts = {"format": "b[ext=mp4]/bv*+ba/b/best", "merge_output_format": "mp4", "outtmpl": f"{tb}.%(ext)s", "quiet": True, "noplaylist": True, "nocheckcertificate": True, "cachedir": False, "extractor_args": {"youtube": ycfg}}
            try_run(lambda: yt_dlp.YoutubeDL(opts).download([clean_yt]))
            for f in [x for x in os.listdir(".") if x.startswith(tb) and is_valid_video(x)]:
                os.rename(f, vpath)
                return
        from pytubefix import YouTube
        for cli in ["ANDROID_VR", "IOS", "WEB_EMBED", "MWEB", "TV"]:
            yt = try_run(lambda: YouTube(clean_yt, client=cli))
            st = try_run(lambda: yt.streams.filter(progressive=True, file_extension="mp4").order_by("resolution").desc().first()) if yt else None
            if st and try_run(lambda: st.download(filename=vpath)) and is_valid_video(vpath):
                return
        for api in [f"https://pipedapi.kavin.rocks/streams/{vid_id}", f"https://inv.nadeko.net/api/v1/videos/{vid_id}"]:
            data = try_run(lambda: json.loads(urllib.request.urlopen(urllib.request.Request(api, headers={"User-Agent": ua_d}), timeout=10).read().decode())) or {}
            for s in (data.get("formatStreams") or data.get("videoStreams") or []):
                if (su := s.get("url")) and (s.get("videoOnly") is not True) and save_stream_url(su, vpath, ua_d):
                    return
        url = clean_yt
    if yt_dlp_download(url, vpath, ua_d):
        return
    import btch_downloader
    dl_u = try_run(lambda: find_mp4(asyncio.run(btch_downloader.xiaohongshu(url) if "xhs" in url or "xiaohongshu" in url else (btch_downloader.ttdl(url) if "tiktok" in url else (btch_downloader.youtube(url) if "youtu" in url else btch_downloader.aio(url))))))
    if dl_u and save_stream_url(dl_u, vpath, ua_d):
        return
    tb2 = vpath.replace(".mp4", "_g")
    for f in [x for x in os.listdir(".") if x.startswith(tb2)]:
        try_run(os.remove, f)
    opts2 = {"format": "b[ext=mp4]/bv*+ba/b/best", "merge_output_format": "mp4", "outtmpl": f"{tb2}.%(ext)s", "quiet": True, "noplaylist": True, "nocheckcertificate": True, "cachedir": False, "http_headers": {"User-Agent": ua_m}}
    try_run(lambda: yt_dlp.YoutubeDL(opts2).download([url]))
    for f in [x for x in os.listdir(".") if x.startswith(tb2) and is_valid_video(x)]:
        os.rename(f, vpath)
        return
    raise Exception("Video Link ဒေါင်းယူ၍ မရပါ။ (Bot အလုပ်မလုပ်ပါက /start နှိပ်ပါ)")

def clean_mm_text(t):
    t = re.sub(r"\([^)]*\)", "", t)
    t = re.sub(r"[*#_`~0-9:a-zA-Z\[\]()|•\-]", " ", t)
    return re.sub(r"\s+", " ", t).strip()

def get_generate_models(client):
    discovered = []
    try:
        for model in client.models.list():
            name = str(getattr(model, "name", "") or "").removeprefix("models/")
            actions = [str(a).lower() for a in (getattr(model, "supported_actions", None) or [])]
            if not name or (actions and not any("generatecontent" in a for a in actions)):
                continue
            low = name.lower()
            # Free-tier safe policy: discover Flash models only. In particular,
            # never auto-promote to a Pro model whose quota may be zero.
            if not any(term in low for term in ("embedding", "tts", "image", "veo", "aqa")) and "flash" in low:
                discovered.append(name)
    except Exception:
        return MODELS
    available = set(discovered)
    preferred = [m for m in MODELS if m in available]
    return preferred + [m for m in discovered if m not in preferred] or MODELS

def _try_models_with_file(client, model_candidates, uf, p, uid, k_i):
    last_err = None
    for m_name in model_candidates:
        try:
            res = client.models.generate_content(model=m_name, contents=[uf, p])
            if res and res.text:
                set_u(uid, "k_idx", k_i)
                return res.text, None
        except Exception as me:
            last_err = me
            # Try the next Flash model on quota/rate-limit and transient auth
            # errors instead of aborting the whole model search immediately.
            if any(w in str(me).upper() for w in ["429", "RESOURCE_EXHAUSTED", "QUOTA", "API_KEY", "PERMISSION", "403"]):
                continue
    return "", last_err

def ai_frame_audio_translate(vpath, sm_v, uid, vdur, n_win):
    subprocess.run(["ffmpeg", "-y", "-threads", "2", "-i", vpath, "-vf", "fps=1,scale=280:-2", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "35", "-c:a", "aac", "-ac", "1", "-ar", "16000", "-b:a", "28k", sm_v], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    up_file = sm_v if (os.path.exists(sm_v) and os.path.getsize(sm_v) > 1000) else vpath
    u = get_u(uid)
    keys = u.get("keys", [])
    if not keys:
        raise Exception("API Key မရှိပါ။ Key အရင်ထည့်ပေးပါ။")
    start_idx = u.get("k_idx", 0) % len(keys)
    w_dur = vdur / n_win
    chars_w = max(65, int((w_dur / 0.85) * 13.2))
    win_desc = "၊ ".join([f"အပိုင်း {i+1} ({int(i*w_dur)}s မှ {int((i+1)*w_dur)}s)" for i in range(n_win)])
    p = f"ဒီ Video ဖိုင်ရဲ့ ရုပ်ပုံ (Frames) နဲ့ မူရင်းအသံ (Audio) နှစ်ခုလုံးကို သေချာကြည့်ရှုနားထောင်ပြီး အောက်ပါ အချိန်အပိုင်းအခြား ({n_win}) ပိုင်းအတိုင်း အတိအကျ ခွဲ၍ အပိုအလိုမရှိဘဲ Burmese Professional Movie Recap စကားပြောပုံစံဖြင့် ဘာသာပြန်ပေးပါ - {win_desc}\n\n"
    p += "အတိအကျ လိုက်နာရမည့် ဥပမာ အရေးအသားပုံစံ -\n"
    p += "အမျိုးသားတစ်ယောက်ဟာ လှေပေါ်မှာ ဓားနဲ့ ကစားနေရင်း မတော်တဆ အပေါက်ဖောက်မိသွားတဲ့အတွက် ရေတွေ စတင် စိမ့်ဝင်လာပါတယ်။ ရေတွေက သင်္ဘောကုန်းပတ်ပေါ်အထိ လျင်မြန်စွာ တက်လာချိန်မှာပဲ တခြားလှေတစ်စင်း ချဉ်းကပ်လာတာကို သူ မြင်လိုက်ရပါတယ်။ ||| သူက သူ့ခြေထောက်နဲ့ အပေါက်ကို အမြန်ဖုံးအုပ်ပြီး ဘာမှမဖြစ်သလို ဟန်ဆောင်လိုက်ပါတယ်။ ရုတ်တရက်ဆိုသလိုပဲ ကင်းလှည့်ရေယာဉ်တစ်စင်း သူတို့ရှေ့မှာ ပေါ်လာပါတယ်။ သူတို့တွေက ငါးဖမ်းသမားတွေလို ဟန်ဆောင်ကြပေမဲ့ ရန်သူခေါင်းဆောင်ကတော့ တစ်ခုခု လွဲနေတယ်လို့ သံသယဝင်သွားပြီး စစ်ဆေးဖို့အတွက် သူတို့လှေပေါ်ကို တက်လာပါတော့တယ်။ ||| ခေါင်းဆောင်က အန်နာ ဆီကို လျှောက်သွားပြီး ငါးမျှားတံကို ကောက်ကိုင်ကာ ငါးဖမ်းဟန်ဆောင်လိုက်ပါတယ်။ အန်နာက လျင်မြန်စွာ လှည့်လိုက်တဲ့အခါ ငါးမျှားတံက သူမလက်ထဲကနေ လွင့်စင်သွားခဲ့ပါတယ်။\n\n"
    p += f"စည်းကမ်းချက် - အပိုင်းတစ်ပိုင်းစီအတွက် အဲ့ဒီအချိန်အတွင်း မြင်ရကြားရသည့်အတိုင်း မြန်မာစာလုံးရေ {chars_w} လုံးခန့် ရေးပြီး အပိုင်းကြားတွင် ||| သင်္ကေတ ခြားပေးပါ။ လူနာမည်များကို မြန်မာလိုသာရေးပါ၊ အင်္ဂလိပ်စာလုံးနှင့် ကွင်းစကွင်းပိတ် လုံးဝ မထည့်ပါနှင့်။"
    raw_txt, last_err = "", None
    for step_i in range(len(keys)):
        k_i = (start_idx + step_i) % len(keys)
        client = genai.Client(api_key=keys[k_i].strip(), http_options=types.HttpOptions(timeout=40000))
        model_candidates = get_generate_models(client)
        uf = try_run(lambda: client.files.upload(file=up_file))
        if not uf:
            continue
        for _ in range(120):
            if getattr(getattr(uf, "state", None), "name", "") == "PROCESSING":
                time.sleep(1)
                uf = try_run(lambda: client.files.get(name=uf.name)) or uf
        if getattr(getattr(uf, "state", None), "name", "") == "PROCESSING":
            last_err = RuntimeError("Gemini video file is still processing after 120 seconds")
            try_run(lambda: client.files.delete(name=uf.name))
            continue
        raw_txt, err_one = _try_models_with_file(client, model_candidates, uf, p, uid, k_i)
        if err_one:
            last_err = err_one
        try_run(lambda: client.files.delete(name=uf.name))
        if raw_txt:
            break
    if not raw_txt:
        err_text = str(last_err or "Unknown Gemini API error")
        upper = err_text.upper()
        if any(w in upper for w in ["429", "RESOURCE_EXHAUSTED", "QUOTA", "LIMIT: 0"]):
            raise Exception("Gemini API quota ပြည့်နေပါသည် (သို့မဟုတ် ဒီ API key အတွက် free-tier quota = 0 ဖြစ်နေပါသည်)။ Google AI Studio တွင် API key အသစ်သုံးပါ၊ သို့မဟုတ် billing/quota ကို စစ်ပြီး key အများကြီးထည့်ပြီး ပြန်စမ်းပါ။")
        raise Exception(f"Gemini API Error: {err_text[:600]}")
    parts = [clean_mm_text(x) for x in raw_txt.split("|||") if len(clean_mm_text(x)) > 5]
    if len(parts) != n_win:
        full_c = clean_mm_text(raw_txt)
        sents = [s.strip() + "။" for s in full_c.split("။") if len(s.strip()) > 5] or [full_c]
        st = max(1, len(sents) / n_win)
        parts = [" ".join(sents[int(round(i*st)):max(int(round(i*st))+1, int(round((i+1)*st)) if i < n_win-1 else len(sents))]) or sents[-1] for i in range(n_win)]
    return parts

def _gemini_tts_to_wav(text, output_wav, voice_key, uid):
    """Generate Gemini native TTS and safely convert returned audio to a valid WAV."""
    u = get_u(uid)
    keys = u.get("keys", [])
    if not keys:
        raise RuntimeError("Gemini API Key မရှိပါ။ Key အရင်ထည့်ပေးပါ။")

    voice_name = VOICE_MAP.get(voice_key, "Zephyr")
    start_idx = int(u.get("k_idx", 0)) % len(keys)
    last_error = None

    for step in range(len(keys)):
        key_i = (start_idx + step) % len(keys)
        try:
            client = genai.Client(
                api_key=keys[key_i].strip(),
                http_options=types.HttpOptions(timeout=90000),
            )
        except Exception as exc:
            last_error = exc
            continue

        for model_name in TTS_MODELS:
            raw_path = output_wav + ".pcm"
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=text,
                    config=types.GenerateContentConfig(
                        response_modalities=["AUDIO"],
                        speech_config=types.SpeechConfig(
                            voice_config=types.VoiceConfig(
                                prebuilt_voice_config=types.PrebuiltVoiceConfig(
                                    voice_name=voice_name
                                )
                            )
                        ),
                    ),
                )

                audio_data = None
                mime_type = ""
                for candidate in (getattr(response, "candidates", None) or []):
                    content = getattr(candidate, "content", None)
                    for part in (getattr(content, "parts", None) or []):
                        inline = getattr(part, "inline_data", None)
                        if inline:
                            data = getattr(inline, "data", None)
                            mime_type = (getattr(inline, "mime_type", "") or "").lower()
                            if isinstance(data, bytes):
                                audio_data = data
                            elif isinstance(data, str):
                                try:
                                    audio_data = base64.b64decode(data)
                                except Exception:
                                    audio_data = None
                            if audio_data:
                                break
                    if audio_data:
                        break

                if not audio_data or len(audio_data) < 200:
                    raise RuntimeError(
                        f"{model_name}: audio data မရှိပါ သို့မဟုတ် တိုလွန်းသည် "
                        f"(size={len(audio_data) if audio_data else 0}, mime={mime_type or 'unknown'})"
                    )

                # Some API responses contain a complete WAV; others contain raw PCM.
                # Preserve WAV input as-is. Convert raw PCM to WAV with an explicit
                # 24 kHz / mono / signed 16-bit little-endian format.
                if audio_data[:4] == b"RIFF" and audio_data[8:12] == b"WAVE":
                    Path(output_wav).write_bytes(audio_data)
                else:
                    Path(raw_path).write_bytes(audio_data)
                    pcm_format = "s16le"
                    # Gemini native TTS normally returns 24 kHz PCM. If MIME metadata
                    # explicitly declares another linear PCM rate, honor that rate.
                    sample_rate = 24000
                    if "16000" in mime_type:
                        sample_rate = 16000
                    elif "48000" in mime_type:
                        sample_rate = 48000
                    result = subprocess.run(
                        ["ffmpeg", "-y", "-f", pcm_format, "-ar", str(sample_rate),
                         "-ac", "1", "-i", raw_path, "-c:a", "pcm_s16le", output_wav],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        text=True, timeout=45,
                    )
                    if result.returncode != 0:
                        raise RuntimeError(
                            f"{model_name}: PCM→WAV conversion failed: {result.stderr[-300:]}"
                        )

                if not is_valid_audio(output_wav):
                    size = os.path.getsize(output_wav) if os.path.exists(output_wav) else 0
                    raise RuntimeError(
                        f"{model_name}: WAV validation failed "
                        f"(size={size}, source_mime={mime_type or 'unknown'})"
                    )

                set_u(uid, "k_idx", key_i)
                return True
            except Exception as exc:
                last_error = exc
                continue
            finally:
                try_run(os.remove, raw_path)
                # Do not leave a corrupt partial output for the next model/key attempt.
                if last_error and os.path.exists(output_wav) and not is_valid_audio(output_wav):
                    try_run(os.remove, output_wav)

    raise RuntimeError(
        f"Gemini Native TTS မအောင်မြင်ပါ: {str(last_error or 'Unknown error')[:700]}"
    )

async def build_free_synced_voice(seg_scripts, voice_key, out_a, uid):
    """Generate native Gemini TTS clips, speed voice to 1.18x, then join clean PCM."""
    voice = VOICE_MAP.get(voice_key, "Zephyr")
    work = f"tts_{uid}_{int(time.time() * 1000)}"
    os.makedirs(work, exist_ok=True)
    wavs, seg_durs = [], []
    try:
        for i, script in enumerate(seg_scripts):
            text = clean_mm_text(script) or "အဲ့ဒီနောက်မှာတော့"
            raw = os.path.join(work, f"raw_{i}.wav")
            wav = os.path.join(work, f"seg_{i}.wav")
            wavs.append(wav)
            await asyncio.wait_for(asyncio.to_thread(_gemini_tts_to_wav, text, raw, voice, uid), timeout=150)
            af = f"atempo={VOICE_SPEED},silenceremove=start_periods=1:start_duration=0.03:start_threshold=-48dB:stop_periods=1:stop_duration=0.03:stop_threshold=-48dB,highpass=f=70,loudnorm=I=-16:TP=-1.5:LRA=11,aresample=24000,asetpts=PTS-STARTPTS"
            r = await asyncio.to_thread(subprocess.run, ["ffmpeg", "-y", "-i", raw, "-af", af, "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", wav], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=90)
            if r.returncode != 0 or not is_valid_audio(wav):
                raise RuntimeError(f"Audio cleanup failed at segment {i + 1}: {r.stderr[-220:]}")
            seg_durs.append(max(0.05, get_dur(wav)))
        if not wavs or len(seg_durs) != len(seg_scripts):
            raise RuntimeError("No usable narration segments were produced")
        list_f = os.path.join(work, "concat.txt")
        Path(list_f).write_text("".join("file '" + os.path.abspath(w).replace("'", "'\\''") + "'\n" for w in wavs), encoding="utf-8")
        joined = os.path.join(work, "joined.wav")
        r = await asyncio.to_thread(subprocess.run, ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_f, "-c:a", "pcm_s16le", "-ar", "24000", "-ac", "1", joined], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=150)
        if r.returncode != 0 or not is_valid_audio(joined):
            raise RuntimeError("Narration concat failed: " + r.stderr[-260:])
        r = await asyncio.to_thread(subprocess.run, ["ffmpeg", "-y", "-i", joined, "-map_metadata", "-1", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", out_a], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=150)
        if r.returncode != 0 or not is_valid_audio(out_a):
            raise RuntimeError("Narration encode failed: " + r.stderr[-260:])
        return seg_durs
    finally:
        try_run(__import__('shutil').rmtree, work, True)


def detect_sub_band(iv, vdur, is_fr, uid):
    raw_f = f"subdet_{uid}.raw"
    w, h = 360, (410 if is_fr else 640)
    vf = f"fps=6/{max(2.0, vdur):.2f},crop=iw*0.92:ih*0.92:iw*0.04:ih*0.04,scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"
    subprocess.run(["ffmpeg", "-y", "-threads", "1", "-i", iv, "-vf", vf, "-frames:v", "6", "-f", "rawvideo", "-pix_fmt", "gray", raw_f], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    dy, dh = (950, 86) if is_fr else (1040, 92)
    if not os.path.exists(raw_f):
        return dy, dh
    data = open(raw_f, "rb").read()
    try_run(os.remove, raw_f)
    n_f = len(data) // (w * h)
    if n_f < 1:
        return dy, dh
    ys, ye, scores = int(h * 0.56), int(h * 0.96), [0.0] * h
    for f_i in range(n_f):
        b = f_i * w * h
        for y in range(ys, ye):
            ro = b + y * w
            hits = sum(1 for x in range(int(w * 0.15), int(w * 0.85), 2) if abs(data[ro + x] - data[ro + x - 2]) > 40)
            if hits > 8:
                scores[y] += hits
    by_i = max(range(ys, ye - 16), key=lambda y: sum(scores[y:y + 16]), default=-1)
    if by_i < 0 or sum(scores[by_i:by_i + 16]) < 25 * n_f:
        return dy, dh
    out_c = int((by_i + 8) * 2.0) + (230 if is_fr else 0)
    return max(240 if is_fr else 100, min((1050 - dh) if is_fr else (1240 - dh), out_c - int(dh * 0.46))), dh

def get_dur(p):
    if not p or not os.path.exists(p):
        raise FileNotFoundError(f"Media file not found: {p}")
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", p], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=20)
    duration = try_run(lambda: float(r.stdout.strip())) or 0.0
    if r.returncode != 0 or duration <= 0:
        raise RuntimeError(f"Invalid media duration for {os.path.basename(p)}")
    return duration

def build_piecewise_pts(source_dur, seg_durs, target_dur=None):
    """Map source-video scene windows onto the narration timeline while slowing video."""
    source_dur = max(0.1, float(source_dur))
    target_dur = float(target_dur if target_dur is not None else source_dur / VIDEO_SPEED)
    if not seg_durs:
        return f"setpts=PTS/{VIDEO_SPEED:.6f},fps=30,tpad=stop_mode=clone:stop_duration=1,trim=duration={target_dur:.6f},setpts=PTS-STARTPTS"
    n = len(seg_durs)
    source_step = source_dur / n
    weights = [max(0.05, float(x)) for x in seg_durs]
    total = sum(weights)
    targets = []
    acc = 0.0
    for d in weights:
        targets.append(acc * target_dur / total)
        acc += d
    expr = f"{targets[-1]:.6f}+(T-{(n-1)*source_step:.6f})*{(target_dur-targets[-1])/source_step:.8f}"
    for i in range(n-2, -1, -1):
        slope = ((weights[i] * target_dur / total) / source_step)
        expr = f"if(lt(T,{(i+1)*source_step:.6f}),{targets[i]:.6f}+(T-{i*source_step:.6f})*{slope:.8f},{expr})"
    return f"settb=AVTB,setpts='({expr})/TB',fps=30,tpad=stop_mode=clone:stop_duration=1,trim=duration={target_dur:.6f},setpts=PTS-STARTPTS"

def _atempo_chain(ratio):
    ratio=max(0.1,min(20.0,float(ratio)))
    parts=[]
    while ratio>2.0: parts.append("atempo=2.0"); ratio/=2.0
    while ratio<0.5: parts.append("atempo=0.5"); ratio/=0.5
    parts.append(f"atempo={ratio:.8f}")
    return ",".join(parts)

def edit_vid(iv, ia, ov, u, uid, vdur, seg_durs):
    """Render with a single master clock and validate both streams before returning."""
    source_dur=get_dur(iv); target=max(1.0,source_dur / VIDEO_SPEED); audio_dur=max(0.1,get_dur(ia))
    # Fit narration once; boundaries are represented by build_piecewise_pts.
    audio_fit=_atempo_chain(audio_dur/target)
    is_fr=u.get("layout","frame")=="frame"; fl=",hflip" if u.get("f") else ""
    adj="crop=iw*0.92:ih*0.92:iw*0.04:ih*0.04,eq=contrast=1.08:brightness=0.02:saturation=1.14:gamma=1.03,hue=h=3,unsharp=3:3:0.65:3:3:0.0"
    video_clock=build_piecewise_pts(source_dur,seg_durs,target)
    b_map={"low":(950,86),"mid":(880,86),"high":(790,86)} if is_fr else {"low":(1040,92),"mid":(940,92),"high":(830,92)}
    by,bh=(detect_sub_band(iv,source_dur,is_fr,uid) if u.get("blur")=="auto" else b_map.get(u.get("blur"),(950,86)))
    by=(int(by)//2)*2; bh=max(64,(int(bh)//2)*2)
    kbps=min(3200,max(1000,int((42*8192)/target)-160))
    inputs=["-i",iv,"-i",ia]
    fc=f"[0:v]{video_clock}{fl}[v0];"
    if is_fr:
        fc+=f"[v0]split=2[bgi][fgi];[bgi]scale=180:320:force_original_aspect_ratio=increase,crop=180:320,gblur=sigma=14,eq=brightness=-0.07,scale=720:1280[bg];[fgi]{adj},scale=720:820:force_original_aspect_ratio=increase:flags=lanczos,crop=720:820[fg];[bg][fg]overlay=0:230[v1]"
    else:
        fc+=f"[v0]{adj},scale=720:1280:force_original_aspect_ratio=increase:flags=lanczos,crop=720:1280[v1]"
    cur="[v1]"
    if u.get("blur")!="off":
        fc+=f";{cur}split=2[m1][m2];[m2]crop=640:{bh}:40:{by},gblur=sigma=26[bst];[m1][bst]overlay=40:{by}[v2]"; cur="[v2]"
    logo=f"logo_{uid}.jpg"
    if u.get("logo") and os.path.exists(logo):
        inputs += ["-i",logo]; ly=165 if is_fr else 80
        fc+=f";[2:v]scale=110:110[lg];{cur}[lg]overlay=12:{ly}[v3]"; cur="[v3]"
    fc+=f";{cur}format=yuv420p[vout]"
    audio_filter=f"asetpts=PTS-STARTPTS,{audio_fit},aresample=async=1:first_pts=0,atrim=duration={target:.6f},asetpts=PTS-STARTPTS"
    cmd=["ffmpeg","-y","-threads","2"]+inputs+["-map_metadata","-1","-map_chapters","-1","-filter_complex",fc,"-map","[vout]","-map","1:a:0","-fps_mode","cfr","-r","30","-c:v","libx264","-preset","superfast","-crf","19","-maxrate",f"{kbps}k","-bufsize",f"{kbps*2}k","-af",audio_filter,"-c:a","aac","-b:a","160k","-t",f"{target:.3f}","-avoid_negative_ts","make_zero","-movflags","+faststart","-fs","47M",ov]
    r=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=max(600,int(target*8)))
    if r.returncode!=0 or not is_valid_video(ov):
        raise RuntimeError("Render failed: "+r.stderr[-700:])
    final=get_dur(ov)
    if abs(final-target)>0.8:
        raise RuntimeError(f"Final duration validation failed: expected {target:.2f}s, got {final:.2f}s")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    cancel_user_task(uid)
    u = get_u(uid)
    if not u.get("verified") or not (await check_joined(context.bot, uid)):
        set_u(uid, "verified", False)
        await update.message.reply_text(join_text(), reply_markup=join_kb(), disable_web_page_preview=True)
        return
    await update.message.reply_text("👋 Sigyi Ai Studio မှ ကြိုဆိုပါတယ်! (System Ready ✅)", reply_markup=main_kb())
    txt, kb = dash_ui(u, uid)
    await update.message.reply_text(txt, reply_markup=kb)

async def handle_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u = get_u(uid)
    if not u.get("verified") or not (await check_joined(context.bot, uid)):
        set_u(uid, "verified", False)
        await update.message.reply_text(join_text(), reply_markup=join_kb(), disable_web_page_preview=True)
        return
    if update.message.photo:
        f = await context.bot.get_file(update.message.photo[-1].file_id)
        await f.download_to_drive(f"logo_{uid}.jpg")
        set_u(uid, "logo", True)
        txt, kb = dash_ui(get_u(uid), uid)
        await update.message.reply_text("✅ Logo ပုံ သိမ်းဆည်းပြီးပါပြီ!\n\n" + txt, reply_markup=kb)
        return
    t = (update.message.text or "").strip()
    if t in ["🎬 Dashboard", "🔑 API Key", "🎙️ Voice ရွေးရန်", "🖼️ Logo ပုံထည့်ရန်"]:
        if t == "🖼️ Logo ပုံထည့်ရန်":
            await update.message.reply_text("🖼️ ဘယ်ဘက်အပေါ်ထောင့်မှာ ထည့်ချင်တဲ့ Logo ပုံ (Photo) ကို ပို့ပေးပါ။")
            return
        txt, kb = dash_ui(u, uid) if t == "🎬 Dashboard" else (key_menu_ui(u) if t == "🔑 API Key" else voice_menu_ui(u))
        await update.message.reply_text(txt, reply_markup=kb)
        return
    um = re.search(r"https?://[^\s，。、！!\"'<>]+", t)
    p_keys = extract_keys(t) if not um else []
    if p_keys:
        added, total = add_user_keys(uid, p_keys)
        try_run(lambda: asyncio.create_task(update.message.delete()))
        txt, kb = dash_ui(get_u(uid), uid)
        await context.bot.send_message(uid, f"✅ API Key ({added}) ခု အသစ်ထပ်ပေါင်းပြီး စုစုပေါင်း ({total}) ခု သိမ်းဆည်းပြီးပါပြီ!\n\n" + txt, reply_markup=kb)
        return
    if not u.get("keys"):
        txt, kb = key_menu_ui(u)
        await update.message.reply_text("🔒 Gemini API Key အရင်ထည့်ပေးပါ!\n\n" + txt, reply_markup=kb)
        return
    cancel_user_task(uid)
    msg = await update.message.reply_text("⏳ [15%] Ultra HD Video ဆွဲယူနေပါတယ်...\n💡 (အကယ်၍ ရပ်နေပါက /start နှိပ်ပါ)", reply_markup=cancel_kb())
    vpath = f"in_{uid}.mp4"
    try:
        if um:
            await asyncio.wait_for(asyncio.to_thread(dl_vid, um.group(0), vpath), timeout=180)
        if not um and (update.message.video or update.message.document):
            v = update.message.video or update.message.document
            f = await context.bot.get_file(v.file_id)
            await f.download_to_drive(vpath)
        if not is_valid_video(vpath):
            await msg.edit_text("❌ Video ဖိုင် မမှန်ကန်ပါ သို့မဟုတ် Video Link ပို့ပါ!\n💡 (Bot အလုပ်မလုပ်ပါက /start နှိပ်ပါ)")
            return
        SESSIONS[uid] = vpath
        txt, kb = dash_ui(u, uid)
        btn = InlineKeyboardButton("🚀 START RENDER (Gemini TTS | Video 0.9x)", callback_data="r|go")
        await msg.edit_text("🎬 Video ရပြီ! စတင်ရန် START RENDER ကို နှိပ်ပါ -\n\n" + txt, reply_markup=InlineKeyboardMarkup(list(kb.inline_keyboard) + [[btn]]))
    except Exception as e:
        err_m = str(e).strip() or type(e).__name__
        await msg.edit_text(f"❌ Download Error: {err_m}\n\n💡 Bot အလုပ်မလုပ်ပါက /start ကို နှိပ်ပါ။")

async def run_render_job(q, context, uid):
    u, vpath = get_u(uid), SESSIONS.get(uid)
    sm_v, apath, opath = f"sm_{uid}.mp4", f"aud_{uid}.m4a", f"out_{uid}.mp4"
    hint="\n\n💡 ရပ်နေပါက /start နှိပ်ပြီး video ပြန်ပို့ပါ"
    stage="စတင်နေပါသည်"
    try:
        if not vpath or not is_valid_video(vpath):
            raise RuntimeError("Input video မမှန်ကန်ပါ")
        source_vdur=max(1.0,get_dur(vpath))
        target_vdur=source_vdur / VIDEO_SPEED
        n_win=3 if source_vdur<=28 else (4 if source_vdur<=60 else (5 if source_vdur<=110 else 6))
        stage="Gemini scene analysis"
        await q.edit_message_text(f"🔄 [30%] Video ကို {n_win} ပိုင်းခွဲပြီး scene analysis လုပ်နေပါသည်...{hint}",reply_markup=cancel_kb())
        seg_scripts=await asyncio.wait_for(asyncio.to_thread(ai_frame_audio_translate,vpath,sm_v,uid,source_vdur,n_win),timeout=max(300,int(source_vdur*3)))
        if len(seg_scripts)!=n_win: raise RuntimeError("AI က narration segments မပြည့်စုံပါ")
        stage="TTS voice generation"
        await q.edit_message_text(f"🔄 [60%] 🎙️ {u['v']} voice ကို clean timeline အဖြစ် တည်ဆောက်နေပါသည်...{hint}",reply_markup=cancel_kb())
        raw_durs=await asyncio.wait_for(build_free_synced_voice(seg_scripts,u["v"],apath,uid),timeout=max(600,int(source_vdur*5)))
        if not raw_durs or not is_valid_audio(apath): raise RuntimeError("Narration audio မမှန်ကန်ပါ")
        stage="FFmpeg video/audio render"
        await q.edit_message_text(f"🔄 [82%] Master clock နဲ့ video/audio sync လုပ်နေပါသည်...{hint}",reply_markup=cancel_kb())
        await asyncio.wait_for(asyncio.to_thread(edit_vid,vpath,apath,opath,u,uid,source_vdur,raw_durs),timeout=max(900,int(target_vdur*8)))
        stage="Output validation"
        final=get_dur(opath)
        if abs(final-target_vdur)>0.8: raise RuntimeError(f"Output duration mismatch: {target_vdur:.2f}s → {final:.2f}s")
        await q.edit_message_text("📤 [98%] Telegram သို့ တင်ပို့နေပါသည်..." )
        with open(opath,"rb") as vf:
            await context.bot.send_video(uid,vf,caption="Powered by SiGyi Ai Studio",read_timeout=300,write_timeout=300)
        await q.delete_message()
    except asyncio.CancelledError: pass
    except asyncio.TimeoutError:
        try_run(lambda: asyncio.create_task(q.edit_message_text(f"❌ {stage} အဆင့်မှာ အချိန်ကျော်သွားပါတယ်။ Video ရှည်လွန်းခြင်း သို့မဟုတ် API/server နှေးခြင်း ဖြစ်နိုင်ပါတယ်။\n\n💡 Video တိုတစ်ခုနဲ့ ပြန်စမ်းပါ၊ သို့မဟုတ် /start နှိပ်ပြီး ပြန်စပါ")))
    except Exception as e:
        err=str(e).strip() or type(e).__name__
        try_run(lambda: asyncio.create_task(q.edit_message_text(f"❌ {stage} Error: {err[:900]}\n\n💡 /start နှိပ်ပြီး ပြန်စမ်းပါ")))
    finally:
        ACTIVE_TASKS.pop(uid,None); SESSIONS.pop(uid,None)
        for p in [vpath,sm_v,apath,opath]:
            if p and os.path.exists(p): try_run(os.remove,p)


async def cb_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid, u = q.from_user.id, get_u(q.from_user.id)
    cat, act = q.data.split("|")
    if cat == "verify":
        if not (await check_joined(context.bot, uid)):
            await q.answer("❌ Channel (၃) ခုလုံးကို အရင် Join ပေးပါ!", show_alert=True)
            return
        set_u(uid, "verified", True)
        await q.answer("✅ Verify အောင်မြင်ပါပြီ!")
        await context.bot.send_message(uid, "🎉 Verify အောင်မြင်ပါပြီ! Sigyi Ai Studio ကို စတင်အသုံးပြုနိုင်ပါပြီ။", reply_markup=main_kb())
        txt, kb = dash_ui(get_u(uid), uid)
        await q.edit_message_text(txt, reply_markup=kb)
        return
    if cat == "r" and act == "cancel":
        cancel_user_task(uid)
        await q.answer("🛑 ရပ်တန့်လိုက်ပါပြီ!")
        txt, kb = dash_ui(get_u(uid), uid)
        await q.edit_message_text("🛑 လုပ်ငန်းစဉ်ကို ရပ်တန့်ပြီး Reset လုပ်ပေးထားပါသည်။\n\n" + txt, reply_markup=kb)
        return
    if not u.get("verified") or not (await check_joined(context.bot, uid)):
        set_u(uid, "verified", False)
        await q.answer("🔒 Channel (၃) ခုကို အရင် Join ပေးပါ!", show_alert=True)
        await q.edit_message_text(join_text(), reply_markup=join_kb(), disable_web_page_preview=True)
        return
    await q.answer()
    if cat == "m" and act in ["key", "vmenu"]:
        txt, kb = key_menu_ui(u) if act == "key" else voice_menu_ui(u)
        await q.edit_message_text(txt, reply_markup=kb)
        return
    if cat == "m" and act == "clrkeys":
        set_u(uid, "keys", [])
        set_u(uid, "k_idx", 0)
        txt, kb = key_menu_ui(get_u(uid))
        await q.edit_message_text(txt, reply_markup=kb)
        return
    if cat == "v" and act in VOICES:
        set_u(uid, "v", act)
    if cat == "m" and act in ["lay", "blur", "logo", "f"]:
        bm = ["auto", "low", "mid", "high", "off"]
        if act == "lay":
            set_u(uid, "layout", "full" if u["layout"] == "frame" else "frame")
        if act == "blur":
            set_u(uid, "blur", bm[(bm.index(u["blur"] if u["blur"] in bm else "auto") + 1) % len(bm)])
        if act == "logo":
            set_u(uid, "logo", not u["logo"])
        if act == "f":
            set_u(uid, "f", not u["f"])
    if cat in ["v", "m"]:
        u_new = get_u(uid)
        txt, kb = dash_ui(u_new, uid)
        rows = list(kb.inline_keyboard)
        if uid in SESSIONS and os.path.exists(SESSIONS[uid]):
            rows.append([InlineKeyboardButton("🚀 START RENDER (Gemini TTS | Video 0.9x)", callback_data="r|go")])
        await q.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(rows))
        return
    if cat == "r" and act == "go":
        if uid not in SESSIONS or not os.path.exists(SESSIONS[uid]):
            await q.edit_message_text("⚠️ Video အသစ် ပြန်ပို့ပါ။ (Bot အလုပ်မလုပ်ပါက /start နှိပ်ပါ)")
            return
        if not u.get("keys"):
            txt, kb = key_menu_ui(u)
            await q.edit_message_text("🔒 API Key အရင်ထည့်ပေးပါ!\n\n" + txt, reply_markup=kb)
            return
        if (old_t := ACTIVE_TASKS.pop(uid, None)) and not old_t.done():
            old_t.cancel()
        ACTIVE_TASKS[uid] = asyncio.create_task(run_render_job(q, context, uid))

def main():
    if not TOKEN:
        raise RuntimeError("Railway Variables ထဲ TELEGRAM_BOT_TOKEN မသတ်မှတ်ရသေးပါ။")
    app = Application.builder().token(TOKEN).concurrent_updates(True).read_timeout(240).write_timeout(240).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO | filters.VIDEO | filters.Document.VIDEO | (filters.TEXT & ~filters.COMMAND), handle_msg))
    app.add_handler(CallbackQueryHandler(cb_handler))
    app.run_polling()

if __name__ == "__main__":
    main()