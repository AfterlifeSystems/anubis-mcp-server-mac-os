from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

DEFAULT_WATCHED_ROOT = "~/Documents/Health Auto Export/health_metric_data"


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def resolve_watched_roots(root_entries: list[str] | tuple[str, ...]) -> tuple[Path, ...]:
    """Expand and resolve folder paths, dropping blanks and duplicates."""
    resolved_roots: list[Path] = []
    for entry in root_entries:
        entry = entry.strip()
        if not entry:
            continue
        path = Path(entry).expanduser().resolve()
        if path not in resolved_roots:
            resolved_roots.append(path)
    return tuple(resolved_roots)


def _watched_roots_from_env() -> tuple[Path, ...]:
    """Read watched folders from MCP_WATCHED_ROOTS (colon-separated), falling
    back to the legacy single-folder HEALTH_DATA_DIR."""
    watched_roots_value = os.getenv("MCP_WATCHED_ROOTS")
    if watched_roots_value:
        roots = resolve_watched_roots(watched_roots_value.split(os.pathsep))
        if roots:
            return roots
    return resolve_watched_roots(
        [os.getenv("HEALTH_DATA_DIR", DEFAULT_WATCHED_ROOT)]
    )


@dataclass(frozen=True)
class ServerSettings:
    host: str
    port: int
    mcp_path: str
    watched_roots: tuple[Path, ...]
    server_name: str
    device_secret: str | None
    public_base_url: str | None
    require_device_auth: bool

    @classmethod
    def from_env(cls, *, device_secret: str | None = None) -> ServerSettings:
        return cls(
            host=os.getenv("MCP_HOST", "127.0.0.1"),
            port=int(os.getenv("PORT", os.getenv("MCP_PORT", "8000"))),
            mcp_path=os.getenv("MCP_PATH", "/mcp"),
            watched_roots=_watched_roots_from_env(),
            server_name=os.getenv("MCP_SERVER_NAME", "macOS-Filesystem"),
            device_secret=device_secret or os.getenv("MCP_DEVICE_SECRET"),
            public_base_url=os.getenv("PUBLIC_BASE_URL"),
            require_device_auth=_env_bool("MCP_REQUIRE_DEVICE_AUTH", True),
        )

    @property
    def allowed_roots(self) -> tuple[Path, ...]:
        """The watched folders that currently exist on disk.

        A configured folder can disappear after the fact — an unmounted drive,
        a deleted directory. Such a folder is filtered out here rather than
        removed from the config, so that it comes back on its own once the
        folder reappears. Filtering at this single accessor keeps the tools,
        /health, and /discovery announcing the same set.
        """
        existing_roots: list[Path] = []
        for root in self.watched_roots:
            if root.is_dir():
                existing_roots.append(root)
            else:
                logger.warning(
                    "Shared folder %s no longer exists; not serving it.", root
                )
        return tuple(existing_roots)

    def local_mcp_url(self) -> str:
        return f"http://{self.host}:{self.port}{self.mcp_path}"

    def local_discovery_url(self) -> str:
        return f"http://{self.host}:{self.port}/discovery"

    def public_mcp_url(self, public_base_url: str | None = None) -> str:
        base = (public_base_url or self.public_base_url or "").rstrip("/")
        if not base:
            return self.local_mcp_url()
        return f"{base}{self.mcp_path}"

    def public_discovery_url(self, public_base_url: str | None = None) -> str:
        base = (public_base_url or self.public_base_url or "").rstrip("/")
        if not base:
            return self.local_discovery_url()
        return f"{base}/discovery"
