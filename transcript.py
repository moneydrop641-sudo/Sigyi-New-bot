from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from google import genai
from google.genai import types


SCHEMA = {
    "type": "object",
    "properties": {
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "number"},
                    "end": {"type": "number"},
                    "narration": {"type": "string"},
                },
                "required": ["start", "end", "narration"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["segments"],
    "additionalProperties": False,
}


class TranscriptError(RuntimeError):
    pass


def _duration(path: str) -> float:
    import subprocess
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", path], capture_output=True, text=True, timeout=30)
    return float(p.stdout.strip() or 0)


def _poll_file(client: genai.Client, uploaded: Any) -> Any:
    for _ in range(180):
        state = str(getattr(getattr(uploaded, "state", None), "name", ""))
        if state != "PROCESSING": return uploaded
        time.sleep(1)
        uploaded = client.files.get(name=uploaded.name)
    raise TranscriptError("Gemini video file processing timeout")


def _validate(data: dict[str, Any], duration: float, count: int) -> list[dict[str, Any]]:
    raw = data.get("segments")
    if not isinstance(raw, list) or not raw: raise TranscriptError("Gemini returned no transcript segments")
    out = []
    for item in raw:
        try:
            start = max(0.0, float(item["start"])); end = min(duration, float(item["end"]))
            text = " ".join(str(item["narration"]).split())
        except Exception as exc:
            raise TranscriptError(f"Invalid transcript segment: {exc}") from exc
        if end <= start or len(text) < 4: continue
        out.append({"start": start, "end": end, "narration": text})
    if len(out) != count:
        raise TranscriptError(f"Expected {count} segments, received {len(out)}")
    out.sort(key=lambda x: x["start"])
    for i, item in enumerate(out):
        expected_start = duration * i / count
        expected_end = duration * (i + 1) / count
        # The model is asked to use equal windows; clamp boundaries to keep the
        # video/audio mapper deterministic even if it returns small deviations.
        item["start"], item["end"] = expected_start, expected_end
    return out


def generate_transcript(video_path: str, api_keys: list[str], count: int) -> list[dict[str, Any]]:
    if not api_keys: raise TranscriptError("GEMINI_API_KEY မရှိပါ")
    duration = _duration(video_path)
    if duration <= 0: raise TranscriptError("Invalid source duration")
    window = duration / count
    prompt = f"""Analyze the uploaded movie video using both its frames and original audio. Return exactly {count} equal-time segments covering 0 to {duration:.3f} seconds. Each segment must cover one equal window: window i starts at duration*i/{count} and ends at duration*(i+1)/{count}. For each segment write a professional Burmese movie-recap narration describing only what is visible/audible in that segment. Each narration should be concise enough to be spoken naturally inside its segment. Do not use English letters, digits, brackets, markdown, or commentary. Use this exact JSON schema and nothing else."""
    last: Exception | None = None
    for key in api_keys:
        client = None; uploaded = None
        try:
            client = genai.Client(api_key=key.strip())
            uploaded = _poll_file(client, client.files.upload(file=video_path))
            response = client.models.generate_content(model="gemini-2.5-flash", contents=[uploaded, prompt], config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=SCHEMA, temperature=0.2))
            data = json.loads(response.text)
            result = _validate(data, duration, count)
            try: client.files.delete(name=uploaded.name)
            except Exception: pass
            return result
        except Exception as exc:
            last = exc
            try:
                if client and uploaded: client.files.delete(name=uploaded.name)
            except Exception: pass
    raise TranscriptError(str(last or "Gemini transcript failed"))
