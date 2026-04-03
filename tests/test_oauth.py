"""Tests for raspiboxdrive.auth.oauth helpers."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from raspiboxdrive.auth.oauth import (
    generate_pkce_pair,
    load_token,
    save_token,
)


class TestTokenStorage:
    def test_save_and_load(self, tmp_path: Path):
        token_file = tmp_path / "tokens" / "test.json"
        data = {"access_token": "tok", "refresh_token": "rtok"}
        save_token(token_file, data)
        loaded = load_token(token_file)
        assert loaded == data

    def test_save_sets_permissions(self, tmp_path: Path):
        token_file = tmp_path / "test.json"
        save_token(token_file, {"key": "val"})
        mode = oct(os.stat(token_file).st_mode)[-3:]
        assert mode == "600"

    def test_load_missing_file_returns_none(self, tmp_path: Path):
        assert load_token(tmp_path / "nonexistent.json") is None

    def test_load_invalid_json_returns_none(self, tmp_path: Path):
        bad = tmp_path / "bad.json"
        bad.write_text("not json{{{")
        assert load_token(bad) is None


class TestPKCE:
    def test_returns_two_non_empty_strings(self):
        verifier, challenge = generate_pkce_pair()
        assert isinstance(verifier, str) and verifier
        assert isinstance(challenge, str) and challenge

    def test_verifier_and_challenge_differ(self):
        verifier, challenge = generate_pkce_pair()
        assert verifier != challenge

    def test_unique_per_call(self):
        v1, c1 = generate_pkce_pair()
        v2, c2 = generate_pkce_pair()
        assert v1 != v2
        assert c1 != c2

    def test_challenge_is_base64url(self):
        import re
        _, challenge = generate_pkce_pair()
        # Base64URL characters only, no padding
        assert re.fullmatch(r"[A-Za-z0-9\-_]+", challenge)
