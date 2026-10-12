from __future__ import annotations

from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class SecretStore:
    """Encrypts per-user secrets with a key kept inside the persistent data volume."""

    def __init__(self, data_dir: Path):
        data_dir.mkdir(parents=True, exist_ok=True)
        self.path = data_dir / ".bot_secret"
        if self.path.exists():
            raw = self.path.read_bytes().strip()
        else:
            raw = Fernet.generate_key()
            self.path.write_bytes(raw + b"\n")
            try:
                self.path.chmod(0o600)
            except OSError:
                pass
        self.fernet = Fernet(raw)

    def encrypt(self, value: str) -> str:
        return self.fernet.encrypt(value.strip().encode()).decode()

    def decrypt(self, value: str) -> str:
        try:
            return self.fernet.decrypt(value.encode()).decode()
        except InvalidToken as exc:
            raise ValueError("Stored secret cannot be decrypted") from exc
