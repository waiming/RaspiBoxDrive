# RaspiBoxDrive

A background sync daemon for Raspberry Pi that continuously mirrors a local
folder to **Google Drive** and/or **Dropbox** – similar to Google Drive for
Desktop or Dropbox's desktop app, but running headlessly on a Raspberry Pi.

---

## Features

| Feature | Details |
|---|---|
| **Real-time file watching** | Uses `inotify` (Linux kernel) via `inotify_simple`, falls back to `watchdog` for portability |
| **Bidirectional sync** | Uploads local changes; downloads / deletes files changed remotely |
| **Conflict resolution** | Detects edits on both sides; keeps a *conflicted copy* of the local version |
| **Large-file support** | Resumable / chunked uploads via Google Drive resumable sessions and Dropbox session API |
| **Local state database** | SQLite tracks file paths, hashes, mtimes, and sync status |
| **Exponential-backoff retry** | Transient network errors are retried automatically |
| **OAuth 2.0** | Full OAuth flow for both providers; tokens stored with `0o600` permissions |
| **Systemd service** | Installs as a per-user systemd service for automatic startup |
| **CLI** | `raspibox-drive start / auth / status / ls-remote` |
| **Structured logging** | Writes to file + console; configurable log level |

---

## Quick Start

### 1. Install

```bash
# In a virtual environment (recommended)
python -m venv venv && source venv/bin/activate

pip install -e .
```

### 2. Configure

The daemon is configured via environment variables (or a `.env` file placed in
`~/.raspiboxdrive/`):

| Variable | Default | Description |
|---|---|---|
| `RASPIBOX_SYNC_DIR` | `~/RaspiBoxDrive` | Local folder to sync |
| `RASPIBOX_PROVIDER` | `gdrive` | `gdrive`, `dropbox`, or `both` |
| `RASPIBOX_DATA_DIR` | `~/.raspiboxdrive` | Data / token storage |
| `RASPIBOX_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `RASPIBOX_POLL_INTERVAL` | `30` | Remote poll interval (seconds) |
| `RASPIBOX_CHUNK_SIZE` | `5242880` | Upload chunk size (bytes) |
| `RASPIBOX_MAX_RETRIES` | `5` | Max retry attempts per API call |
| `RASPIBOX_GDRIVE_CREDENTIALS` | `~/.raspiboxdrive/gdrive_credentials.json` | OAuth credentials file |
| `RASPIBOX_DROPBOX_APP_KEY` | – | Dropbox app key |
| `RASPIBOX_DROPBOX_APP_SECRET` | – | Dropbox app secret |

### 3. Authenticate

#### Google Drive

1. Create a project in [Google Cloud Console](https://console.cloud.google.com/).
2. Enable the **Drive API**.
3. Create an **OAuth 2.0 Desktop client** and download `credentials.json` to
   `~/.raspiboxdrive/gdrive_credentials.json`.
4. Run:

```bash
raspibox-drive auth gdrive
```

This opens a browser window (or prints a URL for headless Pi); authorise the
app and the token is saved automatically.

#### Dropbox

1. Create an app in the [Dropbox App Console](https://www.dropbox.com/developers/apps).
2. Set `RASPIBOX_DROPBOX_APP_KEY` and `RASPIBOX_DROPBOX_APP_SECRET`.
3. Run:

```bash
raspibox-drive auth dropbox
```

### 4. Start syncing

```bash
raspibox-drive start --provider gdrive
```

Or sync both providers simultaneously:

```bash
raspibox-drive start --provider both
```

### 5. Check status

```bash
raspibox-drive status
raspibox-drive ls-remote gdrive
```

---

## Systemd (auto-start on boot)

```bash
# Copy the unit file to the user systemd directory
cp systemd/raspiboxdrive@.service ~/.config/systemd/user/raspiboxdrive.service

# Enable and start
systemctl --user enable raspiboxdrive
systemctl --user start  raspiboxdrive

# Follow logs
journalctl --user -u raspiboxdrive -f
```

---

## Architecture

```
RaspiBoxDrive/
├── raspiboxdrive/
│   ├── config.py           # Environment-driven configuration
│   ├── database.py         # SQLite state store (path, hash, status)
│   ├── watcher.py          # inotify / watchdog file-change detection
│   ├── sync_engine.py      # Two-way reconciliation + conflict resolution
│   ├── daemon.py           # Threading / signal handling / polling loop
│   ├── cli.py              # Click CLI (start, auth, status, ls-remote)
│   ├── auth/
│   │   └── oauth.py        # PKCE helpers, token I/O
│   └── providers/
│       ├── base.py         # CloudProvider abstract interface
│       ├── gdrive.py       # Google Drive implementation
│       └── dropbox_provider.py  # Dropbox implementation
├── tests/                  # pytest test suite (49 tests)
├── systemd/                # systemd unit file
├── pyproject.toml
└── requirements.txt
```

### Sync decision logic

```
For each file seen locally or remotely:

  local_changed  = sha256(local_file) != db.local_hash
  remote_changed = remote_file.content_hash != db.remote_hash

  if  local_changed and  remote_changed → conflict (keep both, upload remote)
  if  local_changed and !remote_changed → upload
  if !local_changed and  remote_changed → download
  if !local_changed and !remote_changed → no-op
```

---

## Development

```bash
pip install -e ".[dev]"
pytest                         # run all 49 tests
pytest --cov=raspiboxdrive     # with coverage
```

---

## License

MIT
