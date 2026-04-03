"""Tests for raspiboxdrive.sync_engine."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

from raspiboxdrive.config import Config
from raspiboxdrive.database import FileRecord, StateDatabase
from raspiboxdrive.providers.base import CloudProvider, RemoteFile
from raspiboxdrive.sync_engine import SyncEngine, sha256_file
from raspiboxdrive.watcher import EventKind, FileEvent, FileWatcher


# ── Helpers ───────────────────────────────────────────────────────────────


def _make_config(tmp_path: Path) -> Config:
    cfg = Config()
    cfg.sync_dir = tmp_path / "sync"
    cfg.data_dir = tmp_path / "data"
    cfg.db_path = tmp_path / "data" / "state.db"
    cfg.token_dir = tmp_path / "data" / "tokens"
    cfg.log_file = tmp_path / "data" / "raspiboxdrive.log"
    cfg.chunk_size = 5 * 1024 * 1024
    cfg.max_retries = 1
    cfg.retry_backoff = 0.0
    cfg.sync_dir.mkdir(parents=True)
    cfg.data_dir.mkdir(parents=True)
    return cfg


def _make_remote_file(**kwargs) -> RemoteFile:
    defaults = {
        "remote_id": "remote-id-1",
        "name": "test.txt",
        "path": "test.txt",
        "mtime": time.time(),
        "size": 10,
        "content_hash": "abc123",
    }
    defaults.update(kwargs)
    return RemoteFile(**defaults)


class _MockProvider(CloudProvider):
    provider_name = "gdrive"

    def __init__(self):
        self.uploaded: list[tuple[Path, str]] = []
        self.downloaded: list[tuple[str, Path]] = []
        self.deleted: list[str] = []
        self.moved: list[tuple[str, str]] = []
        self._list_result: list[RemoteFile] = []
        self._upload_result: Optional[RemoteFile] = None

    def authenticate(self): pass
    def refresh_token(self): pass

    def list_files(self, remote_path=""):
        return self._list_result

    def get_file_metadata(self, remote_id):
        return None

    def upload_file(self, local_path, remote_path, remote_id=None, chunk_size=5242880):
        self.uploaded.append((local_path, remote_path))
        result = self._upload_result or _make_remote_file(
            path=remote_path, name=local_path.name
        )
        return result

    def download_file(self, remote_id, local_path, chunk_size=5242880):
        self.downloaded.append((remote_id, local_path))
        # Write dummy content so sha256_file works
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(b"remote-content")

    def delete_file(self, remote_id):
        self.deleted.append(remote_id)

    def move_file(self, remote_id, new_remote_path):
        self.moved.append((remote_id, new_remote_path))
        return _make_remote_file(remote_id=remote_id, path=new_remote_path)


@pytest.fixture
def engine(tmp_path: Path):
    cfg = _make_config(tmp_path)
    db = StateDatabase(cfg.db_path)
    watcher = MagicMock(spec=FileWatcher)
    provider = _MockProvider()
    return SyncEngine(cfg, provider, db, watcher), cfg, db, provider


# ── sha256_file ───────────────────────────────────────────────────────────


class TestSha256File:
    def test_basic(self, tmp_path: Path):
        f = tmp_path / "x.txt"
        f.write_bytes(b"hello world")
        digest = sha256_file(f)
        assert len(digest) == 64  # hex SHA-256

    def test_deterministic(self, tmp_path: Path):
        f = tmp_path / "x.txt"
        f.write_bytes(b"deterministic")
        assert sha256_file(f) == sha256_file(f)

    def test_different_content(self, tmp_path: Path):
        a = tmp_path / "a.txt"
        b = tmp_path / "b.txt"
        a.write_bytes(b"aaa")
        b.write_bytes(b"bbb")
        assert sha256_file(a) != sha256_file(b)


# ── handle_local_event ────────────────────────────────────────────────────


class TestHandleLocalEvent:
    def test_new_file_upload(self, engine, tmp_path: Path):
        eng, cfg, db, prov = engine
        f = cfg.sync_dir / "new.txt"
        f.write_bytes(b"new content")
        event = FileEvent(EventKind.CREATED, f)
        eng.handle_local_event(event)
        assert len(prov.uploaded) == 1
        assert prov.uploaded[0][1] == "new.txt"
        rec = db.get("new.txt", "gdrive")
        assert rec is not None
        assert rec.sync_status == "synced"

    def test_modified_file_upload(self, engine, tmp_path: Path):
        eng, cfg, db, prov = engine
        f = cfg.sync_dir / "mod.txt"
        f.write_bytes(b"original")
        # Pre-populate DB with old hash
        old_hash = sha256_file(f)
        db.upsert(FileRecord(
            path="mod.txt", provider="gdrive",
            remote_id="r1", local_hash=old_hash, remote_hash=old_hash,
            sync_status="synced",
        ))
        # Change file
        f.write_bytes(b"modified content")
        event = FileEvent(EventKind.MODIFIED, f)
        eng.handle_local_event(event)
        assert len(prov.uploaded) == 1

    def test_deleted_file_triggers_remote_delete(self, engine, tmp_path: Path):
        eng, cfg, db, prov = engine
        db.upsert(FileRecord(
            path="del.txt", provider="gdrive",
            remote_id="remote-del", sync_status="synced",
        ))
        event = FileEvent(EventKind.DELETED, cfg.sync_dir / "del.txt")
        eng.handle_local_event(event)
        assert "remote-del" in prov.deleted
        assert db.get("del.txt", "gdrive") is None

    def test_moved_file_triggers_remote_move(self, engine, tmp_path: Path):
        eng, cfg, db, prov = engine
        db.upsert(FileRecord(
            path="src.txt", provider="gdrive",
            remote_id="remote-src", sync_status="synced",
        ))
        src = cfg.sync_dir / "src.txt"
        dst = cfg.sync_dir / "dst.txt"
        event = FileEvent(EventKind.MOVED, src, dest_path=dst)
        eng.handle_local_event(event)
        assert len(prov.moved) == 1
        assert prov.moved[0] == ("remote-src", "dst.txt")
        assert db.get("src.txt", "gdrive") is None
        assert db.get("dst.txt", "gdrive") is not None

    def test_unmodified_file_no_upload(self, engine, tmp_path: Path):
        eng, cfg, db, prov = engine
        f = cfg.sync_dir / "same.txt"
        f.write_bytes(b"content")
        h = sha256_file(f)
        db.upsert(FileRecord(
            path="same.txt", provider="gdrive",
            remote_id="r1", local_hash=h, remote_hash=h,
            sync_status="synced",
        ))
        event = FileEvent(EventKind.MODIFIED, f)
        eng.handle_local_event(event)
        # Hash unchanged → no upload
        assert len(prov.uploaded) == 0


# ── initial_sync / reconcile_remote ──────────────────────────────────────


class TestInitialSync:
    def test_new_remote_file_downloaded(self, engine, tmp_path: Path):
        eng, cfg, db, prov = engine
        rf = _make_remote_file(remote_id="rf1", name="cloud.txt", path="cloud.txt")
        prov._list_result = [rf]
        eng.initial_sync()

        local_file = cfg.sync_dir / "cloud.txt"
        assert local_file.exists()
        assert ("rf1", local_file) in prov.downloaded

    def test_remote_deleted_file_removed_locally(self, engine, tmp_path: Path):
        eng, cfg, db, prov = engine
        # File known in DB but not returned by list_files
        local_file = cfg.sync_dir / "gone.txt"
        local_file.write_bytes(b"was here")
        db.upsert(FileRecord(
            path="gone.txt", provider="gdrive",
            remote_id="r-gone", local_hash="x", remote_hash="x",
            sync_status="synced",
        ))
        prov._list_result = []  # empty – simulates remote delete
        eng.run_remote_poll()

        assert not local_file.exists()
        assert db.get("gone.txt", "gdrive") is None

    def test_conflict_detection(self, engine, tmp_path: Path):
        eng, cfg, db, prov = engine
        local_file = cfg.sync_dir / "conflict.txt"
        local_file.write_bytes(b"local changes")
        local_hash = sha256_file(local_file)
        original_hash = "original-hash-000"

        db.upsert(FileRecord(
            path="conflict.txt", provider="gdrive",
            remote_id="r-conflict",
            local_hash=original_hash,   # recorded hash differs from actual
            remote_hash=original_hash,
            sync_status="synced",
        ))

        # Remote also changed (new content_hash)
        rf = _make_remote_file(
            remote_id="r-conflict",
            name="conflict.txt",
            path="conflict.txt",
            content_hash="new-remote-hash-999",
        )
        prov._list_result = [rf]
        eng.run_remote_poll()

        # Conflicted copy should exist
        conflict_files = list(cfg.sync_dir.glob("*conflicted copy*"))
        assert len(conflict_files) == 1
        # DB should mark conflict
        rec = db.get("conflict.txt", "gdrive")
        assert rec is not None


# ── retry ─────────────────────────────────────────────────────────────────


class TestRetry:
    def test_retry_succeeds_on_second_attempt(self, engine):
        eng, cfg, db, prov = engine
        call_count = [0]

        def _flaky():
            call_count[0] += 1
            if call_count[0] < 2:
                raise ConnectionError("transient")
            return "ok"

        cfg.max_retries = 3
        cfg.retry_backoff = 0.0
        result = eng._with_retry(_flaky, label="test")
        assert result == "ok"
        assert call_count[0] == 2

    def test_retry_raises_after_max_attempts(self, engine):
        eng, cfg, db, prov = engine
        cfg.max_retries = 2
        cfg.retry_backoff = 0.0

        def _always_fails():
            raise RuntimeError("always fails")

        with pytest.raises(RuntimeError, match="always fails"):
            eng._with_retry(_always_fails, label="test")
