from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastmcp.exceptions import ResourceError

from src.server.app import _resolve_allowed_dir, build_server_settings
from src.server.settings import ServerSettings, resolve_watched_roots


def test_resolve_watched_roots_deduplicates_and_drops_blanks(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    first_root.mkdir()
    roots = resolve_watched_roots([str(first_root), "", f"  {first_root}  "])
    assert roots == (first_root.resolve(),)


def test_settings_from_env_reads_multiple_watched_roots(
    tmp_path: Path, monkeypatch
) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    monkeypatch.setenv(
        "MCP_WATCHED_ROOTS", os.pathsep.join([str(first_root), str(second_root)])
    )

    settings = ServerSettings.from_env()
    assert settings.watched_roots == (first_root.resolve(), second_root.resolve())
    assert settings.allowed_roots == settings.watched_roots


def test_settings_from_env_falls_back_to_health_data_dir(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("MCP_WATCHED_ROOTS", raising=False)
    monkeypatch.setenv("HEALTH_DATA_DIR", str(tmp_path))

    settings = ServerSettings.from_env()
    assert settings.watched_roots == (tmp_path.resolve(),)


def test_build_server_settings_keeps_every_watched_root(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("MCP_WATCHED_ROOTS", raising=False)
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()

    settings = build_server_settings(
        watched_roots=[str(first_root), str(second_root)],
        require_device_auth=False,
    )
    assert settings.allowed_roots == (first_root.resolve(), second_root.resolve())


def test_resolve_allowed_dir_accepts_any_configured_root(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    outside_root = tmp_path / "outside"
    first_root.mkdir()
    second_root.mkdir()
    outside_root.mkdir()
    allowed_roots = (first_root.resolve(), second_root.resolve())

    assert _resolve_allowed_dir(str(first_root), allowed_roots) == first_root.resolve()
    assert (
        _resolve_allowed_dir(str(second_root), allowed_roots) == second_root.resolve()
    )
    with pytest.raises(ResourceError):
        _resolve_allowed_dir(str(outside_root), allowed_roots)


def test_allowed_roots_drops_a_folder_that_disappeared(tmp_path: Path) -> None:
    """A configured folder that vanishes is not served and not announced."""
    present_root = tmp_path / "present"
    vanished_root = tmp_path / "vanished"
    present_root.mkdir()
    vanished_root.mkdir()

    settings = build_server_settings(
        watched_roots=[str(vanished_root), str(present_root)],
        require_device_auth=False,
    )
    assert settings.allowed_roots == (vanished_root.resolve(), present_root.resolve())

    vanished_root.rmdir()
    assert settings.watched_roots == (vanished_root.resolve(), present_root.resolve())
    assert settings.allowed_roots == (present_root.resolve(),)


def test_empty_allowed_roots_denies_every_path(tmp_path: Path) -> None:
    """An empty allow-list must never fall through to allowing the filesystem."""
    some_dir = tmp_path / "anywhere"
    some_dir.mkdir()

    with pytest.raises(ResourceError, match="No shared folders are configured"):
        _resolve_allowed_dir(str(some_dir), ())
