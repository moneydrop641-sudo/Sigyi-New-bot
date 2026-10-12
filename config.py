from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    data_dir: Path
    db_path: Path
    max_workers: int
    download_attempts: int
    download_timeout: int
    max_file_size_mb: int
    gemini_keys: tuple[str, ...]

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = Path(os.getenv("SIGYI_DATA_DIR", "./data")).resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
        keys = tuple(x.strip() for x in os.getenv("GEMINI_API_KEYS", os.getenv("GEMINI_API_KEY", "")).split(",") if x.strip())
        return cls(
            telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            data_dir=data_dir, db_path=data_dir / "jobs.sqlite3",
            max_workers=max(1, int(os.getenv("SIGYI_MAX_WORKERS", "1"))),
            download_attempts=max(1, int(os.getenv("SIGYI_DOWNLOAD_ATTEMPTS", "3"))),
            download_timeout=max(30, int(os.getenv("SIGYI_DOWNLOAD_TIMEOUT", "180"))),
            max_file_size_mb=max(50, int(os.getenv("SIGYI_MAX_FILE_SIZE_MB", "512"))),
            gemini_keys=keys,
        )
