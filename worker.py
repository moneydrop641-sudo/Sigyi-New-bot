from __future__ import annotations

import asyncio
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from db import Database
from downloader import download
from key_store import SecretStore
from pipeline import PipelineError, run_phase34

Progress = Callable[[str, str], Awaitable[None]]


class Worker:
    def __init__(self, db: Database, data_dir: Path, max_workers: int, max_file_size_mb: int, download_timeout: int, gemini_keys: tuple[str, ...], secret_store: SecretStore, progress: Progress):
        self.db = db
        self.data_dir = data_dir
        self.max_file_size_mb = max_file_size_mb
        self.download_timeout = download_timeout
        self.gemini_keys = gemini_keys
        self.secret_store = secret_store
        self.max_workers = max(1, max_workers)
        self.progress = progress
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.tasks: list[asyncio.Task] = []
        self.stop_event = asyncio.Event()
        self.active: set[str] = set()

    async def start(self) -> None:
        for jid in self.db.recover_on_startup():
            await self.queue.put(jid)
        self.tasks = [asyncio.create_task(self.loop(i)) for i in range(self.max_workers)]

    async def stop(self) -> None:
        self.stop_event.set()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def enqueue(self, jid: str) -> None:
        await self.queue.put(jid)

    async def loop(self, worker_id: int) -> None:
        while not self.stop_event.is_set():
            jid = await self.queue.get()
            try:
                await self.run_job(jid)
            except Exception as exc:
                self.db.event(jid, "WORKER_ERROR", str(exc)[:500])
            finally:
                self.queue.task_done()

    async def run_job(self, jid: str) -> None:
        if jid in self.active:
            return
        row = self.db.get_job(jid)
        if not row or row["status"] in {"COMPLETED", "CANCELLED"}:
            return
        self.active.add(jid)
        job_dir = self.data_dir / "jobs" / jid
        job_dir.mkdir(parents=True, exist_ok=True)
        try:
            if self.db.is_cancelled(jid):
                await self.cancel(jid)
                return
            existing = self.db.get_artifact(jid, "SOURCE_VIDEO")
            if existing and Path(existing["path"]).exists():
                source_path = existing["path"]
            else:
                self.db.mark_running(jid, "DOWNLOAD")
                await self.progress(jid, "📥 Video download လုပ်နေပါတယ်")
                try:
                    result = await asyncio.wait_for(asyncio.to_thread(download, row["source_url"], job_dir, self.max_file_size_mb), timeout=self.download_timeout)
                except asyncio.TimeoutError:
                    self.db.stage_failure(jid, "DOWNLOAD", "NETWORK_TIMEOUT", f"Download exceeded {self.download_timeout} seconds", True)
                    await self.progress(jid, "⚠️ Download timeout ဖြစ်ပါတယ်။ Retry ခလုတ်နဲ့ ပြန်စမ်းနိုင်ပါတယ်")
                    return
                if self.db.is_cancelled(jid):
                    await self.cancel(jid)
                    return
                if not result.success:
                    self.db.stage_failure(jid, "DOWNLOAD", result.code or "DOWNLOAD_FAILED", result.message, result.retryable)
                    await self.progress(jid, f"❌ Download မအောင်မြင်ပါ: {result.code}")
                    return
                source_path = result.path
                self.db.add_artifact(jid, "SOURCE_VIDEO", source_path, result.metadata or {})
                self.db.stage_success(jid, "DOWNLOAD", result.metadata)
                self.db.set_job(jid, status="DOWNLOADED", current_stage="TRANSCRIPT")
                await self.progress(jid, f"✅ Video ရပါပြီ ({result.metadata.get('duration', 0):.1f}s)")
            user_keys: list[str] = list(self.gemini_keys)
            encrypted_user_key = self.db.get_user_gemini_key(int(row["user_id"]))
            if encrypted_user_key:
                try:
                    user_key = self.secret_store.decrypt(encrypted_user_key)
                    if user_key and user_key not in user_keys:
                        user_keys.insert(0, user_key)
                except ValueError:
                    self.db.event(jid, "SECRET_ERROR", "User Gemini key could not be decrypted")
            if not user_keys:
                self.db.set_job(jid, status="DOWNLOADED", current_stage="TRANSCRIPT")
                await self.progress(jid, "✅ Download အောင်မြင်ပါပြီ။ GEMINI_API_KEY ထည့်ပြီး Phase 3 ကို ဆက်လုပ်နိုင်ပါတယ်")
                return
            await self.progress(jid, "🧠 [35%] Gemini transcript/script ပြုလုပ်နေပါတယ်")
            source_meta = self.db.get_artifact(jid, "SOURCE_VIDEO")
            seconds = float(source_meta["duration_seconds"] or 60) if source_meta else 60
            result = await asyncio.wait_for(asyncio.to_thread(run_phase34, self.db, jid, source_path, job_dir, user_keys, json_config(row["config_json"])), timeout=max(900, int(seconds * 12)))
            await self.progress(jid, f"✅ [100%] Video ပြီးပါပြီ ({result['duration']:.1f}s)")
        except asyncio.TimeoutError:
            self.db.stage_failure(jid, row["current_stage"] or "PIPELINE", "PIPELINE_TIMEOUT", "Phase 3/4 pipeline timeout", True)
            await self.progress(jid, "❌ Processing အချိန်ကျော်သွားပါတယ်။ အောင်မြင်ပြီးသား stage တွေကို ပြန်မလုပ်ဘဲ Retry လုပ်နိုင်ပါတယ်")
        except (PipelineError, Exception) as exc:
            self.db.stage_failure(jid, row["current_stage"] or "PIPELINE", "PIPELINE_FAILED", str(exc), False)
            await self.progress(jid, f"❌ Processing မအောင်မြင်ပါ: {str(exc)[:350]}")
        finally:
            self.active.discard(jid)

    async def cancel(self, jid: str) -> None:
        self.db.set_job(jid, status="CANCELLED", completed_at=datetime.now(timezone.utc).isoformat())
        await self.progress(jid, "🛑 Job ကို ရပ်တန့်လိုက်ပါပြီ")

    async def cleanup_job(self, jid: str, keep_source: bool = True) -> None:
        root = self.data_dir / "jobs" / jid
        if keep_source:
            for child in root.iterdir() if root.exists() else []:
                if child.name != "source":
                    shutil.rmtree(child, ignore_errors=True) if child.is_dir() else child.unlink(missing_ok=True)
        else:
            shutil.rmtree(root, ignore_errors=True)


def json_config(raw: str) -> dict:
    try:
        return json.loads(raw or "{}")
    except Exception:
        return {}
