from __future__ import annotations

import json
from pathlib import Path

from db import Database
from render import render_video, write_timeline
from transcript import generate_transcript
from tts_engine import build_tts_segments, fit_segments


class PipelineError(RuntimeError):
    pass


def segment_count(duration: float) -> int:
    return 3 if duration <= 28 else 4 if duration <= 60 else 5 if duration <= 110 else 6


def run_phase34(db: Database, job_id: str, source_path: str, job_dir: Path, gemini_keys: list[str], config: dict) -> dict:
    if not gemini_keys: raise PipelineError("GEMINI_API_KEY မရှိပါ။ Phase 3 transcript မစနိုင်ပါ")
    import subprocess
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", source_path], capture_output=True, text=True, timeout=30)
    source_duration = float(p.stdout.strip() or 0)
    count = segment_count(source_duration)
    transcript_json = job_dir / "transcript" / "segments.json"; transcript_json.parent.mkdir(parents=True, exist_ok=True)
    existing = db.get_artifact(job_id, "TRANSCRIPT_JSON")
    if existing and Path(existing["path"]).exists():
        segments = json.loads(Path(existing["path"]).read_text(encoding="utf-8"))["segments"]
    else:
        db.mark_running(job_id, "TRANSCRIPT")
        segments = generate_transcript(source_path, gemini_keys, count)
        transcript_json.write_text(json.dumps({"segments": segments}, ensure_ascii=False, indent=2), encoding="utf-8")
        db.add_artifact(job_id, "TRANSCRIPT_JSON", str(transcript_json), {"segments": len(segments), "duration": source_duration})
        db.stage_success(job_id, "TRANSCRIPT", {"segments": len(segments)})
    tts_dir = job_dir / "tts"
    audio_path = job_dir / "render" / "narration.m4a"; audio_path.parent.mkdir(parents=True, exist_ok=True)
    existing_audio = db.get_artifact(job_id, "NARRATION_AUDIO")
    if existing_audio and Path(existing_audio["path"]).exists():
        timeline = json.loads((job_dir / "render" / "timeline.json").read_text(encoding="utf-8"))["segments"]
    else:
        db.mark_running(job_id, "TTS")
        wavs, _ = build_tts_segments(segments, config.get("voice", "Thiha"), tts_dir)
        timeline = fit_segments(wavs, segments, str(audio_path), job_dir / "render" / "fitted")
        write_timeline(str(job_dir / "render" / "timeline.json"), timeline)
        db.add_artifact(job_id, "NARRATION_AUDIO", str(audio_path), {"duration": source_duration, "has_audio": True})
        db.stage_success(job_id, "TTS", {"segments": len(timeline), "duration": source_duration})
    db.mark_running(job_id, "RENDER")
    final = job_dir / "final" / "movie_recap.mp4"; final.parent.mkdir(parents=True, exist_ok=True)
    result = render_video(source_path, str(audio_path), str(final), config, timeline, job_dir / "render")
    db.add_artifact(job_id, "FINAL_VIDEO", str(final), {"duration": result["duration"], "has_video": True, "has_audio": True})
    db.stage_success(job_id, "RENDER", result)
    db.set_job(job_id, status="READY", current_stage="DELIVERY")
    return result
