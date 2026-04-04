"""Core sync engine.

Responsibilities
----------------
* Detect local changes via the :class:`~raspiboxdrive.watcher.FileWatcher`.
* Detect remote changes by comparing the live remote file list with the
  last-known state stored in :class:`~raspiboxdrive.database.StateDatabase`.
* Decide what action to take (upload, download, delete, conflict) using a
  combination of modification timestamps and content hashes.
* Execute those actions against the configured cloud provider(s).
* Implement exponential-back-off retry for transient API errors.
* Produce "conflicted copy" files when both sides have diverged.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Optional

from raspiboxdrive.config import Config
from raspiboxdrive.database import FileRecord, StateDatabase
from raspiboxdrive.providers.base import CloudProvider, RemoteFile
from raspiboxdrive.watcher import EventKind, FileEvent, FileWatcher

logger = logging.getLogger(__name__)

# ── Helpers ───────────────────────────────────────────────────────────────


def sha256_file(path: Path) -> str:
    """Return the hex SHA-256 digest of *path*."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _relative(path: Path, base: Path) -> str:
    """Return *path* relative to *base* as a forward-slash POSIX string."""
    return path.relative_to(base).as_posix()


# ── Retry decorator ───────────────────────────────────────────────────────


def retry(max_attempts: int = 5, base_delay: float = 2.0):
    """Decorator that retries the wrapped function on ``Exception``."""
    import functools

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            last_exc: Optional[Exception] = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                    delay = base_delay * (2 ** (attempt - 1))
                    logger.warning(
                        "%s failed (attempt %d/%d): %s – retrying in %.1fs",
                        fn.__name__,
                        attempt,
                        max_attempts,
                        exc,
                        delay,
                    )
                    time.sleep(delay)
            raise last_exc  # type: ignore[misc]

        return wrapper

    return decorator


# ── SyncEngine ────────────────────────────────────────────────────────────


class SyncEngine:
    """Bidirectional sync engine for a single cloud provider."""

    def __init__(
        self,
        config: Config,
        provider: CloudProvider,
        db: StateDatabase,
        watcher: FileWatcher,
    ) -> None:
        self._cfg = config
        self._provider = provider
        self._db = db
        self._watcher = watcher
        self._sync_dir = config.sync_dir
        self._provider_name = provider.provider_name

    # ── Public entry points ───────────────────────────────────────────────

    def initial_sync(self) -> None:
        """Perform a full bidirectional reconciliation at startup."""
        logger.info("[%s] Starting initial full sync …", self._provider_name)
        self._reconcile()
        logger.info("[%s] Initial sync complete", self._provider_name)

    def handle_local_event(self, event: FileEvent) -> None:
        """React to a local filesystem change detected by the watcher."""
        rel = _relative(event.path, self._sync_dir)
        logger.info("[%s] Local %s: %s", self._provider_name, event.kind.name, rel)

        if event.kind == EventKind.DELETED:
            self._handle_local_delete(rel)
        elif event.kind == EventKind.MOVED and event.dest_path:
            dest_rel = _relative(event.dest_path, self._sync_dir)
            self._handle_local_move(rel, dest_rel)
        else:
            # CREATED or MODIFIED
            self._handle_local_upsert(rel, event.path)

    def run_remote_poll(self) -> None:
        """Detect and apply remote changes (called periodically)."""
        self._reconcile_remote()

    # ── Reconciliation ────────────────────────────────────────────────────

    def _reconcile(self) -> None:
        """Full two-way reconciliation."""
        # 1. Walk remote → apply anything new / updated / deleted
        self._reconcile_remote()
        # 2. Walk local → upload anything not yet in DB
        self._reconcile_local()

    def _reconcile_remote(self) -> None:
        """Fetch remote file list and apply changes locally."""
        try:
            remote_files = self._provider.list_files()
        except Exception as exc:  # noqa: BLE001
            logger.error("[%s] list_files failed: %s", self._provider_name, exc)
            return

        remote_map: dict[str, RemoteFile] = {rf.path: rf for rf in remote_files}
        known_map: dict[str, FileRecord] = {
            r.path: r for r in self._db.list_all(self._provider_name)
        }

        # Files present remotely
        for remote_path, rf in remote_map.items():
            local_path = self._sync_dir / remote_path
            record = known_map.get(remote_path)

            if record is None:
                # New remote file – download
                self._download_and_record(rf, local_path, remote_path)
            else:
                # Known file – check for changes
                self._resolve_remote_update(rf, record, local_path, remote_path)

        # Files missing from remote but known locally (deleted remotely)
        for known_path, record in known_map.items():
            if known_path not in remote_map and record.sync_status != "pending_upload":
                local_path = self._sync_dir / known_path
                if local_path.exists():
                    logger.info(
                        "[%s] Remote delete detected: %s",
                        self._provider_name,
                        known_path,
                    )
                    local_path.unlink()
                self._db.delete(known_path, self._provider_name)

    def _reconcile_local(self) -> None:
        """Walk the local sync dir and upload files absent from the DB."""
        for local_path in self._sync_dir.rglob("*"):
            if not local_path.is_file():
                continue
            rel = _relative(local_path, self._sync_dir)
            if self._db.get(rel, self._provider_name) is None:
                self._handle_local_upsert(rel, local_path)

    # ── Local change handlers ─────────────────────────────────────────────

    def _handle_local_upsert(self, rel: str, local_path: Path) -> None:
        if not local_path.exists():
            return

        record = self._db.get(rel, self._provider_name)
        local_hash = sha256_file(local_path)
        local_mtime = local_path.stat().st_mtime

        if record:
            if record.local_hash == local_hash:
                return  # no actual change
            # Local file changed – check for remote conflict
            if record.remote_hash and record.remote_hash != record.local_hash:
                # Both sides changed → conflict
                self._create_conflicted_copy(local_path)
                self._mark_conflict(rel)
                return
            # Only local changed → upload
            self._upload_with_retry(local_path, rel, record)
        else:
            self._upload_with_retry(local_path, rel, None)

    def _handle_local_delete(self, rel: str) -> None:
        record = self._db.get(rel, self._provider_name)
        if record and record.remote_id:
            self._delete_remote_with_retry(record.remote_id, rel)
        self._db.delete(rel, self._provider_name)

    def _handle_local_move(self, src_rel: str, dst_rel: str) -> None:
        record = self._db.get(src_rel, self._provider_name)
        if record and record.remote_id:
            try:
                rf = self._with_retry(
                    lambda: self._provider.move_file(record.remote_id, dst_rel),
                    label=f"move {src_rel} → {dst_rel}",
                )
                new_record = FileRecord(
                    path=dst_rel,
                    provider=self._provider_name,
                    remote_id=rf.remote_id,
                    local_mtime=rf.mtime,
                    remote_mtime=rf.mtime,
                    local_hash=record.local_hash,
                    remote_hash=rf.content_hash,
                    sync_status="synced",
                    last_sync=time.time(),
                )
                self._db.delete(src_rel, self._provider_name)
                self._db.upsert(new_record)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "[%s] move_file failed %s → %s: %s",
                    self._provider_name,
                    src_rel,
                    dst_rel,
                    exc,
                )

    # ── Remote change resolution ──────────────────────────────────────────

    def _resolve_remote_update(
        self,
        rf: RemoteFile,
        record: FileRecord,
        local_path: Path,
        remote_path: str,
    ) -> None:
        remote_changed = rf.content_hash != record.remote_hash
        local_changed = (
            local_path.exists()
            and sha256_file(local_path) != record.local_hash
        )

        if not remote_changed:
            return  # no remote change

        if local_changed:
            # Both sides changed → conflict
            self._create_conflicted_copy(local_path)
            self._mark_conflict(remote_path)
            # Download remote as the canonical version
            self._download_and_record(rf, local_path, remote_path)
        else:
            # Only remote changed → download
            self._download_and_record(rf, local_path, remote_path)

    # ── Upload / download wrappers ────────────────────────────────────────

    def _upload_with_retry(
        self,
        local_path: Path,
        rel: str,
        existing_record: Optional[FileRecord],
    ) -> None:
        remote_id = existing_record.remote_id if existing_record else None

        def _do() -> RemoteFile:
            return self._provider.upload_file(
                local_path,
                rel,
                remote_id=remote_id,
                chunk_size=self._cfg.chunk_size,
            )

        try:
            rf = self._with_retry(_do, label=f"upload {rel}")
        except Exception as exc:  # noqa: BLE001
            logger.error("[%s] upload failed %s: %s", self._provider_name, rel, exc)
            return

        local_hash = sha256_file(local_path)
        self._db.upsert(
            FileRecord(
                path=rel,
                provider=self._provider_name,
                remote_id=rf.remote_id,
                local_mtime=local_path.stat().st_mtime,
                remote_mtime=rf.mtime,
                local_hash=local_hash,
                remote_hash=rf.content_hash,
                sync_status="synced",
                last_sync=time.time(),
            )
        )

    def _download_and_record(
        self, rf: RemoteFile, local_path: Path, remote_path: str
    ) -> None:
        def _do() -> None:
            self._provider.download_file(
                rf.remote_id, local_path, chunk_size=self._cfg.chunk_size
            )

        try:
            self._with_retry(_do, label=f"download {remote_path}")
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "[%s] download failed %s: %s", self._provider_name, remote_path, exc
            )
            return

        local_hash = sha256_file(local_path)
        self._db.upsert(
            FileRecord(
                path=remote_path,
                provider=self._provider_name,
                remote_id=rf.remote_id,
                local_mtime=local_path.stat().st_mtime,
                remote_mtime=rf.mtime,
                local_hash=local_hash,
                remote_hash=rf.content_hash,
                sync_status="synced",
                last_sync=time.time(),
            )
        )

    def _delete_remote_with_retry(self, remote_id: str, rel: str) -> None:
        try:
            self._with_retry(
                lambda: self._provider.delete_file(remote_id),
                label=f"delete {rel}",
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "[%s] delete failed %s: %s", self._provider_name, rel, exc
            )

    # ── Conflict handling ─────────────────────────────────────────────────

    def _create_conflicted_copy(self, local_path: Path) -> None:
        """Rename *local_path* to a "conflicted copy" filename and keep it."""
        ts = time.strftime("%Y%m%d_%H%M%S")
        stem = local_path.stem
        suffix = local_path.suffix
        conflict_name = f"{stem} (conflicted copy {ts}){suffix}"
        conflict_path = local_path.parent / conflict_name
        shutil.copy2(local_path, conflict_path)
        logger.warning(
            "[%s] Conflict detected – saved local copy as %s",
            self._provider_name,
            conflict_path.name,
        )

    def _mark_conflict(self, rel: str) -> None:
        self._db.mark_conflict(rel, self._provider_name)

    # ── Generic retry helper ──────────────────────────────────────────────

    def _with_retry(self, fn, label: str = ""):
        max_attempts = self._cfg.max_retries
        base_delay = self._cfg.retry_backoff
        last_exc: Optional[Exception] = None

        for attempt in range(1, max_attempts + 1):
            try:
                return fn()
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                delay = base_delay * (2 ** (attempt - 1))
                logger.warning(
                    "[%s] %s failed (attempt %d/%d): %s – retrying in %.1fs",
                    self._provider_name,
                    label,
                    attempt,
                    max_attempts,
                    exc,
                    delay,
                )
                time.sleep(delay)

        raise last_exc  # type: ignore[misc]
