import os, re, sys, json, time, wave, base64, asyncio, subprocess, urllib.request, urllib.parse, hashlib, shutil, random, math
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from google import genai
from google.genai import types
import yt_dlp

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
DB_FILE = "users.json"
SESSIONS, ACTIVE_TASKS = {}, {}

# Microsoft Edge TTS voice IDs (Burmese neural voices).
VOICES = ["my-MM-NilarNeural", "my-MM-ThihaNeural"]
VOICE_MAP = {name: name for name in VOICES}
VIDEO_SPEED = 0.9
VOICE_SPEED = 1.25
# Advance scene transitions slightly to compensate for observed ~1s visual lag.
# Small frame-level anticipation only; large offsets make narration/visuals drift.
VIDEO_SYNC_ADVANCE = max(0.0, min(0.12, float(os.getenv("VIDEO_SYNC_ADVANCE_SECONDS", "0.04"))))
WORD_ALIGNMENT_ENABLED = os.getenv("WORD_ALIGNMENT_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")
WORD_ALIGNMENT_MODEL = os.getenv("WORD_ALIGNMENT_MODEL", "tiny").strip() or "tiny"
_WORD_ALIGNER = None
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
    v = d.get("v", "my-MM-NilarNeural") if d.get("v") in VOICES else "my-MM-NilarNeural"
    if k not in db or "keys" not in d or d.get("v") not in VOICES or not d.get("visual_v2"):
        # One-time migration: enable mirror by default in the upgraded visual pipeline.
        db[k] = {"keys": keys, "k_idx": d.get("k_idx", 0), "v": v,
                 "f": False,
                 "visual_v2": True, "layout": d.get("layout", "frame"),
                 "blur": d.get("blur", "auto"), "logo": d.get("logo", True),
                 "verified": d.get("verified", False)}
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
    """Extract Google API keys even when pasted with labels or other text."""
    text = text or ""
    # Prefer recognizable Google key prefixes, so "Key 1: AIza..." still works.
    prefixed = re.findall(r"(?<![A-Za-z0-9_\-])(?:AIza[A-Za-z0-9_\-]{25,}|AQ\.[A-Za-z0-9_\-.]{20,})", text)
    if prefixed:
        return list(dict.fromkeys(x.strip().rstrip(".,;:)\"'") for x in prefixed))
    raw = re.split(r"[\s,;]+", text.strip())
    found = []
    for token in raw:
        token = token.strip().strip("[](){}<>\"'` ")
        if len(token) >= 25 and not token.startswith(("http", "/")) and re.fullmatch(r"[A-Za-z0-9_\-.]+", token):
            found.append(token)
    return list(dict.fromkeys(found))

def add_user_keys(uid, new_keys):
    cur = list(dict.fromkeys(k.strip() for k in get_u(uid).get("keys", []) if k and k.strip()))
    added = 0
    for key in new_keys:
        key = (key or "").strip()
        if key and key not in cur:
            cur.append(key)
            added += 1
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
    txt = f"🎙️ Edge TTS အသံရွေးချယ်ရန်\nအသံအမြန်နှုန်း: {VOICE_SPEED:.2f}x | Video အမြန်နှုန်း: Auto Sync\nရွေးထားသောအသံ: {u.get('v', 'my-MM-NilarNeural')}\n━━━━━━━━━━━━━━"
    rows = []
    for i in range(0, len(VOICES), 2):
        rows.append([InlineKeyboardButton(f"🎙️ {v}" + (" ✅" if u.get("v", "my-MM-NilarNeural") == v else ""), callback_data=f"v|{v}") for v in VOICES[i:i+2]])
    rows.append([InlineKeyboardButton("🔙 Dashboard သို့ ပြန်သွားရန်", callback_data="m|dash")])
    return txt, InlineKeyboardMarkup(rows)

def dash_ui(u, uid):
    k_cnt = len(u.get("keys", []))
    kt = f"✅ ({k_cnt}) ခု ချိတ်ပြီး" if k_cnt > 0 else "❌ မရှိသေးပါ"
    v_lbl = f"{u.get('v', 'my-MM-NilarNeural')} ({VOICE_SPEED:.2f}x)"
    lay = "📱 Blur Frame" if u["layout"] == "frame" else "📲 Full Screen"
    bm = {"auto": "🎯 Auto-Detect", "low": "အောက်ခြေ", "mid": "အလယ်အောက်", "high": "အလယ်", "off": "OFF ❌"}
    b_txt = bm.get(u["blur"], "🎯 Auto-Detect")
    lg_t = ("ON ✅" if u["logo"] else "OFF ❌") if os.path.exists(f"logo_{uid}.jpg") else "ပုံမထည့်ရသေး"
    kb = [[InlineKeyboardButton(f"🔑 API Keys: {kt}", callback_data="m|key")], [InlineKeyboardButton(f"🎙️ Voice: {v_lbl}", callback_data="m|vmenu"), InlineKeyboardButton(f"🖼️ {lay}", callback_data="m|lay")], [InlineKeyboardButton(f"🌫️ Blur: {b_txt}", callback_data="m|blur"), InlineKeyboardButton(f"🏷️ Logo: {lg_t}", callback_data="m|logo")], [InlineKeyboardButton("🎨 Crop + Filter + Adjust: ON ✅", callback_data="m|fxinfo")]]
    txt = f"⚡ SIGYI AI STUDIO PRO (HD 720p) ⚡\n━━━━━━━━━━━━━━\n• 🔑 API Keys: {kt} (Auto-Rotation ✅)\n• 🤖 Model: API Key နှင့် ကိုက်ညီသော Gemini Model (Auto)\n• 🎙️ Voice: {v_lbl} | 📺 HD 720p\n• 🖼️ {lay} | 🌫️ Blur: {b_txt}\n• 🎯 Word-level AI Alignment + Audio-Master Sync: Active ✅\n━━━━━━━━━━━━━━\n📥 TikTok / YouTube / Rednote Link သို့မဟုတ် Video ပို့ပါ!\n💡 (Bot အလုပ်မလုပ်ပါက /start ကို နှိပ်ပါ)"
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

def clean_narration_segment(text):
    """Remove occasional Gemini meta-introductions without rewriting the story."""
    text = clean_mm_text(text)
    # These are narration instructions/meta phrases, not story content.
    patterns = [
        r"^(?:ကဲ\s*)?(?:အခု\s*)?(?:ဒီ\s*)?ဗီဒီယို(?:ထဲက|ထဲတွင်|မှာ|ကို)?\s*",
        r"^(?:ကျွန်တော်|ကျွန်မ|ကျွန်ုပ်)\s*(?:ကတော့\s*)?",
        r"^(?:ဒီဇာတ်လမ်းကို\s*)?ဘာသာပြန်(?:ပေးပါမည်|ထားပါတယ်|ပေးလိုက်ပါမယ်)[၊။\s]*",
        r"^(?:အခု\s*)?(?:ဇာတ်လမ်းကို\s*)?စတင်(?:ကြည့်ရအောင်|လိုက်ရအောင်)[၊။\s]*",
    ]
    for pattern in patterns:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"(?:ဒီဗီဒီယိုကို\s*)?ဘာသာပြန်ပေးပါမည်[။၊\s]*", "", text).strip()
    return text

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
    subprocess.run(["ffmpeg", "-y", "-threads", "2", "-i", vpath, "-vf", "fps=3,scale=480:-2:flags=lanczos", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "32", "-c:a", "aac", "-ac", "1", "-ar", "16000", "-b:a", "48k", sm_v], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=max(90, min(240, int(vdur * 2))))
    up_file = sm_v if (os.path.exists(sm_v) and os.path.getsize(sm_v) > 1000) else vpath
    u = get_u(uid)
    keys = u.get("keys", [])
    if not keys:
        raise Exception("API Key မရှိပါ။ Key အရင်ထည့်ပေးပါ။")
    start_idx = u.get("k_idx", 0) % len(keys)
    w_dur = vdur / n_win
    chars_w = max(30, min(90, int(w_dur * 10.5)))
    win_desc = "၊ ".join([f"အပိုင်း {i+1} ({i*w_dur:.1f}s မှ {min(vdur, (i+1)*w_dur):.1f}s)" for i in range(n_win)])
    p = f"ဒီ Video ဖိုင်ကို အချိန်လိုက် သေချာကြည့်ပြီး မူရင်းအသံကိုလည်း နားထောင်ပါ။ အောက်ပါ အချိန်ပိုင်းတစ်ခုချင်းစီအတွက် အဲဒီအချိန်မှာ တကယ်မြင်ရတဲ့ လှုပ်ရှားမှု/တုံ့ပြန်မှုနဲ့ ကိုက်ညီတဲ့ Burmese movie recap ကိုသာ ရေးပါ။ နောက်ပိုင်းဖြစ်ရပ်ကို စောမပြောပါနှင့်၊ အရင်ဖြစ်ရပ်ကို ပြန်မပြောပါနှင့်။ အပိုင်းတိုတို (တစ်ပိုင်းလျှင် ပုံမှန် 3–5 စက္ကန့်) ခွဲထားသည်။ narration စာကြောင်းနဲ့ KEEP clip အချိန်က တူညီတဲ့ အဖြစ်အပျက်ကို ဖော်ပြရမည်။ {n_win} ပိုင်း: {win_desc}\n\n"
    p += "အရေးကြီးသော စည်းကမ်းများ - narration ထဲတွင် မိတ်ဆက်စကား၊ ခေါင်းစဉ်၊ နှုတ်ခွန်းဆက်စကား၊ သို့မဟုတ် meta စကားများ (ဥပမာ ‘ဘာသာပြန်ပေးပါမည်’, ‘ဒီဗီဒီယိုမှာ’, ‘အခု ဘာသာပြန်ကြည့်ရအောင်’) လုံးဝ မထည့်ပါနှင့်။ ဇာတ်လမ်းကို တိုက်ရိုက်စတင်ပါ။ အပိုင်းတိုင်းတွင် တူညီသော recap ပုံစံကို သုံးပါ။ မမြင်ရ/မကြားရသော အချက်အလက် မတီထွင်ပါနှင့်။\n\n" + "အတိအကျ လိုက်နာရမည့် ဥပမာ အရေးအသားပုံစံ -\n"
    p += "အမျိုးသားတစ်ယောက်ဟာ လှေပေါ်မှာ ဓားနဲ့ ကစားနေရင်း မတော်တဆ အပေါက်ဖောက်မိသွားတဲ့အတွက် ရေတွေ စတင် စိမ့်ဝင်လာပါတယ်။ ရေတွေက သင်္ဘောကုန်းပတ်ပေါ်အထိ လျင်မြန်စွာ တက်လာချိန်မှာပဲ တခြားလှေတစ်စင်း ချဉ်းကပ်လာတာကို သူ မြင်လိုက်ရပါတယ်။ ||| သူက သူ့ခြေထောက်နဲ့ အပေါက်ကို အမြန်ဖုံးအုပ်ပြီး ဘာမှမဖြစ်သလို ဟန်ဆောင်လိုက်ပါတယ်။ ရုတ်တရက်ဆိုသလိုပဲ ကင်းလှည့်ရေယာဉ်တစ်စင်း သူတို့ရှေ့မှာ ပေါ်လာပါတယ်။ သူတို့တွေက ငါးဖမ်းသမားတွေလို ဟန်ဆောင်ကြပေမဲ့ ရန်သူခေါင်းဆောင်ကတော့ တစ်ခုခု လွဲနေတယ်လို့ သံသယဝင်သွားပြီး စစ်ဆေးဖို့အတွက် သူတို့လှေပေါ်ကို တက်လာပါတော့တယ်။ ||| ခေါင်းဆောင်က အန်နာ ဆီကို လျှောက်သွားပြီး ငါးမျှားတံကို ကောက်ကိုင်ကာ ငါးဖမ်းဟန်ဆောင်လိုက်ပါတယ်။ အန်နာက လျင်မြန်စွာ လှည့်လိုက်တဲ့အခါ ငါးမျှားတံက သူမလက်ထဲကနေ လွင့်စင်သွားခဲ့ပါတယ်။\n\n"
    p += f"စည်းကမ်းချက် - အပိုင်းတစ်ပိုင်းစီကို မြန်မာစာလုံးရေ {chars_w} လုံးခန့်၊ 3–5 စက္ကန့်အတွင်း ဖတ်ပြီးဆုံးနိုင်မည့် တိုတောင်းသော narration တစ်ခုအဖြစ် ရေးပါ။ အပိုင်းကြားတွင် ||| သုံးပါ။ အပိုင်းတိုင်းကို ဤပုံစံဖြင့်ရေးပါ: [KEEP: 00:03.2-00:04.8,00:05.0-00:07.4] narration။ KEEP အချိန်သည် မူရင်း video မှ စက္ကန့်အချိန်ဖြစ်ပြီး သက်ဆိုင်ရာ window အတွင်းက အချိန်သာ ဖြစ်ရမည်။ ဒသမစက္ကန့် 1 လုံးအထိ တိတိကျကျ ခန့်မှန်းပါ။ narration ထဲက လုပ်ရပ်နဲ့ ကိုက်ညီတဲ့ action/reaction shot ကိုသာ KEEP လုပ်ပါ။ တိတ်ဆိတ်မှု၊ ထပ်နေသော shot၊ အလွတ်လမ်းလျှောက်မှု၊ transition နှင့် ဇာတ်လမ်းမတိုးစေသောအပိုင်းကို ဖြုတ်ပါ။ သို့သော် narration ထဲမှာ ဖော်ပြထားသော action ကို ပြသသည့် frame များကို မဖြုတ်ပါနှင့်။ အချိန်ကို သေချာမသိလျှင် အဲဒီ window အပြည့်ကို KEEP လုပ်ပါ—အချိန်မတီထွင်ပါနှင့်။ KEEP metadata ကို narration အဖြစ် မဖတ်ရပါ။ အပိုင်းများကို မကျော်ပါနှင့်၊ အစဉ်လိုက်ထားပါ၊ မိတ်ဆက်/နိဂုံး မထည့်ပါနှင့်။ လူနာမည်များကို မြန်မာလိုရေးပါ။"
    raw_txt, last_err = "", None
    for step_i in range(len(keys)):
        k_i = (start_idx + step_i) % len(keys)
        client = genai.Client(api_key=keys[k_i].strip(), http_options=types.HttpOptions(timeout=40000))
        model_candidates = get_generate_models(client)
        uf = try_run(lambda: client.files.upload(file=up_file))
        if not uf:
            continue
        for _ in range(75):
            state_name = getattr(getattr(uf, "state", None), "name", "")
            if state_name != "PROCESSING":
                break
            time.sleep(1)
            uf = try_run(lambda: client.files.get(name=uf.name)) or uf
        if getattr(getattr(uf, "state", None), "name", "") == "PROCESSING":
            last_err = RuntimeError("Gemini video file is still processing after 75 seconds")
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
            raise Exception("Gemini API 429 RESOURCE_EXHAUSTED ဖြစ်နေပါသည်။ Key အများကြီးထည့်ထားသော်လည်း key များသည် project တစ်ခုတည်းမှဖြစ်ပါက quota ကို မျှဝေနိုင်ပါသည်။ Google AI Studio ရှိ project တစ်ခုချင်းစီ၏ Usage/Quota ကို စစ်ပါ၊ quota ရှိသော project မှ key သုံးပါ သို့မဟုတ် reset ဖြစ်သည်အထိ စောင့်ပါ။")
        raise Exception(f"Gemini API Error: {err_text[:600]}")
    # Parse AI-selected source intervals. If the model omits/invalidates these,
    # keep the corresponding full window instead of risking missing story actions.
    raw_parts = [x.strip() for x in raw_txt.split("|||") if x.strip()]
    parts, keep_ranges = [], []
    time_re = re.compile(r"(\d{1,2}):(\d{2}(?:\.\d{1,2})?)\s*[-–—]\s*(\d{1,2}):(\d{2}(?:\.\d{1,2})?)")
    for i in range(n_win):
        window_start, window_end = i * w_dur, min(vdur, (i + 1) * w_dur)
        raw_part = raw_parts[i] if i < len(raw_parts) else ""
        marker = re.search(r"\[KEEP:\s*(.*?)\]", raw_part, flags=re.I)
        selected = []
        if marker:
            for m in time_re.finditer(marker.group(1)):
                a = int(m.group(1)) * 60 + float(m.group(2))
                b = int(m.group(3)) * 60 + float(m.group(4))
                a, b = max(window_start, float(a)), min(window_end, float(b))
                if b - a >= 0.65:
                    selected.append((a, b))
        # Merge overlapping ranges; use whole window if model selection is unsafe.
        selected.sort()
        merged = []
        for a, b in selected:
            if merged and a <= merged[-1][1] + 0.12:
                merged[-1] = (merged[-1][0], max(merged[-1][1], b))
            else:
                merged.append((a, b))
        kept = sum(b-a for a,b in merged)
        if not merged or kept < min(2.0, (window_end-window_start)*0.35):
            merged = [(window_start, window_end)]
        keep_ranges.extend((a, b, i) for a, b in merged)
        narration = re.sub(r"\[KEEP:.*?\]", "", raw_part, flags=re.I).strip()
        narration = clean_narration_segment(narration)
        parts.append(narration)
    if len(parts) != n_win or any(len(x) <= 5 for x in parts):
        # Preserve the existing robust fallback if the model ignored the format.
        plain = [clean_narration_segment(x) for x in raw_parts]
        plain = [x for x in plain if len(x) > 5]
        full_c = clean_mm_text(" ".join(plain) or raw_txt)
        sents = [s.strip() + "။" for s in full_c.split("။") if len(s.strip()) > 5] or [full_c]
        st = max(1, len(sents) / n_win)
        parts = [" ".join(sents[int(round(i*st)):max(int(round(i*st))+1, int(round((i+1)*st)) if i < n_win-1 else len(sents))]) or sents[-1] for i in range(n_win)]
        keep_ranges = [(i*w_dur, min(vdur, (i+1)*w_dur), i) for i in range(n_win)]
    return {"scripts": parts, "keep_ranges": keep_ranges}

def _edge_tts_to_wav(text, output_wav, voice_key, uid):
    """Use free Edge TTS first, then free Google Translate TTS as an automatic fallback."""
    voice_name = VOICE_MAP.get(voice_key, "my-MM-NilarNeural")
    normalized_text = clean_mm_text(text) or "အဲ့ဒီနောက်မှာတော့"
    cache_dir = ".tts_cache"
    os.makedirs(cache_dir, exist_ok=True)
    cache_id = hashlib.sha256(
        (normalized_text + "\0" + voice_name + "\0edge-gtts-fallback-v2").encode("utf-8")
    ).hexdigest()
    cache_path = os.path.join(cache_dir, cache_id + ".wav")

    if os.path.exists(cache_path) and is_valid_audio(cache_path):
        shutil.copy2(cache_path, output_wav)
        print(f"TTS cache hit | chars={len(normalized_text)}")
        return True
    elif os.path.exists(cache_path):
        try_run(os.remove, cache_path)

    mp3_path = output_wav + ".tts.mp3"
    errors = []

    # Primary: Microsoft Edge neural Burmese voices.
    try:
        import edge_tts

        async def _generate_edge():
            communicate = edge_tts.Communicate(
                text=normalized_text,
                voice=voice_name,
                rate="+0%",
            )
            await communicate.save(mp3_path)

        # This function runs in asyncio.to_thread, with its own event loop.
        asyncio.run(_generate_edge())
        if not os.path.exists(mp3_path) or os.path.getsize(mp3_path) < 500:
            raise RuntimeError("Edge TTS returned an empty/invalid audio file")
        conv = subprocess.run(
            ["ffmpeg", "-y", "-i", mp3_path, "-vn", "-ac", "1", "-ar", "24000",
             "-c:a", "pcm_s16le", output_wav],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60,
        )
        if conv.returncode != 0 or not is_valid_audio(output_wav):
            raise RuntimeError("Edge TTS MP3-to-WAV conversion failed: " + conv.stderr[-300:])
        engine = "Edge TTS"
    except Exception as exc:
        errors.append(f"Edge TTS: {str(exc)[:350]}")
        try_run(os.remove, mp3_path)
        try_run(os.remove, output_wav)

        # Fallback: gTTS uses Google's free Translate TTS endpoint (Burmese lang=my).
        try:
            from gtts import gTTS
            gTTS(text=normalized_text, lang="my", slow=False).save(mp3_path)
            if not os.path.exists(mp3_path) or os.path.getsize(mp3_path) < 300:
                raise RuntimeError("gTTS returned an empty/invalid audio file")
            conv = subprocess.run(
                ["ffmpeg", "-y", "-i", mp3_path, "-vn", "-ac", "1", "-ar", "24000",
                 "-c:a", "pcm_s16le", output_wav],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60,
            )
            if conv.returncode != 0 or not is_valid_audio(output_wav):
                raise RuntimeError("gTTS MP3-to-WAV conversion failed: " + conv.stderr[-300:])
            engine = "gTTS fallback"
        except Exception as fallback_exc:
            errors.append(f"gTTS fallback: {str(fallback_exc)[:350]}")
            try_run(os.remove, output_wav)
            raise RuntimeError(
                "Free TTS နှစ်မျိုးလုံး မအောင်မြင်ပါ။ Internet connection, edge-tts/gTTS package "
                "နှင့် FFmpeg ကို စစ်ပါ။ အသေးစိတ်: " + " | ".join(errors)
            ) from fallback_exc

    try:
        tmp_cache = cache_path + f".{os.getpid()}.tmp"
        shutil.copy2(output_wav, tmp_cache)
        os.replace(tmp_cache, cache_path)
    finally:
        try_run(os.remove, mp3_path)
    print(f"TTS success | engine={engine} | voice={voice_name} | chars={len(normalized_text)}")
    return True


async def build_free_synced_voice(seg_scripts, voice_key, out_a, uid):
    """Generate Edge TTS clips, apply VOICE_SPEED, then join clean PCM."""
    voice = VOICE_MAP.get(voice_key, "my-MM-NilarNeural")
    work = f"tts_{uid}_{int(time.time() * 1000)}"
    os.makedirs(work, exist_ok=True)
    wavs, seg_durs = [], []
    try:
        for i, script in enumerate(seg_scripts):
            text = clean_mm_text(script) or "အဲ့ဒီနောက်မှာတော့"
            raw = os.path.join(work, f"raw_{i}.wav")
            wav = os.path.join(work, f"seg_{i}.wav")
            wavs.append(wav)
            await asyncio.wait_for(asyncio.to_thread(_edge_tts_to_wav, text, raw, voice, uid), timeout=150)
            # Do not use silenceremove here: it can clip the first soft consonant/word.
            # Keep a tiny lead-in on the first segment so playback never starts mid-phoneme.
            lead_in = ",adelay=140" if i == 0 else ""
            af = f"atempo={VOICE_SPEED},highpass=f=70,loudnorm=I=-16:TP=-1.5:LRA=11,aresample=24000,asetpts=PTS-STARTPTS{lead_in}"
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

def align_narration_words(audio_path):
    """Get approximate word timestamps from the finished Burmese narration.

    Uses a small multilingual Whisper model on CPU. This is best-effort: if the
    model cannot load or the language is not recognized, the renderer safely
    falls back to the exact per-TTS-clip duration timeline.
    """
    global _WORD_ALIGNER
    if not WORD_ALIGNMENT_ENABLED:
        return []
    try:
        from faster_whisper import WhisperModel
        if _WORD_ALIGNER is None:
            _WORD_ALIGNER = WhisperModel(WORD_ALIGNMENT_MODEL, device="cpu", compute_type="int8")
        segments, info = _WORD_ALIGNER.transcribe(
            audio_path, language="my", word_timestamps=True,
            vad_filter=True, beam_size=3, condition_on_previous_text=False
        )
        words = []
        for seg in segments:
            for word in (getattr(seg, "words", None) or []):
                start, end = float(word.start), float(word.end)
                if end > start and start >= 0:
                    words.append({"start": start, "end": end, "text": str(word.word or "").strip()})
        words.sort(key=lambda x: x["start"])
        if words:
            print(f"Word-level alignment: {len(words)} timestamps; language={getattr(info, 'language', 'my')}")
        else:
            print("Word-level alignment returned no words; using exact TTS segment durations")
        return words
    except Exception as exc:
        print(f"Word-level alignment unavailable; using exact TTS segment durations: {str(exc)[:300]}")
        return []


def _snap_to_nearby_word_boundary(bounds, target_dur, word_timestamps, max_shift=0.12):
    """Snap a scene boundary to a nearby word edge only when the change is tiny.

    Word timestamps are ASR estimates, so this deliberately refuses large moves.
    """
    if not word_timestamps or len(bounds) <= 2:
        return bounds
    edges = []
    for word in word_timestamps:
        try:
            a, b = float(word["start"]), float(word["end"])
            if 0 <= a <= target_dur: edges.append(a)
            if 0 <= b <= target_dur: edges.append(b)
        except (TypeError, ValueError, KeyError):
            continue
    if not edges:
        return bounds
    out = [0.0]
    for boundary in bounds[1:-1]:
        nearest = min(edges, key=lambda x: abs(x - boundary))
        if abs(nearest - boundary) <= max_shift:
            out.append(nearest)
        else:
            out.append(boundary)
    out.append(float(target_dur))
    # Keep timestamps monotonic and leave at least two frames per visual segment.
    for i in range(1, len(out)-1):
        out[i] = max(out[i-1] + 2/24, min(out[i], target_dur - (len(out)-1-i)*2/24))
    return out


def _advance_scene_boundaries(bounds, target_dur, advance=None, word_timestamps=None):
    """Anticipate visual scene boundaries by a small frame-level offset; audio duration remains fixed."""
    if advance is None:
        advance = VIDEO_SYNC_ADVANCE
    bounds = _snap_to_nearby_word_boundary(bounds, target_dur, word_timestamps)
    if advance <= 0 or len(bounds) <= 2:
        return bounds
    n_internal = len(bounds) - 2
    out = [0.0]
    for idx in range(1, len(bounds) - 1):
        remaining = len(bounds) - 1 - idx
        low = out[-1] + 0.15
        high = target_dur - remaining * 0.15
        candidate = bounds[idx] - advance
        out.append(max(low, min(high, candidate)))
    out.append(float(target_dur))
    return out

def build_piecewise_pts(source_dur, seg_durs, target_dur=None, word_timestamps=None):
    """Map each equal source-video scene window onto the narration timeline."""
    source_dur = max(0.1, float(source_dur))
    weights = [max(0.05, float(x)) for x in (seg_durs or [])]

    # Audio is the master clock. Use the actual audio duration when supplied;
    # otherwise derive a target from the narration segment durations.
    if target_dur is None:
        target_dur = sum(weights) if weights else source_dur / max(0.1, VIDEO_SPEED)
    target_dur = max(0.1, float(target_dur))

    if not weights:
        return (
            f"setpts=PTS/{VIDEO_SPEED:.6f},fps=30,"
            f"tpad=stop_mode=clone:stop_duration={target_dur + 2.0:.6f},"
            f"trim=duration={target_dur:.6f},setpts=PTS-STARTPTS"
        )

    n = len(weights)
    source_step = source_dur / n
    total_weight = sum(weights)
    # Convert segment durations into boundaries on the exact audio timeline.
    target_bounds = [0.0]
    elapsed = 0.0
    for duration in weights:
        elapsed += duration
        target_bounds.append(target_dur * elapsed / total_weight)
    target_bounds = _advance_scene_boundaries(target_bounds, target_dur, word_timestamps=word_timestamps)

    # FFmpeg setpts evaluates T in seconds. Each source scene window is mapped
    # linearly to its corresponding narration window, preserving scene order.
    expr = (
        f"{target_bounds[-2]:.6f}+(T-{(n-1)*source_step:.6f})*"
        f"{(target_bounds[-1]-target_bounds[-2])/source_step:.8f}"
    )
    for i in range(n-2, -1, -1):
        slope = (target_bounds[i+1] - target_bounds[i]) / source_step
        expr = (
            f"if(lt(T,{(i+1)*source_step:.6f}),"
            f"{target_bounds[i]:.6f}+(T-{i*source_step:.6f})*{slope:.8f},"
            f"{expr})"
        )
    return (
        f"settb=AVTB,setpts='({expr})/TB',fps=30,"
        f"tpad=stop_mode=clone:stop_duration={target_dur + 2.0:.6f},"
        f"trim=duration={target_dur:.6f},setpts=PTS-STARTPTS"
    )

def _atempo_chain(ratio):
    ratio=max(0.1,min(20.0,float(ratio)))
    parts=[]
    while ratio>2.0: parts.append("atempo=2.0"); ratio/=2.0
    while ratio<0.5: parts.append("atempo=0.5"); ratio/=0.5
    parts.append(f"atempo={ratio:.8f}")
    return ",".join(parts)

def edit_vid(iv, ia, ov, u, uid, vdur, seg_durs):
    """Render with a single master clock and validate both streams before returning."""
    source_dur=get_dur(iv); audio_dur=max(0.1,get_dur(ia))
    # Narration is the master clock: final video duration must match audio.
    target=max(0.1, audio_dur)
    is_fr=u.get("layout","frame")=="frame"; fl=""  # Flip/mirror intentionally disabled
    adj="crop=iw*0.92:ih*0.92:iw*0.04:ih*0.04,eq=contrast=1.08:brightness=0.02:saturation=1.14:gamma=1.03,hue=h=3,unsharp=3:3:0.65:3:3:0.0"
    # FFmpeg-only scene sync: each source scene window is mapped to its
    # matching narration segment duration. The audio remains the master clock.
    # seg_durs are measured AFTER VOICE_SPEED is applied, so scene boundaries
    # follow the actual final voiceover timing rather than the original TTS.
    playback_speed = source_dur / target
    keep_ranges = seg_durs if isinstance(seg_durs, dict) else None
    word_timestamps = (keep_ranges or {}).get("word_timestamps", []) if keep_ranges else []
    if keep_ranges:
        # Cut AI-rejected intervals first, concatenate selected clips, then map each
        # kept source-window duration onto its matching narration segment duration.
        ranges = keep_ranges.get("keep_ranges") or []
        weights = [max(0.05, float(x)) for x in keep_ranges.get("audio_segment_durs", [])]
        ranges = [(max(0.0,float(a)), min(source_dur,float(b)), int(i)) for a,b,i in ranges if float(b)-float(a) >= 0.65]
        if not ranges or not weights or max((i for _,_,i in ranges), default=-1) >= len(weights):
            keep_ranges = None
        else:
            # Do not let an uncertain AI response remove most of the footage.
            kept_total = sum(b-a for a,b,_ in ranges)
            if kept_total < source_dur * 0.45:
                ranges = [(0.0, source_dur, 0)]
                weights = [target]
            trim_nodes = []
            # Split the input stream first because FFmpeg cannot fan one input pad
            # directly into several independent trim filters.
            split_labels = "".join(f"[src{j}]" for j in range(len(ranges)))
            trim_nodes.append(f"[0:v]split={len(ranges)}{split_labels}")
            for j,(a,b,_) in enumerate(ranges):
                trim_nodes.append(f"[src{j}]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS[cut{j}]")
            if len(ranges) == 1:
                trim_nodes.append("[cut0]null[rawv]")
            else:
                concat_inputs = "".join(f"[cut{j}]" for j in range(len(ranges)))
                trim_nodes.append(f"{concat_inputs}concat=n={len(ranges)}:v=1:a=0[rawv]")
            # Build a piecewise PTS map using actual retained duration per narration window.
            src_group = [0.0] * len(weights)
            for a,b,i in ranges:
                src_group[i] += b-a
            if any(x <= 0.05 for x in src_group):
                src_group = [sum(b-a for a,b,_ in ranges)]
                weights = [target]
            source_bounds, target_bounds = [0.0], [0.0]
            for x in src_group: source_bounds.append(source_bounds[-1] + x)
            wsum = sum(weights)
            for x in weights: target_bounds.append(target_bounds[-1] + target*x/wsum)
            target_bounds = _advance_scene_boundaries(target_bounds, target, word_timestamps=word_timestamps)
            expr = f"{target_bounds[-1]:.6f}+(T-{source_bounds[-2]:.6f})*{((target_bounds[-1]-target_bounds[-2])/max(0.001,source_bounds[-1]-source_bounds[-2])):.8f}"
            for i in range(len(src_group)-2, -1, -1):
                slope=(target_bounds[i+1]-target_bounds[i])/max(0.001,source_bounds[i+1]-source_bounds[i])
                expr=f"if(lt(T,{source_bounds[i+1]:.6f}),{target_bounds[i]:.6f}+(T-{source_bounds[i]:.6f})*{slope:.8f},{expr})"
            video_clock = ";".join(trim_nodes) + f";[rawv]settb=AVTB,setpts='({expr})/TB',fps=24,tpad=stop_mode=clone:stop_duration={target + 2.0:.6f},trim=duration={target:.6f},setpts=PTS-STARTPTS[v0]"
            video_input_label = "[v0]"
    if not keep_ranges:
        video_clock = build_piecewise_pts(source_dur, seg_durs if not isinstance(seg_durs, dict) else seg_durs.get("audio_segment_durs", []), target, word_timestamps=word_timestamps).replace("fps=30", "fps=24")
        video_clock = f"[0:v]{video_clock}[v0]"
        video_input_label = "[v0]"
    print(
        f"FFmpeg scene sync | video={source_dur:.3f}s | voice={audio_dur:.3f}s | "
        f"video_avg_speed={playback_speed:.3f}x | segments={len(seg_durs or [])} | "
        f"voice_speed={VOICE_SPEED:.2f}x | visual_scene_advance={VIDEO_SYNC_ADVANCE:.2f}s"
    )
    b_map={"low":(950,86),"mid":(880,86),"high":(790,86)} if is_fr else {"low":(1040,92),"mid":(940,92),"high":(830,92)}
    by,bh=(detect_sub_band(iv,source_dur,is_fr,uid) if u.get("blur")=="auto" else b_map.get(u.get("blur"),(950,86)))
    by=(int(by)//2)*2; bh=max(64,(int(bh)//2)*2)
    # Aim for ~30 MiB total output so Telegram's upload limit is not hit.
    # Reserve ~96 kbps for audio and allow low bitrates for long narrations.
    kbps=max(900,min(4200,int((42*8192)/target)-140))
    inputs=["-i",iv,"-i",ia]
    # detect_sub_band returns coordinates based on the previous 720x1280 canvas.
    by=max(0,int(by)//2*2); bh=max(48,(int(bh)//2)*2)
    # video_clock already includes the input/output labels in both normal and AI-cut paths.
    fc=f"{video_clock};"
    # Keep source orientation unchanged: no horizontal flip or mirror.
    vlabel = "[v0]"
    if is_fr:
        fc+=f"{vlabel}split=2[bgi][fgi];[bgi]scale=180:320:force_original_aspect_ratio=increase,crop=180:320,gblur=sigma=18,eq=brightness=-0.07,scale=720:1280[bg];[fgi]{adj},scale=720:827:force_original_aspect_ratio=increase:flags=lanczos,crop=720:827[fg];[bg][fg]overlay=0:226[v1]"
    else:
        fc+=f"{vlabel}{adj},scale=720:1280:force_original_aspect_ratio=increase:flags=lanczos,crop=720:1280[v1]"
    cur="[v1]"
    if u.get("blur")!="off":
        fc+=f";{cur}split=2[m1][m2];[m2]crop=480:{bh}:30:{by},gblur=sigma=26[bst];[m1][bst]overlay=30:{by}[v2]"; cur="[v2]"
    logo=f"logo_{uid}.jpg"
    if u.get("logo") and os.path.exists(logo):
        inputs += ["-i",logo]; ly=124 if is_fr else 60
        fc+=f";[2:v]scale=82:82[lg];{cur}[lg]overlay=9:{ly}[v3]"; cur="[v3]"
    fc+=f";{cur}format=yuv420p[vout]"
    fc+=f";[1:a:0]asetpts=PTS-STARTPTS,aresample=async=1:first_pts=0,apad,atrim=duration={target:.6f},asetpts=PTS-STARTPTS[aout]"
    cmd=["ffmpeg","-y","-threads","2"]+inputs+["-map_metadata","-1","-map_chapters","-1","-filter_complex",fc,"-map","[vout]","-map","[aout]","-fps_mode","cfr","-r","24","-c:v","libx264","-preset","veryfast","-crf","20","-maxrate",f"{kbps}k","-bufsize",f"{kbps*2}k","-c:a","aac","-b:a","128k","-t",f"{target:.3f}","-avoid_negative_ts","make_zero","-movflags","+faststart",ov]
    r=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=max(600,int(target*8)))
    if r.returncode!=0 or not is_valid_video(ov) or not is_valid_audio(ov):
        # Remove partial output before a conservative second pass. This fallback
        # sacrifices AI cuts/effects but preserves the narration and full duration.
        try_run(os.remove, ov)
        fallback_flip = ""  # no flip/mirror in fallback render
        fallback_fc = (
            f"[0:v]settb=AVTB,setpts=PTS-STARTPTS,"
            f"setpts=PTS*{target/max(0.1,source_dur):.8f},fps=24,"
            f"tpad=stop_mode=clone:stop_duration={target + 2.0:.6f},"
            f"trim=duration={target:.6f},setpts=PTS-STARTPTS,"
            f"{fallback_flip}crop=iw*0.92:ih*0.92:iw*0.04:ih*0.04,"
            f"eq=contrast=1.08:brightness=0.02:saturation=1.14:gamma=1.03,"
            f"hue=h=3,unsharp=3:3:0.65:3:3:0.0,"
            f"scale=720:1280:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop=720:1280,format=yuv420p[vout];"
            f"[1:a:0]asetpts=PTS-STARTPTS,aresample=async=1:first_pts=0,"
            f"apad,atrim=duration={target:.6f},asetpts=PTS-STARTPTS[aout]"
        )
        fallback_cmd=["ffmpeg","-y","-threads","2"]+inputs+["-filter_complex",fallback_fc,
            "-map","[vout]","-map","[aout]","-fps_mode","cfr","-r","24",
            "-c:v","libx264","-preset","veryfast","-crf","22","-maxrate",f"{kbps}k",
            "-bufsize",f"{kbps*2}k","-c:a","aac","-b:a","128k","-t",f"{target:.3f}",
            "-max_muxing_queue_size","2048","-movflags","+faststart",ov]
        r2=subprocess.run(fallback_cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=max(600,int(target*8)))
        if r2.returncode!=0 or not is_valid_video(ov) or not is_valid_audio(ov):
            detail=(r2.stderr or r.stderr or "unknown FFmpeg error")[-2200:]
            raise RuntimeError("Scene-sync render နှင့် safe fallback နှစ်ခုလုံး မအောင်မြင်ပါ: "+detail)
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
    p_keys = extract_keys(t)
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
    msg = await update.message.reply_text("⏳ [15%] HD Video ဆွဲယူနေပါတယ်...\n💡 (အကယ်၍ ရပ်နေပါက /start နှိပ်ပါ)", reply_markup=cancel_kb())
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
        btn = InlineKeyboardButton(f"🚀 START RENDER (Edge TTS {VOICE_SPEED:.2f}x | Scene Sync)", callback_data="r|go")
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
        n_win=max(1, min(30, int(math.ceil(source_vdur / 4.5))))
        stage="Gemini scene analysis"
        await q.edit_message_text(f"🔄 [30%] Video ကို {n_win} ပိုင်းတိုတိုခွဲပြီး Action/Scene Alignment analysis လုပ်နေပါသည်...{hint}",reply_markup=cancel_kb())
        analysis=await asyncio.wait_for(asyncio.to_thread(ai_frame_audio_translate,vpath,sm_v,uid,source_vdur,n_win),timeout=max(240,int(source_vdur*2)))
        seg_scripts=analysis.get("scripts", []) if isinstance(analysis, dict) else analysis
        if len(seg_scripts)!=n_win: raise RuntimeError("AI က narration segments မပြည့်စုံပါ")
        stage="TTS voice generation"
        await q.edit_message_text(f"🔄 [60%] 🎙️ {u['v']} voice ကို clean timeline အဖြစ် တည်ဆောက်နေပါသည်...{hint}",reply_markup=cancel_kb())
        raw_durs=await asyncio.wait_for(build_free_synced_voice(seg_scripts,u["v"],apath,uid),timeout=max(600,int(source_vdur*5)))
        if not raw_durs or not is_valid_audio(apath): raise RuntimeError("Narration audio မမှန်ကန်ပါ")
        # Use the finished narration duration as the single master timeline.
        target_vdur=max(0.1, get_dur(apath))
        if isinstance(analysis, dict):
            analysis["audio_segment_durs"] = raw_durs
            # Word-level ASR timestamps refine only nearby scene boundaries; the
            # measured TTS clip durations remain the authoritative timing fallback.
            await q.edit_message_text(f"🔄 [72%] Burmese Word-level AI Alignment လုပ်နေပါသည်...{hint}",reply_markup=cancel_kb())
            analysis["word_timestamps"] = await asyncio.wait_for(
                asyncio.to_thread(align_narration_words, apath), timeout=max(180, min(420, int(source_vdur * 2)))
            )
        stage="FFmpeg video/audio render"
        await q.edit_message_text(f"🔄 [82%] အသံစာသားနဲ့ Scene အချိန်ကို frame-level နီးစပ်အောင် sync လုပ်နေပါသည်...{hint}",reply_markup=cancel_kb())
        await asyncio.wait_for(asyncio.to_thread(edit_vid,vpath,apath,opath,u,uid,source_vdur,analysis if isinstance(analysis, dict) else raw_durs),timeout=max(600,int(target_vdur*5)))
        stage="Output size check / compression"
        # Keep a safety margin below Telegram Bot API's upload-size ceiling.
        max_bot_size = 48 * 1024 * 1024
        file_size = os.path.getsize(opath) if os.path.exists(opath) else 0
        if file_size > max_bot_size:
            await q.edit_message_text(f"🔄 [90%] Telegram upload limit အတွက် Video ကို ပြန်ချုံ့နေပါသည်... ({file_size/(1024*1024):.1f}MB)", reply_markup=cancel_kb())
            compressed = f"out_{uid}_compressed.mp4"
            # First retry: keep 540x960, reduce video/audio bitrate.
            compress_kbps = max(700, min(3000, int((42 * 8192) / max(1.0, target_vdur) - 160)))
            compress_cmd = ["ffmpeg", "-y", "-threads", "2", "-i", opath,
                "-vf", "scale=720:1280:force_original_aspect_ratio=decrease,pad=720:1280:(ow-iw)/2:(oh-ih)/2,fps=24,format=yuv420p",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-maxrate", f"{compress_kbps}k", "-bufsize", f"{compress_kbps*2}k",
                "-c:a", "aac", "-b:a", "64k", "-t", f"{target_vdur:.3f}", "-movflags", "+faststart", compressed]
            r = await asyncio.wait_for(asyncio.to_thread(subprocess.run, compress_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=570), timeout=600)
            if r.returncode == 0 and os.path.exists(compressed) and is_valid_video(compressed) and is_valid_audio(compressed):
                if os.path.getsize(compressed) < os.path.getsize(opath):
                    os.replace(compressed, opath)
                else:
                    try_run(os.remove, compressed)
            else:
                try_run(os.remove, compressed)
            file_size = os.path.getsize(opath) if os.path.exists(opath) else 0
            # Last-resort retry: retain 720p while lowering bitrate for Telegram compatibility.
            if file_size > max_bot_size:
                await q.edit_message_text(f"🔄 [94%] Video ကို ထပ်မံချုံ့နေပါသည်... ({file_size/(1024*1024):.1f}MB)", reply_markup=cancel_kb())
                compressed = f"out_{uid}_compressed2.mp4"
                low_kbps = max(350, min(1200, int((42 * 8192) / max(1.0, target_vdur) - 180)))
                compress_cmd = ["ffmpeg", "-y", "-threads", "2", "-i", opath,
                    "-vf", "scale=720:1280:force_original_aspect_ratio=decrease,pad=720:1280:(ow-iw)/2:(oh-ih)/2,fps=24,format=yuv420p",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "24", "-maxrate", f"{low_kbps}k", "-bufsize", f"{low_kbps*2}k",
                    "-c:a", "aac", "-b:a", "48k", "-t", f"{target_vdur:.3f}", "-movflags", "+faststart", compressed]
                r = await asyncio.wait_for(asyncio.to_thread(subprocess.run, compress_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=570), timeout=600)
                if r.returncode == 0 and os.path.exists(compressed) and is_valid_video(compressed) and is_valid_audio(compressed):
                    if os.path.getsize(compressed) < os.path.getsize(opath):
                        os.replace(compressed, opath)
                    else:
                        try_run(os.remove, compressed)
                else:
                    try_run(os.remove, compressed)
                file_size = os.path.getsize(opath) if os.path.exists(opath) else 0
        stage="Output validation"
        final=get_dur(opath)
        if abs(final-target_vdur)>0.8: raise RuntimeError(f"Output duration mismatch: {target_vdur:.2f}s → {final:.2f}s")
        final_size_mb = os.path.getsize(opath) / (1024 * 1024)
        if final_size_mb > 49:
            raise RuntimeError(f"Video size ({final_size_mb:.1f}MB) still exceeds Telegram upload limit. Shorter video ဖြင့် ပြန်စမ်းပါ။")
        if not is_valid_video(opath) or not is_valid_audio(opath):
            raise RuntimeError("Output validation failed: video သို့မဟုတ် audio stream မပါဝင်ပါ")
        await q.edit_message_text(f"📤 [98%] Telegram သို့ တင်ပို့နေပါသည်... ({final_size_mb:.1f}MB)")
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
        for p in [vpath,sm_v,apath,opath,f"out_{uid}_compressed.mp4",f"out_{uid}_compressed2.mp4"]:
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
    if cat == "m" and act == "fxinfo":
        await q.answer("Crop, Filter, Adjust နဲ့ Video Blur ကို ဖွင့်ထားပါတယ်။ Flip/Mirror ကို ပိတ်ထားပါတယ်။", show_alert=True)
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
            set_u(uid, "f", False)
    if cat in ["v", "m"]:
        u_new = get_u(uid)
        txt, kb = dash_ui(u_new, uid)
        rows = list(kb.inline_keyboard)
        if uid in SESSIONS and os.path.exists(SESSIONS[uid]):
            rows.append([InlineKeyboardButton(f"🚀 START RENDER (Edge TTS {VOICE_SPEED:.2f}x | Scene Sync)", callback_data="r|go")])
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