"""Background daemon that ties together the watcher, sync engine, and
remote-polling loop.

The :class:`SyncDaemon` class can be embedded in a script or run directly;
the accompanying :mod:`raspiboxdrive.cli` provides a ``start`` command that
calls it.  A systemd unit file in ``systemd/raspiboxdrive.service`` manages
long-running operation on a Raspberry Pi.
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from typing import Optional

from raspiboxdrive.config import Config, get_config
from raspiboxdrive.database import StateDatabase
from raspiboxdrive.providers.base import CloudProvider
from raspiboxdrive.sync_engine import SyncEngine
from raspiboxdrive.watcher import FileEvent, FileWatcher

logger = logging.getLogger(__name__)


def _setup_logging(config: Config) -> None:
    """Configure root-level logging to file + console."""
    config.data_dir.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
    handlers: list[logging.Handler] = [
        logging.StreamHandler(),
        logging.FileHandler(str(config.log_file)),
    ]
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format=fmt,
        handlers=handlers,
        force=True,
    )


class SyncDaemon:
    """Orchestrates file watching and bidirectional sync for one or more providers.

    Usage::

        daemon = SyncDaemon(config, providers=[gdrive_provider])
        daemon.start()   # blocks until stopped

    Or for non-blocking use in tests / embedding::

        daemon.start_background()
        …
        daemon.stop()
    """

    def __init__(
        self,
        config: Optional[Config] = None,
        providers: Optional[list[CloudProvider]] = None,
    ) -> None:
        self._cfg = config or get_config()
        self._cfg.ensure_dirs()
        _setup_logging(self._cfg)

        self._providers = providers or []
        self._db = StateDatabase(self._cfg.db_path)
        self._watcher = FileWatcher(self._cfg.sync_dir)
        self._engines: list[SyncEngine] = [
            SyncEngine(self._cfg, p, self._db, self._watcher)
            for p in self._providers
        ]

        self._stop_event = threading.Event()
        self._threads: list[threading.Thread] = []

    # ── Add providers after construction ──────────────────────────────────

    def add_provider(self, provider: CloudProvider) -> None:
        """Attach an additional cloud provider at run-time."""
        self._providers.append(provider)
        self._engines.append(SyncEngine(self._cfg, provider, self._db, self._watcher))

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the daemon and block until a SIGTERM/SIGINT is received."""
        self._register_signals()
        self._boot()
        logger.info("RaspiBoxDrive daemon running.  Press Ctrl+C to stop.")
        self._stop_event.wait()
        self._shutdown()

    def start_background(self) -> None:
        """Start the daemon in background threads (non-blocking)."""
        self._boot()

    def stop(self) -> None:
        """Signal the daemon to stop."""
        self._stop_event.set()

    # ── Internal ──────────────────────────────────────────────────────────

    def _boot(self) -> None:
        if not self._engines:
            logger.warning("No providers configured – daemon is idle.")

        # Authenticate all providers
        for engine in self._engines:
            try:
                engine._provider.authenticate()
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Authentication failed for %s: %s",
                    engine._provider.provider_name,
                    exc,
                )

        # Initial full sync
        for engine in self._engines:
            t = threading.Thread(
                target=self._safe_run,
                args=(engine.initial_sync,),
                daemon=True,
                name=f"initial-sync-{engine._provider.provider_name}",
            )
            t.start()
            self._threads.append(t)

        # Start file watcher
        self._watcher.start()

        # Event dispatch loop
        dispatch_thread = threading.Thread(
            target=self._event_loop,
            daemon=True,
            name="event-dispatch",
        )
        dispatch_thread.start()
        self._threads.append(dispatch_thread)

        # Remote polling loop
        poll_thread = threading.Thread(
            target=self._poll_loop,
            daemon=True,
            name="remote-poll",
        )
        poll_thread.start()
        self._threads.append(poll_thread)

        logger.info("RaspiBoxDrive daemon started")

    def _event_loop(self) -> None:
        """Dispatch local filesystem events to each engine."""
        while not self._stop_event.is_set():
            event: Optional[FileEvent] = self._watcher.get_event(timeout=1.0)
            if event is None:
                continue
            for engine in self._engines:
                try:
                    engine.handle_local_event(event)
                except Exception as exc:  # noqa: BLE001
                    logger.exception(
                        "Unhandled error in event loop (%s): %s",
                        engine._provider.provider_name,
                        exc,
                    )

    def _poll_loop(self) -> None:
        """Periodically poll for remote changes."""
        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=float(self._cfg.poll_interval))
            if self._stop_event.is_set():
                break
            for engine in self._engines:
                self._safe_run(engine.run_remote_poll)

    def _shutdown(self) -> None:
        logger.info("Shutting down RaspiBoxDrive …")
        self._watcher.stop()
        self._db.close()
        logger.info("RaspiBoxDrive stopped")

    def _register_signals(self) -> None:
        signal.signal(signal.SIGTERM, lambda *_: self._stop_event.set())
        signal.signal(signal.SIGINT, lambda *_: self._stop_event.set())

    @staticmethod
    def _safe_run(fn) -> None:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unhandled error in %s: %s", fn.__name__, exc)
