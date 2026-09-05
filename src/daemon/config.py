from __future__ import annotations

import json
import os
import secrets
import socket
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

DEFAULT_API_BASE_URL = "https://api.neuralnexus.site"

# Canonical platform names the Neural Nexus API records for each machine.
# ``sys.platform`` reports the interpreter's build target ("linux", "darwin",
# "win32"), which is not what a person calls their machine, so it is normalized
# once here rather than at each call site.
_PLATFORM_BY_SYSTEM_PREFIX = (
    ("linux", "ubuntu"),
    ("darwin", "macos"),
    ("win", "windows"),
)


def detect_platform() -> str:
    """Canonical platform name for this machine, for the API to record."""
    system = sys.platform.lower()
    for prefix, canonical_platform in _PLATFORM_BY_SYSTEM_PREFIX:
        if system.startswith(prefix):
            return canonical_platform
    return "unknown"


def detect_device_label() -> str:
    """Default human-readable name for this machine.

    The hostname is used rather than the platform name because a user may run
    the daemon on two machines of the SAME platform: "evans-macbook" and
    "evans-mac-mini" are distinguishable when the avatar reports which machine a
    file came from, whereas two machines both called "macOS" are not. The API
    falls back to a platform-derived name when no label is sent, so this is an
    improvement on that fallback rather than a requirement for connecting.
    """
    hostname = ""
    try:
        hostname = (socket.gethostname() or "").strip()
    except OSError:
        hostname = ""
    # Drop any domain suffix (macOS hostnames commonly end in ".local") so the
    # label stays short enough to say out loud in a reply.
    hostname = hostname.split(".", 1)[0]
    return hostname or detect_platform()


def is_placeholder_api_url(url: str) -> bool:
    """True for test/invalid API hosts that should not ship in user configs."""
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return True
    if host in {"api.example.test", "example.test", "localhost", "127.0.0.1"}:
        return True
    return host.endswith(".test") or host.endswith(".invalid")


def resolve_api_base_url(configured: str | None = None) -> str:
    return (
        os.getenv("NEURALNEXUS_API_BASE_URL")
        or configured
        or DEFAULT_API_BASE_URL
    ).rstrip("/")


def resolve_existing_directory(root: str) -> str:
    """Expand a shared-folder path, rejecting anything that is not a directory.

    A folder that does not exist would still be announced to the avatar as a
    shared root, and every tool call against it would then fail on the
    is_dir() check inside the MCP server.
    """
    resolved = Path(root).expanduser().resolve()
    if not resolved.is_dir():
        raise ValueError(f"Not a directory: {root}")
    return str(resolved)


def config_dir() -> Path:
    """Resolve the config directory on every call.

    Resolved lazily rather than bound at import time so that
    NEURALNEXUS_MCP_CONFIG_DIR always wins — an import-time constant is
    captured before pytest fixtures can redirect it, which lets a test run
    overwrite the real user config.
    """
    override = os.getenv("NEURALNEXUS_MCP_CONFIG_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return Path(os.getenv("XDG_CONFIG_HOME", Path.home() / ".config")) / "neuralnexus-mcp"


def config_path() -> Path:
    return config_dir() / "config.json"


def credentials_path() -> Path:
    return config_dir() / "credentials.json"


@dataclass
class DaemonConfig:
    api_base_url: str = DEFAULT_API_BASE_URL
    watched_roots: list[str] = field(default_factory=list)
    device_secret: str | None = None
    device_id: str | None = None
    # Human-readable name for this machine, announced to the API so the avatar
    # can say WHICH machine a result came from when several are connected at
    # once. Persisted so a user who renames the machine keeps the chosen name
    # across restarts; defaults to the hostname on first run.
    device_label: str | None = None
    connection_mode: str = "relay"  # relay | local
    public_base_url: str | None = None
    local_port: int = 8000
    last_registered_at: str | None = None

    @classmethod
    def load(cls) -> DaemonConfig:
        path = config_path()
        if not path.exists():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        config = cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        return config.normalize()

    def normalize(self) -> DaemonConfig:
        """Migrate legacy configs (including removed tunnel mode) to the relay default."""
        changed = False
        if self.connection_mode not in {"relay", "local"}:
            self.connection_mode = "relay"
            changed = True
        if is_placeholder_api_url(self.api_base_url):
            resolved = resolve_api_base_url()
            if self.api_base_url != resolved:
                self.api_base_url = resolved
                changed = True
        if changed:
            self.save()
        return self

    def save(self) -> None:
        config_dir().mkdir(parents=True, exist_ok=True)
        config_path().write_text(
            json.dumps(asdict(self), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def ensure_device_identity(self) -> None:
        changed = False
        if not self.device_id:
            self.device_id = secrets.token_urlsafe(12)
            changed = True
        if not self.device_secret:
            self.device_secret = f"mcp_dev_{secrets.token_urlsafe(32)}"
            changed = True
        if not self.device_label:
            self.device_label = detect_device_label()
            changed = True
        if changed:
            self.save()

    def set_device_label(self, label: str) -> None:
        """Rename this machine as the avatar refers to it in conversation."""
        self.device_label = label.strip() or detect_device_label()
        self.save()

    def set_watched_roots(self, roots: list[str]) -> None:
        self.watched_roots = [resolve_existing_directory(root) for root in roots]
        self.save()

    def add_watched_root(self, root: str) -> None:
        resolved = resolve_existing_directory(root)
        if resolved not in self.watched_roots:
            self.watched_roots.append(resolved)
            self.save()

    def remove_watched_root(self, root: str) -> None:
        resolved = str(Path(root).expanduser().resolve())
        if resolved in self.watched_roots:
            self.watched_roots.remove(resolved)
            self.save()

    def primary_watch_root(self) -> str | None:
        return self.watched_roots[0] if self.watched_roots else None

    def mark_registered(self) -> None:
        self.last_registered_at = datetime.now(UTC).isoformat()
        self.save()

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "api_base_url": self.api_base_url,
            "connection_mode": self.connection_mode,
            "device_id": self.device_id,
            "device_label": self.device_label,
            "platform": detect_platform(),
            "watched_roots": self.watched_roots,
            "public_base_url": self.public_base_url,
            "local_port": self.local_port,
            "last_registered_at": self.last_registered_at,
            "has_api_key": credentials_path().exists(),
            "config_dir": str(config_dir()),
        }


@dataclass
class Credentials:
    api_key: str

    @staticmethod
    def is_placeholder_api_key(api_key: str) -> bool:
        return api_key in {"sk-test-key", "test", ""} or api_key.startswith("sk-test")

    @classmethod
    def load(cls) -> Credentials | None:
        path = credentials_path()
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        api_key = data.get("api_key")
        if not api_key or cls.is_placeholder_api_key(api_key):
            return None
        return cls(api_key=api_key)

    @classmethod
    def save_api_key(cls, api_key: str) -> None:
        config_dir().mkdir(parents=True, exist_ok=True)
        path = credentials_path()
        path.write_text(
            json.dumps({"api_key": api_key}, indent=2) + "\n",
            encoding="utf-8",
        )
        path.chmod(0o600)

    def clear(self) -> None:
        path = credentials_path()
        if path.exists():
            path.unlink()
