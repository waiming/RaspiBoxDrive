"""Command-line interface for RaspiBoxDrive.

Usage examples::

    raspibox-drive start --provider gdrive
    raspibox-drive start --provider dropbox
    raspibox-drive start --provider both
    raspibox-drive auth gdrive
    raspibox-drive auth dropbox
    raspibox-drive status
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

import click

from raspiboxdrive.config import Config, get_config

logger = logging.getLogger(__name__)


# ── Provider factory ─────────────────────────────────────────────────────


def _make_gdrive_provider(cfg: Config):
    from raspiboxdrive.providers.gdrive import GoogleDriveProvider

    return GoogleDriveProvider(
        credentials_file=cfg.gdrive_credentials_file,
        token_file=cfg.gdrive_token_file,
        chunk_size=cfg.chunk_size,
    )


def _make_dropbox_provider(cfg: Config):
    from raspiboxdrive.providers.dropbox_provider import DropboxProvider

    if not cfg.dropbox_app_key or not cfg.dropbox_app_secret:
        raise click.ClickException(
            "Dropbox app key/secret not configured.  "
            "Set RASPIBOX_DROPBOX_APP_KEY and RASPIBOX_DROPBOX_APP_SECRET."
        )
    return DropboxProvider(
        app_key=cfg.dropbox_app_key,
        app_secret=cfg.dropbox_app_secret,
        token_file=cfg.dropbox_token_file,
    )


def _build_providers(provider_name: str, cfg: Config) -> list:
    providers = []
    if provider_name in ("gdrive", "both"):
        providers.append(_make_gdrive_provider(cfg))
    if provider_name in ("dropbox", "both"):
        providers.append(_make_dropbox_provider(cfg))
    return providers


# ── CLI root ──────────────────────────────────────────────────────────────


@click.group()
@click.version_option(package_name="raspibox-drive")
def main() -> None:
    """RaspiBoxDrive – cloud storage sync for Raspberry Pi."""


# ── start ─────────────────────────────────────────────────────────────────


@main.command()
@click.option(
    "--provider",
    default=None,
    type=click.Choice(["gdrive", "dropbox", "both"]),
    help="Cloud provider to sync with (overrides RASPIBOX_PROVIDER).",
)
@click.option(
    "--sync-dir",
    default=None,
    type=click.Path(),
    help="Local directory to sync (overrides RASPIBOX_SYNC_DIR).",
)
@click.option(
    "--log-level",
    default=None,
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False),
    help="Logging verbosity.",
)
def start(
    provider: Optional[str],
    sync_dir: Optional[str],
    log_level: Optional[str],
) -> None:
    """Start the sync daemon (blocking)."""
    cfg = get_config()
    if provider:
        cfg.provider = provider  # type: ignore[assignment]
    if sync_dir:
        cfg.sync_dir = Path(sync_dir)
    if log_level:
        cfg.log_level = log_level

    cfg.ensure_dirs()
    click.echo(
        f"Starting RaspiBoxDrive\n"
        f"  Sync dir : {cfg.sync_dir}\n"
        f"  Provider : {cfg.provider}\n"
        f"  Data dir : {cfg.data_dir}\n"
    )

    providers = _build_providers(cfg.provider, cfg)
    if not providers:
        raise click.ClickException("No providers configured.")

    from raspiboxdrive.daemon import SyncDaemon

    daemon = SyncDaemon(config=cfg, providers=providers)
    daemon.start()


# ── auth ─────────────────────────────────────────────────────────────────


@main.command()
@click.argument("provider_name", metavar="PROVIDER", type=click.Choice(["gdrive", "dropbox"]))
def auth(provider_name: str) -> None:
    """Authenticate with a cloud provider and save the token."""
    cfg = get_config()
    cfg.ensure_dirs()

    if provider_name == "gdrive":
        prov = _make_gdrive_provider(cfg)
    else:
        prov = _make_dropbox_provider(cfg)

    try:
        prov.authenticate()
        click.echo(f"✓ Authenticated with {provider_name} successfully.")
    except Exception as exc:
        raise click.ClickException(f"Authentication failed: {exc}") from exc


# ── status ────────────────────────────────────────────────────────────────


@main.command()
@click.option(
    "--provider",
    default=None,
    type=click.Choice(["gdrive", "dropbox"]),
    help="Filter by provider.",
)
def status(provider: Optional[str]) -> None:
    """Show current sync status from the local state database."""
    from raspiboxdrive.database import StateDatabase

    cfg = get_config()
    if not cfg.db_path.exists():
        click.echo("No state database found – has the daemon been run yet?")
        return

    db = StateDatabase(cfg.db_path)
    providers = [provider] if provider else ["gdrive", "dropbox"]

    for prov in providers:
        records = db.list_all(prov)
        if not records:
            continue
        click.echo(f"\n{'='*60}")
        click.echo(f"  Provider: {prov.upper()}")
        click.echo(f"  Files tracked: {len(records)}")
        click.echo(f"{'='*60}")

        by_status: dict[str, list] = {}
        for r in records:
            by_status.setdefault(r.sync_status, []).append(r)

        for st, recs in sorted(by_status.items()):
            click.echo(f"\n  [{st.upper()}] ({len(recs)} files)")
            for r in recs[:20]:
                click.echo(f"    {r.path}")
            if len(recs) > 20:
                click.echo(f"    … and {len(recs) - 20} more")

    db.close()


# ── ls-remote ─────────────────────────────────────────────────────────────


@main.command("ls-remote")
@click.argument("provider_name", metavar="PROVIDER", type=click.Choice(["gdrive", "dropbox"]))
@click.option("--path", "remote_path", default="", help="Remote folder path.")
def ls_remote(provider_name: str, remote_path: str) -> None:
    """List files currently stored in the cloud provider."""
    cfg = get_config()
    cfg.ensure_dirs()

    if provider_name == "gdrive":
        prov = _make_gdrive_provider(cfg)
    else:
        prov = _make_dropbox_provider(cfg)

    prov.authenticate()
    files = prov.list_files(remote_path)

    if not files:
        click.echo("No files found.")
        return

    click.echo(f"{'NAME':<40} {'SIZE':>12}  {'MODIFIED'}")
    click.echo("-" * 72)
    for f in files:
        from datetime import datetime, timezone

        mtime = datetime.fromtimestamp(f.mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        click.echo(f"{f.name:<40} {f.size:>12,}  {mtime}")


if __name__ == "__main__":
    main()
