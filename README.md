# SiGyi Telegram Bot — Phase 1–4 Implementation

ဒီ package က reliability rebuild ရဲ့ Phase 1 မှ Phase 4 အထိ ပါဝင်တဲ့ Telegram Bot implementation ဖြစ်ပါတယ်။

## Pipeline

```text
Telegram URL
  -> SQLite job
  -> background worker
  -> TikTok/YouTube/Rednote download
  -> ffprobe validation
  -> Gemini video analysis + timestamped Burmese script
  -> Google TTS per segment
  -> per-segment audio fit
  -> master timeline validation
  -> FFmpeg render
  -> final MP4 validation
  -> Telegram delivery
```

## Phase 1/2

- SQLite job state and stage records
- Background queue and startup recovery
- Retry/resume and cancellation
- Persistent artifacts under `data/jobs/<job_id>/`
- TikTok, YouTube, Rednote downloader adapters
- `yt-dlp` retry profiles
- Rednote HTML/OpenGraph/CDN extraction
- `ffprobe` validation

## Phase 3 — Transcript and Burmese script

Gemini key ကို Railway Variable အနေနဲ့ထည့်စရာမလိုဘဲ Telegram bot ထဲက `🔑 API Key` ခလုတ် သို့မဟုတ် `/apikey` command နဲ့ user ကိုယ်တိုင်ထည့်နိုင်ပါတယ်။ Bot က key ကို persistent `/app/data/.bot_secret` နဲ့ encrypt လုပ်ပြီး SQLite ထဲမှာ encrypted value ပဲသိမ်းပါတယ်။ Key message ကို save ပြီးနောက် Telegram message history ထဲက ဖျက်ဖို့ကြိုးစားပါတယ်။

အခြားနည်းအဖြစ် `GEMINI_API_KEY` သို့မဟုတ် comma-separated `GEMINI_API_KEYS` environment variables ကိုလည်း support လုပ်ထားပါတယ်။ User ထည့်ထားတဲ့ key က env key ထက် ဦးစားပေးအသုံးပြုပါတယ်။ Worker က validated video ကို Gemini သို့ upload လုပ်ပြီး strict JSON output တောင်းကာ segment count/timestamps validate လုပ်ပြီး `transcript/segments.json` သိမ်းပါတယ်။ Transcript artifact ရှိပြီးသားဆို retry မှာ Gemini quota မသုံးဘဲ ပြန်အသုံးပြုပါတယ်။

The default model is `gemini-2.5-flash` in `transcript.py`. Change it only after checking that the configured Google AI Studio account/key has quota for the selected model.

## Phase 4 — TTS, sync and render

Each Burmese script segment is generated separately through Google TTS, converted to clean 24 kHz mono PCM, measured, and then fitted to its exact source-video interval. The fitted segments are concatenated into one AAC narration file. The timeline is validated before FFmpeg starts. The final MP4 is checked for duration and valid video/audio streams before Telegram delivery.

The sync policy does not stretch the entire narration blindly at the end. It fits each segment independently, which prevents accumulated drift at scene boundaries. Small AAC container padding is accepted during validation; the video stream itself is forced to the source duration and constant 30 FPS.

## Run locally

```bash
sudo apt-get update && sudo apt-get install -y ffmpeg
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN='your-token'
export GEMINI_API_KEY='your-gemini-key'
python bot.py
```

## Environment variables

| Variable | Default | Purpose |
|---|---:|---|
| `TELEGRAM_BOT_TOKEN` | required | Telegram bot token |
| `GEMINI_API_KEY` | empty | One Gemini key |
| `GEMINI_API_KEYS` | empty | Comma-separated Gemini key rotation |
| `SIGYI_DATA_DIR` | `./data` | SQLite database and job artifacts |
| `SIGYI_MAX_WORKERS` | `1` | Concurrent jobs; one is safest for FFmpeg |
| `SIGYI_DOWNLOAD_ATTEMPTS` | `3` | Download retry limit |
| `SIGYI_DOWNLOAD_TIMEOUT` | `180` | Download stage timeout in seconds |
| `SIGYI_MAX_FILE_SIZE_MB` | `512` | Accepted media size limit |

## Telegram commands

- `/start` — bot welcome message
- `/status` — latest jobs
- `/apikey` — Gemini API key ထည့်ရန်
- `/delapikey` — ကိုယ်ပိုင် Gemini API key ဖျက်ရန်
- `/cancelkey` — API key input ပယ်ဖျက်ရန်
- Send a TikTok, YouTube or Rednote URL — create and process a job
- `Cancel` — request cancellation
- `Retry` — retry a failed job/stage

## Important limitations

The code is fully syntax-checked and the deterministic FFmpeg render path is tested with synthetic 12-second video/audio. Live Gemini transcript and Google TTS calls require your own credentials and available quota, so those external calls cannot be guaranteed by a local test. If Gemini quota is zero, the worker will stop at transcript and preserve the downloaded artifact rather than redownloading it.

The `data/` directory must be persistent. If a container is recreated without persistent storage, SQLite records and media artifacts disappear. Keep `SIGYI_MAX_WORKERS=1` during the first deployment.

## GitHub upload

Extract this package into a new repository root and push all files, including `Dockerfile`, `railway.toml`, `.env.example`, and `.github/workflows/compile.yml`. Do not commit a real `.env`, Telegram token, or Gemini key. The included GitHub Action only installs dependencies and runs Python compile checks.

## Railway deploy

Create a Railway service from the GitHub repository. Railway will use the included `Dockerfile`. Add these Variables in Railway:

```text
TELEGRAM_BOT_TOKEN=your Telegram bot token
GEMINI_API_KEY=your Gemini API key
SIGYI_DATA_DIR=/app/data
SIGYI_MAX_WORKERS=1
SIGYI_DOWNLOAD_ATTEMPTS=3
SIGYI_DOWNLOAD_TIMEOUT=180
SIGYI_MAX_FILE_SIZE_MB=512
```

Attach a persistent volume mounted at `/app/data` if job recovery and downloaded artifacts must survive redeploys. Without a persistent volume, the bot can run, but SQLite jobs and media files are ephemeral. Deploy with one worker first; increase concurrency only after checking CPU, RAM, disk and provider quotas.
