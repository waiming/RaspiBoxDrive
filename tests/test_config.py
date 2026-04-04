"""Tests for raspiboxdrive.config."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import raspiboxdrive.config as cfg_module
from raspiboxdrive.config import Config, get_config, reset_config


@pytest.fixture(autouse=True)
def reset_singleton():
    """Ensure the global singleton is reset between tests."""
    reset_config()
    yield
    reset_config()


class TestConfig:
    def test_defaults(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("RASPIBOX_SYNC_DIR", raising=False)
        monkeypatch.delenv("RASPIBOX_PROVIDER", raising=False)
        monkeypatch.delenv("RASPIBOX_DATA_DIR", raising=False)
        c = Config()
        assert c.provider == "gdrive"
        assert c.max_retries == 5
        assert c.chunk_size == 5 * 1024 * 1024
        assert c.log_level == "INFO"

    def test_env_override_provider(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("RASPIBOX_PROVIDER", "dropbox")
        c = Config()
        assert c.provider == "dropbox"

    def test_env_override_poll_interval(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("RASPIBOX_POLL_INTERVAL", "60")
        c = Config()
        assert c.poll_interval == 60

    def test_env_override_sync_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("RASPIBOX_SYNC_DIR", str(tmp_path / "mybox"))
        c = Config()
        assert c.sync_dir == tmp_path / "mybox"

    def test_ensure_dirs_creates_directories(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("RASPIBOX_SYNC_DIR", str(tmp_path / "sync"))
        monkeypatch.setenv("RASPIBOX_DATA_DIR", str(tmp_path / "data"))
        c = Config()
        c.ensure_dirs()
        assert (tmp_path / "sync").is_dir()
        assert (tmp_path / "data").is_dir()

    def test_get_config_singleton(self):
        c1 = get_config()
        c2 = get_config()
        assert c1 is c2

    def test_reset_config(self):
        c1 = get_config()
        reset_config()
        c2 = get_config()
        assert c1 is not c2

    def test_notifications_disabled_by_default(self):
        c = Config()
        assert c.enable_notifications is False

    def test_notifications_enabled_via_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("RASPIBOX_NOTIFICATIONS", "true")
        c = Config()
        assert c.enable_notifications is True
