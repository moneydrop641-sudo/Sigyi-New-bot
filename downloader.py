from __future__ import annotations

import html
import json
import os
import re
import subprocess
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yt_dlp


UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128 Safari/537.36"


@dataclass
class DownloadResult:
    success: bool
    platform: str
    path: str | None = None
    metadata: dict[str, Any] | None = None
    code: str | None = None
    message: str = ""
    retryable: bool = True


def normalize_url(url: str) -> str:
    return url.strip().rstrip("，。、！!?,.;:)>\"'")


def classify_url(url: str) -> str:
    host = urllib.parse.urlparse(normalize_url(url)).netloc.lower().split(":")[0]
    if host.endswith("tiktok.com"):
        return "tiktok"
    if host in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"} or host.endswith(".youtube.com"):
        return "youtube"
    if host.endswith("xiaohongshu.com") or host == "xhslink.com" or host.endswith(".xhslink.com"):
        return "rednote"
    return "unknown"


def _ffprobe(path: str) -> dict[str, Any]:
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration,size:stream=index,codec_type,codec_name,width,height,r_frame_rate,sample_rate,channels", "-of", "json", path]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)
    if p.returncode != 0: raise RuntimeError(p.stderr[-500:] or "ffprobe failed")
    data = json.loads(p.stdout); streams = data.get("streams", [])
    video = next((x for x in streams if x.get("codec_type") == "video"), None)
    audio = next((x for x in streams if x.get("codec_type") == "audio"), None)
    duration = float((data.get("format") or {}).get("duration") or 0)
    return {"duration": duration, "size": int((data.get("format") or {}).get("size") or 0), "has_video": bool(video), "has_audio": bool(audio), "video": video or {}, "audio": audio or {}, "streams": streams}


def validate_media(path: str, max_size_mb: int) -> dict[str, Any]:
    p = Path(path)
    if not p.exists() or p.stat().st_size < 100 * 1024: raise ValueError("EMPTY_FILE")
    if p.stat().st_size > max_size_mb * 1024 * 1024: raise ValueError("FILE_TOO_LARGE")
    info = _ffprobe(str(p))
    if not info["has_video"]: raise ValueError("VIDEO_MISSING")
    if info["duration"] <= 0.1: raise ValueError("INVALID_DURATION")
    return info


def _ydl(url: str, outbase: str, platform: str, profile: dict[str, Any]) -> list[str]:
    extra = profile or {}
    opts = {
        "format": "bv*[ext=mp4][height<=720]+ba[ext=m4a]/b[ext=mp4][height<=720]/b[ext=mp4]/b",
        "merge_output_format": "mp4", "outtmpl": outbase + ".%(ext)s",
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "nocheckcertificate": True, "retries": 3, "fragment_retries": 3,
        "extractor_retries": 3, "socket_timeout": 25, "force_ipv4": True,
        "concurrent_fragment_downloads": 1,
        "http_headers": {"User-Agent": UA, "Referer": "https://www.tiktok.com/"},
        **extra,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    return [str(x) for x in Path(outbase).parent.glob(Path(outbase).name + ".*")]


def _candidate_profiles(platform: str) -> list[dict[str, Any]]:
    if platform == "tiktok":
        return [{"extractor_args": {"tiktok": {"app_name": "musically"}}}, {"extractor_args": {"tiktok": {"app_name": "trill"}}}, {}]
    if platform == "youtube":
        return [{"extractor_args": {"youtube": {"player_client": ["android_vr"]}}}, {"extractor_args": {"youtube": {"player_client": ["ios"]}}}, {}]
    return [{}]


def download_with_ytdlp(url: str, platform: str, attempt_dir: Path, max_size_mb: int) -> tuple[str, dict[str, Any]]:
    for i, profile in enumerate(_candidate_profiles(platform), 1):
        base = attempt_dir / f"media_{i}"
        try:
            files = _ydl(url, str(base), platform, profile)
            valid = []
            for f in files:
                try:
                    meta = validate_media(f, max_size_mb); valid.append((f, meta))
                except Exception:
                    pass
            if valid:
                return max(valid, key=lambda x: Path(x[0]).stat().st_size)
        except Exception:
            continue
    raise RuntimeError("EXTRACTOR_FAILED")


def download_rednote(url: str, attempt_dir: Path, max_size_mb: int) -> tuple[str, dict[str, Any]]:
    headers = {"User-Agent": UA, "Cookie": "webBuild=4.36.0; xsecappid=xhs-pc;"}
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
        raw = r.read().decode("utf-8", "ignore")
    candidates: list[str] = []
    for pattern in [r'<meta[^>]+(?:property|name)=["\']og:video["\'][^>]+content=["\']([^"\']+)', r'"masterUrl"\s*:\s*"([^"]+)', r'"originVideoKey"\s*:\s*"([^"]+)', r'(https?://sns-video-[a-z0-9-]+\.xhscdn\.com/[^"\'\\\s]+)']:
        candidates += re.findall(pattern, raw, flags=re.I)
    for c in candidates:
        c = html.unescape(c).replace("\\u002F", "/").replace("\\/", "/")
        if c.startswith("//"): c = "https:" + c
        if not c.startswith("http"): continue
        out = attempt_dir / f"rednote_{len(candidates)}.mp4"
        try:
            req = urllib.request.Request(c, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as r, out.open("wb") as f:
                while chunk := r.read(1024 * 1024): f.write(chunk)
            meta = validate_media(str(out), max_size_mb)
            return str(out), meta
        except Exception:
            try: out.unlink(missing_ok=True)
            except Exception: pass
    raise RuntimeError("CDN_DOWNLOAD_FAILED")


def download(url: str, job_dir: Path, max_size_mb: int) -> DownloadResult:
    url = normalize_url(url); platform = classify_url(url)
    if platform == "unknown": return DownloadResult(False, platform, code="PLATFORM_UNSUPPORTED", message="Supported links: TikTok, YouTube, Rednote", retryable=False)
    attempt_dir = job_dir / "download" / "attempt-001"
    attempt_dir.mkdir(parents=True, exist_ok=True)
    try:
        if platform == "rednote": path, meta = download_rednote(url, attempt_dir, max_size_mb)
        else: path, meta = download_with_ytdlp(url, platform, attempt_dir, max_size_mb)
        final = job_dir / "source" / "original.mp4"; final.parent.mkdir(parents=True, exist_ok=True)
        os.replace(path, final)
        return DownloadResult(True, platform, str(final), meta, message="Download and validation succeeded")
    except Exception as exc:
        text = str(exc)
        code = text if text.isupper() and " " not in text else "DOWNLOAD_FAILED"
        retryable = code not in {"PLATFORM_UNSUPPORTED", "SOURCE_PRIVATE", "SOURCE_UNAVAILABLE"}
        return DownloadResult(False, platform, code=code, message=text[:1000], retryable=retryable)
