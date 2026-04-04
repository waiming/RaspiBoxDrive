"""Abstract base class for cloud storage providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional


class RemoteFile:
    """Metadata returned from cloud storage listing calls."""

    __slots__ = ("remote_id", "name", "path", "mtime", "size", "content_hash")

    def __init__(
        self,
        remote_id: str,
        name: str,
        path: str,
        mtime: float,
        size: int,
        content_hash: str = "",
    ) -> None:
        self.remote_id = remote_id
        self.name = name
        self.path = path          # remote path (provider-relative)
        self.mtime = mtime        # Unix epoch float
        self.size = size          # bytes
        self.content_hash = content_hash  # etag / sha256 / etc.

    def __repr__(self) -> str:
        return f"RemoteFile(path={self.path!r}, id={self.remote_id!r})"


class CloudProvider(ABC):
    """Interface every cloud storage backend must implement."""

    # ── Lifecycle ──────────────────────────────────────────────────────────
    @abstractmethod
    def authenticate(self) -> None:
        """Perform OAuth flow / load stored token and prepare the API client."""

    @abstractmethod
    def refresh_token(self) -> None:
        """Refresh the access token if it has expired."""

    # ── Remote introspection ───────────────────────────────────────────────
    @abstractmethod
    def list_files(self, remote_path: str = "") -> list[RemoteFile]:
        """Return a flat list of all files under *remote_path*."""

    @abstractmethod
    def get_file_metadata(self, remote_id: str) -> Optional[RemoteFile]:
        """Return metadata for a single remote file, or ``None``."""

    # ── Transfer ───────────────────────────────────────────────────────────
    @abstractmethod
    def upload_file(
        self,
        local_path: Path,
        remote_path: str,
        remote_id: Optional[str] = None,
        chunk_size: int = 5 * 1024 * 1024,
    ) -> RemoteFile:
        """Upload *local_path* to *remote_path*.

        If *remote_id* is provided the file is updated in-place; otherwise a
        new file is created.  Large files are uploaded in chunks of
        *chunk_size* bytes.
        """

    @abstractmethod
    def download_file(
        self,
        remote_id: str,
        local_path: Path,
        chunk_size: int = 5 * 1024 * 1024,
    ) -> None:
        """Download the remote file identified by *remote_id* to *local_path*."""

    @abstractmethod
    def delete_file(self, remote_id: str) -> None:
        """Permanently delete the remote file identified by *remote_id*."""

    @abstractmethod
    def move_file(self, remote_id: str, new_remote_path: str) -> RemoteFile:
        """Rename / move a remote file and return updated metadata."""

    # ── Convenience ───────────────────────────────────────────────────────
    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Short identifier string, e.g. ``"gdrive"`` or ``"dropbox"``."""
