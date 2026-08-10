from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.daemon.config import (
    DEFAULT_API_BASE_URL,
    Credentials,
    DaemonConfig,
    config_path,
    credentials_path,
    is_placeholder_api_url,
)
from src.daemon.registrar import ApiRegistrar
from src.daemon.relay import relay_http_url, relay_ws_url


def test_daemon_config_round_trip(tmp_path: Path) -> None:
    watched_root = tmp_path / "data"
    watched_root.mkdir()

    config = DaemonConfig.load()
    config.api_base_url = "https://api.example.test"
    config.connection_mode = "relay"
    config.set_watched_roots([str(watched_root)])
    config.ensure_device_identity()
    config.save()

    reloaded = DaemonConfig.load()
    assert reloaded.api_base_url == "https://api.neuralnexus.site"
    assert reloaded.connection_mode == "relay"
    assert reloaded.watched_roots == [str(watched_root.resolve())]
    assert reloaded.device_id
    assert reloaded.device_secret.startswith("mcp_dev_")
    assert config_path().exists()


def test_config_writes_stay_inside_the_isolated_config_dir(tmp_path: Path) -> None:
    """Guards the conftest sandbox — a test must never touch the real config."""
    watched_root = tmp_path / "data"
    watched_root.mkdir()

    config = DaemonConfig.load()
    config.set_watched_roots([str(watched_root)])
    Credentials.save_api_key("sk-user-integration-key")

    real_config_dir = Path.home() / ".config" / "neuralnexus-mcp"
    assert real_config_dir not in config_path().parents
    assert real_config_dir not in credentials_path().parents


def test_watched_root_must_be_an_existing_directory(tmp_path: Path) -> None:
    config = DaemonConfig.load()
    missing_root = tmp_path / "not-created"
    a_file = tmp_path / "notes.txt"
    a_file.write_text("x", encoding="utf-8")

    with pytest.raises(ValueError, match="Not a directory"):
        config.add_watched_root(str(missing_root))
    with pytest.raises(ValueError, match="Not a directory"):
        config.add_watched_root(str(a_file))
    with pytest.raises(ValueError, match="Not a directory"):
        config.set_watched_roots([str(missing_root)])

    assert config.watched_roots == []


def test_credentials_saved_with_restrictive_permissions() -> None:
    Credentials.save_api_key("sk-user-integration-key")
    assert credentials_path().exists()
    assert oct(credentials_path().stat().st_mode & 0o777) == oct(0o600)
    loaded = Credentials.load()
    assert loaded is not None
    assert loaded.api_key == "sk-user-integration-key"


def test_relay_registration_payload_shape(tmp_path: Path) -> None:
    watched_root = tmp_path / "watch"
    watched_root.mkdir()
    config = DaemonConfig.load()
    config.api_base_url = "https://api.example.test"
    config.connection_mode = "relay"
    config.set_watched_roots([str(watched_root)])
    config.ensure_device_identity()

    payload = ApiRegistrar.build_payload(
        config=config,
        server_name="macOS-Filesystem",
        mcp_path="/mcp",
        allowed_roots=[str(watched_root.resolve())],
    )
    body = payload.to_json()
    assert body["connection_mode"] == "relay"
    assert body["transport"] == "relay"
    assert body["mcp_url"] == relay_http_url("https://api.example.test", config.device_id or "")
    assert "discovery_url" not in body
    assert body["allowed_roots"] == [str(watched_root.resolve())]
    assert json.loads(json.dumps(body)) == body


def test_registration_announces_served_roots_not_raw_config(tmp_path: Path) -> None:
    """Only folders the MCP tools will actually serve reach the avatar."""
    served_root = tmp_path / "served"
    vanished_root = tmp_path / "vanished"
    served_root.mkdir()
    vanished_root.mkdir()

    config = DaemonConfig.load()
    config.connection_mode = "relay"
    config.set_watched_roots([str(vanished_root), str(served_root)])
    config.ensure_device_identity()
    vanished_root.rmdir()

    payload = ApiRegistrar.build_payload(
        config=config,
        server_name="macOS-Filesystem",
        mcp_path="/mcp",
        allowed_roots=[str(served_root.resolve())],
    )
    assert payload.to_json()["allowed_roots"] == [str(served_root.resolve())]


def test_local_registration_payload_shape(tmp_path: Path) -> None:
    watched_root = tmp_path / "watch"
    watched_root.mkdir()
    config = DaemonConfig.load()
    config.connection_mode = "local"
    config.set_watched_roots([str(watched_root)])
    config.ensure_device_identity()

    payload = ApiRegistrar.build_payload(
        config=config,
        server_name="macOS-Filesystem",
        mcp_path="/mcp",
        allowed_roots=[str(watched_root.resolve())],
    )
    body = payload.to_json()
    assert body["transport"] == "streamable_http"
    assert body["mcp_url"] == "http://127.0.0.1:8000/mcp"
    assert body["discovery_url"] == "http://127.0.0.1:8000/discovery"


def test_legacy_tunnel_config_migrates_to_relay(tmp_path: Path) -> None:
    watched_root = tmp_path / "data"
    watched_root.mkdir()
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text(
        json.dumps(
            {
                "api_base_url": "https://api.neuralnexus.site",
                "connection_mode": "tunnel",
                "tunnel_mode": "auto",
                "watched_roots": [str(watched_root)],
            }
        ),
        encoding="utf-8",
    )

    config = DaemonConfig.load()
    assert config.connection_mode == "relay"


def test_placeholder_api_key_is_ignored() -> None:
    Credentials.save_api_key("sk-test-key")
    assert Credentials.load() is None


def test_placeholder_api_url_migrates_to_production() -> None:
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text(
        json.dumps({"api_base_url": "https://api.example.test"}),
        encoding="utf-8",
    )

    config = DaemonConfig.load()
    assert config.api_base_url == DEFAULT_API_BASE_URL
    assert not is_placeholder_api_url(config.api_base_url)


def test_relay_ws_url() -> None:
    assert relay_ws_url("https://api.neuralnexus.site") == "wss://api.neuralnexus.site/mcp/relay"
    assert relay_ws_url("http://localhost:8000") == "ws://localhost:8000/mcp/relay"
