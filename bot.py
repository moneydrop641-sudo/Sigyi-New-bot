import os, re, sys, json, time, wave, base64, asyncio, subprocess, urllib.request, urllib.parse, hashlib, shutil, random, math, tempfile, threading, queue
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
# Anchor-based scene sync. Keep the audio as the master clock; lead only scene
# transitions slightly instead of delaying/freezing the opening video frames.
FPS = 24
FRAME_DUR = 1.0 / FPS
VIDEO_LEAD_SECONDS = max(0.0, min(0.60, float(os.getenv(
    "VIDEO_LEAD_SECONDS", os.getenv("VIDEO_SYNC_ADVANCE_SECONDS", "0.30")
))))
ANCHOR_WINDOW_SECONDS = max(3.0, min(20.0, float(os.getenv("ANCHOR_WINDOW_SECONDS", "10.0"))))
MAX_DRIFT_SECONDS = max(0.10, min(0.80, float(os.getenv("MAX_DRIFT_SECONDS", "0.30"))))
# Backward compatibility for older code/logs.
VIDEO_SYNC_ADVANCE = VIDEO_LEAD_SECONDS
WORD_ALIGNMENT_ENABLED = os.getenv("WORD_ALIGNMENT_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")
WORD_ALIGNMENT_MODEL = os.getenv("WORD_ALIGNMENT_MODEL", "tiny").strip() or "tiny"
_WORD_ALIGNER = None
_AUDIO_ASR_MODEL = None
_AUDIO_ASR_MODEL_NAME = None
CHANNELS = [("@moviezone336", "https://t.me/moviezone336", "📢 Channel 1 Join ရန်"), ("@color6777", "https://t.me/color6777", "📢 Channel 2 Join ရန်"), ("@sigyi89", "https://t.me/sigyi89", "📢 Channel 3 Join ရန်")]
# Keep the default path on Flash models. Pro models commonly have zero free-tier
# quota and can make an otherwise healthy bot fail before the fallback is tried.
# Use currently supported stable Flash models by default. Explicitly remove
# shut-down endpoints even if the hosting environment still sets GEMINI_MODELS.
_RETIRED_MODEL_IDS = {
    "gemini-2.0-flash", "gemini-2.0-flash-001",
    "gemini-2.0-flash-lite", "gemini-2.0-flash-lite-001",
}
_SAFE_MODEL_DEFAULTS = [
    "gemini-3.5-flash-lite",
    "gemini-3.6-flash",
    "gemini-3.8-flash",
]
_configured_models = [
    x.strip().removeprefix("models/")
    for x in os.getenv("GEMINI_MODELS", ",".join(_SAFE_MODEL_DEFAULTS)).split(",")
    if x.strip() and "flash" in x.lower()
]
MODELS = list(dict.fromkeys(
    [m for m in _configured_models if m.lower() not in _RETIRED_MODEL_IDS]
    + _SAFE_MODEL_DEFAULTS
))

def get_safe_model_candidates(limit=None):
    """Return safe defaults first, then configured Flash fallbacks."""
    candidates = [m for m in _SAFE_MODEL_DEFAULTS if m and m.lower() not in _RETIRED_MODEL_IDS]
    candidates += [m for m in MODELS if m and m.lower() not in _RETIRED_MODEL_IDS and m not in candidates]
    return candidates[:limit] if limit else candidates

_TRANSIENT_API_MARKERS = (
    "503", "UNAVAILABLE", "500", "INTERNAL", "502", "BAD GATEWAY",
    "504", "DEADLINE_EXCEEDED", "429", "RESOURCE_EXHAUSTED",
    "QUOTA", "RATE LIMIT", "TOO MANY REQUESTS", "TIMEOUT", "TIMED OUT"
)

def _is_transient_api_error(exc):
    return any(marker in str(exc).upper() for marker in _TRANSIENT_API_MARKERS)

def _backoff_seconds(attempt, base=1.2, cap=8.0):
    """Bounded exponential backoff with jitter for temporary Gemini failures."""
    return min(cap, base * (2 ** max(0, int(attempt)))) + random.uniform(0.1, 0.6)

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
    lay = "📱 Blur Frame (Always ON)"
    b_txt = "စာတန်းဖုံး Blur: OFF"
    lg_t = ("ON ✅" if u["logo"] else "OFF ❌") if os.path.exists(f"logo_{uid}.jpg") else "ပုံမထည့်ရသေး"
    kb = [[InlineKeyboardButton(f"🔑 API Keys: {kt}", callback_data="m|key")], [InlineKeyboardButton(f"🎙️ Voice: {v_lbl}", callback_data="m|vmenu"), InlineKeyboardButton(f"🖼️ {lay}", callback_data="m|fxinfo")], [InlineKeyboardButton(f"🌫️ {b_txt}", callback_data="m|fxinfo"), InlineKeyboardButton(f"🏷️ Logo: {lg_t}", callback_data="m|logo")], [InlineKeyboardButton("🎨 Crop + Filter + Adjust: ON ✅", callback_data="m|fxinfo")]]
    txt = f"⚡ SIGYI AI STUDIO PRO (HD 720p) ⚡\n━━━━━━━━━━━━━━\n• 🔑 API Keys: {kt} (Auto-Rotation ✅)\n• 🤖 Model: API Key နှင့် ကိုက်ညီသော Gemini Model (Auto)\n• 🎙️ Voice: {v_lbl} | 📺 HD 720p\n• 🖼️ {lay} | 🌫️ Subtitle-cover blur: OFF\n• 🎯 Word-level AI Alignment + Audio-Master Sync: Active ✅\n━━━━━━━━━━━━━━\n📥 TikTok / YouTube / Rednote Link သို့မဟုတ် Video ပို့ပါ!\n💡 (Bot အလုပ်မလုပ်ပါက /start ကို နှိပ်ပါ)"
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
    """Remove formatting noise without deleting numbers, names, or English terms."""
    t = str(t or "")
    t = re.sub(r"```.*?```", " ", t, flags=re.S)
    t = re.sub(r"\[(?:KEEP|TIME|SEGMENT):.*?\]", " ", t, flags=re.I | re.S)
    t = re.sub(r"[*#_`~|•]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t

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
        return get_safe_model_candidates()
    available = set(discovered)
    preferred = [m for m in MODELS if m in available]
    return preferred + [m for m in discovered if m not in preferred] or get_safe_model_candidates()

def _try_models_with_file(client, model_candidates, uf, p, uid, k_i):
    """Try each model with bounded retry on transient 503/429 errors."""
    last_err = None
    for m_name in model_candidates:
        for retry in range(3):
            try:
                res = client.models.generate_content(model=m_name, contents=[uf, p])
                if res and res.text:
                    set_u(uid, "k_idx", k_i)
                    return res.text, None
            except Exception as me:
                last_err = me
                upper = str(me).upper()
                # Retired/missing model: skip immediately.
                if ("404" in upper or "NOT_FOUND" in upper
                        or "NO LONGER AVAILABLE" in upper):
                    break
                # Transient error: retry with backoff.
                if _is_transient_api_error(me) and retry < 2:
                    time.sleep(_backoff_seconds(retry, base=1.0, cap=5.0))
                    continue
                break
    return "", last_err

def detect_scene_boundaries(video_path, threshold=0.35):
    """Use ffmpeg scene detection to find natural cut points in the source video.

    Returns a sorted list of timestamps (seconds) where scene changes occur.
    """
    try:
        r = subprocess.run(
            ["ffmpeg", "-i", video_path, "-filter:v",
             f"select='gt(scene,{threshold})',showinfo",
             "-f", "null", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=max(60, int(get_dur(video_path) * 2)),
        )
        boundaries = []
        for line in (r.stderr or "").splitlines():
            m = re.search(r"pts_time:([0-9]+(?:\\.[0-9]+)?)", line)
            if m:
                try:
                    t = float(m.group(1))
                    if t > 0.3:
                        boundaries.append(t)
                except ValueError:
                    continue
        return sorted(set(boundaries))
    except Exception as e:
        print(f"Scene detection failed: {e}")
        return []


def ai_frame_audio_translate(vpath, sm_v, uid, vdur, n_win):
    """Transcribe ORIGINAL AUDIO only, then translate the transcript into Burmese.

    No video frames/video file are uploaded to Gemini. Source audio timestamps are
    retained so the FFmpeg renderer can map each spoken chunk to its source window.
    Local faster-whisper is optional; if it is not installed, Gemini transcribes
    the extracted audio file instead.
    """
    audio_path = f"srcaud_{uid}.wav"
    extract_cmd = [
        "ffmpeg", "-y", "-threads", "2", "-i", vpath, "-vn",
        "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", audio_path
    ]
    r = subprocess.run(extract_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       text=True, timeout=max(90, min(240, int(vdur * 2))))
    if r.returncode != 0 or not is_valid_audio(audio_path):
        raise RuntimeError("မူရင်း video မှ အသံထုတ်ယူ၍ မရပါ: " + (r.stderr or "FFmpeg audio extraction failed")[-400:])

    # Diagnose the extracted source audio before sending it to any ASR engine.
    try:
        audio_size = os.path.getsize(audio_path) if os.path.exists(audio_path) else 0
        print(f"🔍 Audio extract | path={audio_path} | size={audio_size} bytes | duration={get_dur(audio_path):.2f}s")
        vol = subprocess.run(
            ["ffmpeg", "-hide_banner", "-i", audio_path, "-af", "volumedetect", "-f", "null", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30,
        )
        for line in (vol.stderr or "").splitlines():
            if "mean_volume:" in line or "max_volume:" in line:
                print(f"🔊 {line.strip()}")
    except Exception as debug_err:
        print(f"Audio diagnostics skipped: {debug_err}")

    # Prefer local Whisper ASR when installed. If faster-whisper is absent on the
    # hosting server, use Gemini on the extracted AUDIO file (never the video).
    # This keeps the normal link -> download -> audio transcript workflow working
    # without requiring the server operator to install a large ASR package.
    transcript_segments = []
    asr_language = "unknown"
    try:
        from faster_whisper import WhisperModel
        # Cache the ASR model across jobs; repeatedly loading it can leave the bot
        # appearing stuck at 30%. The multilingual base model is much lighter on CPU
        # than small, while retaining Burmese speech recognition support.
        model_name = os.getenv("AUDIO_ASR_MODEL", "base").strip() or "base"
        global _AUDIO_ASR_MODEL, _AUDIO_ASR_MODEL_NAME
        if _AUDIO_ASR_MODEL is None or _AUDIO_ASR_MODEL_NAME != model_name:
            _AUDIO_ASR_MODEL = WhisperModel(
                model_name, device=os.getenv("WHISPER_DEVICE", "cpu"),
                compute_type=os.getenv("WHISPER_COMPUTE_TYPE", "int8")
            )
            _AUDIO_ASR_MODEL_NAME = model_name
        asr = _AUDIO_ASR_MODEL
        print(f"Audio ASR started | model={model_name} | duration={vdur:.1f}s")
        segments, info = asr.transcribe(
            audio_path, language=None, word_timestamps=False, vad_filter=True,
            beam_size=1, condition_on_previous_text=False
        )
        asr_language = getattr(info, "language", "unknown")
        for seg in segments:
            text = re.sub(r"\s+", " ", str(getattr(seg, "text", "") or "")).strip()
            try:
                a, b = float(seg.start), float(seg.end)
            except (TypeError, ValueError):
                continue
            if text and b > a:
                transcript_segments.append({"start": max(0.0, a), "end": min(vdur, b), "text": text})
        if not transcript_segments:
            raise RuntimeError("Local Whisper returned no speech segments; trying Gemini fallback")
        print(f"Audio ASR finished | language={asr_language} | segments={len(transcript_segments)}")
    except Exception as local_asr_error:
        # Missing package, model-download issue, unsupported runtime, or empty local
        # transcript all fall back to Gemini instead of leaving the job at 30%.
        print(f"Local Whisper unavailable ({str(local_asr_error)[:220]}); switching to Gemini audio-only transcription")
        transcript_segments = []
        u0 = get_u(uid)
        keys0 = [str(k).strip() for k in (u0.get("keys") or []) if str(k).strip()]
        if not keys0:
            raise RuntimeError("faster-whisper မရှိသေးပါ။ Gemini API Key ထည့်ထားမှ audio transcript လုပ်နိုင်ပါသည်။")
        start0 = int(u0.get("k_idx", 0) or 0) % len(keys0)
        prompt_asr = (
            "You are a speech-to-text engine. Listen to the attached AUDIO file and transcribe "
            "all audible spoken words in their original language. Do not summarize, translate, "
            "infer from visuals, or add commentary. If speech is audible, return the speech segments; "
            "do not return an empty segments array just because the speech is difficult to recognize. "
            "If the audio is genuinely silent or contains no speech, return an empty segments array. "
            "Split speech into short consecutive chunks (about 2-8 seconds each), with accurate "
            "approximate timestamps. Preserve names, numbers, and negation. Omit silence, never invent speech. "
            "Return ONLY valid JSON, no markdown fences: "
            '{\"segments\":[{\"start\":0.0,\"end\":2.5,\"text\":\"spoken words\"}]} '
            f"Audio duration is approximately {vdur:.2f} seconds; timestamps must be between 0 and {vdur:.2f}. "
            "This is AUDIO TRANSCRIPTION only; do not describe or analyze any video."
        )
        last_asr_error = None
        # Rotate through keys twice before giving up; 503 spikes may be temporary.
        for step0 in range(len(keys0) * 2):
            k0 = (start0 + step0) % len(keys0)
            uploaded = None
            try:
                client0 = genai.Client(api_key=keys0[k0], http_options=types.HttpOptions(timeout=120000))
                uploaded = client0.files.upload(file=audio_path)
                # Do not call models.list() for every key: discovery adds latency and
                # can itself fail during a Gemini service incident. Try configured
                # Flash models directly, with bounded backoff on transient 5xx/429.
                model_candidates0 = get_safe_model_candidates(limit=5)
                # Rotate the first model across API-key passes. If one model returns
                # empty segments, the next key pass starts with a different model.
                if model_candidates0:
                    rotate_by = step0 % len(model_candidates0)
                    model_candidates0 = model_candidates0[rotate_by:] + model_candidates0[:rotate_by]
                response0 = None
                # Try each model with at most one short retry. If a model is overloaded,
                # move on quickly instead of spending the whole retry budget on it.
                for model0 in model_candidates0:
                    for retry0 in range(2):
                        try:
                            response0 = client0.models.generate_content(
                                model=model0, contents=[uploaded, prompt_asr]
                            )
                            if response0 and response0.text:
                                break
                            response0 = None
                        except Exception as exc0:
                            last_asr_error = exc0
                            err_upper0 = str(exc0).upper()
                            # A retired/missing model is not transient: skip it immediately.
                            if ("404" in err_upper0 or "NOT_FOUND" in err_upper0
                                    or "NO LONGER AVAILABLE" in err_upper0):
                                break
                            # Retry transient errors once, then try the next model.
                            if _is_transient_api_error(exc0) and retry0 < 1:
                                time.sleep(_backoff_seconds(retry0, base=1.5, cap=4.0))
                                continue
                            break
                    if response0 and getattr(response0, "text", None):
                        break
                    response0 = None
                    # Give the next model a moment before switching endpoints.
                    if model0 != model_candidates0[-1]:
                        time.sleep(0.5)
                if not response0 or not response0.text:
                    # Give Google's service a short recovery window before trying the
                    # next API key. Keys from the same project may share quota, so this
                    # is a fallback strategy, not a guarantee against server-side 503s.
                    time.sleep(_backoff_seconds(step0, base=2.0, cap=8.0))
                    continue
                raw_asr = response0.text.strip()
                # Gemini sometimes returns almost-JSON (for example, an unescaped
                # quote in spoken text). Parse once, then ask the SAME model to repair
                # the text format once before rotating keys; this avoids wasting a full
                # audio upload/request on a formatting-only failure.
                def _parse_asr_payload(payload):
                    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(payload or "").strip(), flags=re.I)
                    left, right = cleaned.find("{"), cleaned.rfind("}")
                    if left < 0 or right <= left:
                        raise ValueError("Gemini ASR response did not contain a JSON object")
                    candidate = cleaned[left:right + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        # Harmless trailing commas are common in model-generated JSON.
                        candidate = re.sub(r",\s*([}\]])", r"\1", candidate)
                        return json.loads(candidate)
                try:
                    data_asr = _parse_asr_payload(raw_asr)
                except (ValueError, json.JSONDecodeError) as parse_err:
                    repair_prompt = (
                        "Repair the following attempted speech-transcription response into valid JSON only. "
                        "Do not invent, remove, translate, or summarize any spoken words. Preserve all segment "
                        "text and timestamps. Required schema: {\"segments\":[{\"start\":0.0,\"end\":2.5,\"text\":\"spoken words\"}]}. "
                        "Return no markdown fences and no commentary.\nBROKEN RESPONSE:\n" + raw_asr[:12000]
                    )
                    try:
                        repaired0 = client0.models.generate_content(model=model0, contents=[repair_prompt])
                        data_asr = _parse_asr_payload(getattr(repaired0, "text", ""))
                    except Exception as repair_err:
                        last_asr_error = RuntimeError(
                            f"Gemini transcript JSON မမှန်ပါ; repair failed: {str(repair_err)[:180]}"
                        )
                        continue
                raw_segments = data_asr.get("segments", [])
                if not isinstance(raw_segments, list):
                    raw_segments = []
                print(f"Gemini ASR returned {len(raw_segments)} segments (note={data_asr.get('note', 'n/a')}) | model={model0}")
                # A valid JSON response with zero segments is not a successful
                # transcription. Ask once more with a focused recovery prompt before
                # rotating to another model/key.
                if not raw_segments:
                    try:
                        retry_prompt = (
                            "Please listen to the attached audio again. The previous response contained no speech segments. "
                            "If any human speech is audible, transcribe it in the original language and return valid JSON only, "
                            'using {\"segments\":[{\"start\":0.0,\"end\":3.0,\"text\":\"actual words heard\"}]}. '
                            "Do not summarize, translate, or invent words. Only return an empty array if there is truly no speech. "
                            f"Audio duration: {vdur:.2f} seconds."
                        )
                        retry_response = client0.models.generate_content(
                            model=model0, contents=[uploaded, retry_prompt]
                        )
                        retry_payload = getattr(retry_response, "text", "") or ""
                        if retry_payload.strip():
                            data_asr = _parse_asr_payload(retry_payload)
                            raw_segments = data_asr.get("segments", [])
                            if not isinstance(raw_segments, list):
                                raw_segments = []
                            print(f"Gemini ASR recovery returned {len(raw_segments)} segments | model={model0}")
                    except Exception as retry_asr_err:
                        last_asr_error = retry_asr_err
                for seg0 in raw_segments:
                    if not isinstance(seg0, dict):
                        continue
                    txt0 = re.sub(r"\s+", " ", str(seg0.get("text", "") or "")).strip()
                    try:
                        a0, b0 = float(seg0.get("start", 0)), float(seg0.get("end", 0))
                    except (TypeError, ValueError):
                        continue
                    a0, b0 = max(0.0, min(vdur, a0)), max(0.0, min(vdur, b0))
                    if txt0 and b0 > a0:
                        transcript_segments.append({"start": a0, "end": b0, "text": txt0})
                if transcript_segments:
                    set_u(uid, "k_idx", k0)
                    asr_language = "Gemini audio transcription"
                    break
            except Exception as exc0:
                last_asr_error = exc0
                if _is_transient_api_error(exc0):
                    time.sleep(_backoff_seconds(min(step0, 3), base=0.8, cap=5.0))
            finally:
                if uploaded is not None:
                    try:
                        client0.files.delete(name=uploaded.name)
                    except Exception:
                        pass
        if not transcript_segments:
            # Make the failure actionable: distinguish a likely silent source from
            # a model/API response that contained no usable segments.
            volume_note = "အသံအဆင့်ကို log ထဲတွင် စစ်ဆေးပါ။"
            try:
                vol = subprocess.run(
                    ["ffmpeg", "-hide_banner", "-i", audio_path, "-af", "volumedetect", "-f", "null", "-"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30,
                )
                for line in (vol.stderr or "").splitlines():
                    if "mean_volume:" in line:
                        volume_note = line.strip()
                        break
            except Exception:
                pass
            raise RuntimeError(
                "Gemini က အသုံးပြုနိုင်တဲ့ speech segments မပြန်ပါ။ Audio duration/volume ကို log ထဲမှာ စစ်ပါ။ "
                f"Audio={get_dur(audio_path):.1f}s, {volume_note}, detail={str(last_asr_error or 'Audio transcription returned no segments')[:220]}"
            )
    except Exception as exc:
        if isinstance(exc, RuntimeError):
            raise
        raise RuntimeError(f"Audio transcription မအောင်မြင်ပါ: {str(exc)[:300]}")

    if not transcript_segments:
        raise RuntimeError("မူရင်းအသံထဲမှ စကားပြောစာသား မတွေ့ပါ။ အသံရှင်းလင်းမှုကို စစ်ဆေးပါ။")

    # Do not silently render a long source from a tiny partial Gemini transcript.
    # This catches the earlier symptom where a long recap produced only ~4 seconds
    # of narration. Some sources genuinely contain long silence, so only flag when
    # both the final timestamp and total detected speech are unusually short.
    last_speech_end = max(float(seg["end"]) for seg in transcript_segments)
    total_speech = sum(max(0.0, float(seg["end"]) - float(seg["start"])) for seg in transcript_segments)
    if float(vdur) >= 20.0 and last_speech_end < min(12.0, float(vdur) * 0.35) and total_speech < min(8.0, float(vdur) * 0.20):
        raise RuntimeError(
            f"မူရင်းအသံ transcript မပြည့်စုံပါ (video={vdur:.1f}s, transcript_end={last_speech_end:.1f}s)။ "
            "၄ စက္ကန့်စာလို partial result ကို အသံအပြည့်အဖြစ် မထုတ်တော့ပါ။ ခဏစောင့်ပြီး ပြန်စမ်းပါ၊ သို့မဟုတ် local faster-whisper ကို တပ်ဆင်ပါ။"
        )

    # Scene-aware bucketing: use ffmpeg scene detection to find natural cut
    # points, then merge ASR speech with those boundaries.
    n_win = max(1, int(n_win))
    scene_bounds = detect_scene_boundaries(vpath, threshold=0.35)
    all_bounds = sorted(set([0.0] + scene_bounds + [float(vdur)]))
    max_segments = max(n_win, 8)
    if len(all_bounds) - 1 > max_segments:
        speech_ranges = [(s["start"], s["end"]) for s in transcript_segments]
        def coverage(a, b):
            return sum(max(0.0, min(b, e) - max(a, s)) for s, e in speech_ranges)
        mids = all_bounds[1:-1]
        keep = sorted(
            range(len(mids)),
            key=lambda i: coverage(all_bounds[i], all_bounds[i + 1]),
            reverse=True,
        )[:max_segments - 1]
        all_bounds = [all_bounds[0]] + sorted(mids[i] for i in keep) + [all_bounds[-1]]
    print(f"Scene-aware buckets | scenes={len(all_bounds)-1} | ASR segments={len(transcript_segments)}")

    # Assign every ASR segment to exactly ONE scene bucket (the bucket with the
    # greatest time overlap). The previous overlap-based loop copied one spoken
    # segment into both buckets whenever it crossed a scene cut, which caused
    # Burmese narration to repeat the same sentence.
    buckets = [[] for _ in range(len(all_bounds) - 1)]
    for seg in transcript_segments:
        s0, s1 = float(seg["start"]), float(seg["end"])
        if s1 <= s0:
            continue
        overlaps = []
        for wi in range(len(all_bounds) - 1):
            a, b = all_bounds[wi], all_bounds[wi + 1]
            overlaps.append(max(0.0, min(b, s1) - max(a, s0)))
        if not overlaps or max(overlaps) <= 0:
            continue
        # A deterministic tie-break puts a segment in the bucket containing its midpoint.
        midpoint = (s0 + s1) / 2.0
        best = max(range(len(overlaps)), key=lambda idx: (overlaps[idx],
                   1 if all_bounds[idx] <= midpoint < all_bounds[idx + 1] else 0))
        buckets[best].append(seg)

    active = []
    seen_text_times = {}
    for wi, bucket in enumerate(buckets):
        a, b = all_bounds[wi], all_bounds[wi + 1]
        bucket.sort(key=lambda seg: (float(seg["start"]), float(seg["end"])))
        unique = []
        for seg in bucket:
            # Defend against exact duplicate ASR rows returned by the API. Only
            # collapse the same normalized text when timestamps are very close;
            # legitimate repeated dialogue farther apart is preserved.
            norm = re.sub(r"[^\w\u1000-\u109f]+", "", str(seg["text"]).casefold())
            prev_time = seen_text_times.get(norm, -999.0)
            if norm and norm == re.sub(r"[^\w\u1000-\u109f]+", "", str(seg["text"]).casefold()) and float(seg["start"]) - prev_time < 2.0:
                continue
            unique.append(seg)
            if norm:
                seen_text_times[norm] = float(seg["start"])
        source_text = " ".join(str(seg["text"]).strip() for seg in unique if str(seg["text"]).strip()).strip()
        if source_text:
            active.append({"source_start": a, "source_end": b, "transcript": source_text})
    if not active:
        raise RuntimeError("ASR မှ အသုံးပြုနိုင်သော စကားပြောစာသား မရရှိပါ။")

    u = get_u(uid)
    keys = [str(k).strip() for k in (u.get("keys") or []) if str(k).strip()]
    if not keys:
        raise RuntimeError("API Key မရှိပါ။ Key အရင်ထည့်ပေးပါ။")
    start_idx = int(u.get("k_idx", 0) or 0) % len(keys)

    # Plain-text prompt only: Gemini receives transcript text, never video/audio.
    chunks = []
    for i, item in enumerate(active):
        chunks.append(f"SEGMENT {i+1} [{item['source_start']:.2f}-{item['source_end']:.2f}s]\n{item['transcript']}")
    prompt = (
        "You are a senior Burmese-language film recap narrator and faithful adaptation editor. "
        "Turn each transcript segment into smooth, natural, professional Burmese narration suitable "
        "for a polished Burmese movie recap voice-over, not a word-for-word classroom translation. "
        "SOURCE OF TRUTH IS ONLY THE TRANSCRIBED SPEECH. Do not use video or infer anything from visuals. "
        "Preserve every meaningful plot fact, action, relationship, cause-and-effect, emotion explicitly "
        "spoken, names, numbers, negation, and chronological order. Do NOT summarize, shorten away details, "
        "invent events, exaggerate, explain, add hooks, add moral judgments, add an intro/outro, or repeat "
        "the same idea. Avoid stiff literal calques and awkward translationese such as repetitive 'ယင်းသည်', "
        "'၎င်းသည်', '...ဖြစ်ပါသည်'; prefer concise, fluent spoken Burmese phrasing and natural transitions. "
        "Use a cinematic recap narrator tone like a professional human storyteller, with only the words needed "
        "to convey the source meaning. If the source segment is already Burmese, polish it into natural spoken "
        "recap narration while preserving its information; do not merely copy awkward ASR wording. Do not add "
        "filler to silence. Keep each output segment aligned to its corresponding input segment and in order. "
        "Return ONLY valid JSON in this exact schema: "
        '{"translations":[{"id":1,"text":"မြန်မာ recap narration"}]}. '
        f"The translations array MUST contain exactly {len(active)} items, with integer ids 1 through {len(active)} "
        "in order, one item for every input segment. Do not merge, split, add, or omit items. Escape quotes correctly.\n\n"
        + "\n\n".join(chunks)
    )

    raw_txt, last_err = "", None
    for step_i in range(len(keys)):
        k_i = (start_idx + step_i) % len(keys)
        try:
            client = genai.Client(api_key=keys[k_i], http_options=types.HttpOptions(timeout=60000))
            # Static configured model list avoids a slow models.list() round-trip
            # on every key, especially when the service is overloaded.
            model_candidates = get_safe_model_candidates(limit=3)
            for candidate_model in model_candidates:
                for retry_i in range(3):
                    try:
                        res = client.models.generate_content(model=candidate_model, contents=prompt)
                        if res and res.text:
                            raw_txt = res.text.strip()
                            set_u(uid, "k_idx", k_i)
                            break
                    except Exception as exc:
                        last_err = exc
                        err_upper = str(exc).upper()
                        # Do not waste retries on model IDs that the API says are gone.
                        if ("404" in err_upper or "NOT_FOUND" in err_upper or "NO LONGER AVAILABLE" in err_upper):
                            break
                        if _is_transient_api_error(exc) and retry_i < 2:
                            time.sleep(_backoff_seconds(retry_i, base=0.8, cap=4.0))
                            continue
                        break
                if raw_txt:
                    break
            if raw_txt:
                break
        except Exception as exc:
            last_err = exc
            if _is_transient_api_error(exc):
                time.sleep(_backoff_seconds(min(step_i, 3), base=0.8, cap=5.0))

    if not raw_txt:
        raise RuntimeError("Transcript ဘာသာပြန်ရန် Gemini API မအောင်မြင်ပါ: " + str(last_err or "Unknown API error")[:450])

    # Prefer structured JSON so a delimiter accidentally emitted inside Burmese text
    # cannot create a false extra segment (e.g. 31 outputs for 30 inputs).
    translated = []
    try:
        json_txt = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_txt.strip(), flags=re.I)
        obj = json.loads(json_txt[json_txt.find("{"):json_txt.rfind("}") + 1])
        rows = obj.get("translations", []) if isinstance(obj, dict) else []
        by_id = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                rid = int(row.get("id"))
            except (TypeError, ValueError):
                continue
            val = clean_mm_text(str(row.get("text", "") or ""))
            if 1 <= rid <= len(active) and val:
                by_id[rid] = val
        if len(by_id) == len(active):
            translated = [by_id[i] for i in range(1, len(active) + 1)]
    except Exception:
        pass

    # Backward-compatible fallback for a model response using the old delimiter.
    if not translated:
        translated = [clean_mm_text(x) for x in raw_txt.split("|||")]

    # If there is exactly one extra delimiter-created piece, recover without dropping
    # any words by asking a Flash model to reformat the existing transcript + draft
    # into the required indexed JSON. This keeps segment count aligned to the source.
    if len(translated) != len(active) or any(not x.strip() for x in translated):
        repair_prompt = (
            "Reformat the following transcript and draft translations into valid JSON only: "
            '{"translations":[{"id":1,"text":"..."}]}. '
            f"Return exactly {len(active)} items, ids 1 through {len(active)}, one per source segment, in order. "
            "Use the draft translations to preserve meaning, do not add facts, do not omit source meaning, "
            "and do not include the delimiter ||| inside text.\n\nSOURCE SEGMENTS:\n"
            + "\n\n".join(chunks)
            + "\n\nDRAFT TRANSLATION OUTPUT:\n" + raw_txt
        )
        repaired = ""
        for retry_key_offset in range(len(keys)):
            repair_key_idx = (start_idx + retry_key_offset) % len(keys)
            try:
                repair_client = genai.Client(api_key=keys[repair_key_idx], http_options=types.HttpOptions(timeout=60000))
                for repair_model in get_generate_models(repair_client):
                    try:
                        repair_res = repair_client.models.generate_content(model=repair_model, contents=repair_prompt)
                        if repair_res and repair_res.text:
                            candidate = repair_res.text.strip()
                            candidate_json = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.I)
                            candidate_obj = json.loads(candidate_json[candidate_json.find("{"):candidate_json.rfind("}") + 1])
                            candidate_rows = candidate_obj.get("translations", [])
                            candidate_map = {}
                            for row in candidate_rows:
                                if isinstance(row, dict):
                                    try:
                                        rid = int(row.get("id"))
                                    except (TypeError, ValueError):
                                        continue
                                    val = clean_mm_text(str(row.get("text", "") or ""))
                                    if 1 <= rid <= len(active) and val:
                                        candidate_map[rid] = val
                            if len(candidate_map) == len(active):
                                translated = [candidate_map[i] for i in range(1, len(active) + 1)]
                                repaired = candidate
                                set_u(uid, "k_idx", repair_key_idx)
                                break
                    except Exception as repair_exc:
                        last_err = repair_exc
                        continue
                if repaired:
                    break
            except Exception as repair_exc:
                last_err = repair_exc
                continue
        if not repaired:
            raise RuntimeError(f"Gemini ဘာသာပြန်အပိုင်းအရေအတွက် မကိုက်ညီပါ (လိုအပ်={len(active)}, ရရှိ={len(translated)})။ ပြန်စမ်းပါ။")

    keep_ranges = [(item["source_start"], item["source_end"], i) for i, item in enumerate(active)]
    print(f"Audio-only ASR | engine={asr_language} | speech_segments={len(transcript_segments)} | voice_chunks={len(active)}")
    return {"scripts": translated, "keep_ranges": keep_ranges, "source_transcript": [x["transcript"] for x in active]}

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

def _get_stream_duration(path, selector):
    """Return a stream-specific duration when ffprobe exposes one."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", selector,
             "-show_entries", "stream=duration", "-of", "default=noprint_wrappers=1:nokey=1", path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15
        )
        for line in (result.stdout or "").splitlines():
            try:
                value = float(line.strip())
                if math.isfinite(value) and value > 0:
                    return value
            except (TypeError, ValueError, OverflowError):
                continue
    except Exception:
        pass
    return get_dur(path)


def get_dur(p):
    if not p or not os.path.exists(p):
        raise FileNotFoundError(f"Media file not found: {p}")
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", p], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=20)
    duration = try_run(lambda: float(r.stdout.strip())) or 0.0
    if r.returncode != 0 or duration <= 0:
        raise RuntimeError(f"Invalid media duration for {os.path.basename(p)}")
    return duration

def _probe_video_size(path):
    """Read source dimensions quickly with ffprobe; fallback to portrait canvas."""
    try:
        raw = subprocess.check_output([
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", path
        ], text=True, timeout=15).strip()
        w, h = raw.split("x", 1)
        return max(2, int(w)), max(2, int(h))
    except Exception:
        return 720, 1280

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
    weights = []
    for value in (seg_durs or []):
        try:
            duration = float(value)
            if math.isfinite(duration) and duration > 0:
                weights.append(max(0.05, duration))
        except (TypeError, ValueError, OverflowError):
            continue

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

def _snap_to_frame(t):
    """Snap a time to the nearest 24-fps frame boundary."""
    try:
        value = float(t)
        if not math.isfinite(value):
            return 0.0
        return round(value / FRAME_DUR) * FRAME_DUR
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _snap_range(start, end):
    """Frame-align a source range and ensure it contains at least one frame."""
    start = max(0.0, _snap_to_frame(start))
    end = _snap_to_frame(end)
    if end <= start:
        end = start + FRAME_DUR
    return start, end


def _build_global_linear(source_dur, target):
    """Safe full-source fallback: map source duration exactly onto audio duration."""
    src = max(0.1, float(source_dur))
    tgt = max(0.1, float(target))
    scale = tgt / src
    return (
        f"settb=AVTB,setpts=PTS*{scale:.10f},fps={FPS},"
        f"tpad=stop_mode=clone:stop_duration={tgt + 2.0:.6f},"
        f"trim=duration={tgt:.6f},setpts=PTS-STARTPTS"
    )


def _build_anchor_cut_expr(ranges, src_group, weights, target, video_lead=None):
    """Build a frame-aligned cut/concat graph with per-scene timing."""
    if video_lead is None:
        video_lead = VIDEO_LEAD_SECONDS
    n = len(src_group)
    if not ranges or n == 0 or len(weights) != n:
        raise ValueError("Anchor sync requires matching source and audio segments")
    if any((not math.isfinite(float(x)) or float(x) <= FRAME_DUR) for x in src_group):
        raise ValueError("Anchor sync contains an empty/too-short source segment")
    if any((not math.isfinite(float(x)) or float(x) <= 0) for x in weights):
        raise ValueError("Anchor sync contains an invalid audio segment duration")

    source_bounds = [0.0]
    for duration in src_group:
        source_bounds.append(source_bounds[-1] + float(duration))

    target_bounds = [0.0]
    for duration in weights:
        target_bounds.append(target_bounds[-1] + float(duration))

    total_narration = target_bounds[-1]
    if abs(total_narration - float(target)) > 0.5:
        scale = float(target) / max(0.1, total_narration)
        target_bounds = [b * scale for b in target_bounds]

    lead = max(0.0, min(0.60, float(video_lead)))
    for i in range(1, len(target_bounds) - 1):
        low = target_bounds[i - 1] + max(5 * FRAME_DUR, 0.20)
        high = target_bounds[i + 1] - max(5 * FRAME_DUR, 0.20)
        target_bounds[i] = max(low, min(high, target_bounds[i] - lead))

    anchors = list(range(n + 1))
    trim_nodes = []
    split_labels = "".join(f"[src{j}]" for j in range(len(ranges)))
    trim_nodes.append(f"[0:v]fps={FPS},split={len(ranges)}{split_labels}")
    for j, (a, b, _) in enumerate(ranges):
        trim_nodes.append(
            f"[src{j}]trim=start={a:.6f}:end={b:.6f},setpts=PTS-STARTPTS[cut{j}]"
        )
    if len(ranges) == 1:
        trim_nodes.append("[cut0]null[rawv]")
    else:
        concat_inputs = "".join(f"[cut{j}]" for j in range(len(ranges)))
        trim_nodes.append(f"{concat_inputs}concat=n={len(ranges)}:v=1:a=0[rawv]")

    def slope(i_a, i_b):
        source_span = max(FRAME_DUR, source_bounds[i_b] - source_bounds[i_a])
        target_span = max(FRAME_DUR, target_bounds[i_b] - target_bounds[i_a])
        return target_span / source_span

    a0, a1 = anchors[-2], anchors[-1]
    expr = f"{target_bounds[a0]:.6f}+(T-{source_bounds[a0]:.6f})*{slope(a0, a1):.10f}"
    for j in range(len(anchors) - 3, -1, -1):
        a, b = anchors[j], anchors[j + 1]
        expr = (
            f"if(lt(T,{source_bounds[b]:.6f}),"
            f"{target_bounds[a]:.6f}+(T-{source_bounds[a]:.6f})*{slope(a, b):.10f},"
            f"{expr})"
        )

    graph = ";".join(trim_nodes) + (
        f";[rawv]settb=AVTB,setpts='({expr})/TB',fps={FPS},"
        f"tpad=stop_mode=clone:stop_duration={float(target) + 2.0:.6f},"
        f"trim=duration={float(target):.6f},setpts=PTS-STARTPTS[v0]"
    )
    print(
        f"🎯 Anchor sync | scenes={n} | ranges={len(ranges)} | "
        f"per_scene_speed=ON | lead={lead:.2f}s"
    )
    return graph


def _run_ffmpeg_logged(cmd, timeout, stage):
    """Run FFmpeg with live logs and a hard timeout; return CompletedProcess."""
    run_cmd = list(cmd)
    if run_cmd and os.path.basename(run_cmd[0]).lower().startswith("ffmpeg"):
        run_cmd[1:1] = ["-hide_banner", "-loglevel", "warning", "-progress", "pipe:1", "-nostats"]
    print(f"FFMPEG[{stage}] START | timeout={int(timeout)}s", flush=True)
    print(f"FFMPEG[{stage}] COMMAND: {subprocess.list2cmdline(run_cmd)}", flush=True)
    output_lines = []
    q = queue.Queue()

    def _reader(pipe):
        try:
            for line in iter(pipe.readline, ""):
                q.put(line.rstrip("\r\n"))
        finally:
            q.put(None)

    proc = subprocess.Popen(run_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    reader = threading.Thread(target=_reader, args=(proc.stdout,), daemon=True)
    reader.start()
    deadline = time.monotonic() + max(1, float(timeout))
    ended = False
    while not ended:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timeout_line = f"FFMPEG[{stage}] TIMEOUT after {int(timeout)}s; terminating process"
            print(timeout_line, flush=True)
            output_lines.append(timeout_line)
            try:
                proc.terminate()
                proc.wait(timeout=8)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            break
        try:
            line = q.get(timeout=min(1.0, remaining))
        except queue.Empty:
            if proc.poll() is not None and not reader.is_alive():
                break
            continue
        if line is None:
            ended = True
            continue
        output_lines.append(line)
        if line.startswith(("frame=", "fps=", "out_time=", "out_time_ms=", "progress=")) or "Error" in line or "error" in line.lower() or "Invalid" in line:
            print(f"FFMPEG[{stage}] {line}", flush=True)
    try:
        returncode = proc.wait(timeout=8)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
        returncode = proc.wait()
    output = "\n".join(output_lines)
    if returncode == 0 and "TIMEOUT" in output:
        returncode = 124
    print(f"FFMPEG[{stage}] END | returncode={returncode}", flush=True)
    return subprocess.CompletedProcess(run_cmd, returncode, stdout=output, stderr=output)


def _write_filter_script(graph, prefix):
    """Write an FFmpeg filter graph to a unique UTF-8 script file."""
    safe_prefix = re.sub(r"[^A-Za-z0-9_.-]", "_", str(prefix))[:48] or "ffmpeg_filter"
    fd, path = tempfile.mkstemp(prefix=f"{safe_prefix}_", suffix=".ffgraph", dir=".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(graph)
    except Exception:
        try:
            os.remove(path)
        except OSError:
            pass
        raise
    return path


def edit_vid(iv, ia, ov, u, uid, vdur, seg_durs):
    """Render with a single master clock and validate both streams before returning."""
    source_dur = get_dur(iv)
    audio_dur = max(0.1, get_dur(ia))
    target = audio_dur  # finished narration is the single master clock
    is_fr = True
    adj = ("crop=iw*0.96:ih*0.96:iw*0.02:ih*0.02,"
           "eq=contrast=1.04:brightness=0.01:saturation=1.06:gamma=1.01,"
           "unsharp=3:3:0.30:3:3:0.0")
    playback_speed = source_dur / target
    keep_data = seg_durs if isinstance(seg_durs, dict) else None
    word_timestamps = (keep_data or {}).get("word_timestamps", [])
    weights = []
    if keep_data:
        for value in (keep_data.get("audio_segment_durs") or []):
            try:
                value = float(value)
                if math.isfinite(value) and value > 0:
                    weights.append(max(0.05, value))
            except (TypeError, ValueError, OverflowError):
                continue

    # Diagnostic mode: disable anchor/piecewise scene timing temporarily.
    # This isolates whether the complex scene-sync clock causes FFmpeg frame=0 stalls.
    video_clock = f"[0:v]{_build_global_linear(source_dur, target)}[v0]"
    use_cuts = False
    print(
        f"🧪 GLOBAL-LINEAR SYNC TEST | video={source_dur:.2f}s → audio={target:.2f}s; "
        "anchor/piecewise scene timing temporarily disabled"
    )
    video_input_label = "[v0]"
    print(
        f"FFmpeg scene sync | video={source_dur:.3f}s | voice={audio_dur:.3f}s | "
        f"video_avg_speed={playback_speed:.3f}x | segments={len(seg_durs or [])} | "
        f"voice_speed={VOICE_SPEED:.2f}x | transition_lead={VIDEO_LEAD_SECONDS:.2f}s | anchor={ANCHOR_WINDOW_SECONDS:.1f}s"
    )
    # Aim for ~30 MiB total output so Telegram's upload limit is not hit.
    # Reserve ~96 kbps for audio and allow low bitrates for long narrations.
    kbps=max(1200,min(4200,int((42*8192)/target)-140))
    inputs=["-i",iv,"-i",ia]
    # Full-canvas blurred background is always enabled. Foreground uses
    # aspect-ratio DECREASE (never increase+crop), so letterboxed and non-9:16
    # clips are not zoomed in. The old subtitle-band blur overlay is removed.
    src_w, src_h = _probe_video_size(iv)
    crop_w, crop_h = src_w * 0.96, src_h * 0.96
    fit_scale = min(720.0 / max(1.0, crop_w), 1280.0 / max(1.0, crop_h))
    fg_w = max(2, int(round(crop_w * fit_scale / 2.0) * 2))
    side_bar = max(0, min(360, (720 - fg_w) // 2))
    fc=(f"{video_clock};[v0]split=2[bgi][fgi];"
        f"[bgi]scale=720:1280:force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop=720:1280,gblur=sigma=32,eq=brightness=-0.10:saturation=1.05[bg];"
        f"[fgi]{adj},scale=720:1280:force_original_aspect_ratio=decrease:flags=lanczos[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2[comp]")
    if side_bar > 0:
        fc += (f";[comp]drawbox=x=0:y=0:w={side_bar}:h=1280:color=black:t=fill[side1];"
               f"[side1]drawbox=x={720-side_bar}:y=0:w={side_bar}:h=1280:color=black:t=fill[v1]")
    else:
        fc += ";[comp]null[v1]"
    cur="[v1]"
    logo=f"logo_{uid}.jpg"
    if u.get("logo") and os.path.exists(logo):
        inputs += ["-i",logo]; ly=124 if is_fr else 60
        fc+=f";[2:v]scale=82:82[lg];{cur}[lg]overlay=9:{ly}[v3]"; cur="[v3]"
    fc+=f";{cur}format=yuv420p[vout]"
    fc+=f";[1:a:0]asetpts=PTS-STARTPTS,aresample=async=1:first_pts=0,apad,atrim=duration={target:.6f},asetpts=PTS-STARTPTS[aout]"
    cmd=["ffmpeg","-y","-threads","2","-filter_complex_threads","1"]+inputs+["-map_metadata","-1","-map_chapters","-1","-filter_complex",fc,"-map","[vout]","-map","[aout]","-fps_mode","cfr","-r","24","-c:v","libx264","-preset","veryfast","-b:v",f"{kbps}k","-maxrate",f"{int(kbps*1.5)}k","-bufsize",f"{kbps*2}k","-c:a","aac","-b:a","128k","-t",f"{target:.3f}","-avoid_negative_ts","make_zero","-movflags","+faststart",ov]
    r=_run_ffmpeg_logged(cmd, timeout=max(600,int(target*8)), stage="scene-sync-global-linear-test")
    if r.returncode != 0:
        print("=== PRIMARY RENDER STDERR ===", flush=True)
        print(r.stderr or "(no FFmpeg output)", flush=True)
        print("=== PRIMARY FILTER GRAPH ===", flush=True)
        print(fc, flush=True)
    if r.returncode!=0 or not is_valid_video(ov) or not is_valid_audio(ov):
        # Remove partial output before a conservative second pass. This fallback
        # sacrifices AI cuts/effects but preserves the narration and full duration.
        try_run(os.remove, ov)
        fallback_flip = ""  # no flip/mirror in fallback render
        # Minimal fallback graph: intentionally avoids split/overlay/blur so a
        # failure in the presentation graph cannot also break the recovery render.
        # Keep aspect ratio and pad to portrait; do not flip or mirror.
        fallback_fc = (
            f"[0:v]settb=AVTB,setpts=PTS-STARTPTS,"
            f"setpts=PTS*{target/max(0.1,source_dur):.8f},fps=24,"
            f"tpad=stop_mode=clone:stop_duration={target + 2.0:.6f},"
            f"trim=duration={target:.6f},setpts=PTS-STARTPTS,"
            f"scale=720:1280:force_original_aspect_ratio=decrease:flags=lanczos,"
            f"pad=720:1280:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,"
            f"format=yuv420p[vout];"
            f"[1:a:0]asetpts=PTS-STARTPTS,aresample=async=1:first_pts=0,"
            f"apad,atrim=duration={target:.6f},asetpts=PTS-STARTPTS[aout]"
        )
        fallback_cmd=["ffmpeg","-y","-threads","2","-filter_complex_threads","1"]+inputs+["-filter_complex",fallback_fc,
            "-map","[vout]","-map","[aout]","-fps_mode","cfr","-r","24",
            "-c:v","libx264","-preset","veryfast","-b:v",f"{kbps}k",
            "-maxrate",f"{int(kbps*1.5)}k","-bufsize",f"{kbps*2}k","-c:a","aac","-b:a","128k","-t",f"{target:.3f}",
            "-max_muxing_queue_size","2048","-movflags","+faststart",ov]
        r2=_run_ffmpeg_logged(fallback_cmd, timeout=max(600,int(target*8)), stage="safe-fallback")
        if r2.returncode != 0:
            print("=== FALLBACK RENDER STDERR ===", flush=True)
            print(r2.stderr or "(no FFmpeg output)", flush=True)
            print("=== FALLBACK FILTER GRAPH ===", flush=True)
            print(fallback_fc, flush=True)
        if r2.returncode!=0 or not is_valid_video(ov) or not is_valid_audio(ov):
            primary_detail = (r.stderr or "no primary FFmpeg diagnostics")[-1400:]
            fallback_detail = (r2.stderr or "no fallback FFmpeg diagnostics")[-1800:]
            detail = "PRIMARY RENDER:\n" + primary_detail + "\nMINIMAL FALLBACK:\n" + fallback_detail
            raise RuntimeError("Main render နှင့် minimal fallback နှစ်ခုလုံး မအောင်မြင်ပါ: " + detail)
    final = get_dur(ov)
    final_video_dur = _get_stream_duration(ov, "v:0")
    final_audio_dur = _get_stream_duration(ov, "a:0")
    drift = abs(final_video_dur - final_audio_dur)
    print(
        f"✅ SYNC DURATION CHECK | container={final:.3f}s | "
        f"video_stream={final_video_dur:.3f}s | audio_stream={final_audio_dur:.3f}s | "
        f"stream_drift={drift:.3f}s"
    )
    if drift > MAX_DRIFT_SECONDS + FRAME_DUR:
        raise RuntimeError(
            f"Final stream duration mismatch: video={final_video_dur:.2f}s, "
            f"voice={final_audio_dur:.2f}s, drift={drift:.3f}s "
            f"(limit={MAX_DRIFT_SECONDS:.2f}s)"
        )
    if abs(final - target) > MAX_DRIFT_SECONDS + FRAME_DUR:
        raise RuntimeError(
            f"Final container duration mismatch: expected={target:.2f}s, got={final:.2f}s"
        )


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
        # target_vdur is set after TTS, using the finished audio duration.
        target_vdur = source_vdur  # placeholder; overwritten below
        # n_win is only an upper bound; actual buckets follow detected scenes.
        n_win = max(4, min(30, int(math.ceil(source_vdur / 4.5))))
        stage="Original audio transcription / Burmese translation"
        await q.edit_message_text(
            f"🔄 [30%] မူရင်းအသံကို စာသားအဖြစ်ထုတ်ယူပြီး မြန်မာလို ပြန်ဆိုနေပါသည်...\n"
            f"⏱️ Gemini server ဝန်ပိနေပါက ၁-၂ မိနစ် စောင့်ပါ။ Auto-Retry လုပ်နေပါသည်...{hint}",
            reply_markup=cancel_kb()
        )
        analysis=await asyncio.wait_for(asyncio.to_thread(ai_frame_audio_translate,vpath,sm_v,uid,source_vdur,n_win),timeout=max(240,int(source_vdur*2)))
        seg_scripts = analysis.get("scripts", []) if isinstance(analysis, dict) else analysis
        # The transcript is bucketed by source-audio time, and silent buckets are
        # intentionally omitted. Therefore the number of valid narration chunks
        # is NOT required to equal n_win (which is only the maximum time-window
        # count). Validate against the retained source ranges instead, so videos
        # containing pauses/silence do not fail after a successful transcript.
        if not isinstance(seg_scripts, list) or not seg_scripts:
            raise RuntimeError("မူရင်းအသံမှ ပြန်ဆိုရန် narration စာသားမရရှိပါ။")
        if isinstance(analysis, dict):
            expected_chunks = len(analysis.get("keep_ranges") or [])
            if expected_chunks == 0 or len(seg_scripts) != expected_chunks:
                raise RuntimeError(
                    f"Transcript/voice segments မကိုက်ညီပါ (script={len(seg_scripts)}, source_ranges={expected_chunks})။ ပြန်စမ်းပါ။"
                )
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
        await asyncio.wait_for(asyncio.to_thread(edit_vid,vpath,apath,opath,u,uid,source_vdur,analysis if isinstance(analysis, dict) else raw_durs),timeout=max(720,int(target_vdur*9)+60))
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
                "-c:v", "libx264", "-preset", "veryfast", "-b:v", f"{compress_kbps}k", "-maxrate", f"{int(compress_kbps*1.5)}k", "-bufsize", f"{compress_kbps*2}k",
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
                    "-c:v", "libx264", "-preset", "veryfast", "-b:v", f"{low_kbps}k", "-maxrate", f"{int(low_kbps*1.5)}k", "-bufsize", f"{low_kbps*2}k",
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
        for p in [vpath,sm_v,apath,opath,f"srcaud_{uid}.wav",f"out_{uid}_compressed.mp4",f"out_{uid}_compressed2.mp4"]:
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