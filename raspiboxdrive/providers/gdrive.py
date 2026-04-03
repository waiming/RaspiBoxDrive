"""Google Drive cloud provider implementation.

Authentication uses the standard Google OAuth 2.0 flow with a *credentials*
JSON file that the user downloads from Google Cloud Console.  The resulting
token is persisted locally and refreshed automatically.
"""

from __future__ import annotations

import io
import logging
import time
from pathlib import Path
from typing import Optional

from raspiboxdrive.providers.base import CloudProvider, RemoteFile

logger = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/drive"]
_MIME_FOLDER = "application/vnd.google-apps.folder"


class GoogleDriveProvider(CloudProvider):
    """Google Drive backend using the official Python client library."""

    def __init__(
        self,
        credentials_file: Path,
        token_file: Path,
        chunk_size: int = 5 * 1024 * 1024,
    ) -> None:
        self._credentials_file = credentials_file
        self._token_file = token_file
        self._chunk_size = chunk_size
        self._service = None  # google.auth.Resource

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def authenticate(self) -> None:
        """Load or create OAuth credentials, launching browser flow if needed."""
        from google.oauth2.credentials import Credentials  # type: ignore[import-not-found]
        from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore[import-not-found]
        from googleapiclient.discovery import build  # type: ignore[import-not-found]

        creds = None
        if self._token_file.exists():
            creds = Credentials.from_authorized_user_file(
                str(self._token_file), _SCOPES
            )

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                self.refresh_token()
                return
            flow = InstalledAppFlow.from_client_secrets_file(
                str(self._credentials_file), _SCOPES
            )
            creds = flow.run_local_server(port=0)
            self._token_file.parent.mkdir(parents=True, exist_ok=True)
            self._token_file.write_text(creds.to_json())
            logger.info("Google Drive token saved to %s", self._token_file)

        self._service = build("drive", "v3", credentials=creds)
        logger.info("Google Drive authenticated successfully")

    def refresh_token(self) -> None:
        from google.auth.transport.requests import Request  # type: ignore[import-not-found]
        from google.oauth2.credentials import Credentials  # type: ignore[import-not-found]
        from googleapiclient.discovery import build  # type: ignore[import-not-found]

        if not self._token_file.exists():
            raise RuntimeError("No token file – call authenticate() first")

        creds = Credentials.from_authorized_user_file(
            str(self._token_file), _SCOPES
        )
        creds.refresh(Request())
        self._token_file.write_text(creds.to_json())
        self._service = build("drive", "v3", credentials=creds)
        logger.info("Google Drive token refreshed")

    # ── Listing ────────────────────────────────────────────────────────────

    def list_files(self, remote_path: str = "") -> list[RemoteFile]:
        """Return all non-folder files under *remote_path* (folder name or ID)."""
        self._ensure_authenticated()
        parent_id = self._resolve_folder(remote_path) if remote_path else "root"
        results: list[RemoteFile] = []
        page_token = None

        while True:
            query = (
                f"'{parent_id}' in parents and "
                f"mimeType != '{_MIME_FOLDER}' and trashed = false"
            )
            resp = (
                self._service.files()
                .list(
                    q=query,
                    fields="nextPageToken, files(id, name, modifiedTime, size, md5Checksum)",
                    pageToken=page_token,
                )
                .execute()
            )
            for f in resp.get("files", []):
                mtime = self._parse_gdrive_time(f.get("modifiedTime", ""))
                results.append(
                    RemoteFile(
                        remote_id=f["id"],
                        name=f["name"],
                        path=f["name"],
                        mtime=mtime,
                        size=int(f.get("size", 0)),
                        content_hash=f.get("md5Checksum", ""),
                    )
                )
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        return results

    def get_file_metadata(self, remote_id: str) -> Optional[RemoteFile]:
        self._ensure_authenticated()
        try:
            f = (
                self._service.files()
                .get(
                    fileId=remote_id,
                    fields="id, name, modifiedTime, size, md5Checksum",
                )
                .execute()
            )
            mtime = self._parse_gdrive_time(f.get("modifiedTime", ""))
            return RemoteFile(
                remote_id=f["id"],
                name=f["name"],
                path=f["name"],
                mtime=mtime,
                size=int(f.get("size", 0)),
                content_hash=f.get("md5Checksum", ""),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("GDrive get_file_metadata failed: %s", exc)
            return None

    # ── Transfer ───────────────────────────────────────────────────────────

    def upload_file(
        self,
        local_path: Path,
        remote_path: str,
        remote_id: Optional[str] = None,
        chunk_size: int = 5 * 1024 * 1024,
    ) -> RemoteFile:
        from googleapiclient.http import MediaFileUpload  # type: ignore[import-not-found]

        self._ensure_authenticated()
        file_size = local_path.stat().st_size
        use_resumable = file_size > chunk_size

        media = MediaFileUpload(
            str(local_path),
            resumable=use_resumable,
            chunksize=chunk_size,
        )

        if remote_id:
            # Update existing file
            request = self._service.files().update(
                fileId=remote_id,
                media_body=media,
                fields="id, name, modifiedTime, size, md5Checksum",
            )
        else:
            name = Path(remote_path).name
            metadata = {"name": name}
            request = self._service.files().create(
                body=metadata,
                media_body=media,
                fields="id, name, modifiedTime, size, md5Checksum",
            )

        if use_resumable:
            response = None
            while response is None:
                status, response = request.next_chunk()
                if status:
                    logger.debug(
                        "GDrive upload %s: %.0f%%",
                        local_path.name,
                        status.progress() * 100,
                    )
        else:
            response = request.execute()

        logger.info("Uploaded %s → Google Drive (%s)", local_path.name, response["id"])
        return RemoteFile(
            remote_id=response["id"],
            name=response["name"],
            path=remote_path,
            mtime=self._parse_gdrive_time(response.get("modifiedTime", "")),
            size=int(response.get("size", 0)),
            content_hash=response.get("md5Checksum", ""),
        )

    def download_file(
        self,
        remote_id: str,
        local_path: Path,
        chunk_size: int = 5 * 1024 * 1024,
    ) -> None:
        from googleapiclient.http import MediaIoBaseDownload  # type: ignore[import-not-found]

        self._ensure_authenticated()
        local_path.parent.mkdir(parents=True, exist_ok=True)
        request = self._service.files().get_media(fileId=remote_id)

        with open(local_path, "wb") as fh:
            downloader = MediaIoBaseDownload(fh, request, chunksize=chunk_size)
            done = False
            while not done:
                status, done = downloader.next_chunk()
                if status:
                    logger.debug(
                        "GDrive download %s: %.0f%%",
                        local_path.name,
                        status.progress() * 100,
                    )

        logger.info("Downloaded Google Drive %s → %s", remote_id, local_path)

    def delete_file(self, remote_id: str) -> None:
        self._ensure_authenticated()
        self._service.files().delete(fileId=remote_id).execute()
        logger.info("Deleted Google Drive file %s", remote_id)

    def move_file(self, remote_id: str, new_remote_path: str) -> RemoteFile:
        self._ensure_authenticated()
        new_name = Path(new_remote_path).name
        f = (
            self._service.files()
            .update(
                fileId=remote_id,
                body={"name": new_name},
                fields="id, name, modifiedTime, size, md5Checksum",
            )
            .execute()
        )
        return RemoteFile(
            remote_id=f["id"],
            name=f["name"],
            path=new_remote_path,
            mtime=self._parse_gdrive_time(f.get("modifiedTime", "")),
            size=int(f.get("size", 0)),
            content_hash=f.get("md5Checksum", ""),
        )

    # ── Convenience ───────────────────────────────────────────────────────

    @property
    def provider_name(self) -> str:
        return "gdrive"

    # ── Internal helpers ───────────────────────────────────────────────────

    def _ensure_authenticated(self) -> None:
        if self._service is None:
            raise RuntimeError(
                "Not authenticated.  Call GoogleDriveProvider.authenticate() first."
            )

    def _resolve_folder(self, folder_name_or_id: str) -> str:
        """Return the Drive file ID for *folder_name_or_id*.

        If the value already looks like a Drive file ID it is returned as-is;
        otherwise we search by name.
        """
        # Drive IDs are typically 33-char base64url strings – heuristic only
        if len(folder_name_or_id) > 25 and " " not in folder_name_or_id:
            return folder_name_or_id

        resp = (
            self._service.files()
            .list(
                q=(
                    f"name='{folder_name_or_id}' and "
                    f"mimeType='{_MIME_FOLDER}' and trashed=false"
                ),
                fields="files(id)",
            )
            .execute()
        )
        files = resp.get("files", [])
        if not files:
            raise ValueError(f"Google Drive folder not found: {folder_name_or_id!r}")
        return files[0]["id"]

    @staticmethod
    def _parse_gdrive_time(ts: str) -> float:
        """Convert RFC 3339 timestamp to Unix epoch float."""
        if not ts:
            return 0.0
        from datetime import datetime, timezone

        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).timestamp()
