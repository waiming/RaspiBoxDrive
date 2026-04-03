"""Tests for raspiboxdrive.database."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from raspiboxdrive.database import FileRecord, StateDatabase


@pytest.fixture
def db(tmp_path: Path) -> StateDatabase:
    return StateDatabase(tmp_path / "state.db")


class TestFileRecord:
    def test_defaults(self):
        r = FileRecord(path="foo/bar.txt", provider="gdrive")
        assert r.sync_status == "synced"
        assert r.remote_id is None

    def test_repr(self):
        r = FileRecord(path="a.txt", provider="dropbox", sync_status="conflict")
        assert "conflict" in repr(r)


class TestStateDatabase:
    def test_upsert_and_get(self, db: StateDatabase):
        rec = FileRecord(
            path="docs/readme.txt",
            provider="gdrive",
            remote_id="abc123",
            local_mtime=1000.0,
            remote_mtime=1000.0,
            local_hash="deadbeef",
            remote_hash="deadbeef",
            sync_status="synced",
        )
        db.upsert(rec)

        result = db.get("docs/readme.txt", "gdrive")
        assert result is not None
        assert result.remote_id == "abc123"
        assert result.local_hash == "deadbeef"
        assert result.sync_status == "synced"

    def test_upsert_updates_existing(self, db: StateDatabase):
        rec = FileRecord(path="a.txt", provider="gdrive", sync_status="pending_upload")
        db.upsert(rec)

        rec.sync_status = "synced"
        rec.remote_id = "xyz"
        db.upsert(rec)

        result = db.get("a.txt", "gdrive")
        assert result.sync_status == "synced"
        assert result.remote_id == "xyz"

    def test_get_missing_returns_none(self, db: StateDatabase):
        assert db.get("nonexistent.txt", "gdrive") is None

    def test_delete(self, db: StateDatabase):
        rec = FileRecord(path="b.txt", provider="dropbox")
        db.upsert(rec)
        db.delete("b.txt", "dropbox")
        assert db.get("b.txt", "dropbox") is None

    def test_delete_nonexistent_is_noop(self, db: StateDatabase):
        # Should not raise
        db.delete("ghost.txt", "gdrive")

    def test_list_by_status(self, db: StateDatabase):
        db.upsert(FileRecord(path="x.txt", provider="gdrive", sync_status="synced"))
        db.upsert(FileRecord(path="y.txt", provider="gdrive", sync_status="conflict"))
        db.upsert(FileRecord(path="z.txt", provider="gdrive", sync_status="conflict"))

        conflicts = db.list_by_status("conflict", "gdrive")
        assert len(conflicts) == 2
        assert all(r.sync_status == "conflict" for r in conflicts)

    def test_list_all(self, db: StateDatabase):
        db.upsert(FileRecord(path="a.txt", provider="gdrive"))
        db.upsert(FileRecord(path="b.txt", provider="gdrive"))
        db.upsert(FileRecord(path="c.txt", provider="dropbox"))

        gdrive_records = db.list_all("gdrive")
        assert len(gdrive_records) == 2

        dropbox_records = db.list_all("dropbox")
        assert len(dropbox_records) == 1

    def test_mark_synced(self, db: StateDatabase):
        rec = FileRecord(path="doc.txt", provider="gdrive", sync_status="pending_upload")
        db.upsert(rec)
        before = time.time()
        db.mark_synced("doc.txt", "gdrive")
        result = db.get("doc.txt", "gdrive")
        assert result.sync_status == "synced"
        assert result.last_sync >= before

    def test_mark_conflict(self, db: StateDatabase):
        rec = FileRecord(path="doc.txt", provider="gdrive", sync_status="synced")
        db.upsert(rec)
        db.mark_conflict("doc.txt", "gdrive")
        result = db.get("doc.txt", "gdrive")
        assert result.sync_status == "conflict"

    def test_provider_isolation(self, db: StateDatabase):
        db.upsert(FileRecord(path="shared.txt", provider="gdrive", remote_id="g1"))
        db.upsert(FileRecord(path="shared.txt", provider="dropbox", remote_id="d1"))

        g = db.get("shared.txt", "gdrive")
        d = db.get("shared.txt", "dropbox")
        assert g.remote_id == "g1"
        assert d.remote_id == "d1"

    def test_close_and_reopen(self, tmp_path: Path):
        db = StateDatabase(tmp_path / "state.db")
        db.upsert(FileRecord(path="persist.txt", provider="gdrive", remote_id="persist"))
        db.close()

        db2 = StateDatabase(tmp_path / "state.db")
        r = db2.get("persist.txt", "gdrive")
        assert r is not None
        assert r.remote_id == "persist"
        db2.close()
