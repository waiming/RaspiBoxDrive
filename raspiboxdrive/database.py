"""SQLite-backed state database for tracking synced files.

Schema
------
``files`` table – one row per file tracked by the sync engine:

    path          TEXT  – relative path inside the sync directory (primary key)
    provider      TEXT  – "gdrive" | "dropbox"
    remote_id     TEXT  – opaque remote file / object identifier
    local_mtime   REAL  – local file modification time (Unix epoch float)
    remote_mtime  REAL  – remote file modification time (Unix epoch float)
    local_hash    TEXT  – SHA-256 of the local file contents (hex)
    remote_hash   TEXT  – remote etag / content_hash (provider-specific)
    sync_status   TEXT  – "synced" | "pending_upload" | "pending_download"
                           | "conflict" | "deleted_local" | "deleted_remote"
    last_sync     REAL  – Unix epoch float of the last successful sync
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Generator, Optional


_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path          TEXT    NOT NULL,
    provider      TEXT    NOT NULL,
    remote_id     TEXT,
    local_mtime   REAL,
    remote_mtime  REAL,
    local_hash    TEXT,
    remote_hash   TEXT,
    sync_status   TEXT    NOT NULL DEFAULT 'synced',
    last_sync     REAL,
    PRIMARY KEY (path, provider)
);

CREATE INDEX IF NOT EXISTS idx_files_provider       ON files (provider);
CREATE INDEX IF NOT EXISTS idx_files_sync_status    ON files (sync_status);
"""


class FileRecord:
    """Lightweight value-object representing a row in the ``files`` table."""

    __slots__ = (
        "path",
        "provider",
        "remote_id",
        "local_mtime",
        "remote_mtime",
        "local_hash",
        "remote_hash",
        "sync_status",
        "last_sync",
    )

    def __init__(
        self,
        path: str,
        provider: str,
        remote_id: Optional[str] = None,
        local_mtime: Optional[float] = None,
        remote_mtime: Optional[float] = None,
        local_hash: Optional[str] = None,
        remote_hash: Optional[str] = None,
        sync_status: str = "synced",
        last_sync: Optional[float] = None,
    ) -> None:
        self.path = path
        self.provider = provider
        self.remote_id = remote_id
        self.local_mtime = local_mtime
        self.remote_mtime = remote_mtime
        self.local_hash = local_hash
        self.remote_hash = remote_hash
        self.sync_status = sync_status
        self.last_sync = last_sync

    def __repr__(self) -> str:
        return (
            f"FileRecord(path={self.path!r}, provider={self.provider!r}, "
            f"status={self.sync_status!r})"
        )


class StateDatabase:
    """Thread-safe SQLite wrapper for tracking sync state."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._apply_schema()

    # ── Internal helpers ────────────────────────────────────────────────────

    def _apply_schema(self) -> None:
        with self._conn:
            self._conn.executescript(_SCHEMA)

    @contextmanager
    def _tx(self) -> Generator[sqlite3.Connection, None, None]:
        """Context manager that commits on success, rolls back on error."""
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # ── Public API ──────────────────────────────────────────────────────────

    def upsert(self, record: FileRecord) -> None:
        """Insert or replace a file record."""
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO files
                    (path, provider, remote_id, local_mtime, remote_mtime,
                     local_hash, remote_hash, sync_status, last_sync)
                VALUES
                    (:path, :provider, :remote_id, :local_mtime, :remote_mtime,
                     :local_hash, :remote_hash, :sync_status, :last_sync)
                ON CONFLICT(path, provider) DO UPDATE SET
                    remote_id     = excluded.remote_id,
                    local_mtime   = excluded.local_mtime,
                    remote_mtime  = excluded.remote_mtime,
                    local_hash    = excluded.local_hash,
                    remote_hash   = excluded.remote_hash,
                    sync_status   = excluded.sync_status,
                    last_sync     = excluded.last_sync
                """,
                {
                    "path": record.path,
                    "provider": record.provider,
                    "remote_id": record.remote_id,
                    "local_mtime": record.local_mtime,
                    "remote_mtime": record.remote_mtime,
                    "local_hash": record.local_hash,
                    "remote_hash": record.remote_hash,
                    "sync_status": record.sync_status,
                    "last_sync": record.last_sync,
                },
            )

    def get(self, path: str, provider: str) -> Optional[FileRecord]:
        """Return the record for *path* / *provider*, or ``None``."""
        cursor = self._conn.execute(
            "SELECT * FROM files WHERE path=? AND provider=?",
            (path, provider),
        )
        row = cursor.fetchone()
        return self._row_to_record(row) if row else None

    def delete(self, path: str, provider: str) -> None:
        """Remove the record for *path* / *provider*."""
        with self._tx() as conn:
            conn.execute(
                "DELETE FROM files WHERE path=? AND provider=?",
                (path, provider),
            )

    def list_by_status(self, status: str, provider: str) -> list[FileRecord]:
        """Return all records matching *status* for *provider*."""
        cursor = self._conn.execute(
            "SELECT * FROM files WHERE sync_status=? AND provider=?",
            (status, provider),
        )
        return [self._row_to_record(r) for r in cursor.fetchall()]

    def list_all(self, provider: str) -> list[FileRecord]:
        """Return all records for *provider*."""
        cursor = self._conn.execute(
            "SELECT * FROM files WHERE provider=?",
            (provider,),
        )
        return [self._row_to_record(r) for r in cursor.fetchall()]

    def mark_synced(self, path: str, provider: str) -> None:
        """Update *sync_status* to ``'synced'`` and stamp *last_sync*."""
        with self._tx() as conn:
            conn.execute(
                """
                UPDATE files
                SET sync_status=?, last_sync=?
                WHERE path=? AND provider=?
                """,
                ("synced", time.time(), path, provider),
            )

    def mark_conflict(self, path: str, provider: str) -> None:
        """Update *sync_status* to ``'conflict'``."""
        with self._tx() as conn:
            conn.execute(
                "UPDATE files SET sync_status=? WHERE path=? AND provider=?",
                ("conflict", path, provider),
            )

    def close(self) -> None:
        self._conn.close()

    # ── Private ─────────────────────────────────────────────────────────────

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> FileRecord:
        return FileRecord(
            path=row["path"],
            provider=row["provider"],
            remote_id=row["remote_id"],
            local_mtime=row["local_mtime"],
            remote_mtime=row["remote_mtime"],
            local_hash=row["local_hash"],
            remote_hash=row["remote_hash"],
            sync_status=row["sync_status"],
            last_sync=row["last_sync"],
        )
