from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


UTC = timezone.utc


def now() -> str:
    return datetime.now(UTC).isoformat()


def job_id() -> str:
    return "job_" + uuid.uuid4().hex[:16]


class Database:
    def __init__(self, path: Path):
        self.path = str(path)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.init_schema()

    def init_schema(self) -> None:
        with self.lock, self.conn:
            self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                source_url TEXT NOT NULL,
                source_url_hash TEXT NOT NULL,
                platform TEXT NOT NULL,
                status TEXT NOT NULL,
                current_stage TEXT NOT NULL,
                config_json TEXT NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 3,
                last_error_code TEXT,
                last_error_message TEXT,
                next_retry_at TEXT,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_jobs_user ON jobs(user_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, next_retry_at);
            CREATE TABLE IF NOT EXISTS job_stages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                stage TEXT NOT NULL,
                status TEXT NOT NULL,
                attempt INTEGER NOT NULL DEFAULT 0,
                started_at TEXT,
                finished_at TEXT,
                error_code TEXT,
                error_message TEXT,
                metadata_json TEXT,
                UNIQUE(job_id, stage)
            );
            CREATE TABLE IF NOT EXISTS artifacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                path TEXT NOT NULL,
                size_bytes INTEGER,
                sha256 TEXT,
                duration_seconds REAL,
                has_video INTEGER NOT NULL DEFAULT 0,
                has_audio INTEGER NOT NULL DEFAULT 0,
                valid INTEGER NOT NULL DEFAULT 0,
                metadata_json TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(job_id, kind)
            );
            CREATE TABLE IF NOT EXISTS job_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                event_type TEXT NOT NULL,
                message TEXT,
                payload_json TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS user_secrets (
                user_id INTEGER PRIMARY KEY,
                gemini_key_encrypted TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """)

    @staticmethod
    def url_hash(url: str) -> str:
        return hashlib.sha256(url.strip().encode()).hexdigest()

    def create_job(self, user_id: int, url: str, platform: str, config: dict[str, Any], max_attempts: int = 3) -> str:
        jid = job_id(); t = now()
        with self.lock, self.conn:
            self.conn.execute("""INSERT INTO jobs
                (id,user_id,source_url,source_url_hash,platform,status,current_stage,config_json,max_attempts,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (jid, user_id, url, self.url_hash(url), platform, "QUEUED", "DOWNLOAD", json.dumps(config), max_attempts, t, t))
            self.conn.execute("INSERT INTO job_stages(job_id,stage,status) VALUES(?,?,?)", (jid, "DOWNLOAD", "PENDING"))
            self.event(jid, "JOB_CREATED", "Job queued", {"platform": platform})
        return jid

    def find_active_duplicate(self, user_id: int, url: str) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute("""SELECT * FROM jobs WHERE user_id=? AND source_url_hash=?
                AND status NOT IN ('COMPLETED','FAILED','CANCELLED') ORDER BY created_at DESC LIMIT 1""", (user_id, self.url_hash(url))).fetchone()

    def get_job(self, jid: str) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()

    def list_user_jobs(self, user_id: int, limit: int = 8) -> list[sqlite3.Row]:
        with self.lock:
            return list(self.conn.execute("SELECT * FROM jobs WHERE user_id=? ORDER BY created_at DESC LIMIT ?", (user_id, limit)))

    def set_job(self, jid: str, **fields: Any) -> None:
        if not fields: return
        fields["updated_at"] = now()
        cols = ",".join(f"{k}=?" for k in fields)
        with self.lock, self.conn:
            self.conn.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), jid))

    def mark_running(self, jid: str, stage: str) -> int:
        with self.lock, self.conn:
            row = self.conn.execute("SELECT attempt_count FROM jobs WHERE id=?", (jid,)).fetchone()
            attempt = int(row[0] or 0) + 1
            t = now()
            self.conn.execute("UPDATE jobs SET status='RUNNING',current_stage=?,attempt_count=?,updated_at=? WHERE id=?", (stage, attempt, t, jid))
            self.conn.execute("""INSERT INTO job_stages(job_id,stage,status,attempt,started_at)
                VALUES(?,?,?,?,?) ON CONFLICT(job_id,stage) DO UPDATE SET status='RUNNING',attempt=excluded.attempt,started_at=excluded.started_at,finished_at=NULL,error_code=NULL,error_message=NULL""", (jid, stage, "RUNNING", attempt, t))
            self.event(jid, "STAGE_STARTED", stage, {"attempt": attempt})
            return attempt

    def stage_success(self, jid: str, stage: str, metadata: dict[str, Any] | None = None) -> None:
        with self.lock, self.conn:
            t = now(); self.conn.execute("UPDATE job_stages SET status='SUCCEEDED',finished_at=?,metadata_json=? WHERE job_id=? AND stage=?", (t, json.dumps(metadata or {}), jid, stage))
            self.event(jid, "STAGE_SUCCEEDED", stage, metadata or {})

    def stage_failure(self, jid: str, stage: str, code: str, message: str, retryable: bool) -> None:
        with self.lock, self.conn:
            t = now(); self.conn.execute("UPDATE job_stages SET status='FAILED',finished_at=?,error_code=?,error_message=? WHERE job_id=? AND stage=?", (t, code, message[:2000], jid, stage))
            self.conn.execute("UPDATE jobs SET status=?,last_error_code=?,last_error_message=?,updated_at=? WHERE id=?", ("RETRY_WAIT" if retryable else "FAILED", code, message[:2000], t, jid))
            self.event(jid, "STAGE_FAILED", message[:500], {"stage": stage, "code": code, "retryable": retryable})

    def event(self, jid: str, event_type: str, message: str = "", payload: dict[str, Any] | None = None) -> None:
        self.conn.execute("INSERT INTO job_events(job_id,event_type,message,payload_json,created_at) VALUES(?,?,?,?,?)", (jid, event_type, message, json.dumps(payload or {}), now()))

    def add_artifact(self, jid: str, kind: str, path: str, metadata: dict[str, Any]) -> None:
        p = Path(path); data = p.read_bytes(); digest = hashlib.sha256(data).hexdigest()
        with self.lock, self.conn:
            self.conn.execute("""INSERT INTO artifacts(job_id,kind,path,size_bytes,sha256,duration_seconds,has_video,has_audio,valid,metadata_json,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(job_id,kind) DO UPDATE SET path=excluded.path,size_bytes=excluded.size_bytes,sha256=excluded.sha256,duration_seconds=excluded.duration_seconds,has_video=excluded.has_video,has_audio=excluded.has_audio,valid=excluded.valid,metadata_json=excluded.metadata_json,created_at=excluded.created_at""", (jid, kind, str(p), len(data), digest, metadata.get("duration"), int(metadata.get("has_video", False)), int(metadata.get("has_audio", False)), 1, json.dumps(metadata)))
            self.event(jid, "ARTIFACT_SAVED", kind, metadata)

    def get_artifact(self, jid: str, kind: str) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute("SELECT * FROM artifacts WHERE job_id=? AND kind=? AND valid=1", (jid, kind)).fetchone()

    def set_user_gemini_key(self, user_id: int, encrypted_key: str) -> None:
        with self.lock, self.conn:
            self.conn.execute("""INSERT INTO user_secrets(user_id,gemini_key_encrypted,updated_at)
                VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET gemini_key_encrypted=excluded.gemini_key_encrypted,updated_at=excluded.updated_at""", (user_id, encrypted_key, now()))

    def get_user_gemini_key(self, user_id: int) -> str | None:
        with self.lock:
            row = self.conn.execute("SELECT gemini_key_encrypted FROM user_secrets WHERE user_id=?", (user_id,)).fetchone()
            return str(row[0]) if row else None

    def delete_user_gemini_key(self, user_id: int) -> None:
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM user_secrets WHERE user_id=?", (user_id,))

    def request_cancel(self, jid: str) -> None:
        self.set_job(jid, cancel_requested=1, status="CANCEL_REQUESTED")
        with self.lock, self.conn: self.event(jid, "CANCEL_REQUESTED", "User requested cancellation")

    def is_cancelled(self, jid: str) -> bool:
        row = self.get_job(jid); return bool(row and row["cancel_requested"])

    def recover_on_startup(self) -> list[str]:
        with self.lock, self.conn:
            self.conn.execute("UPDATE jobs SET status='RETRY_WAIT',last_error_code='WORKER_RESTART',last_error_message='Worker restarted; job requeued',updated_at=? WHERE status IN ('RUNNING','CANCEL_REQUESTED')", (now(),))
            rows = self.conn.execute("SELECT id FROM jobs WHERE status IN ('QUEUED','RETRY_WAIT') ORDER BY created_at").fetchall()
            return [r[0] for r in rows]

    def close(self) -> None:
        with self.lock: self.conn.close()
