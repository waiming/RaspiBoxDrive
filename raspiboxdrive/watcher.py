"""inotify-based file system watcher for the sync directory.

Uses ``inotify_simple`` when available (Linux / Raspberry Pi) and falls back to
``watchdog`` (cross-platform polling) when it is not.  The public API is the
same in both cases: callers subscribe to change events via a :class:`queue.Queue`.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class EventKind(Enum):
    CREATED = auto()
    MODIFIED = auto()
    DELETED = auto()
    MOVED = auto()


@dataclass
class FileEvent:
    kind: EventKind
    path: Path          # absolute path
    dest_path: Optional[Path] = None   # only for MOVED events


# ── inotify backend ─────────────────────────────────────────────────────────

def _inotify_available() -> bool:
    try:
        import inotify_simple  # noqa: F401
        return True
    except ImportError:
        return False


class _InotifyWatcher(threading.Thread):
    """Watches *watch_dir* recursively using inotify_simple."""

    def __init__(self, watch_dir: Path, event_queue: "queue.Queue[FileEvent]") -> None:
        super().__init__(daemon=True, name="inotify-watcher")
        self._watch_dir = watch_dir
        self._queue = event_queue
        self._stop_event = threading.Event()
        # map wd -> path
        self._wd_map: dict[int, Path] = {}
        self._inotify: "inotify_simple.INotify | None" = None  # type: ignore[name-defined]

    def run(self) -> None:
        import inotify_simple  # type: ignore[import-not-found]

        flags = (
            inotify_simple.flags.CREATE
            | inotify_simple.flags.MODIFY
            | inotify_simple.flags.DELETE
            | inotify_simple.flags.MOVED_FROM
            | inotify_simple.flags.MOVED_TO
            | inotify_simple.flags.CLOSE_WRITE
            | inotify_simple.flags.ONLYDIR  # ignored on non-dirs
        )
        # We add explicit non-ONLYDIR watches too
        file_flags = (
            inotify_simple.flags.CREATE
            | inotify_simple.flags.MODIFY
            | inotify_simple.flags.DELETE
            | inotify_simple.flags.MOVED_FROM
            | inotify_simple.flags.MOVED_TO
            | inotify_simple.flags.CLOSE_WRITE
        )

        self._inotify = inotify_simple.INotify()

        def add_watch(directory: Path) -> None:
            try:
                wd = self._inotify.add_watch(str(directory), file_flags)
                self._wd_map[wd] = directory
            except OSError as exc:
                logger.warning("inotify: cannot watch %s: %s", directory, exc)

        add_watch(self._watch_dir)
        for dirpath, dirnames, _ in os.walk(self._watch_dir):
            for d in dirnames:
                add_watch(Path(dirpath) / d)

        move_from: dict[int, Path] = {}  # cookie -> path

        logger.info("inotify watcher started on %s", self._watch_dir)

        while not self._stop_event.is_set():
            try:
                events = self._inotify.read(timeout=500)
            except OSError:
                break

            for ev in events:
                if ev.mask & inotify_simple.flags.IGNORED:
                    continue

                parent = self._wd_map.get(ev.wd, self._watch_dir)
                name = ev.name
                if not name:
                    continue
                path = parent / name

                if ev.mask & inotify_simple.flags.ISDIR:
                    # New sub-directory: start watching it
                    if ev.mask & inotify_simple.flags.CREATE:
                        add_watch(path)
                    continue

                if ev.mask & (inotify_simple.flags.CREATE | inotify_simple.flags.MOVED_TO):
                    if ev.mask & inotify_simple.flags.MOVED_TO and ev.cookie in move_from:
                        self._queue.put(
                            FileEvent(
                                EventKind.MOVED,
                                move_from.pop(ev.cookie),
                                dest_path=path,
                            )
                        )
                    else:
                        self._queue.put(FileEvent(EventKind.CREATED, path))

                elif ev.mask & inotify_simple.flags.MOVED_FROM:
                    move_from[ev.cookie] = path

                elif ev.mask & (
                    inotify_simple.flags.MODIFY | inotify_simple.flags.CLOSE_WRITE
                ):
                    self._queue.put(FileEvent(EventKind.MODIFIED, path))

                elif ev.mask & inotify_simple.flags.DELETE:
                    self._queue.put(FileEvent(EventKind.DELETED, path))

        logger.info("inotify watcher stopped")
        self._inotify.close()

    def stop(self) -> None:
        self._stop_event.set()


# ── watchdog fallback backend ────────────────────────────────────────────────

class _WatchdogWatcher:
    """Wraps ``watchdog`` so it exposes the same minimal interface."""

    def __init__(self, watch_dir: Path, event_queue: "queue.Queue[FileEvent]") -> None:
        self._watch_dir = watch_dir
        self._queue = event_queue
        self._observer: "Observer | None" = None  # type: ignore[name-defined]

    def start(self) -> None:
        from watchdog.observers import Observer  # type: ignore[import-not-found]
        from watchdog.events import (  # type: ignore[import-not-found]
            FileSystemEventHandler,
            FileCreatedEvent,
            FileModifiedEvent,
            FileDeletedEvent,
            FileMovedEvent,
        )

        queue_ref = self._queue

        class _Handler(FileSystemEventHandler):
            def on_created(self, event: FileCreatedEvent) -> None:
                if not event.is_directory:
                    queue_ref.put(FileEvent(EventKind.CREATED, Path(event.src_path)))

            def on_modified(self, event: FileModifiedEvent) -> None:
                if not event.is_directory:
                    queue_ref.put(FileEvent(EventKind.MODIFIED, Path(event.src_path)))

            def on_deleted(self, event: FileDeletedEvent) -> None:
                if not event.is_directory:
                    queue_ref.put(FileEvent(EventKind.DELETED, Path(event.src_path)))

            def on_moved(self, event: FileMovedEvent) -> None:
                if not event.is_directory:
                    queue_ref.put(
                        FileEvent(
                            EventKind.MOVED,
                            Path(event.src_path),
                            dest_path=Path(event.dest_path),
                        )
                    )

        self._observer = Observer()
        self._observer.schedule(_Handler(), str(self._watch_dir), recursive=True)
        self._observer.start()
        logger.info("watchdog watcher started on %s", self._watch_dir)

    def stop(self) -> None:
        if self._observer:
            self._observer.stop()
            self._observer.join()
            logger.info("watchdog watcher stopped")

    def is_alive(self) -> bool:
        return bool(self._observer and self._observer.is_alive())


# ── Public façade ─────────────────────────────────────────────────────────────

class FileWatcher:
    """Unified file watcher that prefers inotify over watchdog.

    Usage::

        watcher = FileWatcher(Path("/home/pi/RaspiBoxDrive"))
        watcher.start()
        while True:
            event = watcher.get_event(timeout=1)
            if event:
                process(event)
        watcher.stop()
    """

    def __init__(self, watch_dir: Path, queue_maxsize: int = 0) -> None:
        self._watch_dir = watch_dir
        self._queue: queue.Queue[FileEvent] = queue.Queue(maxsize=queue_maxsize)
        self._backend: _InotifyWatcher | _WatchdogWatcher | None = None

    def start(self) -> None:
        watch_dir = self._watch_dir
        watch_dir.mkdir(parents=True, exist_ok=True)

        if _inotify_available():
            backend: _InotifyWatcher | _WatchdogWatcher = _InotifyWatcher(
                watch_dir, self._queue
            )
            backend.start()
        else:
            logger.info("inotify_simple not available; falling back to watchdog")
            backend = _WatchdogWatcher(watch_dir, self._queue)
            backend.start()

        self._backend = backend

    def stop(self) -> None:
        if self._backend is not None:
            self._backend.stop()

    def get_event(self, timeout: float = 1.0) -> Optional[FileEvent]:
        """Return the next :class:`FileEvent` or ``None`` if the queue is empty."""
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    @property
    def queue(self) -> "queue.Queue[FileEvent]":
        return self._queue
