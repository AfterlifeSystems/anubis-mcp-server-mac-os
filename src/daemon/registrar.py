from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from src.daemon.config import DaemonConfig
from src.daemon.relay import relay_http_url
from src.server.app import MCP_TRANSPORT

logger = logging.getLogger(__name__)

REGISTER_PATH = "/mcp/register"
HEARTBEAT_PATH = "/mcp/heartbeat"
UNREGISTER_PATH = "/mcp/unregister"


@dataclass(frozen=True)
class RegistrationPayload:
    connection_mode: str
    server_name: str
    transport: str
    mcp_url: str
    discovery_url: str | None
    allowed_roots: list[str]
    device_secret: str
    device_id: str

    def to_json(self) -> dict[str, Any]:
        payload = {
            "connection_mode": self.connection_mode,
            "server_name": self.server_name,
            "transport": self.transport,
            "mcp_url": self.mcp_url,
            "allowed_roots": self.allowed_roots,
            "device_secret": self.device_secret,
            "device_id": self.device_id,
        }
        if self.discovery_url is not None:
            payload["discovery_url"] = self.discovery_url
        return payload


class ApiRegistrar:
    """Push-based presence: local daemon reaches out to api.neuralnexus.site."""

    def __init__(self, config: DaemonConfig, api_key: str) -> None:
        self._config = config
        self._api_key = api_key
        self._client = httpx.AsyncClient(
            base_url=config.api_base_url.rstrip("/"),
            headers={"API-KEY": api_key},
            timeout=httpx.Timeout(15.0, connect=10.0),
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def register(self, payload: RegistrationPayload) -> dict[str, Any]:
        response = await self._client.post(REGISTER_PATH, json=payload.to_json())
        if response.status_code == 404:
            logger.warning(
                "API endpoint %s is not deployed yet; continuing locally.",
                REGISTER_PATH,
            )
            return {"status": "local_only", "reason": "endpoint_missing"}
        response.raise_for_status()
        body = response.json()
        self._config.mark_registered()
        logger.info("Registered MCP server with API for device %s", payload.device_id)
        return body

    async def heartbeat(self, *, device_id: str, mcp_url: str) -> dict[str, Any]:
        response = await self._client.post(
            HEARTBEAT_PATH,
            json={
                "device_id": device_id,
                "mcp_url": mcp_url,
                "connection_mode": self._config.connection_mode,
            },
        )
        if response.status_code == 404:
            return {"status": "local_only", "reason": "endpoint_missing"}
        response.raise_for_status()
        return response.json()

    async def unregister(self, *, device_id: str) -> None:
        try:
            response = await self._client.post(
                UNREGISTER_PATH,
                json={"device_id": device_id},
            )
            if response.status_code == 404:
                return
            response.raise_for_status()
        except httpx.HTTPError:
            logger.warning("Failed to unregister MCP device %s with API", device_id)

    @staticmethod
    def build_payload(
        *,
        config: DaemonConfig,
        server_name: str,
        mcp_path: str,
        allowed_roots: list[str],
    ) -> RegistrationPayload:
        config.ensure_device_identity()
        device_id = config.device_id or ""

        if config.connection_mode == "relay":
            return RegistrationPayload(
                connection_mode="relay",
                server_name=server_name,
                transport="relay",
                mcp_url=relay_http_url(config.api_base_url, device_id),
                discovery_url=None,
                allowed_roots=list(allowed_roots),
                device_secret=config.device_secret or "",
                device_id=device_id,
            )

        base = f"http://127.0.0.1:{config.local_port}"

        return RegistrationPayload(
            connection_mode=config.connection_mode,
            server_name=server_name,
            transport=MCP_TRANSPORT,
            mcp_url=f"{base}{mcp_path}",
            discovery_url=f"{base}/discovery",
            allowed_roots=list(allowed_roots),
            device_secret=config.device_secret or "",
            device_id=device_id,
        )
