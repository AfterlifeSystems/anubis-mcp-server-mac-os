from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from fastmcp import FastMCP
from fastmcp.exceptions import ResourceError
from fastmcp.resources import DirectoryResource
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from sse_starlette.sse import EventSourceResponse

from src.server.settings import ServerSettings, resolve_watched_roots

MCP_TRANSPORT = "streamable_http"


@dataclass
class PublicEndpoints:
    base_url: str
    mcp_path: str = "/mcp"

    @property
    def mcp_url(self) -> str:
        return f"{self.base_url.rstrip('/')}{self.mcp_path}"

    @property
    def discovery_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/discovery"


def _build_auth(device_secret: str | None, *, require_device_auth: bool):
    if not device_secret:
        if require_device_auth:
            raise ValueError(
                "MCP device secret is required. Run `python -m src.daemon login` "
                "and `python -m src.daemon start` to generate one."
            )
        return None
    return StaticTokenVerifier(
        tokens={
            device_secret: {
                "client_id": "neuralnexus-api",
                "scopes": ["mcp:tools"],
            }
        },
        required_scopes=["mcp:tools"],
    )


def _resolve_allowed_path(path_str: str, allowed_roots: tuple[Path, ...]) -> Path:
    # An empty allow-list denies everything. Treating it as "allow all" would
    # expose the whole filesystem through read_file_bytes the moment the last
    # shared folder went missing.
    if not allowed_roots:
        raise ResourceError("No shared folders are configured on this device.")
    path = Path(path_str).expanduser().resolve()
    if not any(path.is_relative_to(root) for root in allowed_roots):
        raise ResourceError(f"Path not allowed: {path_str}")
    return path


def _resolve_allowed_dir(directory: str, allowed_roots: tuple[Path, ...]) -> Path:
    path = _resolve_allowed_path(directory, allowed_roots)
    if not path.is_dir():
        raise ResourceError(f"Not a directory: {directory}")
    return path


def _resolve_allowed_file(file_path: str, allowed_roots: tuple[Path, ...]) -> Path:
    path = _resolve_allowed_path(file_path, allowed_roots)
    if not path.is_file():
        raise ResourceError(f"Not a file: {file_path}")
    return path


def create_mcp_server(
    settings: ServerSettings,
    *,
    endpoints: PublicEndpoints | None = None,
) -> FastMCP:
    allowed_roots = settings.allowed_roots
    auth = _build_auth(
        settings.device_secret,
        require_device_auth=settings.require_device_auth,
    )
    mcp = FastMCP(settings.server_name, auth=auth)
    public = endpoints or PublicEndpoints(
        base_url=(settings.public_base_url or f"http://127.0.0.1:{settings.port}"),
        mcp_path=settings.mcp_path,
    )

    @mcp.tool()
    async def list_all_files(directory: str, recursive: bool = True) -> list[str]:
        """List all files under the given directory. Discover CSV/JSON exports."""
        root = _resolve_allowed_dir(directory, allowed_roots)

        def _list_files() -> list[str]:
            if recursive:
                return sorted(str(p) for p in root.rglob("*") if p.is_file())
            return sorted(str(p) for p in root.iterdir() if p.is_file())

        return await asyncio.to_thread(_list_files)

    @mcp.tool()
    async def read_file_bytes(file_path: str) -> str:
        """Return base64-encoded file bytes for sandbox upload."""
        path = _resolve_allowed_file(file_path, allowed_roots)

        def _read() -> str:
            return base64.b64encode(path.read_bytes()).decode("utf-8")

        return await asyncio.to_thread(_read)

    @mcp.tool()
    async def get_file_info(file_path: str) -> dict:
        """Return size, extension, and modified date for a file."""
        path = _resolve_allowed_file(file_path, allowed_roots)
        stat = path.stat()
        return {
            "path": str(path),
            "name": path.name,
            "extension": path.suffix,
            "size_bytes": stat.st_size,
            "modified_at": datetime.fromtimestamp(
                stat.st_mtime, tz=timezone.utc
            ).isoformat(),
        }

    @mcp.tool()
    async def preview_data(file_path: str, n_rows: int = 5) -> dict:
        """Return a small preview of CSV/JSON data."""
        path = _resolve_allowed_file(file_path, allowed_roots)
        suffix = path.suffix.lower()

        def _preview() -> dict:
            if suffix == ".csv":
                df = pd.read_csv(path, nrows=n_rows)
            elif suffix == ".json":
                df = pd.read_json(path)
                df = df.head(n_rows)
            else:
                if suffix == ".txt":
                    return path.read_text()[:10]
                raise ResourceError("Only CSV and JSON previews are supported.")
            return {
                "path": str(path),
                "columns": list(df.columns),
                "rows": df.to_dict(orient="records"),
            }

        return await asyncio.to_thread(_preview)

    @mcp.tool()
    async def read_files_for_sandbox(file_paths: list[str]) -> list[dict]:
        """Read multiple files as base64 payloads for sandbox upload."""
        results = []
        for fp in file_paths:
            info = await get_file_info(fp)
            content_b64 = await read_file_bytes(fp)
            results.append({**info, "content_b64": content_b64})
        return results

    used_resource_names: set[str] = set()
    for root in allowed_roots:
        if root.is_dir():
            resource_name = root.name
            if resource_name in used_resource_names:
                resource_name = f"{root.parent.name}-{root.name}"
            suffix = 2
            while resource_name in used_resource_names:
                resource_name = f"{root.name}-{suffix}"
                suffix += 1
            used_resource_names.add(resource_name)
            mcp.add_resource(
                DirectoryResource(
                    uri=f"health://files/{resource_name}",
                    path=root,
                    name=f"Watched Files ({resource_name})",
                    description=f"List files under {root}.",
                    recursive=True,
                )
            )

    @mcp.custom_route("/discovery", methods=["GET"])
    async def discovery(request: Request) -> Response:
        """SSE channel used by Anubis to discover this MCP server."""

        async def _announcement_stream():
            payload = {
                "url": public.mcp_url,
                "transport": MCP_TRANSPORT,
                "server_name": settings.server_name,
                "allowed_roots": [str(root) for root in allowed_roots],
            }
            yield {
                "event": "announce",
                "data": json.dumps(payload),
            }

        return EventSourceResponse(_announcement_stream())

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_request: Request) -> Response:
        return JSONResponse(
            {
                "status": "ok",
                "server_name": settings.server_name,
                "allowed_roots": [str(root) for root in allowed_roots],
            }
        )

    return mcp


def main() -> None:
    """Run the MCP HTTP server directly (local development without the daemon)."""
    settings = build_server_settings(require_device_auth=False)
    mcp = create_mcp_server(settings)
    mcp.run(
        transport="streamable-http",
        host=settings.host,
        port=settings.port,
        path=settings.mcp_path,
    )


def build_server_settings(
    *,
    watched_roots: list[str] | None = None,
    device_secret: str | None = None,
    public_base_url: str | None = None,
    port: int | None = None,
    require_device_auth: bool | None = None,
) -> ServerSettings:
    settings = ServerSettings.from_env(device_secret=device_secret)
    resolved_roots = (
        resolve_watched_roots(watched_roots)
        if watched_roots
        else settings.watched_roots
    )
    return ServerSettings(
        host=settings.host,
        port=port or settings.port,
        mcp_path=settings.mcp_path,
        watched_roots=resolved_roots,
        server_name=settings.server_name,
        device_secret=device_secret or settings.device_secret,
        public_base_url=public_base_url or settings.public_base_url,
        require_device_auth=(
            settings.require_device_auth
            if require_device_auth is None
            else require_device_auth
        ),
    )


if __name__ == "__main__":
    main()
