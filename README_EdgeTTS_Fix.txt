SiGyi AI Studio Movie Recap Bot — V2 Rebuild

ဒီ package က မူလ bot ရဲ့ UI၊ Gemini scene analysis၊ TikTok/YouTube/Rednote download၊ voice selection နဲ့ logo/layout options တွေကို ထိန်းထားပြီး movie recap render pipeline ကို အစအဆုံး ပြန်တည်ဆောက်ထားတဲ့ version ဖြစ်ပါတယ်။

V2 မှာ ပြင်ထားတဲ့အချက်များ

1. TTS segment တစ်ခုချင်းစီကို temporary isolated workspace ထဲမှာ generate လုပ်ပြီး Edge TTS ကို retry ၄ ကြိမ်အထိ စမ်းပါတယ်။ Edge TTS 403 သို့မဟုတ် connection failure ဖြစ်ရင် Google TTS fallback သုံးပြီး job ကို မပျက်စေပါဘူး။
2. Segment တစ်ခုချင်းစီကို silence trim၊ high-pass၊ loudness normalization၊ 24 kHz mono PCM အဖြစ် ပြောင်းပြီး AAC မလုပ်ခင် PCM timeline တစ်ခုတည်းအဖြစ် concat လုပ်ပါတယ်။ ဒါကြောင့် segment တစ်ခုချင်းစီမှာ encoder delay ထပ်မဝင်တော့ပါဘူး။
3. Narration boundary တွေကို source video ရဲ့ scene window တွေနဲ့ piecewise master-clock mapping လုပ်ထားပါတယ်။ အသံမြန်နှုန်းကို နောက်ဆုံးမှာ တစ်ကြိမ်တည်း fit လုပ်ပြီး audio PTS ကို zero ကနေစပါတယ်။
4. Video ကို constant 30 FPS clock အဖြစ် ပြန်တည်ဆောက်ပြီး လိုအပ်ရင် နောက်ဆုံး frame ကို clone padding လုပ်ပါတယ်။ ဒါကြောင့် video stream က target duration ကို အပြည့်ရပါတယ်။
5. Final output duration၊ video validity၊ audio validity ကို Telegram မပို့ခင် validate လုပ်ပါတယ်။ Failed render ဖြစ်ရင် partial files တွေကို cleanup လုပ်ပြီး မြန်မာလို error ပြပါတယ်။
6. TTS temporary directory၊ intermediate media၊ final output တွေကို job ပြီးတိုင်း သန့်ရှင်းပေးပါတယ်။

Railway deployment

Project root ထဲမှာ ဒီ package ထဲက `bot.py`, `requirements.txt`, `Dockerfile`, `README_EdgeTTS_Fix.txt` ကို upload/replace လုပ်ပါ။ Railway Variables ထဲမှာ `TELEGRAM_BOT_TOKEN` ထည့်ပြီး deploy လုပ်ပါ။ Deploy ပြီးရင် bot ထဲက API Key menu ကနေ Gemini API key ထည့်ပါ။ Dockerfile က FFmpeg နဲ့ Noto fonts ကို install လုပ်ပေးပါတယ်။

စမ်းသပ်မှု

Python syntax compile အောင်မြင်ပြီး synthetic 12-second media နဲ့ end-to-end FFmpeg render စမ်းသပ်ထားပါတယ်။ Output video stream က 12.000 seconds၊ 30 FPS၊ 360 frames ထွက်ပါတယ်။ သို့သော် Edge TTS၊ Google TTS၊ Gemini၊ Telegram download API တွေက external service ဖြစ်တာကြောင့် live Railway test မလုပ်ဘဲ error အားလုံးကို 100% အာမခံနိုင်မှာ မဟုတ်ပါ။

Gemini quota error handling

A message such as `429 RESOURCE_EXHAUSTED` with `limit: 0` is a Google AI Studio quota/account limitation, not an FFmpeg or Telegram bug. V2 now discovers and uses Flash models only, avoids auto-selecting Pro models, tries the next configured model/key when a quota or rate-limit error occurs, and returns a clear Burmese message when every key has zero quota. If every key reports `limit: 0`, a new/eligible Gemini API key or an account with available quota is required; code cannot bypass Google's quota.

Downloader rebuild

TikTok and YouTube now use a shared yt-dlp engine first, with redirect following, format validation, bounded retries, IPv4 fallback, and 720p/MP4 preference. Third-party JSON downloader APIs are fallback-only. The Telegram download timeout is 180 seconds so a normal short-link redirect or a slow CDN does not fail prematurely. The provided TikTok short-link test resolved successfully to a valid 164.37-second, 18 MB media file in the sandbox. A YouTube link can still fail when the source video itself is private, deleted, region-blocked, or marked unavailable; no downloader can recover a video that YouTube does not serve.

Long-video timeout rebuild

The render job now uses duration-based timeouts: Gemini analysis has at least 300 seconds, TTS has at least 420 seconds, and FFmpeg has at least 900 seconds with additional time for longer source videos. FFmpeg itself is also given a duration-based timeout. Timeout failures now identify the exact stage instead of showing only `TimeoutError`.
