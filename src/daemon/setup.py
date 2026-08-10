from __future__ import annotations

import os
import sys
from pathlib import Path

from src.daemon.config import Credentials, DaemonConfig, config_dir, resolve_api_base_url

DEFAULT_WATCH_CANDIDATES = [
    "~/Documents/Health Auto Export/health_metric_data",
    "~/Library/Mobile Documents/com~apple~CloudDocs/Health Auto Export/health_metric_data",
    "~/Documents",
    "~/Downloads",
]


def _expand(path: str) -> Path:
    return Path(path).expanduser().resolve()


def _default_watch_folder() -> str | None:
    for candidate in DEFAULT_WATCH_CANDIDATES:
        path = _expand(candidate)
        if path.is_dir():
            return str(path)
    return None


def _prompt_api_key(*, preset: str | None = None) -> str:
    if preset:
        return preset.strip()
    while True:
        value = input(
            "NeuralNexus API key (from Account settings, starts with sk-): "
        ).strip()
        if value.startswith("sk-"):
            return value
        print("API keys start with sk-. Paste the full key and try again.", file=sys.stderr)


def _prompt_watch_folder(*, preset: str | None = None) -> str:
    if preset:
        path = _expand(preset)
        if not path.is_dir():
            raise ValueError(f"Watch folder does not exist: {path}")
        return str(path)

    default = _default_watch_folder()
    prompt = "Folder to share with NeuralNexus"
    if default:
        prompt += f" [{default}]"
    prompt += ": "

    while True:
        entered = input(prompt).strip()
        chosen = entered or default
        if not chosen:
            print("A folder path is required.", file=sys.stderr)
            continue
        path = _expand(chosen)
        if path.is_dir():
            return str(path)
        print(f"Not a directory: {path}", file=sys.stderr)


def run_interactive_setup(
    *,
    api_key: str | None = None,
    watch_folder: str | None = None,
    api_base_url: str | None = None,
    non_interactive: bool = False,
) -> DaemonConfig:
    """Collect first-run configuration with minimal follow-up questions."""
    api_key = api_key or os.getenv("NEURALNEXUS_API_KEY")
    watch_folder = watch_folder or os.getenv("NEURALNEXUS_WATCH_FOLDER")
    api_base_url = resolve_api_base_url(
        api_base_url or os.getenv("NEURALNEXUS_API_BASE_URL")
    )

    if non_interactive and not api_key:
        raise ValueError(
            "Non-interactive setup requires NEURALNEXUS_API_KEY or --api-key."
        )

    print("NeuralNexus MCP — one-time setup")
    print("This connects your computer to api.neuralnexus.site (outbound only).")
    print("No account configuration or port forwarding is required.")
    print()

    if not api_key and not non_interactive:
        api_key = _prompt_api_key()
    elif not api_key:
        raise ValueError("API key is required.")

    Credentials.save_api_key(api_key)

    config = DaemonConfig.load()
    config.api_base_url = api_base_url
    config.connection_mode = "relay"

    if not watch_folder and not non_interactive:
        watch_folder = _prompt_watch_folder()
    elif watch_folder:
        watch_folder = _prompt_watch_folder(preset=watch_folder)
    elif non_interactive:
        default = _default_watch_folder()
        if not default:
            raise ValueError(
                "Non-interactive setup requires NEURALNEXUS_WATCH_FOLDER when no "
                "default folder exists."
            )
        watch_folder = default
    else:
        watch_folder = _prompt_watch_folder()

    config.set_watched_roots([watch_folder])
    config.ensure_device_identity()
    config.save()

    print()
    print("Setup complete.")
    print(f"  Config: {config_dir()}")
    print(f"  API:    {config.api_base_url}")
    print(f"  Folder: {watch_folder}")
    print(f"  Device: {config.device_id}")
    return config


def is_first_run() -> bool:
    return Credentials.load() is None
