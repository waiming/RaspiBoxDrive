"""Configuration management for RaspiBoxDrive.

Settings are read (in order of priority):
1. Environment variables
2. A `.env` file in the current working directory
3. Hard-coded defaults
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

load_dotenv()

_HOME = Path.home()


def _data_dir() -> Path:
    return Path(os.getenv("RASPIBOX_DATA_DIR", _HOME / ".raspiboxdrive"))


@dataclass
class Config:
    """Top-level application configuration."""

    # ── Paths ──────────────────────────────────────────────────────────────
    sync_dir: Path = field(
        default_factory=lambda: Path(
            os.getenv("RASPIBOX_SYNC_DIR", _HOME / "RaspiBoxDrive")
        )
    )
    data_dir: Path = field(default_factory=_data_dir)
    db_path: Path = field(
        default_factory=lambda: _data_dir() / "state.db"
    )
    token_dir: Path = field(
        default_factory=lambda: _data_dir() / "tokens"
    )
    log_file: Path = field(
        default_factory=lambda: _data_dir() / "raspiboxdrive.log"
    )

    # ── Sync behaviour ─────────────────────────────────────────────────────
    provider: Literal["gdrive", "dropbox", "both"] = field(
        default_factory=lambda: os.getenv("RASPIBOX_PROVIDER", "gdrive")  # type: ignore[return-value]
    )
    # Polling interval (seconds) used as fallback when inotify is unavailable
    poll_interval: int = field(
        default_factory=lambda: int(os.getenv("RASPIBOX_POLL_INTERVAL", "30"))
    )
    # Maximum size (bytes) for a single non-chunked upload (default 5 MB)
    chunk_threshold: int = field(
        default_factory=lambda: int(
            os.getenv("RASPIBOX_CHUNK_THRESHOLD", str(5 * 1024 * 1024))
        )
    )
    # Chunk size for resumable uploads (default 5 MB)
    chunk_size: int = field(
        default_factory=lambda: int(
            os.getenv("RASPIBOX_CHUNK_SIZE", str(5 * 1024 * 1024))
        )
    )
    # Number of retry attempts for transient API failures
    max_retries: int = field(
        default_factory=lambda: int(os.getenv("RASPIBOX_MAX_RETRIES", "5"))
    )
    # Base back-off delay between retries (seconds)
    retry_backoff: float = field(
        default_factory=lambda: float(os.getenv("RASPIBOX_RETRY_BACKOFF", "2.0"))
    )

    # ── Google Drive ────────────────────────────────────────────────────────
    gdrive_credentials_file: Path = field(
        default_factory=lambda: Path(
            os.getenv(
                "RASPIBOX_GDRIVE_CREDENTIALS",
                _data_dir() / "gdrive_credentials.json",
            )
        )
    )
    gdrive_token_file: Path = field(
        default_factory=lambda: Path(
            os.getenv("RASPIBOX_GDRIVE_TOKEN", _data_dir() / "tokens" / "gdrive_token.json")
        )
    )

    # ── Dropbox ─────────────────────────────────────────────────────────────
    dropbox_app_key: str = field(
        default_factory=lambda: os.getenv("RASPIBOX_DROPBOX_APP_KEY", "")
    )
    dropbox_app_secret: str = field(
        default_factory=lambda: os.getenv("RASPIBOX_DROPBOX_APP_SECRET", "")
    )
    dropbox_token_file: Path = field(
        default_factory=lambda: Path(
            os.getenv(
                "RASPIBOX_DROPBOX_TOKEN",
                _data_dir() / "tokens" / "dropbox_token.json",
            )
        )
    )

    # ── Logging ─────────────────────────────────────────────────────────────
    log_level: str = field(
        default_factory=lambda: os.getenv("RASPIBOX_LOG_LEVEL", "INFO")
    )

    # ── Notifications ────────────────────────────────────────────────────────
    enable_notifications: bool = field(
        default_factory=lambda: os.getenv(
            "RASPIBOX_NOTIFICATIONS", "false"
        ).lower() in {"1", "true", "yes"}
    )

    def ensure_dirs(self) -> None:
        """Create required directories if they do not already exist."""
        for directory in (self.sync_dir, self.data_dir, self.token_dir):
            directory.mkdir(parents=True, exist_ok=True)


# Module-level singleton – lazily instantiated
_config: Config | None = None


def get_config() -> Config:
    """Return the global :class:`Config` singleton."""
    global _config
    if _config is None:
        _config = Config()
    return _config


def reset_config() -> None:
    """Reset the global singleton (useful in tests)."""
    global _config
    _config = None
