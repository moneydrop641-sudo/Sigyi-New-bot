# Sigyi AI Studio — Edge TTS Bot

Telegram bot for video processing with Burmese Microsoft Edge neural voices, Gemini-powered text processing, and FFmpeg video/audio rendering.

## Files in this repository

- `bot.py` — bot application (Edge TTS voices: `my-MM-NilarNeural`, `my-MM-ThihaNeural`).
- `requirements.txt` — Python dependencies.
- `Dockerfile` — installs FFmpeg and runs the bot; recommended for Railway or another Docker host.
- `.env.example` — environment variable template. Do not commit real tokens or API keys.
- `.gitignore` — excludes secrets, user data, and generated media.

## Required setup

1. Create a Telegram bot with BotFather and copy its token.
2. Obtain a Gemini API key from Google AI Studio. Users can add their Gemini API keys through the bot UI if that feature is enabled in the code.
3. Deploy on a host that supports a continuously running process and Docker. FFmpeg is required; the included Dockerfile installs it.
4. Set `TELEGRAM_BOT_TOKEN` in the host's environment variables. Do not put secrets directly into `bot.py` or commit them to GitHub.

## Run locally

Requires Python 3.12+, FFmpeg, and a valid Telegram bot token.

```bash
python -m venv .venv
# Linux/macOS
source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Set the environment variable before starting:

```bash
export TELEGRAM_BOT_TOKEN="your_bot_token_here"
python bot.py
```

On Windows PowerShell:

```powershell
$env:TELEGRAM_BOT_TOKEN="your_bot_token_here"
python bot.py
```

## Deploy with Docker

Build and run with the token supplied as an environment variable:

```bash
docker build -t sigyi-edge-tts-bot .
docker run --rm -e TELEGRAM_BOT_TOKEN="your_bot_token_here" sigyi-edge-tts-bot
```

For Railway, connect this GitHub repository and set `TELEGRAM_BOT_TOKEN` in **Variables**. The included Dockerfile installs FFmpeg. Use a persistent volume if you need `users.json` to survive redeployments.

## Important notes

- Edge TTS needs an internet connection. Microsoft Edge TTS voice availability and service behavior can change.
- Gemini API access is still needed for the Gemini-powered features. Edge TTS replaces the text-to-speech component, not Gemini text processing.
- The bot's channel membership checks depend on the configured Telegram channels and the bot's permissions.
- Crop, flip, and visual filters do not guarantee that a platform will avoid copyright claims. Only upload or process videos you have permission to use.
- Test the deployment with a short video before using longer files.
