"""Shared OAuth utilities.

Provider-specific auth logic lives inside each provider module.  This module
contains helpers used across providers: a lightweight local HTTP redirect
receiver and secure token-file I/O.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

# ── Secure token storage ───────────────────────────────────────────────────


def save_token(token_file: Path, token_data: dict) -> None:
    """Write *token_data* to *token_file* with restricted permissions (0o600)."""
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(json.dumps(token_data, indent=2))
    token_file.chmod(0o600)


def load_token(token_file: Path) -> Optional[dict]:
    """Load token data from *token_file* or return ``None`` if not found."""
    if not token_file.exists():
        return None
    try:
        return json.loads(token_file.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not load token file %s: %s", token_file, exc)
        return None


# ── Local redirect receiver ────────────────────────────────────────────────

_DEFAULT_REDIRECT_PORT_RANGE = range(8080, 8090)


def find_free_port(port_range=_DEFAULT_REDIRECT_PORT_RANGE) -> int:
    """Return the first available TCP port in *port_range*."""
    for port in port_range:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise OSError("No free port found in range")


class _OAuthCallbackHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler that captures the OAuth redirect code parameter."""

    code: Optional[str] = None
    error: Optional[str] = None
    _event: threading.Event

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        if "code" in params:
            _OAuthCallbackHandler.code = params["code"][0]
            msg = b"<html><body><h2>Authorisation successful. You may close this tab.</h2></body></html>"
        else:
            _OAuthCallbackHandler.error = params.get("error", ["unknown"])[0]
            msg = b"<html><body><h2>Authorisation failed. Check the terminal.</h2></body></html>"

        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(msg)))
        self.end_headers()
        self.wfile.write(msg)
        _OAuthCallbackHandler._event.set()

    def log_message(self, *args):  # type: ignore[override]
        pass  # suppress request logging


def wait_for_oauth_redirect(port: int, timeout: float = 120.0) -> str:
    """Start a local HTTP server and block until the OAuth redirect arrives.

    Returns the ``code`` query parameter value.
    Raises :exc:`TimeoutError` if no redirect arrives within *timeout* seconds.
    """
    event = threading.Event()
    _OAuthCallbackHandler._event = event
    _OAuthCallbackHandler.code = None
    _OAuthCallbackHandler.error = None

    server = HTTPServer(("127.0.0.1", port), _OAuthCallbackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        if not event.wait(timeout=timeout):
            raise TimeoutError(
                f"OAuth redirect not received within {timeout:.0f} seconds"
            )
        if _OAuthCallbackHandler.error:
            raise PermissionError(
                f"OAuth authorisation denied: {_OAuthCallbackHandler.error}"
            )
        return _OAuthCallbackHandler.code  # type: ignore[return-value]
    finally:
        server.shutdown()


# ── PKCE helpers ───────────────────────────────────────────────────────────


def generate_pkce_pair() -> tuple[str, str]:
    """Return ``(code_verifier, code_challenge)`` for PKCE flows."""
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    return verifier, challenge
