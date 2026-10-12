from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from config import Settings
from db import Database
from downloader import classify_url, normalize_url
from key_store import SecretStore
from worker import Worker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sigyi")
settings = Settings.from_env()
db = Database(settings.db_path)
secret_store = SecretStore(settings.data_dir)
message_ids: dict[str, int] = {}
awaiting_api_key: set[int] = set()


MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [["🔑 API Key", "📋 Status"], ["/start"]],
    resize_keyboard=True,
)


def cancel_kb(jid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🛑 Cancel", callback_data=f"cancel:{jid}")]])


def retry_kb(jid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔁 Retry", callback_data=f"retry:{jid}")]])


async def progress(jid: str, text: str) -> None:
    row = db.get_job(jid)
    if not row: return
    chat_id = int(row["user_id"])
    mid = message_ids.get(jid)
    try:
        if mid:
            await app.bot.edit_message_text(chat_id=chat_id, message_id=mid, text=f"#{jid[-8:]}\n{text}")
        else:
            m = await app.bot.send_message(chat_id=chat_id, text=f"#{jid[-8:]}\n{text}", reply_markup=cancel_kb(jid))
            message_ids[jid] = m.message_id
    except Exception as exc:
        log.debug("progress update failed: %s", exc)
    if "Video ပြီးပါပြီ" in text:
        artifact = db.get_artifact(jid, "FINAL_VIDEO")
        if artifact and Path(artifact["path"]).exists():
            try:
                with open(artifact["path"], "rb") as video:
                    await app.bot.send_video(chat_id=chat_id, video=video, caption="✅ SiGyi Movie Recap — Phase 3/4", read_timeout=300, write_timeout=300)
                db.set_job(jid, status="COMPLETED", current_stage="COMPLETED")
            except Exception as exc:
                db.event(jid, "DELIVERY_FAILED", str(exc)[:500])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("🎬 SiGyi Movie Recap Bot\n\nTikTok, YouTube, Rednote link ပို့ပါ။\n\n🔑 API Key ခလုတ်ကနေ ကိုယ့် Gemini API key ထည့်နိုင်ပါတယ်။", reply_markup=MAIN_KEYBOARD)


async def api_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    awaiting_api_key.add(update.effective_user.id)
    await update.message.reply_text("🔑 Gemini API Key ကို ဒီ chat ထဲ paste လုပ်ပါ။\n\nသိမ်းပြီးရင် key ကို message history ထဲက ဖျက်ဖို့ကြိုးစားပါမယ်။ မသိမ်းချင်တော့ရင် /cancelkey ပို့ပါ။")


async def delete_api_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    awaiting_api_key.discard(update.effective_user.id)
    db.delete_user_gemini_key(update.effective_user.id)
    await update.message.reply_text("✅ သင့် Gemini API key ကို ဖျက်လိုက်ပါပြီ။")


async def cancel_api_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    awaiting_api_key.discard(update.effective_user.id)
    await update.message.reply_text("API Key ထည့်တာကို ပယ်ဖျက်လိုက်ပါပြီ။")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = db.list_user_jobs(update.effective_user.id)
    if not rows:
        await update.message.reply_text("Job မရှိသေးပါ။")
        return
    lines = [f"{r['id'][-8:]} | {r['platform']} | {r['status']} | {r['current_stage']}" for r in rows]
    await update.message.reply_text("\n".join(lines), reply_markup=MAIN_KEYBOARD)


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()
    user_id = update.effective_user.id
    if text == "🔑 API Key":
        await api_key(update, context)
        return
    if text == "📋 Status":
        await status(update, context)
        return
    if user_id in awaiting_api_key:
        if len(text) < 20 or not text.startswith("AIza"):
            await update.message.reply_text("❌ API key ပုံစံမမှန်ပါ။ Google AI Studio key ကို ပြန် paste လုပ်ပါ၊ မဟုတ်ရင် /cancelkey ပို့ပါ။")
            return
        db.set_user_gemini_key(user_id, secret_store.encrypt(text))
        awaiting_api_key.discard(user_id)
        try:
            await update.message.delete()
        except Exception:
            pass
        await update.message.reply_text("✅ Gemini API key ကို လုံခြုံစွာ သိမ်းပြီးပါပြီ။ အခု link ပို့ပြီး video စမ်းနိုင်ပါတယ်။", reply_markup=MAIN_KEYBOARD)
        return
    match = re.search(r"https?://[^\s<>]+", text)
    if not match:
        await update.message.reply_text("TikTok၊ YouTube သို့မဟုတ် Rednote link ပို့ပါ။")
        return
    url = normalize_url(match.group(0)); platform = classify_url(url)
    if platform == "unknown":
        await update.message.reply_text("❌ ဒီ link က support မလုပ်ထားတဲ့ platform ဖြစ်ပါတယ်။")
        return
    duplicate = db.find_active_duplicate(update.effective_user.id, url)
    if duplicate:
        await update.message.reply_text(f"ဒီ link အတွက် job ရှိပြီးသားပါ။\nJob: #{duplicate['id'][-8:]}\nStatus: {duplicate['status']}")
        return
    config = {"voice": "Thiha", "layout": "frame", "blur": "auto", "logo": False}
    jid = db.create_job(update.effective_user.id, url, platform, config, settings.download_attempts)
    m = await update.message.reply_text(f"⏳ Job လက်ခံပြီးပါပြီ\n#{jid[-8:]}\nPlatform: {platform}\n📥 Queue ထဲထည့်နေပါတယ်", reply_markup=cancel_kb(jid))
    message_ids[jid] = m.message_id
    await worker.enqueue(jid)


async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query; await q.answer()
    action, jid = q.data.split(":", 1)
    row = db.get_job(jid)
    if not row or int(row["user_id"]) != update.effective_user.id:
        await q.edit_message_text("Job မတွေ့ပါ သို့မဟုတ် permission မရှိပါ။"); return
    if action == "cancel":
        db.request_cancel(jid); await q.edit_message_text(f"🛑 #{jid[-8:]} cancel request ပို့ပြီးပါပြီ။")
    elif action == "retry":
        if row["status"] not in {"FAILED", "RETRY_WAIT"}:
            await q.edit_message_text(f"ဒီ job ကို retry မလုပ်နိုင်ပါ။ Status: {row['status']}"); return
        db.set_job(jid, status="QUEUED", cancel_requested=0, last_error_code=None, last_error_message=None)
        await q.edit_message_text(f"🔁 #{jid[-8:]} retry queue ထဲ ထည့်ပြီးပါပြီ။")
        await worker.enqueue(jid)


async def post_init(application: Application) -> None:
    await worker.start()


async def post_shutdown(application: Application) -> None:
    await worker.stop(); db.close()


worker = Worker(db, settings.data_dir, settings.max_workers, settings.max_file_size_mb, settings.download_timeout, settings.gemini_keys, secret_store, progress)
app = Application.builder().token(settings.telegram_token).post_init(post_init).post_shutdown(post_shutdown).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("status", status))
app.add_handler(CommandHandler("apikey", api_key))
app.add_handler(CommandHandler("delapikey", delete_api_key))
app.add_handler(CommandHandler("cancelkey", cancel_api_key))
app.add_handler(CallbackQueryHandler(callback))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))


if __name__ == "__main__":
    if not settings.telegram_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is required")
    app.run_polling(allowed_updates=Update.ALL_TYPES)
