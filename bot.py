import os, re, sys, site, json, time, wave, base64, shutil, zipfile, asyncio, subprocess, urllib.request, urllib.parse, logging, html
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Install dependencies at deploy time using requirements.txt.
# Optional POT provider setup intentionally disabled at startup. Configure it separately if your deployment needs it.
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from google import genai
from google.genai import types
import yt_dlp, edge_tts
from gtts import gTTS

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
DB_FILE = "users.json"
SESSIONS, ACTIVE_TASKS = {}, {}
VOICES = ["Zephyr", "Charon", "Puck", "Kore", "Fenrir", "Aoede", "Leda", "Orus", "Thiha", "Nilar"]
EDGE_MAP = {"Thiha": "my-MM-ThihaNeural", "Nilar": "my-MM-NilarNeural"}
C1 = ("@moviezone336", "https://t.me/moviezone336", "📢 Channel 1 Join ရန်")
C2 = ("@color6777", "https://t.me/color6777", "📢 Channel 2 Join ရန်")
C3 = ("@sigyi89", "https://t.me/sigyi89", "📢 Channel 3 Join ရန်")
CHANNELS = [C1, C2, C3]

RAW_M = os.getenv("GEMINI_MODELS", "gemini-2.5-flash,gemini-2.0-flash").split(",")
MODELS = [x.strip().removeprefix("models/") for x in RAW_M if x.strip() and "flash" in x.lower()] or ["gemini-2.5-flash", "gemini-2.0-flash"]
TTS_MODELS = ["gemini-2.5-flash-preview-tts", "gemini-3.1-flash-tts-preview"]

def safe_execution(func, *args, **kwargs):
    try:
        return func(*args, **kwargs)
    except Exception as e:
        logging.exception("Error in %s", getattr(func, "__name__", "fn"))
        return None

def _write_db(db):
    """Persist the user database atomically to reduce corruption on crashes."""
    tmp = DB_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, DB_FILE)

def get_db():
    if not os.path.exists(DB_FILE):
        return {}
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as e:
        logging.error("Database read error: %s", e)
        return {}

def get_u(uid):
    db, k = get_db(), str(uid)
    old = db.get(k, {})
    if not isinstance(old, dict):
        old = {}
    keys = old.get("keys", []) or ([old["key"]] if old.get("key") else [])
    if not isinstance(keys, list):
        keys = []
    defaults = {
        "keys": keys,
        "k_idx": old.get("k_idx", 0),
        "v": old.get("v") if old.get("v") in VOICES else "Zephyr",
        "f": old.get("f", True),
        "layout": old.get("layout", "frame"),
        "blur": old.get("blur", "auto"),
        "logo": old.get("logo", True),
        "verified": old.get("verified", False),
    }
    # Normalize older records and protect code from missing/invalid settings.
    if defaults["layout"] not in ("frame", "full"):
        defaults["layout"] = "frame"
    if defaults["blur"] not in ("auto", "low", "mid", "high", "off"):
        defaults["blur"] = "auto"
    try:
        defaults["k_idx"] = max(0, int(defaults["k_idx"]))
    except (TypeError, ValueError):
        defaults["k_idx"] = 0
    if db.get(k) != defaults:
        db[k] = defaults
        try:
            _write_db(db)
        except OSError:
            logging.exception("Database write error while migrating user")
    return defaults

def set_u(uid, field, val):
    db, k = get_db(), str(uid)
    if k not in db or not isinstance(db[k], dict):
        db[k] = get_u(uid)
        db = get_db()
    db[k][field] = val
    try:
        _write_db(db)
    except OSError:
        logging.exception("Database write error for user %s", uid)

def extract_keys(text):
    raw = re.split(r"[\s,;]+", text.strip())
    return [
        x.strip() for x in raw
        if len(x.strip()) >= 20
        and not x.startswith(("http", "/"))
        and (x.startswith(("AIza", "AQ.")) or re.match(r"^[A-Za-z0-9_\-\.]{25,}$", x.strip()))
    ]

def add_user_keys(uid, new_keys):
    cur = list(get_u(uid).get("keys", []))
    added = 0
    for nk in new_keys:
        if nk not in cur:
            cur.append(nk)
            added += 1
    set_u(uid, "keys", cur)
    return added, len(cur)

async def check_joined(bot, uid):
    for ch_id, _, _ in CHANNELS:
        try:
            member = await bot.get_chat_member(chat_id=ch_id, user_id=uid)
            if member.status in ("left", "kicked"):
                return False
        except Exception:
            # Fallback to ignore errors if bot is not admin or user hasn't joined.
            continue
    return True

def cancel_user_task(uid):
    t = ACTIVE_TASKS.pop(uid, None)
    if t and not t.done():
        t.cancel()
    # Remove only this user's known intermediate/output files.
    for name in (f"in_{uid}.mp4", f"sm_{uid}.mp4", f"aud_{uid}.m4a", f"out_{uid}.mp4",
                 f"subdet_{uid}.raw"):
        if os.path.isfile(name):
            safe_execution(os.remove, name)
    SESSIONS.pop(uid, None)

def join_kb():
    rows = [[InlineKeyboardButton(lbl, url=url)] for _, url, lbl in CHANNELS]
    rows.append([InlineKeyboardButton("✅ Join ပြီးပါပြီ (Verify လုပ်မည်)", callback_data="verify|check")])
    return InlineKeyboardMarkup(rows)

def join_text():
    return "🔒 Bot ကို အသုံးပြုရန် အောက်ပါ Channel (၃) ခုကို အရင် Join ပေးပါ -\n\n1️⃣ https://t.me/moviezone336\n2️⃣ https://t.me/color6777\n3️⃣ https://t.me/sigyi89\n\n✅ (၃) ခုလုံး Join ပြီးပါက အောက်ပါ Verify ခလုတ်ကို နှိပ်ပါ -"

def main_kb():
    r1 = [KeyboardButton("🎬 Dashboard"), KeyboardButton("🔑 API Key")]
    r2 = [KeyboardButton("🎙️ Voice ရွေးရန်"), KeyboardButton("🖼️ Logo ပုံထည့်ရန်")]
    return ReplyKeyboardMarkup([r1, r2, [KeyboardButton("/start")]], resize_keyboard=True)

def cancel_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🛑 ရပ်တန့်မည် / ပြန်စမည် (/start)", callback_data="r|cancel")]])

def key_menu_ui(u):
    keys = u.get("keys", [])
    lines = [f"{i+1}. {k[:6]}••••{k[-4:]}" for i, k in enumerate(keys[:10])]
    k_info = f"✅ သိမ်းထားသော Key ({len(keys)}) ခု -\n" + "\n".join(lines) if keys else "❌ API Key မထည့်ရသေးပါ။"
    txt = f"🔑 GEMINI API KEY ROTATION စနစ်\n━━━━━━━━━━━━━━\n{k_info}\n\n💡 Key တစ်ခုချင်းဖြစ်စေ၊ အများကြီးကို Comma (,) ခြားပြီးဖြစ်စေ ပို့နိုင်ပါသည်။\n⚠️ Bot အလုပ်မလုပ်ပါက /start ကို နှိပ်ပါ။"
    kb = [[InlineKeyboardButton("🌐 Google AI Studio မှ Key ယူရန်", url="https://aistudio.google.com/app/apikey")]]
    if keys:
        kb.append([InlineKeyboardButton("🗑️ Key အားလုံး ဖျက်မည်", callback_data="m|clrkeys")])
    kb.append([InlineKeyboardButton("🔙 Dashboard သို့ ပြန်သွားရန်", callback_data="m|dash")])
    return txt, InlineKeyboardMarkup(kb)

def voice_menu_ui(u):
    cur = u.get("v", "Zephyr")
    txt = f"🎙️ Gemini AI & Fast Voice ရွေးချယ်ရန် (Speed: 1.20x)\nလက်ရှိအသံ - 🌟 {cur}\n━━━━━━━━━━━━━━"
    kb, row = [], []
    for v in VOICES:
        row.append(InlineKeyboardButton(f"{'🟢' if cur==v else '⚪'} {v}", callback_data=f"v|{v}"))
        if len(row) == 2:
            kb.append(row)
            row = []
    if row: kb.append(row)
    kb.append([InlineKeyboardButton("🔙 Dashboard သို့ ပြန်သွားရန်", callback_data="m|dash")])
    return txt, InlineKeyboardMarkup(kb)

def dash_ui(u, uid):
    k_cnt = len(u.get("keys", []))
    kt = f"✅ ({k_cnt}) ခု ချိတ်ပြီး" if k_cnt > 0 else "❌ မရှိသေးပါ"
    v_lbl = f"🌟 {u.get('v', 'Zephyr')} (1.20x)"
    lay = "📱 TikTok 9:16"
    bm = {"auto": "🎯 Auto-Detect", "low": "အောက်ခြေ", "mid": "အလယ်အောက်", "high": "အလယ်", "off": "OFF ❌"}
    b_txt = bm.get(u.get("blur", "auto"), "🎯 Auto-Detect")
    lg_t = ("ON ✅" if u.get("logo", True) else "OFF ❌") if os.path.exists(f"logo_{uid}.jpg") else "ပုံမထည့်ရသေး"
    fl_t = "ON ✅" if u.get("f", True) else "OFF ❌"
    kb = [
        [InlineKeyboardButton(f"🔑 API Keys: {kt}", callback_data="m|key")],
        [InlineKeyboardButton(f"🎙️ Voice: {v_lbl}", callback_data="m|vmenu"), InlineKeyboardButton(f"🖼️ {lay}", callback_data="m|lay")],
        [InlineKeyboardButton(f"🌫️ Blur: {b_txt}", callback_data="m|blur"), InlineKeyboardButton(f"🏷️ Logo: {lg_t}", callback_data="m|logo")],
        [InlineKeyboardButton(f"🪞 Flip + Filter + Crop: {fl_t}", callback_data="m|f")]
    ]
    txt = "⚡ SIGYI AI STUDIO PRO (FAST & PERFECT SYNC) ⚡\n━━━━━━━━━━━━━━\n"
    txt += f"• 🔑 API Keys: {kt} (Auto-Rotation ✅)\n"
    txt += f"• 🎙️ Voice: {v_lbl} (Single-Call Fast TTS ✅)\n"
    txt += "• 📜 Script: TikTok Pro Movie Recap Style ✅\n"
    txt += f"• 🖼️ {lay} | 🌫️ Blur: {b_txt}\n"
    txt += "• 🎯 Scene-by-Scene CFR 30fps Sync: Active ✅\n━━━━━━━━━━━━━━\n"
    txt += "📥 TikTok / YouTube / Rednote Link သို့မဟုတ် Video ပို့ပါ!\n"
    txt += "💡 (Bot အလုပ်မလုပ်ပါက /start ကို နှိပ်ပါ)"
    return txt, InlineKeyboardMarkup(kb)

def is_valid_video(p):
    if not os.path.exists(p) or os.path.getsize(p) < 10000: return False
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_type", "-of", "csv=p=0", p], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=20)
    return "video" in r.stdout.lower()

def is_valid_audio(p):
    if not os.path.exists(p) or os.path.getsize(p) < 600: return False
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_type", "-of", "csv=p=0", p], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=20)
    return "audio" in r.stdout.lower()

def save_stream_url(dl_url, vpath, ua, ref=""):
    try:
        safe_execution(os.remove, vpath)
        hdrs = {"User-Agent": ua, **({"Referer": ref} if ref else {})}
        with urllib.request.urlopen(urllib.request.Request(dl_url, headers=hdrs), timeout=20) as r, open(vpath, "wb") as f:
            while chunk := r.read(65536): f.write(chunk)
        if is_valid_video(vpath): return True
    except Exception:
        pass
    safe_execution(os.remove, vpath)
    return False
    
    def download_video(url, vpath):
    url = url.strip().rstrip("，。、！!?,.;:)>\"'")
    ua_m = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
    ua_d = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    safe_execution(os.remove, vpath)
    res_u = safe_execution(lambda: urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": ua_m}), timeout=8).geturl()) or url
    if "tiktok.com" in url.lower() or "tiktok.com" in res_u.lower():
        for tu in ([url, res_u] if res_u != url else [url]):
            api1 = f"https://www.tikwm.com/api/?url={urllib.parse.quote(tu)}&hd=1"
            resp1 = safe_execution(lambda: json.loads(urllib.request.urlopen(urllib.request.Request(api1, headers={"User-Agent": ua_m}), timeout=12).read().decode())) or {}
            d1 = resp1.get("data") or {}
            for fk in ["play", "hdplay", "wmplay"]:
                if pu := d1.get(fk):
                    pu = "https://www.tikwm.com" + pu if pu.startswith("/") else pu
                    if save_stream_url(pu, vpath, ua_m, "https://www.tikwm.com/"): return True
            api2 = f"https://api.tiklydown.eu.org/api/download?url={urllib.parse.quote(tu)}"
            d2 = (safe_execution(lambda: json.loads(urllib.request.urlopen(urllib.request.Request(api2, headers={"User-Agent": ua_d}), timeout=12).read().decode())) or {}).get("video", {})
            if (pu2 := (d2.get("noWatermark") or d2.get("watermark"))) and save_stream_url(pu2, vpath, ua_d): return True
        ydl_opts = {'format': 'best', 'outtmpl': vpath, 'quiet': True, 'no_warnings': True, 'http_headers': {'User-Agent': ua_m}}
        safe_execution(lambda: yt_dlp.YoutubeDL(ydl_opts).download([url]))
        if is_valid_video(vpath): return True
    ym = re.search(r"(?:shorts/|v=|youtu\.be/|embed/)([A-Za-z0-9_-]{11})", res_u) or re.search(r"(?:shorts/|v=|youtu\.be/|embed/)([A-Za-z0-9_-]{11})", url)
    if ym:
        vid_id = ym.group(1)
        clean_yt = f"https://www.youtube.com/watch?v={vid_id}"
        ydl_opts = {'format': 'bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/best[ext=mp4]/best', 'merge_output_format': 'mp4', 'outtmpl': vpath, 'quiet': True, 'nocheckcertificate': True, 'extractor_args': {'youtube': {'player_client': ['android_vr', 'android', 'ios', 'web'], 'skip': ['hls', 'dash']}}, 'http_headers': {'User-Agent': ua_d}}
        if os.path.exists('cookies.txt'): ydl_opts['cookiefile'] = 'cookies.txt'
        safe_execution(lambda: yt_dlp.YoutubeDL(ydl_opts).download([clean_yt]))
        if is_valid_video(vpath): return True
        from pytubefix import YouTube
        for cli in ["ANDROID_VR", "IOS", "WEB_EMBED", "MWEB"]:
            try:
                yt = YouTube(clean_yt, client=cli)
                st = yt.streams.filter(progressive=True, file_extension="mp4").order_by("resolution").desc().first()
                if st and safe_execution(lambda: st.download(filename=vpath)) and is_valid_video(vpath): return True
            except Exception:
                continue
        u_os = f"https://p.oceansaver.in/ajax/download.php?format=720&url={urllib.parse.quote(clean_yt)}"
        tid = (safe_execution(lambda: json.loads(urllib.request.urlopen(urllib.request.Request(u_os, headers={"User-Agent": ua_d}), timeout=10).read().decode())) or {}).get("id")
        for _ in range(7 if tid else 0):
            time.sleep(2)
            dl_u = (safe_execution(lambda: json.loads(urllib.request.urlopen(urllib.request.Request(f"https://p.oceansaver.in/ajax/progress.php?id={tid}", headers={"User-Agent": ua_d}), timeout=8).read().decode())) or {}).get("download_url")
            if dl_u and save_stream_url(dl_u, vpath, ua_d): return True
    ydl_g = {'format': 'best[ext=mp4]/best', 'outtmpl': vpath, 'quiet': True, 'nocheckcertificate': True}
    if os.path.exists('cookies.txt'): ydl_g['cookiefile'] = 'cookies.txt'
    safe_execution(lambda: yt_dlp.YoutubeDL(ydl_g).download([url]))
    return is_valid_video(vpath)

def clean_mm_text(t):
    t = re.sub(r"\([^)]*\)", "", t)
    t = re.sub(r"[*#_`~0-9:a-zA-Z\[\]()|•\-]", " ", t)
    return re.sub(r"\s+", " ", t).strip()

def get_duration(file_path):
    cmd = ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', file_path]
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, text=True, stderr=subprocess.PIPE, timeout=15)
        return float(result.stdout.strip())
    except Exception:
        return 0.0

def get_intro_silence(video_path):
    cmd = ['ffmpeg', '-t', '4', '-i', video_path, '-af', 'silencedetect=noise=-32dB:d=0.35', '-f', 'null', '-']
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=20)
    st_m = re.search(r"silence_start:\s*([0-9.]+)", r.stderr)
    en_m = re.search(r"silence_end:\s*([0-9.]+)", r.stderr)
    if st_m and en_m and float(st_m.group(1)) <= 0.15:
        return min(2.2, max(0.0, float(en_m.group(1))))
    return 0.0

def ai_frame_audio_translate(vpath, sm_v, uid, vdur, n_win):
    cmd_sm = ["ffmpeg", "-y", "-threads", "2", "-i", vpath, "-t", f"{vdur:.2f}", "-vf", "fps=1,scale=280:-2", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "35", "-c:a", "aac", "-ac", "1", "-ar", "16000", "-b:a", "28k", sm_v]
    subprocess.run(cmd_sm, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=40)
    up_file = sm_v if (os.path.exists(sm_v) and os.path.getsize(sm_v) > 1000) else vpath
    u = get_u(uid)
    keys = u.get("keys", [])
    if not keys: raise RuntimeError("API Key မရှိပါ။ Key အရင်ထည့်ပေးပါ။")
    start_idx = u.get("k_idx", 0) % len(keys)
    w_dur = vdur / n_win
    chars_w = max(55, int(w_dur * 13.5))
    win_desc = "၊ ".join([f"အပိုင်း {i+1} ({int(i*w_dur)}s-{int((i+1)*w_dur)}s)" for i in range(n_win)])
    p = f"ဒီ Video ဖိုင်ရဲ့ ရုပ်ပုံ (Frames) နဲ့ မူရင်းအသံ (Audio) ကို သေချာကြည့်ရှုနားထောင်ပြီး ({n_win}) ပိုင်း အတိအကျခွဲ၍ အပိုအလိုမရှိဘဲ TikTok မှာ ခေတ်စားနေတဲ့ Burmese Professional Movie Recap စကားပြောပုံစံဖြင့် ဘာသာပြန်ပေးပါ - {win_desc}\n"
    p += "ဥပမာ အရေးအသား - အမျိုးသားတစ်ယောက်ဟာ လှေပေါ်မှာ ဓားနဲ့ ကစားနေရင်း မတော်တဆ အပေါက်ဖောက်မိသွားတဲ့အတွက် ရေတွေ စတင် စိမ့်ဝင်လာပါတယ်။ ||| သူက သူ့ခြေထောက်နဲ့ အပေါက်ကို အမြန်ဖုံးအုပ်ပြီး ဘာမှမဖြစ်သလို ဟန်ဆောင်လိုက်ပါတယ်။ ||| ခေါင်းဆောင်က အန်နာ ဆီကို လျှောက်သွားပြီး ငါးမျှားတံကို ကောက်ကိုင်ကာ ငါးဖမ်းဟန်ဆောင်လိုက်ပါတယ်။\n"
    p += f"စည်းကမ်းချက် - မူရင်းဇာတ်လမ်းအတိုင်း အပိုအလိုမရှိ တစ်ပိုင်းစီအတွက် မြန်မာစာလုံးရေ {chars_w} လုံးခန့် ရေးပြီး အပိုင်းကြားတွင် ||| သင်္ကေတ ခြားပေးပါ။ အင်္ဂလိပ်စာ၊ ကွင်းစကွင်းပိတ်နှင့် နှုတ်ဆက်စကား လုံးဝ မထည့်ပါနှင့်။"
    raw_txt, last_err = "", None
    for step_i in range(len(keys)):
        k_i = (start_idx + step_i) % len(keys)
        client = genai.Client(api_key=keys[k_i].strip(), http_options=types.HttpOptions(timeout=35000))
        uf = safe_execution(lambda: client.files.upload(file=up_file))
        if not uf: continue
        for _ in range(60):
            if getattr(getattr(uf, "state", None), "name", "") == "PROCESSING":
                time.sleep(1)
                uf = safe_execution(lambda: client.files.get(name=uf.name)) or uf
        for m_name in MODELS:
            try:
                res = client.models.generate_content(model=m_name, contents=[uf, p])
                if res and res.text:
                    raw_txt = res.text
                    set_u(uid, "k_idx", k_i)
                    break
            except Exception as me:
                last_err = me
        safe_execution(lambda: client.files.delete(name=uf.name))
        if raw_txt: break
    if not raw_txt:
        raise RuntimeError(f"Gemini Script Error: {str(last_err)[:350]}")
    parts = [clean_mm_text(x) for x in raw_txt.split("|||") if len(clean_mm_text(x)) > 5]
    if len(parts) != n_win:
        full_c = clean_mm_text(raw_txt)
        sents = [s.strip() + "။" for s in full_c.split("။") if len(s.strip()) > 4] or [full_c]
        st = max(1, len(sents) / n_win)
        parts = [" ".join(sents[int(round(i*st)):max(int(round(i*st))+1, int(round((i+1)*st)) if i < n_win-1 else len(sents))]) or sents[-1] for i in range(n_win)]
    return parts

def _try_gemini_single_tts(seg_scripts, voice_name, out_wav, uid):
    if voice_name in EDGE_MAP: return False
    u = get_u(uid)
    keys = u.get("keys", [])
    if not keys: return False
    joined_text = "\n\n".join(clean_mm_text(s) for s in seg_scripts if clean_mm_text(s))
    pvc = types.PrebuiltVoiceConfig(voice_name=voice_name)
    sc = types.SpeechConfig(voice_config=types.VoiceConfig(prebuilt_voice_config=pvc))
    cfg = types.GenerateContentConfig(response_modalities=["AUDIO"], speech_config=sc)
    prompt = f"Read this Burmese narration aloud naturally and smoothly without adding any extra words:\n\n{joined_text}"
    start_idx = (u.get("k_idx", 0) + 1) % len(keys)
    for step_i in range(min(len(keys), 4)):
        k_i = (start_idx + step_i) % len(keys)
        client = genai.Client(api_key=keys[k_i].strip(), http_options=types.HttpOptions(timeout=25000))
        for tm in TTS_MODELS:
            try:
                res = client.models.generate_content(model=tm, contents=prompt, config=cfg)
                pcm = b""
                for part in res.candidates[0].content.parts:
                    if getattr(part, "inline_data", None) and part.inline_data.data:
                        d = part.inline_data.data
                        pcm += base64.b64decode(d) if isinstance(d, str) else d
                if pcm and len(pcm) > 1000:
                    if pcm[:4] == b"RIFF": open(out_wav, "wb").write(pcm)
                    else:
                        with wave.open(out_wav, "wb") as wf:
                            wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(24000); wf.writeframes(pcm)
                    if is_valid_audio(out_wav):
                        set_u(uid, "k_idx", k_i)
                        return True
            except Exception as e:
                if "404" in str(e): continue
                break
    return False

def _split_gtts_text(text, max_len=140):
    words, chunks, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 <= max_len: cur = (cur + " " + w).strip()
        else:
            if cur: chunks.append(cur)
            cur = w
    if cur: chunks.append(cur)
    out = []
    for c in (chunks or [text]):
        for i in range(0, len(c), max_len): out.append(c[i:i + max_len])
    return out

def _dl_one_gtts(args):
    chunk, path = args
    q = urllib.parse.quote(chunk)
    for host in ["translate.googleapis.com", "translate.google.com"]:
        u_g = f"https://{host}/translate_tts?ie=UTF-8&client=gtx&tl=my&q={q}"
        data = safe_execution(lambda: urllib.request.urlopen(urllib.request.Request(u_g, headers={"User-Agent": "Mozilla/5.0"}), timeout=8).read())
        if data:
            open(path, "wb").write(data)
            if is_valid_audio(path): return True
    safe_execution(lambda: gTTS(text=chunk, lang="my", lang_check=False).save(path))
    return is_valid_audio(path)

def _fast_google_tts_segment(text, out_mp3, work, seg_idx):
    chunks = _split_gtts_text(clean_mm_text(text) or "အဲ့ဒီနောက်မှာတော့", 140)
    c_paths = [os.path.join(work, f"gt_{seg_idx}_{i}.mp3") for i in range(len(chunks))]
    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(_dl_one_gtts, zip(chunks, c_paths)))
    if not all(results): return False
    if len(c_paths) == 1:
        shutil.move(c_paths[0], out_mp3)
        return is_valid_audio(out_mp3)
    list_p = os.path.join(work, f"gt_l_{seg_idx}.txt")
    Path(list_p).write_text("".join(f"file '{os.path.abspath(p)}'\n" for p in c_paths), encoding="utf-8")
    subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_p, "-c:a", "libmp3lame", "-q:a", "3", out_mp3], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    return is_valid_audio(out_mp3)

def _detect_silence_midpoints(wav_path):
    r = subprocess.run(["ffmpeg", "-i", wav_path, "-af", "silencedetect=n=-36dB:d=0.10", "-f", "null", "-"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    starts = [float(x) for x in re.findall(r"silence_start:\s*([0-9.]+)", r.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end:\s*([0-9.]+)", r.stderr)]
    return [(s + e) / 2.0 for s, e in zip(starts, ends)]

async def build_synced_voice(seg_scripts, voice_name, out_a, uid, vdur, intro_delay):
    work = f"tts_{uid}_{int(time.time() * 1000)}"
    os.makedirs(work, exist_ok=True)
    try:
        raw_full = os.path.join(work, "gem_raw.wav")
        gem_ok = await asyncio.to_thread(_try_gemini_single_tts, seg_scripts, voice_name, raw_full, uid)
        if gem_ok and is_valid_audio(raw_full):
            delay_ms = int(intro_delay * 1000)
            d_f = f",adelay={delay_ms}|{delay_ms}" if delay_ms > 150 else ""
            af = f"silenceremove=start_periods=1:start_duration=0.02:start_threshold=-45dB,areverse,silenceremove=start_periods=1:start_duration=0.02:start_threshold=-45dB,areverse,atempo=1.20,highpass=f=80,loudnorm=I=-16:TP=-1.5:LRA=11{d_f},aresample=24000,asetpts=PTS-STARTPTS"
            subprocess.run(["ffmpeg", "-y", "-i", raw_full, "-af", af, "-ac", "1", "-ar", "24000", "-c:a", "aac", "-b:a", "160k", out_a], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45)
            if is_valid_audio(out_a):
                tot_dur = max(1.0, get_duration(out_a))
                mids = _detect_silence_midpoints(out_a)
                weights = [max(1, len(clean_mm_text(s))) for s in seg_scripts]
                tot_w = sum(weights) or 1
                cuts, acc = [0.0], 0.0
                speech_dur = max(1.0, tot_dur - intro_delay)
                for i in range(len(seg_scripts) - 1):
                    acc += weights[i]
                    exp_t = intro_delay + speech_dur * (acc / tot_w)
                    near = [m for m in mids if abs(m - exp_t) <= max(1.6, speech_dur * 0.08) and m > cuts[-1] + 0.4]
                    best = min(near, key=lambda m: abs(m - exp_t)) if near else max(cuts[-1] + 0.4, exp_t)
                    cuts.append(min(tot_dur - 0.3, best))
                cuts.append(tot_dur)
                return [max(0.4, cuts[i+1] - cuts[i]) for i in range(len(seg_scripts))]
        is_male = voice_name in ["Charon", "Puck", "Fenrir", "Orus", "Thiha"]
        edge_v = "my-MM-ThihaNeural" if is_male else "my-MM-NilarNeural"
        edge_wavs, edge_ok = [], True
        for i, s in enumerate(seg_scripts):
            txt = clean_mm_text(s) or "အဲ့ဒီနောက်မှာတော့"
            rm, cw = os.path.join(work, f"ed_{i}.mp3"), os.path.join(work, f"ed_{i}.wav")
            try:
                await asyncio.wait_for(edge_tts.Communicate(txt, edge_v, rate="+20%").save(rm), timeout=7)
            except Exception:
                pass
            if not is_valid_audio(rm):
                edge_ok = False; break
            d_f = f",adelay={int(intro_delay*1000)}|{int(intro_delay*1000)}" if (i == 0 and intro_delay > 0.15) else ""
            af = f"silenceremove=start_periods=1:start_duration=0.02:start_threshold=-45dB,areverse,silenceremove=start_periods=1:start_duration=0.02:start_threshold=-45dB,areverse,highpass=f=80,loudnorm=I=-16:TP=-1.5:LRA=11{d_f},aresample=24000,asetpts=PTS-STARTPTS"
            subprocess.run(["ffmpeg", "-y", "-i", rm, "-af", af, "-ac", "1", "-ar", "24000", cw], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=25)
            if not is_valid_audio(cw):
                edge_ok = False; break
            edge_wavs.append(cw)
        if not edge_ok or len(edge_wavs) != len(seg_scripts):
            edge_wavs = []
            spd_f = "asetrate=24000*0.89,aresample=24000,atempo=1.35" if is_male else "atempo=1.22"
            for i, s in enumerate(seg_scripts):
                rm, cw = os.path.join(work, f"gt_{i}.mp3"), os.path.join(work, f"gt_{i}.wav")
                ok_g = await asyncio.to_thread(_fast_google_tts_segment, s, rm, work, i)
                if not ok_g: raise RuntimeError("TTS အသံထုတ်မရပါ။")
                d_f = f",adelay={int(intro_delay*1000)}|{int(intro_delay*1000)}" if (i == 0 and intro_delay > 0.15) else ""
                af = f"silenceremove=start_periods=1:start_duration=0.02:start_threshold=-45dB,areverse,silenceremove=start_periods=1:start_duration=0.02:start_threshold=-45dB,areverse,{spd_f},highpass=f=80,loudnorm=I=-16:TP=-1.5:LRA=11{d_f},aresample=24000,asetpts=PTS-STARTPTS"
                subprocess.run(["ffmpeg", "-y", "-i", rm, "-af", af, "-ac", "1", "-ar", "24000", cw], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=25)
                edge_wavs.append(cw)
        list_f = os.path.join(work, "concat.txt")
        Path(list_f).write_text("".join(f"file '{os.path.abspath(w)}'\n" for w in edge_wavs), encoding="utf-8")
        subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_f, "-c:a", "aac", "-b:a", "160k", out_a], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        return [max(0.4, get_duration(w)) for w in edge_wavs]
    finally:
        safe_execution(shutil.rmtree, work, True)

def detect_sub_band(iv, vdur, is_fr, uid):
    raw_f = f"subdet_{uid}.raw"
    w, h = 360, (410 if is_fr else 640)
    vf = f"fps=6/{max(2.0, vdur):.2f},crop=iw*0.92:ih*0.92:iw*0.04:ih*0.04,scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"
    subprocess.run(["ffmpeg", "-y", "-threads", "1", "-i", iv, "-vf", vf, "-frames:v", "6", "-f", "rawvideo", "-pix_fmt", "gray", raw_f], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    dy, dh = (950, 86) if is_fr else (1040, 92)
    if not os.path.exists(raw_f): return dy, dh
    data = open(raw_f, "rb").read()
    safe_execution(os.remove, raw_f)
    n_f = len(data) // (w * h)
    if n_f < 1: return dy, dh
    ys, ye, scores = int(h * 0.56), int(h * 0.96), [0.0] * h
    for f_i in range(n_f):
        b = f_i * w * h
        for y in range(ys, ye):
            ro = b + y * w
            hits = sum(1 for x in range(int(w * 0.15), int(w * 0.85), 2) if abs(data[ro + x] - data[ro + x - 2]) > 40)
            if hits > 8: scores[y] += hits
    by_i = max(range(ys, ye - 16), key=lambda y: sum(scores[y:y + 16]), default=-1)
    if by_i < 0 or sum(scores[by_i:by_i + 16]) < 25 * n_f: return dy, dh
    out_c = int((by_i + 8) * 2.0) + (230 if is_fr else 0)
    return max(240 if is_fr else 100, min((1050 - dh) if is_fr else (1240 - dh), out_c - int(dh * 0.46))), dh

def build_piecewise_pts(vdur, seg_durs):
    n = len(seg_durs)
    w_d = max(0.2, vdur / max(1, n))
    ts = [i * w_d for i in range(n)]
    ss = [sum(seg_durs[:i]) for i in range(n)]
    sl = [d / w_d for d in seg_durs]
    ex = f"{ss[-1]:.4f}+(T-{ts[-1]:.4f})*{sl[-1]:.4f}"
    for i in range(n - 2, -1, -1):
        ex = f"if(lt(T,{ts[i+1]:.4f}),{ss[i]:.4f}+(T-{ts[i]:.4f})*{sl[i]:.4f},{ex})"
    return f"trim=duration={vdur:.3f},fps=30,settb=AVTB,setpts=PTS-STARTPTS,setpts='({ex})/TB',fps=30"

def _ass_time(seconds):
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    sec = seconds % 60
    return f"{h}:{m:02d}:{sec:05.2f}"

def _ass_escape(text):
    text = html.escape(clean_mm_text(text), quote=False)
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}").replace("\n", " ")

def make_burmese_ass(seg_scripts, seg_durs, ass_path):
    """Create mobile-safe Burmese subtitles using one Unicode font and dark styling."""
    font = "/usr/share/fonts/truetype/noto/NotoSansMyanmar-Medium.ttf"
    if not os.path.exists(font):
        font = "/usr/share/fonts/truetype/padauk/Padauk-Regular.ttf"
    t = [
        "[Script Info]", "ScriptType: v4.00+", "PlayResX: 720", "PlayResY: 1280",
        "ScaledBorderAndShadow: yes", "WrapStyle: 2", "YCbCr Matrix: TV.709",
        "", "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        # Yellow text, near-black outline/shadow; centered in the lower blur band.
        "Style: Burmese,Noto Sans Myanmar,39,&H0000D7FF,&H0000D7FF,&H00101010,&H90101010,0,0,0,0,100,100,0,0,1,5,2,2,42,42,178,1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"
    ]
    cur = 0.0
    for script, duration in zip(seg_scripts, seg_durs):
        dur = max(0.40, float(duration))
        end = cur + dur
        text = _ass_escape(script) or "အဲ့ဒီနောက်မှာတော့"
        t.append(f"Dialogue: 0,{_ass_time(cur)},{_ass_time(end)},Burmese,,0,0,0,,{text}")
        cur = end
    Path(ass_path).write_text("\n".join(t) + "\n", encoding="utf-8")
    return ass_path

def _ffmpeg_filter_path(path):
    # FFmpeg filter syntax needs escaping for backslashes, colon and apostrophes.
    return str(path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")

def render_perfect_sync(iv, ia, ov, u, uid, vdur, seg_durs, seg_scripts):
    """Render a fixed 720x1280 TikTok video and time-warp video to the generated voice."""
    out_dur = max(get_duration(ia), 1.0)
    # Always produce TikTok 9:16 as requested, regardless of legacy layout setting.
    is_fr, fl = True, (",hflip" if u.get("f") else "")
    adj = "crop=iw*0.92:ih*0.92:iw*0.04:ih*0.04,eq=contrast=1.08:brightness=0.02:saturation=1.14:gamma=1.03,hue=h=3,unsharp=3:3:0.65:3:3:0.0"
    pw_s = build_piecewise_pts(vdur, seg_durs)
    un_s = f"trim=duration={vdur:.3f},fps=30,setpts={out_dur/max(vdur,1.0):.4f}*(PTS-STARTPTS),fps=30"
    b_map = {"low": (950, 86), "mid": (880, 86), "high": (790, 86)}
    by, bh = detect_sub_band(iv, vdur, True, uid) if u.get("blur") == "auto" else b_map.get(u.get("blur"), (950, 86))
    by, bh = (int(by) // 2) * 2, max(64, (int(bh) // 2) * 2)
    ass_path = f"sub_{uid}.ass"
    make_burmese_ass(seg_scripts, seg_durs, ass_path)
    ass_filter = _ffmpeg_filter_path(os.path.abspath(ass_path))
    kbps = min(3200, max(1000, int((42 * 8192) / out_dur) - 160))
    last_err = ""
    try:
        for sync_f in [pw_s, un_s]:
            safe_execution(os.remove, ov)
            fc = f"[0:v]{sync_f}{fl}[v0];"
            fc += f"[v0]split=2[bgi][fgi];[bgi]scale=180:320:force_original_aspect_ratio=increase,crop=180:320,gblur=sigma=14,eq=brightness=-0.07,scale=720:1280[bg];[fgi]{adj},scale=720:820:force_original_aspect_ratio=increase:flags=lanczos,crop=720:820[fg];[bg][fg]overlay=0:230[v1]"
            cur = "[v1]"
            if u.get("blur") != "off":
                fc += f";{cur}split=2[m1][m2];[m2]crop=640:{bh}:40:{by},gblur=sigma=26[bst];[m1][bst]overlay=40:{by}[v2]"
                cur = "[v2]"
            inputs, logo_p = ["-i", iv, "-i", ia], f"logo_{uid}.jpg"
            if u.get("logo") and os.path.exists(logo_p):
                inputs.extend(["-i", logo_p])
                fc += f";[2:v]scale=110:110[lg];{cur}[lg]overlay=12:165[v3]"
                cur = "[v3]"
            # Burn subtitles after blur/logo so yellow text is always visible on the blur band.
            fc += f";{cur}subtitles='{ass_filter}':fontsdir='/usr/share/fonts/truetype/noto'[vsub];[vsub]format=yuv420p[vout]"
            c = ["ffmpeg", "-y", "-threads", "2"] + inputs + ["-map_metadata", "-1", "-map_chapters", "-1", "-filter_complex", fc, "-map", "[vout]", "-map", "1:a:0", "-c:v", "libx264", "-preset", "superfast", "-crf", "19", "-pix_fmt", "yuv420p", "-maxrate", f"{kbps}k", "-bufsize", f"{kbps*2}k", "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2", "-shortest", "-t", f"{out_dur:.2f}", "-fs", "47M", ov]
            r = subprocess.run(c, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=max(300, int(out_dur * 5)))
            if r.returncode == 0 and is_valid_video(ov):
                return True
            last_err = r.stderr[-1000:]
        raise RuntimeError(f"Render Error: {last_err}")
    finally:
        safe_execution(os.remove, ass_path)

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
        safe_execution(lambda: asyncio.create_task(update.message.delete()))
        txt, kb = dash_ui(get_u(uid), uid)
        await context.bot.send_message(uid, f"✅ API Key ({added}) ခု အသစ်ထပ်ပေါင်းပြီး စုစုပေါင်း ({total}) ခု သိမ်းဆည်းပြီးပါပြီ!\n\n" + txt, reply_markup=kb)
        return
    if not u.get("keys"):
        txt, kb = key_menu_ui(u)
        await update.message.reply_text("🔒 Gemini API Key အရင်ထည့်ပေးပါ!\n\n" + txt, reply_markup=kb)
        return
    cancel_user_task(uid)
    msg = await update.message.reply_text("⏳ [15%] Video ဆွဲယူနေပါတယ်...\n💡 (အကယ်၍ ရပ်နေပါက /start နှိပ်ပါ)", reply_markup=cancel_kb())
    vpath = f"in_{uid}.mp4"
    try:
        if um:
            ok = await asyncio.wait_for(asyncio.to_thread(download_video, um.group(0), vpath), timeout=150)
            if not ok: raise RuntimeError("Video Link ဒေါင်းယူ၍ မရပါ။")
        if not um and (update.message.video or update.message.document):
            v = update.message.video or update.message.document
            f = await context.bot.get_file(v.file_id)
            await f.download_to_drive(vpath)
        if not is_valid_video(vpath):
            await msg.edit_text("❌ Video ဖိုင် မမှန်ကန်ပါ သို့မဟုတ် Video Link ပို့ပါ!\n💡 (Bot အလုပ်မလုပ်ပါက /start နှိပ်ပါ)")
            return
        SESSIONS[uid] = vpath
        txt, kb = dash_ui(u, uid)
        btn = InlineKeyboardButton(f"🚀 START RENDER (🌟 {u['v']} 1.20x | Ultra HD)", callback_data="r|go")
        await msg.edit_text("🎬 Video ရပြီ! စတင်ရန် START RENDER ကို နှိပ်ပါ -\n\n" + txt, reply_markup=InlineKeyboardMarkup(list(kb.inline_keyboard) + [[btn]]))
    except Exception as e:
        err_m = str(e).strip() or type(e).__name__
        await msg.edit_text(f"❌ Download Error: {err_m}\n\n💡 Bot အလုပ်မလုပ်ပါက /start ကို နှိပ်ပါ။")

async def run_render_job(q, context, uid):
    u, vpath = get_u(uid), SESSIONS.get(uid)
    sm_v, apath, opath = f"sm_{uid}.mp4", f"aud_{uid}.m4a", f"out_{uid}.mp4"
    hint = "\n\n💡 ရပ်နေပါက /start နှိပ်ပြီး video ပြန်ပို့ပါ"
    stage = "စတင်နေပါသည်"
    try:
        if not vpath or not is_valid_video(vpath):
            raise RuntimeError("Input video မမှန်ကန်ပါ")
        vdur = min(max(1.0, get_duration(vpath)), 180.0)
        intro_delay = get_intro_silence(vpath)
        n_win = 3 if vdur <= 28 else (4 if vdur <= 60 else (5 if vdur <= 110 else 6))
        stage = "Gemini scene analysis"
        await q.edit_message_text(f"🔄 [30%] Video ကို အပိုင်း ({n_win}) ပိုင်းခွဲ၍ TikTok Pro Recap ဘာသာပြန်နေပါသည်...{hint}", reply_markup=cancel_kb())
        seg_scripts = await asyncio.wait_for(asyncio.to_thread(ai_frame_audio_translate, vpath, sm_v, uid, vdur, n_win), timeout=160)
        stage = "TTS voice generation"
        await q.edit_message_text(f"🔄 [60%] 🎙️ {u['v']} (1.20x) ဖြင့် အသံနှင့် Scene အချိန် ချိန်ညှိနေပါသည်...{hint}", reply_markup=cancel_kb())
        seg_durs = await asyncio.wait_for(build_synced_voice(seg_scripts, u["v"], apath, uid, vdur, intro_delay), timeout=110)
        stage = "FFmpeg video/audio render"
        await q.edit_message_text(f"🔄 [85%] Scene-by-Scene CFR 30fps Perfect Sync Render လုပ်နေပါသည်...{hint}", reply_markup=cancel_kb())
        await asyncio.wait_for(asyncio.to_thread(render_perfect_sync, vpath, apath, opath, u, uid, vdur, seg_durs, seg_scripts), timeout=max(300, int(vdur * 5)))
        await q.edit_message_text("📤 [98%] Ultra HD Video ပို့ပေးနေပါတယ်...")
        with open(opath, "rb") as vf:
            await context.bot.send_video(uid, vf, caption="✅ ပြီးပါပြီ။\nPowered by SiGyi Ai Studio", read_timeout=300, write_timeout=300)
        # ဤနေရာတွင် မူလ Error ဖြစ်စေသော await q.delete_message() ကို await q.message.delete() ဖြင့် အစားထိုးပြင်ဆင်ထားပါသည်။
        await q.message.delete()
    except asyncio.CancelledError:
        pass
    except asyncio.TimeoutError:
        safe_execution(lambda: asyncio.create_task(q.edit_message_text(f"❌ {stage} အဆင့်မှာ အချိန်ကျော်သွားပါတယ်။ /start နှိပ်ပြီး ပြန်စမ်းပေးပါ။")))
    except Exception as e:
        err = str(e).strip() or type(e).__name__
        safe_execution(lambda: asyncio.create_task(q.edit_message_text(f"❌ {stage} Error: {err[:800]}\n\n💡 /start နှိပ်ပြီး ပြန်စမ်းပါ")))
    finally:
        ACTIVE_TASKS.pop(uid, None)
        SESSIONS.pop(uid, None)
        for p in [vpath, sm_v, apath, opath]:
            if p and os.path.exists(p):
                safe_execution(os.remove, p)

async def cb_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid, u = q.from_user.id, get_u(q.from_user.id)
    if not q.data or "|" not in q.data:
        await q.answer("ခလုတ်အချက်အလက် မမှန်ကန်ပါ။", show_alert=True)
        return
    cat, act = q.data.split("|", 1)
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
            rows.append([InlineKeyboardButton(f"🚀 START RENDER (🌟 {u_new['v']} 1.20x | Ultra HD)", callback_data="r|go")])
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