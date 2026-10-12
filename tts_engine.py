from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path

from gtts import gTTS


class TTSError(RuntimeError):
    pass


def valid_audio(path: str) -> bool:
    if not os.path.exists(path) or os.path.getsize(path) < 600: return False
    p = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_type", "-of", "csv=p=0", path], capture_output=True, text=True, timeout=20)
    return "audio" in p.stdout.lower()


def duration(path: str) -> float:
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", path], capture_output=True, text=True, timeout=20)
    value = float(p.stdout.strip() or 0)
    if value <= 0: raise TTSError(f"Invalid audio duration: {path}")
    return value


def _google_tts(text: str, out: str) -> bool:
    try:
        gTTS(text=text, lang="my", lang_check=False).save(out)
        if valid_audio(out): return True
    except Exception: pass
    try:
        url = "https://translate.googleapis.com/translate_tts?ie=UTF-8&client=gtx&tl=my&q=" + urllib.parse.quote(text)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r: Path(out).write_bytes(r.read())
        return valid_audio(out)
    except Exception: return False


def _clean_audio(raw: str, wav: str) -> None:
    af = "silenceremove=start_periods=1:start_duration=0.03:start_threshold=-48dB:stop_periods=1:stop_duration=0.03:stop_threshold=-48dB,highpass=f=70,loudnorm=I=-16:TP=-1.5:LRA=11,aresample=24000,asetpts=PTS-STARTPTS"
    p = subprocess.run(["ffmpeg", "-y", "-i", raw, "-af", af, "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", wav], capture_output=True, text=True, timeout=90)
    if p.returncode != 0 or not valid_audio(wav): raise TTSError(p.stderr[-500:])


def build_tts_segments(segments: list[dict], voice: str, work_dir: Path) -> tuple[list[str], list[float]]:
    work_dir.mkdir(parents=True, exist_ok=True); wavs=[]; durations=[]
    for i, segment in enumerate(segments):
        text = str(segment["narration"]).strip() or "အဲ့ဒီနောက်မှာတော့"
        raw = work_dir / f"raw_{i}.mp3"; wav = work_dir / f"clean_{i}.wav"
        ok = False
        for attempt in range(3):
            raw.unlink(missing_ok=True); wav.unlink(missing_ok=True)
            if _google_tts(text, str(raw)):
                ok = True; break
            if attempt < 2: time_sleep = 1.5 * (attempt + 1); import time; time.sleep(time_sleep)
        if not ok: raise TTSError(f"Google TTS failed at segment {i + 1}")
        _clean_audio(str(raw), str(wav)); wavs.append(str(wav)); durations.append(duration(str(wav)))
    return wavs, durations


def _atempo(ratio: float) -> str:
    ratio = max(0.5, min(2.0, float(ratio))); return f"atempo={ratio:.8f}"


def fit_segments(wavs: list[str], source_segments: list[dict], out_audio: str, work_dir: Path) -> list[dict]:
    if len(wavs) != len(source_segments): raise TTSError("TTS/source segment count mismatch")
    fitted=[]; timeline=[]; work_dir.mkdir(parents=True, exist_ok=True)
    for i, (wav, seg) in enumerate(zip(wavs, source_segments)):
        target = max(0.25, float(seg["end"]) - float(seg["start"]))
        actual = duration(wav)
        # atempo ratio is input/output duration. One filter per segment preserves
        # natural boundaries and avoids accumulated drift.
        out = work_dir / f"fit_{i}.wav"
        ratio = actual / target
        af = _atempo(ratio)
        p = subprocess.run(["ffmpeg", "-y", "-i", wav, "-af", f"{af},apad,atrim=duration={target:.6f},asetpts=PTS-STARTPTS", "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", str(out)], capture_output=True, text=True, timeout=90)
        if p.returncode != 0 or not valid_audio(str(out)): raise TTSError(f"Audio fit failed at segment {i + 1}: {p.stderr[-400:]}")
        fitted.append(str(out)); timeline.append({"index": i, "start": float(seg["start"]), "end": float(seg["end"]), "source_duration": actual, "target_duration": target, "speed_ratio": ratio})
    list_file = work_dir / "concat.txt"; list_file.write_text("".join(f"file '{Path(x).resolve()}'\n" for x in fitted), encoding="utf-8")
    joined = work_dir / "joined.wav"
    p = subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file), "-c:a", "pcm_s16le", "-ar", "24000", "-ac", "1", str(joined)], capture_output=True, text=True, timeout=150)
    if p.returncode != 0 or not valid_audio(str(joined)): raise TTSError("Audio timeline concat failed")
    p = subprocess.run(["ffmpeg", "-y", "-i", str(joined), "-map_metadata", "-1", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", out_audio], capture_output=True, text=True, timeout=150)
    if p.returncode != 0 or not valid_audio(out_audio): raise TTSError("Audio AAC encode failed")
    return timeline
