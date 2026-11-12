"""Configuratie uit omgevingsvariabelen (alleen niet-geheime instellingen)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on", "ja"}


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("DATA_DIR", "/data")))
    master_key_file: Path = field(
        default_factory=lambda: Path(os.environ.get("MASTER_KEY_FILE", "/run/secrets/app_master_key"))
    )
    whisper_model: str = field(default_factory=lambda: os.environ.get("WHISPER_MODEL", "small"))
    whisper_device: str = field(default_factory=lambda: os.environ.get("WHISPER_DEVICE", "cpu"))
    whisper_compute: str = field(default_factory=lambda: os.environ.get("WHISPER_COMPUTE", "int8"))
    cookie_secure: bool = field(default_factory=lambda: _bool("COOKIE_SECURE", False))
    run_worker: bool = field(default_factory=lambda: _bool("RUN_WORKER", True))
    session_days: int = 14
    max_upload_mb: int = field(default_factory=lambda: int(os.environ.get("MAX_UPLOAD_MB", "8192")))

    @property
    def input_dir(self) -> Path:
        return self.data_dir / "input"

    @property
    def output_dir(self) -> Path:
        return self.data_dir / "output"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "db.sqlite"

    @property
    def generated_master_key(self) -> Path:
        return self.data_dir / ".master_key"

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.input_dir, self.output_dir):
            p.mkdir(parents=True, exist_ok=True)


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def set_settings(s: Settings) -> None:
    global _settings
    _settings = s
