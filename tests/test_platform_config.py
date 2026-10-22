from __future__ import annotations

import os
from pathlib import Path

import pytest

from ops import platform_config


def test_origins_are_exact_and_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("D3M0N_ALLOWED_ORIGINS", " HTTP://LOCALHOST:5055/ , https://Example.test:443 ")
    monkeypatch.delenv("D3M0N_PUBLIC_ORIGIN", raising=False)
    assert platform_config.get_allowed_origins() == frozenset({"http://localhost:5055", "https://example.test"})


def test_http_origin_requires_local_or_private_host() -> None:
    assert platform_config.normalize_origin("http://192.168.50.20:5055") == "http://192.168.50.20:5055"
    assert platform_config.normalize_origin("http://localhost:5055") == "http://localhost:5055"
    with pytest.raises(ValueError):
        platform_config.normalize_origin("http://public.example:5055")
    with pytest.raises(ValueError):
        platform_config.normalize_origin("*")


def test_public_origin_is_explicit_https_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("D3M0N_PUBLIC_ORIGIN", "https://dashboard.example:443/")
    assert platform_config.get_public_origin() == "https://dashboard.example"
    monkeypatch.setenv("D3M0N_PUBLIC_ORIGIN", "http://192.168.50.20:5055")
    with pytest.raises(ValueError):
        platform_config.get_public_origin()


def test_state_directory_never_defaults_to_checkout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("D3M0N_STATE_DIR", str(tmp_path / "private-state"))
    assert platform_config.state_dir() == tmp_path / "private-state"
    assert "repo" not in str(platform_config.state_dir()).lower()


def test_atomic_private_json_write_round_trips(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(platform_config, "_windows_private_acl", lambda *args, **kwargs: None)
    path = tmp_path / "state" / "record.json"
    platform_config.atomic_write_json(path, {"version": 1, "kind": "synthetic"})
    assert path.read_text(encoding="utf-8") == '{\n  "kind": "synthetic",\n  "version": 1\n}\n'
    if os.name != "nt":
        assert path.stat().st_mode & 0o077 == 0
