from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any


class RenderError(RuntimeError):
    pass


def media_duration(path: str) -> float:
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", path], capture_output=True, text=True, timeout=30)
    value = float(p.stdout.strip() or 0)
    if value <= 0: raise RenderError("Invalid media duration")
    return value


def validate_timeline(source_segments: list[dict], audio_timeline: list[dict], source_duration: float) -> None:
    if not source_segments or len(source_segments) != len(audio_timeline): raise RenderError("Timeline segment count mismatch")
    previous = 0.0
    for seg, audio in zip(source_segments, audio_timeline):
        start, end = float(seg["start"]), float(seg["end"])
        if start < previous - 0.02 or end <= start or end > source_duration + 0.05: raise RenderError("Invalid or overlapping timeline")
        if abs((end - start) - float(audio["target_duration"])) > 0.08: raise RenderError("Audio target does not match video segment")
        previous = end
    if abs(previous - source_duration) > 0.5: raise RenderError("Timeline does not cover the full video")


def render_video(source: str, audio: str, output: str, config: dict[str, Any], timeline: list[dict], work_dir: Path) -> dict[str, Any]:
    source_duration = media_duration(source); audio_duration = media_duration(audio)
    source_segments = [{"start": x["start"], "end": x["end"]} for x in timeline]
    validate_timeline(source_segments, timeline, source_duration)
    if abs(source_duration - audio_duration) > 0.6: raise RenderError(f"Audio/video duration mismatch: {source_duration:.2f}/{audio_duration:.2f}")
    work_dir.mkdir(parents=True, exist_ok=True)
    is_frame = config.get("layout", "frame") == "frame"; flip = ",hflip" if config.get("flip") else ""
    adj = "crop=iw*0.92:ih*0.92:iw*0.04:ih*0.04,eq=contrast=1.06:brightness=0.01:saturation=1.08,unsharp=3:3:0.5:3:3:0"
    inputs = ["-i", source, "-i", audio]
    fc = f"[0:v]setpts=PTS-STARTPTS{flip}[v0];"
    if is_frame:
        fc += "[v0]split=2[b][f];[b]scale=180:320:force_original_aspect_ratio=increase,crop=180:320,gblur=sigma=14,scale=720:1280[bg];[f]" + adj + ",scale=720:820:force_original_aspect_ratio=increase:flags=lanczos,crop=720:820[fg];[bg][fg]overlay=0:230[v1]"
    else:
        fc += f"[v0]{adj},scale=720:1280:force_original_aspect_ratio=increase:flags=lanczos,crop=720:1280[v1]"
    cur = "[v1]"
    if config.get("blur", "off") != "off":
        by, bh = (940, 92) if not is_frame else (880, 86)
        fc += f";{cur}split=2[m][b2];[b2]crop=640:{bh}:40:{by},gblur=sigma=26[blur];[m][blur]overlay=40:{by}[v2]"; cur = "[v2]"
    if config.get("logo") and os.path.exists(config.get("logo", "")):
        inputs += ["-i", config["logo"]]; fc += f";[2:v]scale=110:110[lg];{cur}[lg]overlay=12:80[v3]"; cur = "[v3]"
    fc += f";{cur}format=yuv420p[vout]"
    cmd = ["ffmpeg", "-y", "-threads", "2"] + inputs + ["-filter_complex", fc, "-map", "[vout]", "-map", "1:a:0", "-fps_mode", "cfr", "-r", "30", "-c:v", "libx264", "-preset", "superfast", "-crf", "20", "-c:a", "aac", "-b:a", "160k", "-t", f"{source_duration:.3f}", "-avoid_negative_ts", "make_zero", "-movflags", "+faststart", output]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=max(600, int(source_duration * 8)))
    if p.returncode != 0 or not os.path.exists(output): raise RenderError(p.stderr[-800:] or "FFmpeg render failed")
    final_duration = media_duration(output)
    if abs(final_duration - source_duration) > 0.6: raise RenderError(f"Final duration mismatch: {final_duration:.2f}/{source_duration:.2f}")
    return {"duration": final_duration, "timeline": timeline, "output": output}


def write_timeline(path: str, timeline: list[dict]) -> None:
    Path(path).write_text(json.dumps({"segments": timeline}, ensure_ascii=False, indent=2), encoding="utf-8")
