"""Dropbox cloud provider implementation.

Authentication uses the Dropbox PKCE / offline-access OAuth 2.0 flow.  The
app key and secret are read from config; the resulting refresh token is
persisted to a JSON file and reused on subsequent runs.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional

from raspiboxdrive.providers.base import CloudProvider, RemoteFile

logger = logging.getLogger(__name__)


class DropboxProvider(CloudProvider):
    """Dropbox backend using the official ``dropbox`` Python SDK."""

    def __init__(
        self,
        app_key: str,
        app_secret: str,
        token_file: Path,
    ) -> None:
        self._app_key = app_key
        self._app_secret = app_secret
        self._token_file = token_file
        self._dbx = None  # dropbox.Dropbox instance

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def authenticate(self) -> None:
        import dropbox  # type: ignore[import-not-found]
        from dropbox import DropboxOAuth2FlowNoRedirect  # type: ignore[import-not-found]

        # Try to load a saved refresh token first
        if self._token_file.exists():
            try:
                data = json.loads(self._token_file.read_text())
                self._dbx = dropbox.Dropbox(
                    oauth2_refresh_token=data["refresh_token"],
                    app_key=self._app_key,
                    app_secret=self._app_secret,
                )
                self._dbx.check_and_refresh_access_token()
                logger.info("Dropbox authenticated from saved token")
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not load Dropbox token: %s – re-authorising", exc)

        # Interactive OAuth PKCE flow
        auth_flow = DropboxOAuth2FlowNoRedirect(
            self._app_key,
            consumer_secret=self._app_secret,
            token_access_type="offline",
        )
        url = auth_flow.start()
        print(f"\nVisit this URL to authorise Dropbox access:\n  {url}\n")
        auth_code = input("Enter the authorisation code: ").strip()
        oauth_result = auth_flow.finish(auth_code)

        self._dbx = dropbox.Dropbox(
            oauth2_refresh_token=oauth_result.refresh_token,
            app_key=self._app_key,
            app_secret=self._app_secret,
        )

        self._token_file.parent.mkdir(parents=True, exist_ok=True)
        self._token_file.write_text(
            json.dumps({"refresh_token": oauth_result.refresh_token})
        )
        logger.info("Dropbox token saved to %s", self._token_file)

    def refresh_token(self) -> None:
        self._ensure_authenticated()
        self._dbx.check_and_refresh_access_token()
        logger.debug("Dropbox token refreshed")

    # ── Listing ────────────────────────────────────────────────────────────

    def list_files(self, remote_path: str = "") -> list[RemoteFile]:
        self._ensure_authenticated()
        folder = ("/" + remote_path.lstrip("/")) if remote_path else ""
        results: list[RemoteFile] = []

        try:
            res = self._dbx.files_list_folder(folder, recursive=True)
        except Exception as exc:  # noqa: BLE001
            logger.error("Dropbox list_files error: %s", exc)
            return results

        while True:
            for entry in res.entries:
                import dropbox.files as dbxfiles  # type: ignore[import-not-found]

                if isinstance(entry, dbxfiles.FileMetadata):
                    results.append(
                        RemoteFile(
                            remote_id=entry.id,
                            name=entry.name,
                            path=entry.path_display or entry.path_lower or entry.name,
                            mtime=entry.server_modified.timestamp(),
                            size=entry.size,
                            content_hash=entry.content_hash or "",
                        )
                    )
            if not res.has_more:
                break
            res = self._dbx.files_list_folder_continue(res.cursor)

        return results

    def get_file_metadata(self, remote_id: str) -> Optional[RemoteFile]:
        self._ensure_authenticated()
        import dropbox.files as dbxfiles  # type: ignore[import-not-found]

        try:
            # Dropbox IDs start with "id:"
            entry = self._dbx.files_get_metadata(remote_id)
            if isinstance(entry, dbxfiles.FileMetadata):
                return RemoteFile(
                    remote_id=entry.id,
                    name=entry.name,
                    path=entry.path_display or entry.name,
                    mtime=entry.server_modified.timestamp(),
                    size=entry.size,
                    content_hash=entry.content_hash or "",
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Dropbox get_file_metadata failed: %s", exc)
        return None

    # ── Transfer ───────────────────────────────────────────────────────────

    def upload_file(
        self,
        local_path: Path,
        remote_path: str,
        remote_id: Optional[str] = None,
        chunk_size: int = 5 * 1024 * 1024,
    ) -> RemoteFile:
        import dropbox.files as dbxfiles  # type: ignore[import-not-found]

        self._ensure_authenticated()
        dest = ("/" + remote_path.lstrip("/")) if not remote_path.startswith("/") else remote_path
        file_size = local_path.stat().st_size
        mode = dbxfiles.WriteMode.overwrite

        if file_size <= chunk_size:
            with open(local_path, "rb") as fh:
                entry = self._dbx.files_upload(fh.read(), dest, mode=mode, mute=True)
        else:
            entry = self._upload_chunked(local_path, dest, chunk_size, mode)

        logger.info("Uploaded %s → Dropbox %s", local_path.name, dest)
        return RemoteFile(
            remote_id=entry.id,
            name=entry.name,
            path=entry.path_display or dest,
            mtime=entry.server_modified.timestamp(),
            size=entry.size,
            content_hash=entry.content_hash or "",
        )

    def _upload_chunked(
        self,
        local_path: Path,
        dest: str,
        chunk_size: int,
        mode,  # type: ignore[no-untyped-def]
    ):
        import dropbox.files as dbxfiles  # type: ignore[import-not-found]

        file_size = local_path.stat().st_size
        uploaded = 0

        with open(local_path, "rb") as fh:
            chunk = fh.read(chunk_size)
            session = self._dbx.files_upload_session_start(chunk)
            uploaded += len(chunk)
            cursor = dbxfiles.UploadSessionCursor(
                session_id=session.session_id, offset=uploaded
            )

            while uploaded < file_size:
                chunk = fh.read(chunk_size)
                if uploaded + len(chunk) < file_size:
                    self._dbx.files_upload_session_append_v2(chunk, cursor)
                    uploaded += len(chunk)
                    cursor = dbxfiles.UploadSessionCursor(
                        session_id=cursor.session_id, offset=uploaded
                    )
                    logger.debug(
                        "Dropbox chunked upload %s: %.0f%%",
                        local_path.name,
                        100 * uploaded / file_size,
                    )
                else:
                    commit = dbxfiles.CommitInfo(path=dest, mode=mode, mute=True)
                    entry = self._dbx.files_upload_session_finish(chunk, cursor, commit)
                    return entry

        # Edge case: file fits exactly in the last chunk handled above,
        # but if we somehow exit the loop, commit an empty finish.
        commit = dbxfiles.CommitInfo(path=dest, mode=mode, mute=True)
        return self._dbx.files_upload_session_finish(b"", cursor, commit)

    def download_file(
        self,
        remote_id: str,
        local_path: Path,
        chunk_size: int = 5 * 1024 * 1024,
    ) -> None:
        self._ensure_authenticated()
        local_path.parent.mkdir(parents=True, exist_ok=True)

        _, response = self._dbx.files_download(remote_id)
        with open(local_path, "wb") as fh:
            for block in response.iter_content(chunk_size):
                fh.write(block)

        logger.info("Downloaded Dropbox %s → %s", remote_id, local_path)

    def delete_file(self, remote_id: str) -> None:
        self._ensure_authenticated()
        # Dropbox delete requires path, not id – fetch it first
        meta = self.get_file_metadata(remote_id)
        if meta:
            self._dbx.files_delete_v2(meta.path)
            logger.info("Deleted Dropbox file %s", meta.path)

    def move_file(self, remote_id: str, new_remote_path: str) -> RemoteFile:
        import dropbox.files as dbxfiles  # type: ignore[import-not-found]

        self._ensure_authenticated()
        meta = self.get_file_metadata(remote_id)
        if not meta:
            raise ValueError(f"Remote file not found: {remote_id}")

        dest = (
            ("/" + new_remote_path.lstrip("/"))
            if not new_remote_path.startswith("/")
            else new_remote_path
        )
        result = self._dbx.files_move_v2(meta.path, dest)
        entry = result.metadata
        return RemoteFile(
            remote_id=entry.id,
            name=entry.name,
            path=entry.path_display or dest,
            mtime=entry.server_modified.timestamp(),
            size=entry.size,
            content_hash=getattr(entry, "content_hash", "") or "",
        )

    # ── Convenience ───────────────────────────────────────────────────────

    @property
    def provider_name(self) -> str:
        return "dropbox"

    # ── Internal helpers ───────────────────────────────────────────────────

    def _ensure_authenticated(self) -> None:
        if self._dbx is None:
            raise RuntimeError(
                "Not authenticated.  Call DropboxProvider.authenticate() first."
            )
