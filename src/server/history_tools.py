"""Model Context Protocol tools over the person's own browsing history.

Three tools, in the order the avatar uses them:

- ``browsing_activity_summary`` — how much browsing has happened since a
  watermark. Asked every few minutes while the person works; it returns
  counts, never rows, so a quiet period costs nothing.
- ``read_browser_history`` — the visits themselves, newer than a watermark,
  with the address, the page title, and the words typed into a search.
- ``list_browser_history_profiles`` — which browsers on this machine the
  history can come from.

The tools are thin: :mod:`src.server.browser_history` does the reading, and it
does it the same way on Linux, macOS, and Windows so the avatar's side of this
never learns which platform it is talking to.

Turning it off: ``EXPOSE_BROWSER_HISTORY=false`` makes every tool here answer
``{"disabled": true}`` instead of data, the same switch shape the Claude Code
session tools use.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastmcp.exceptions import ResourceError

from src.server.browser_history import (
    DEFAULT_ROWS_PER_READ,
    BrowserHistoryError,
    discover_history_profiles,
    history_is_exposed,
    read_history,
    summarize_activity,
)

logger = logging.getLogger(__name__)

DISABLED_ANSWER: dict[str, Any] = {
    "disabled": True,
    "reason": (
        "Browsing history is not shared from this machine. Set "
        "EXPOSE_BROWSER_HISTORY=true in the connector's environment to share it."
    ),
}


async def browsing_activity(since: str = "", default_days: int = 30) -> dict[str, Any]:
    """Count the visits newer than a watermark, without returning any of them."""
    if not history_is_exposed():
        return dict(DISABLED_ANSWER)
    try:
        return await asyncio.to_thread(
            summarize_activity, since=since, default_days=default_days
        )
    except BrowserHistoryError as history_error:
        raise ResourceError(str(history_error)) from history_error


async def browser_history(
    since: str = "",
    until: str = "",
    limit: int = DEFAULT_ROWS_PER_READ,
    profile_identifier: str = "",
    default_days: int = 30,
) -> dict[str, Any]:
    """Return the visits newer than a watermark, oldest first."""
    if not history_is_exposed():
        return dict(DISABLED_ANSWER)
    try:
        return await asyncio.to_thread(
            read_history,
            since=since,
            until=until or None,
            limit=limit,
            profile_identifier=profile_identifier,
            default_days=default_days,
        )
    except BrowserHistoryError as history_error:
        raise ResourceError(str(history_error)) from history_error


async def history_profiles() -> list[dict[str, Any]]:
    """Return the browsers on this machine whose history can be read."""
    if not history_is_exposed():
        return []
    profiles = await asyncio.to_thread(discover_history_profiles)
    return [profile.as_dict() for profile in profiles]


def register_history_tools(mcp: Any) -> None:
    """Register the browsing-history tools on the daemon's server."""

    @mcp.tool()
    async def browsing_activity_summary(since: str = "", default_days: int = 30) -> dict:
        """How much browsing has happened since a moment in time.

        Cheap: returns counts and the newest visit time, never the visits
        themselves. Call this before reading history, so a period with no new
        browsing costs nothing. ``since`` takes the ``watermark`` from a
        previous call, an ISO timestamp, or a number of days.
        """
        return await browsing_activity(since=since, default_days=default_days)

    @mcp.tool()
    async def read_browser_history(
        since: str = "",
        until: str = "",
        limit: int = DEFAULT_ROWS_PER_READ,
        profile_identifier: str = "",
        default_days: int = 30,
    ) -> dict:
        """The owner's web browsing: address, page title, and search terms.

        Returns visits newer than ``since``, oldest first, from every browser
        on this machine (Chrome, Chromium, Brave, Edge, Opera, Vivaldi, Arc,
        Firefox and its forks, and Safari on macOS), plus a ``watermark`` to
        pass as the next ``since``. At most 20000 visits come back in one call.
        """
        return await browser_history(
            since=since,
            until=until,
            limit=limit,
            profile_identifier=profile_identifier,
            default_days=default_days,
        )

    @mcp.tool()
    async def list_browser_history_profiles() -> list[dict]:
        """The browsers and profiles on this machine whose history can be read."""
        return await history_profiles()
