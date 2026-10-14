Railway deploy notes — Myanmar Movie Recap Bot (Thiha/Nilar 1.25x, no SRT)

Files:
- bot.py: bot source
- requirements.txt: Python dependencies
- Dockerfile: installs FFmpeg and Noto fonts
- nixpacks.toml: alternative builder configuration

Railway setup:
1. Upload these files to the same GitHub repository and deploy on Railway.
2. Prefer Dockerfile builder so FFmpeg is installed consistently.
3. Set TELEGRAM_BOT_TOKEN in Railway Variables.
4. Add a Gemini API key using the bot's API Key menu. Gemini is used for video analysis and Burmese recap text.
5. Run one replica because Telegram long polling and users.json are local.

Voice:
- Voice selection is limited to Thiha (my-MM-ThihaNeural) and Nilar (my-MM-NilarNeural).
- Speech rate is set to +25% (approximately 1.25x).
- Edge TTS is an unofficial client and may be rejected upstream (for example, 403). Retries are included, but no code can guarantee the external service will never fail.

Subtitles:
- SRT generation, SRT delivery, and burned-in subtitle rendering have been removed as requested.

Other notes:
- Gemini model discovery uses models exposed by the API key, with GEMINI_MODELS as fallback if listing fails. Set it only to IDs actually available to your key.
- Some private, removed, region-blocked, or anti-bot-protected video links may fail to download.
- Railway local storage may reset after redeploy. Attach a persistent Volume if users.json must persist.
- Syntax and local media pipeline checks are possible, but live Edge TTS, Gemini API, and Railway networking require real deployment credentials and cannot be guaranteed by local checks.
Never share Telegram bot tokens or Gemini API keys publicly.
