"""Tests for raspiboxdrive.watcher (event types and queue behaviour)."""

from __future__ import annotations

import queue
import time
from pathlib import Path

import pytest

from raspiboxdrive.watcher import EventKind, FileEvent, FileWatcher


class TestFileEvent:
    def test_basic_attributes(self):
        p = Path("/tmp/test.txt")
        ev = FileEvent(EventKind.CREATED, p)
        assert ev.kind == EventKind.CREATED
        assert ev.path == p
        assert ev.dest_path is None

    def test_moved_event_has_dest(self):
        src = Path("/tmp/src.txt")
        dst = Path("/tmp/dst.txt")
        ev = FileEvent(EventKind.MOVED, src, dest_path=dst)
        assert ev.dest_path == dst


class TestFileWatcher:
    def test_get_event_returns_none_on_timeout(self, tmp_path: Path):
        watcher = FileWatcher(tmp_path)
        watcher.start()
        try:
            result = watcher.get_event(timeout=0.1)
            assert result is None
        finally:
            watcher.stop()

    def test_queue_accessible(self, tmp_path: Path):
        watcher = FileWatcher(tmp_path)
        assert isinstance(watcher.queue, queue.Queue)

    def test_manual_queue_put(self, tmp_path: Path):
        """Verify that manually enqueued events are dequeued correctly."""
        watcher = FileWatcher(tmp_path)
        ev = FileEvent(EventKind.MODIFIED, tmp_path / "x.txt")
        watcher.queue.put(ev)
        result = watcher.get_event(timeout=0.5)
        assert result is not None
        assert result.kind == EventKind.MODIFIED

    def test_start_creates_sync_dir(self, tmp_path: Path):
        new_dir = tmp_path / "newdir"
        watcher = FileWatcher(new_dir)
        watcher.start()
        try:
            assert new_dir.is_dir()
        finally:
            watcher.stop()
