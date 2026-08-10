from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx
import websockets
from websockets.exceptions import ConnectionClosed

from src.daemon.config import DEFAULT_API_BASE_URL, DaemonConfig
from src.server.app import MCP_TRANSPORT

logger = logging.getLogger(__name__)

RELAY_PATH = "/mcp/relay"
RECONNECT_DELAY_SECONDS = 5.0


def relay_ws_url(api_base_url: str) -> str:
    parsed = urlparse(api_base_url.rstrip("/"))
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunparse((scheme, parsed.netloc, RELAY_PATH, "", "", ""))


def relay_http_url(api_base_url: str, device_id: str) -> str:
    return f"{api_base_url.rstrip('/')}{RELAY_PATH}/{device_id}"


@dataclass
class RelayHandle:
    task: asyncio.Task[None]

    async def stop(self) -> None:
        self.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.task


class OutboundRelay:
    """Maintain an outbound WebSocket to the API and proxy MCP HTTP locally.

    No inbound ports, firewalls, or public tunnels required — the user's
    machine only needs outbound HTTPS/WSS access to api.neuralnexus.site.
    """

    def __init__(
        self,
        *,
        config: DaemonConfig,
        api_key: str,
        local_mcp_url: str,
        server_name: str,
        allowed_roots: list[str],
    ) -> None:
        self._config = config
        self._api_key = api_key
        self._local_mcp_url = local_mcp_url.rstrip("/")
        self._server_name = server_name
        self._allowed_roots = allowed_roots
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(120.0, connect=10.0),
            follow_redirects=True,
        )

    async def close(self) -> None:
        await self._http.aclose()

    def _register_message(self) -> dict[str, Any]:
        self._config.ensure_device_identity()
        return {
            "type": "register",
            "connection_mode": "relay",
            "device_id": self._config.device_id,
            "device_secret": self._config.device_secret,
            "server_name": self._server_name,
            "transport": MCP_TRANSPORT,
            "allowed_roots": list(self._allowed_roots),
            "local_mcp_url": self._local_mcp_url,
        }

    async def run_until_stopped(self, stop_event: asyncio.Event) -> None:
        ws_url = relay_ws_url(self._config.api_base_url)
        headers = {"API-KEY": self._api_key}

        while not stop_event.is_set():
            try:
                async with websockets.connect(
                    ws_url,
                    additional_headers=headers,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                ) as websocket:
                    await websocket.send(json.dumps(self._register_message()))
                    logger.info("Outbound relay connected to %s", ws_url)
                    await self._listen(websocket, stop_event)
            except ConnectionClosed as exc:
                logger.warning("Relay connection closed (%s); reconnecting...", exc)
            except OSError as exc:
                logger.warning(
                    "Relay cannot reach %s (%s); retrying in %ss",
                    ws_url,
                    exc,
                    RECONNECT_DELAY_SECONDS,
                )
            except Exception as exc:
                logger.warning(
                    "Relay connection failed (%s); retrying in %ss",
                    exc,
                    RECONNECT_DELAY_SECONDS,
                )

            if stop_event.is_set():
                break
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=RECONNECT_DELAY_SECONDS,
                )
            except TimeoutError:
                continue

    async def _listen(
        self, websocket: websockets.ClientConnection, stop_event: asyncio.Event
    ) -> None:
        while not stop_event.is_set():
            try:
                raw = await asyncio.wait_for(websocket.recv(), timeout=1.0)
            except TimeoutError:
                continue
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("Ignoring non-JSON relay message: %s", raw[:200])
                continue
            await self._handle_message(websocket, message)

    async def _handle_message(
        self, websocket: websockets.ClientConnection, message: dict[str, Any]
    ) -> None:
        msg_type = message.get("type")
        if msg_type in {"ping", "heartbeat"}:
            await websocket.send(json.dumps({"type": "pong"}))
            return
        if msg_type == "registered":
            logger.info(
                "API acknowledged relay registration for %s",
                message.get("device_id"),
            )
            return
        if msg_type != "proxy":
            logger.debug("Unhandled relay message type: %s", msg_type)
            return

        response = await self._proxy_to_local_mcp(message)
        await websocket.send(json.dumps(response))

    async def _proxy_to_local_mcp(self, message: dict[str, Any]) -> dict[str, Any]:
        request_id = message.get("request_id")
        method = (message.get("method") or "POST").upper()
        path = message.get("path") or "/mcp"
        headers = dict(message.get("headers") or {})
        body = message.get("body")

        if not headers.get("Authorization") and self._config.device_secret:
            headers["Authorization"] = f"Bearer {self._config.device_secret}"

        request_body: bytes | None
        if body is None:
            request_body = None
        elif isinstance(body, str):
            if message.get("body_encoding") == "base64":
                request_body = base64.b64decode(body)
            else:
                request_body = body.encode("utf-8")
        else:
            request_body = json.dumps(body).encode("utf-8")

        url = f"{self._local_mcp_url.rstrip('/')}{path}"
        try:
            response = await self._http.request(
                method,
                url,
                headers=headers,
                content=request_body,
            )
            content = response.content
            body_encoding = "base64" if _needs_base64(content) else "text"
            body_out = (
                base64.b64encode(content).decode("ascii")
                if body_encoding == "base64"
                else content.decode("utf-8", errors="replace")
            )
            return {
                "type": "proxy_response",
                "request_id": request_id,
                "status_code": response.status_code,
                "headers": dict(response.headers),
                "body": body_out,
                "body_encoding": body_encoding,
            }
        except httpx.HTTPError as exc:
            logger.warning("Local MCP proxy failed for %s %s: %s", method, url, exc)
            return {
                "type": "proxy_response",
                "request_id": request_id,
                "status_code": 502,
                "headers": {"content-type": "application/json"},
                "body": json.dumps({"error": str(exc)}),
                "body_encoding": "text",
            }


def _needs_base64(content: bytes) -> bool:
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def start_relay(
    *,
    config: DaemonConfig,
    api_key: str,
    local_mcp_url: str,
    server_name: str,
    allowed_roots: list[str],
    stop_event: asyncio.Event,
) -> tuple[OutboundRelay, RelayHandle]:
    relay = OutboundRelay(
        config=config,
        api_key=api_key,
        local_mcp_url=local_mcp_url,
        server_name=server_name,
        allowed_roots=allowed_roots,
    )
    task = asyncio.create_task(relay.run_until_stopped(stop_event), name="outbound-relay")
    return relay, RelayHandle(task=task)
