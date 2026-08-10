from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import sys

import uvicorn
import httpx

from src.daemon.config import (
    Credentials,
    DaemonConfig,
    config_dir,
    resolve_api_base_url,
    resolve_existing_directory,
)
from src.daemon.registrar import ApiRegistrar
from src.daemon.relay import OutboundRelay, start_relay
from src.daemon.setup import is_first_run, run_interactive_setup
from src.server.app import PublicEndpoints, build_server_settings, create_mcp_server

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_SECONDS = 30.0


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def cmd_setup(args: argparse.Namespace) -> int:
    try:
        run_interactive_setup(
            api_key=args.api_key,
            watch_folder=args.watch,
            api_base_url=args.api_base_url,
            non_interactive=args.non_interactive,
        )
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    if args.start:
        return cmd_start(args)
    return 0


def cmd_login(args: argparse.Namespace) -> int:
    api_key = args.api_key or input("NeuralNexus API key (sk-...): ").strip()
    if not api_key:
        print("API key is required.", file=sys.stderr)
        return 1
    Credentials.save_api_key(api_key)
    config = DaemonConfig.load()
    if args.api_base_url:
        config.api_base_url = args.api_base_url.rstrip("/")
        config.save()
    print(f"Saved API credentials in {config_dir()}")
    return 0


def cmd_configure(args: argparse.Namespace) -> int:
    config = DaemonConfig.load()
    if args.api_base_url:
        config.api_base_url = args.api_base_url.rstrip("/")
    if args.connection_mode:
        config.connection_mode = args.connection_mode
    if args.public_base_url:
        config.public_base_url = args.public_base_url.rstrip("/")
    if args.port:
        config.local_port = args.port
    # Validate every requested folder up front so a bad path in the middle of
    # the list cannot leave the config half-applied.
    try:
        for root in (args.watch or []) + (args.add_watch or []):
            resolve_existing_directory(root)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    if args.watch:
        config.set_watched_roots(args.watch)
    if args.add_watch:
        for root in args.add_watch:
            config.add_watched_root(root)
    if args.remove_watch:
        for root in args.remove_watch:
            config.remove_watched_root(root)
    config.save()
    print("Configuration updated:")
    for key, value in config.to_public_dict().items():
        print(f"  {key}: {value}")
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    config = DaemonConfig.load()
    creds = Credentials.load()
    print("NeuralNexus MCP daemon status")
    for key, value in config.to_public_dict().items():
        print(f"  {key}: {value}")
    print(f"  logged_in: {creds is not None}")
    return 0


async def _run_daemon(config: DaemonConfig, credentials: Credentials) -> int:
    config.ensure_device_identity()
    if not config.watched_roots:
        default_root = os.path.expanduser(
            os.getenv(
                "HEALTH_DATA_DIR",
                "~/Documents/Health Auto Export/health_metric_data",
            )
        )
        if os.path.isdir(default_root):
            config.add_watched_root(default_root)

    settings = build_server_settings(
        watched_roots=config.watched_roots,
        device_secret=config.device_secret,
        public_base_url=config.public_base_url,
        port=config.local_port,
    )
    # The folders the tools will actually serve — a configured folder that has
    # since disappeared is dropped here. Announce this set rather than the raw
    # config so the avatar is never offered a folder it cannot list.
    served_roots = [str(root) for root in settings.allowed_roots]
    local_base = f"http://127.0.0.1:{settings.port}"
    endpoints = PublicEndpoints(base_url=local_base, mcp_path=settings.mcp_path)
    mcp = create_mcp_server(settings, endpoints=endpoints)

    stop_event = asyncio.Event()

    def _request_stop(*_args: object) -> None:
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:
            signal.signal(sig, lambda *_: stop_event.set())

    app = mcp.http_app(transport="streamable-http", path=settings.mcp_path)
    server = uvicorn.Server(
        uvicorn.Config(app, host=settings.host, port=settings.port, log_level="info")
    )
    serve_task = asyncio.create_task(server.serve(), name="mcp-http-server")
    await asyncio.sleep(0.5)

    relay: OutboundRelay | None = None
    relay_handle = None

    if config.connection_mode == "relay":
        relay, relay_handle = start_relay(
            config=config,
            api_key=credentials.api_key,
            local_mcp_url=local_base,
            server_name=settings.server_name,
            allowed_roots=served_roots,
            stop_event=stop_event,
        )
        endpoints.base_url = config.api_base_url

    registrar = ApiRegistrar(config, credentials.api_key)
    payload = ApiRegistrar.build_payload(
        config=config,
        server_name=settings.server_name,
        mcp_path=settings.mcp_path,
        allowed_roots=served_roots,
    )

    try:
        await registrar.register(payload)
    except httpx.HTTPError as exc:
        logger.warning(
            "API registration failed for %s (%s). Local MCP is running; "
            "the relay will keep retrying in the background.",
            config.api_base_url,
            exc,
        )
    except Exception as exc:
        logger.warning(
            "API registration failed for %s (%s). Local MCP is still running.",
            config.api_base_url,
            exc,
        )

    async def _heartbeat_loop() -> None:
        while not stop_event.is_set():
            try:
                await registrar.heartbeat(
                    device_id=config.device_id or "",
                    mcp_url=payload.mcp_url,
                )
            except httpx.HTTPError as exc:
                logger.debug("Heartbeat failed for %s: %s", config.api_base_url, exc)
            except Exception as exc:
                logger.debug("Heartbeat failed: %s", exc)
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=HEARTBEAT_INTERVAL_SECONDS,
                )
            except TimeoutError:
                continue

    heartbeat_task = asyncio.create_task(_heartbeat_loop(), name="api-heartbeat")

    logger.info(
        "MCP server listening on http://%s:%s%s",
        settings.host,
        settings.port,
        settings.mcp_path,
    )
    if config.connection_mode == "relay":
        logger.info(
            "Connected to API via outbound relay (no port forwarding required)."
        )
    else:
        logger.info("Local-only mode on %s", local_base)
    logger.info("Press Ctrl+C to stop.")

    await stop_event.wait()

    heartbeat_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await heartbeat_task

    server.should_exit = True
    await serve_task

    if config.device_id:
        await registrar.unregister(device_id=config.device_id)
    await registrar.close()
    if relay is not None:
        if relay_handle is not None:
            await relay_handle.stop()
        await relay.close()
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    _configure_logging(args.verbose)
    if is_first_run():
        print("First run — starting setup.", file=sys.stderr)
        code = cmd_setup(
            argparse.Namespace(
                api_key=args.api_key,
                watch=args.watch,
                api_base_url=args.api_base_url,
                non_interactive=args.non_interactive,
                start=False,
                verbose=args.verbose,
            )
        )
        if code != 0:
            return code
    credentials = Credentials.load()
    if credentials is None:
        print(
            "No API key found. Run `./scripts/install.sh` or `python -m src.daemon setup`.",
            file=sys.stderr,
        )
        return 1
    config = DaemonConfig.load()
    return asyncio.run(_run_daemon(config, credentials))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="neuralnexus-mcp",
        description=(
            "Local NeuralNexus MCP daemon. Default connection uses an outbound "
            "relay to api.neuralnexus.site — no port forwarding required."
        ),
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)

    setup = subparsers.add_parser(
        "setup",
        help="One-time interactive setup (API key + folder to share)",
    )
    setup.add_argument("--api-key", help="sk-... API key")
    setup.add_argument("--watch", help="Folder to expose for analysis")
    setup.add_argument("--api-base-url", help="Defaults to https://api.neuralnexus.site")
    setup.add_argument(
        "--non-interactive",
        action="store_true",
        help="Use NEURALNEXUS_API_KEY / NEURALNEXUS_WATCH_FOLDER env vars only",
    )
    setup.add_argument(
        "--start",
        action="store_true",
        help="Start the daemon immediately after setup",
    )
    setup.set_defaults(func=cmd_setup)

    login = subparsers.add_parser("login", help="Save the user's NeuralNexus API key")
    login.add_argument("--api-key", help="sk-... API key")
    login.add_argument("--api-base-url", help="Defaults to https://api.neuralnexus.site")
    login.set_defaults(func=cmd_login)

    configure = subparsers.add_parser("configure", help="Update daemon settings")
    configure.add_argument("--api-base-url")
    configure.add_argument(
        "--connection-mode",
        choices=["relay", "local"],
        dest="connection_mode",
    )
    configure.add_argument("--public-base-url")
    configure.add_argument("--port", type=int, help="Local MCP HTTP port")
    configure.add_argument("--watch", nargs="+", help="Replace watched folders")
    configure.add_argument("--add-watch", nargs="+", help="Append watched folders")
    configure.add_argument(
        "--remove-watch", nargs="+", help="Remove watched folders"
    )
    configure.set_defaults(func=cmd_configure)

    status = subparsers.add_parser("status", help="Show saved configuration")
    status.set_defaults(func=cmd_status)

    start = subparsers.add_parser("start", help="Run MCP server and connect to API")
    start.add_argument("--api-key", help="sk-... (first run only)")
    start.add_argument("--watch", help="Folder to share (first run only)")
    start.add_argument("--api-base-url", help="API base URL (first run only)")
    start.add_argument("--non-interactive", action="store_true")
    start.set_defaults(func=cmd_start)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
