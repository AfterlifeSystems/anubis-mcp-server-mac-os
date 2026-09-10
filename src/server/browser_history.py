"""The person's own web browsing history, read the same way on every platform.

What somebody reads, searches for, buys, and returns to says more about who
they are than most of what they will ever write down. This module hands that
record to the avatar so the analysis running on the API can say what the
person is interested in, what they are working on, when they are awake, and
what they keep coming back to.

PLATFORM AGNOSTIC ON PURPOSE
    One reader serves Linux, macOS, and Windows, and three database layouts:

    - **Chromium family** (Chrome, Chromium, Brave, Edge, Opera, Vivaldi, Arc):
      a ``History`` database per profile, with ``urls`` and ``visits``, timed in
      microseconds since 1601-01-01.
    - **Firefox family** (Firefox, Developer Edition, LibreWolf, Waterfox, Zen):
      ``places.sqlite`` per profile, with ``moz_places`` and ``moz_historyvisits``,
      timed in microseconds since 1970-01-01.
    - **Safari** (macOS only): ``History.db``, with ``history_items`` and
      ``history_visits``, timed in seconds since 2001-01-01. Reading it needs
      Full Disk Access, and the refusal says so rather than reporting an empty
      history.

    Snap and Flatpak layouts on Linux are covered, as are the roaming and
    local application-data layouts on Windows. A browser that is not installed
    contributes nothing and costs one ``is_dir`` check.

INCREMENTAL BY DESIGN
    The avatar re-reads this every few minutes while the person browses, so
    every read takes a ``since`` and returns rows newer than it, plus the
    timestamp of the newest row as the next watermark.
    :func:`summarize_activity` answers the cheap question — how many visits
    are newer than the watermark — without returning any of them, so a pass
    that finds nothing new costs no model call and no data transfer.

Every function is a plain function over paths and rows, so the whole reader is
unit-tested against fabricated databases with no browser installed.
"""

from __future__ import annotations

import logging
import os
import platform
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, urlparse

from src.server.local_databases import LocalDatabaseError, opened_copy, table_exists

logger = logging.getLogger(__name__)

# Epoch offsets, in seconds, from each browser's own zero to the Unix epoch.
CHROMIUM_EPOCH_OFFSET_SECONDS = 11644473600  # 1601-01-01
SAFARI_EPOCH_OFFSET_SECONDS = 978307200  # 2001-01-01

FAMILY_CHROMIUM = "chromium"
FAMILY_FIREFOX = "firefox"
FAMILY_SAFARI = "safari"

# How many rows one read returns at most, whatever the caller asks for. A
# month of heavy browsing is tens of thousands of visits, and the analysis on
# the other side reads a digest, not the raw table.
MAXIMUM_ROWS_PER_READ = 20000
DEFAULT_ROWS_PER_READ = 2000


class BrowserHistoryError(Exception):
    """A history database could not be read; the message is owner-safe."""


@dataclass(frozen=True)
class HistoryProfile:
    """One browser profile on this machine that holds a history database."""

    browser_name: str
    profile_name: str
    family: str
    database_path: Path

    @property
    def identifier(self) -> str:
        """A stable name the avatar and the person can both refer to."""
        return f"{self.browser_name}:{self.profile_name}"

    def as_dict(self) -> dict[str, Any]:
        """The profile as the daemon tool reports it."""
        return {
            "identifier": self.identifier,
            "browser_name": self.browser_name,
            "profile_name": self.profile_name,
            "family": self.family,
            "database_path": str(self.database_path),
        }


# ---------------------------------------------------------------------------
# Where each browser keeps its history, per operating system
# ---------------------------------------------------------------------------

# (browser name, path relative to the home directory) for the Chromium family.
# The last path segment is the directory that CONTAINS the profile directories.
CHROMIUM_ROOTS: dict[str, tuple[tuple[str, str], ...]] = {
    "Linux": (
        ("Google Chrome", ".config/google-chrome"),
        ("Google Chrome Beta", ".config/google-chrome-beta"),
        ("Chromium", ".config/chromium"),
        ("Brave", ".config/BraveSoftware/Brave-Browser"),
        ("Microsoft Edge", ".config/microsoft-edge"),
        ("Opera", ".config/opera"),
        ("Vivaldi", ".config/vivaldi"),
        ("Chromium", "snap/chromium/common/chromium"),
        ("Brave", "snap/brave/current/.config/BraveSoftware/Brave-Browser"),
        ("Chromium", ".var/app/org.chromium.Chromium/config/chromium"),
        ("Brave", ".var/app/com.brave.Browser/config/BraveSoftware/Brave-Browser"),
        ("Google Chrome", ".var/app/com.google.Chrome/config/google-chrome"),
    ),
    "Darwin": (
        ("Google Chrome", "Library/Application Support/Google/Chrome"),
        ("Google Chrome Beta", "Library/Application Support/Google/Chrome Beta"),
        ("Chromium", "Library/Application Support/Chromium"),
        ("Brave", "Library/Application Support/BraveSoftware/Brave-Browser"),
        ("Microsoft Edge", "Library/Application Support/Microsoft Edge"),
        ("Opera", "Library/Application Support/com.operasoftware.Opera"),
        ("Vivaldi", "Library/Application Support/Vivaldi"),
        ("Arc", "Library/Application Support/Arc/User Data"),
    ),
    "Windows": (
        ("Google Chrome", "AppData/Local/Google/Chrome/User Data"),
        ("Google Chrome Beta", "AppData/Local/Google/Chrome Beta/User Data"),
        ("Chromium", "AppData/Local/Chromium/User Data"),
        ("Brave", "AppData/Local/BraveSoftware/Brave-Browser/User Data"),
        ("Microsoft Edge", "AppData/Local/Microsoft/Edge/User Data"),
        ("Opera", "AppData/Roaming/Opera Software/Opera Stable"),
        ("Vivaldi", "AppData/Local/Vivaldi/User Data"),
    ),
}

FIREFOX_ROOTS: dict[str, tuple[tuple[str, str], ...]] = {
    "Linux": (
        ("Firefox", ".mozilla/firefox"),
        ("Firefox", "snap/firefox/common/.mozilla/firefox"),
        ("Firefox", ".var/app/org.mozilla.firefox/.mozilla/firefox"),
        ("LibreWolf", ".librewolf"),
        ("Waterfox", ".waterfox"),
        ("Zen", ".zen"),
    ),
    "Darwin": (
        ("Firefox", "Library/Application Support/Firefox/Profiles"),
        ("LibreWolf", "Library/Application Support/LibreWolf/Profiles"),
        ("Waterfox", "Library/Application Support/Waterfox/Profiles"),
        ("Zen", "Library/Application Support/zen/Profiles"),
    ),
    "Windows": (
        ("Firefox", "AppData/Roaming/Mozilla/Firefox/Profiles"),
        ("LibreWolf", "AppData/Roaming/librewolf/Profiles"),
        ("Waterfox", "AppData/Roaming/Waterfox/Profiles"),
    ),
}

# Safari exists on macOS alone, and keeps one history database, not one per
# profile. The second path is where a sandboxed build of this daemon would see
# it, which is also the path that fails without Full Disk Access.
SAFARI_DATABASE_PATHS: tuple[str, ...] = (
    "Library/Safari/History.db",
    "Library/Containers/com.apple.Safari/Data/Library/Safari/History.db",
)


def current_platform() -> str:
    """The operating system name the path tables are keyed by."""
    return platform.system() or "Linux"


def discover_history_profiles(
    home_directory: Path | None = None, system_name: str | None = None
) -> list[HistoryProfile]:
    """Find every browser profile on this machine that holds a history database.

    Ordered so the browser the person most likely browses with comes first,
    and within one browser the default profile before the others.
    """
    home = Path(home_directory or Path.home())
    system = system_name or current_platform()
    profiles: list[HistoryProfile] = []
    for browser_name, relative_root in CHROMIUM_ROOTS.get(system, ()):
        root = home / relative_root
        if not root.is_dir():
            continue
        for profile_directory in _chromium_profile_directories(root):
            database_path = profile_directory / "History"
            if database_path.is_file():
                profiles.append(
                    HistoryProfile(
                        browser_name=browser_name,
                        profile_name=profile_directory.name,
                        family=FAMILY_CHROMIUM,
                        database_path=database_path,
                    )
                )
    for browser_name, relative_root in FIREFOX_ROOTS.get(system, ()):
        root = home / relative_root
        if not root.is_dir():
            continue
        for profile_directory in sorted(root.iterdir()):
            if not profile_directory.is_dir():
                continue
            database_path = profile_directory / "places.sqlite"
            if database_path.is_file():
                profiles.append(
                    HistoryProfile(
                        browser_name=browser_name,
                        profile_name=profile_directory.name,
                        family=FAMILY_FIREFOX,
                        database_path=database_path,
                    )
                )
    if system == "Darwin":
        for relative_path in SAFARI_DATABASE_PATHS:
            database_path = home / relative_path
            if database_path.is_file():
                profiles.append(
                    HistoryProfile(
                        browser_name="Safari",
                        profile_name="Default",
                        family=FAMILY_SAFARI,
                        database_path=database_path,
                    )
                )
                break
    return _deduplicated(profiles)


def _deduplicated(profiles: Iterable[HistoryProfile]) -> list[HistoryProfile]:
    """Drop profiles naming the same database twice (a Snap and a native path)."""
    seen: set[str] = set()
    unique: list[HistoryProfile] = []
    for profile in profiles:
        key = str(profile.database_path.resolve())
        if key in seen:
            continue
        seen.add(key)
        unique.append(profile)
    return unique


def _chromium_profile_directories(root: Path) -> list[Path]:
    """Profile directories of one Chromium browser, the default one first.

    Opera on Linux and Windows keeps its history in the root directory itself
    rather than in a ``Default`` subdirectory, so the root counts as a profile
    when it holds a database.
    """
    directories: list[Path] = []
    if (root / "History").is_file():
        directories.append(root)
    default_directory = root / "Default"
    if default_directory.is_dir():
        directories.append(default_directory)
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name == "Default":
            continue
        if entry.name.startswith("Profile ") or entry.name.startswith("Guest"):
            directories.append(entry)
    return directories


# ---------------------------------------------------------------------------
# Times
# ---------------------------------------------------------------------------


def parse_since(since: str | None, *, default_days: int = 30) -> datetime:
    """Read a caller's ``since`` as a moment in time.

    Accepts an ISO timestamp (what the avatar passes back as a watermark), a
    plain date, or a bare number of days. An empty value means the default
    window, so a first call with no watermark still returns something useful.
    """
    text = str(since or "").strip()
    if not text:
        return datetime.now(UTC) - timedelta(days=default_days)
    if re.fullmatch(r"\d+", text):
        return datetime.now(UTC) - timedelta(days=int(text))
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as parse_error:
        raise BrowserHistoryError(
            f"{since!r} is not a time. Pass an ISO timestamp, a date, or a "
            "number of days."
        ) from parse_error
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _chromium_time(microseconds: Any) -> datetime | None:
    """A Chromium visit time as a moment in time."""
    try:
        value = int(microseconds or 0)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return datetime.fromtimestamp(
        value / 1_000_000 - CHROMIUM_EPOCH_OFFSET_SECONDS, tz=UTC
    )


def _firefox_time(microseconds: Any) -> datetime | None:
    """A Firefox visit time as a moment in time."""
    try:
        value = int(microseconds or 0)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return datetime.fromtimestamp(value / 1_000_000, tz=UTC)


def _safari_time(seconds: Any) -> datetime | None:
    """A Safari visit time as a moment in time."""
    try:
        value = float(seconds or 0)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return datetime.fromtimestamp(value + SAFARI_EPOCH_OFFSET_SECONDS, tz=UTC)


def _to_chromium_time(moment: datetime) -> int:
    """A moment in time as Chromium counts it."""
    return int((moment.timestamp() + CHROMIUM_EPOCH_OFFSET_SECONDS) * 1_000_000)


def _to_firefox_time(moment: datetime) -> int:
    """A moment in time as Firefox counts it."""
    return int(moment.timestamp() * 1_000_000)


def _to_safari_time(moment: datetime) -> float:
    """A moment in time as Safari counts it."""
    return moment.timestamp() - SAFARI_EPOCH_OFFSET_SECONDS


# ---------------------------------------------------------------------------
# What a visit says about itself
# ---------------------------------------------------------------------------

# The search engines whose result pages carry the person's own words, and the
# query parameter each one uses. A search is the most revealing row in a
# history database — it is the person asking a question in their own words —
# so it is pulled out explicitly rather than left inside a URL.
SEARCH_QUERY_PARAMETERS: dict[str, str] = {
    "google.": "q",
    "bing.com": "q",
    "duckduckgo.com": "q",
    "search.brave.com": "q",
    "ecosia.org": "q",
    "startpage.com": "query",
    "yandex.": "text",
    "baidu.com": "wd",
    "youtube.com": "search_query",
    "amazon.": "k",
    "ebay.": "_nkw",
    "reddit.com": "q",
    "stackoverflow.com": "q",
    "github.com": "q",
    "perplexity.ai": "q",
    "chatgpt.com": "q",
}


def hostname_of(url: str) -> str:
    """The host of a URL, lowercased and without its port."""
    try:
        return (urlparse(str(url or "")).hostname or "").lower()
    except ValueError:
        return ""


def search_terms_of(url: str) -> str:
    """The words the person typed, when the address is a search."""
    address = str(url or "")
    host = hostname_of(address)
    if not host:
        return ""
    parameter = next(
        (
            parameter
            for engine, parameter in SEARCH_QUERY_PARAMETERS.items()
            if engine in host
        ),
        "",
    )
    if not parameter:
        return ""
    try:
        values = parse_qs(urlparse(address).query).get(parameter) or []
    except ValueError:
        return ""
    return str(values[0]).strip() if values else ""


def _row(
    *,
    visited_at: datetime,
    url: str,
    title: str,
    profile: HistoryProfile,
    visit_count: int = 0,
    duration_seconds: float = 0.0,
) -> dict[str, Any]:
    """One visit in the shape every platform returns."""
    return {
        "visited_at": visited_at.isoformat(),
        "url": str(url or ""),
        "title": str(title or ""),
        "host": hostname_of(url),
        "search_terms": search_terms_of(url),
        "visit_count": int(visit_count or 0),
        "duration_seconds": round(float(duration_seconds or 0.0), 3),
        "browser_name": profile.browser_name,
        "profile_name": profile.profile_name,
        "family": profile.family,
    }


# ---------------------------------------------------------------------------
# Reading one profile
# ---------------------------------------------------------------------------


def read_profile_visits(
    profile: HistoryProfile, *, since: datetime, until: datetime | None = None,
    limit: int = DEFAULT_ROWS_PER_READ,
) -> list[dict[str, Any]]:
    """Every visit in one profile between two moments, newest last.

    Raises:
        BrowserHistoryError: when the database cannot be read at all. A
            database whose tables are not the expected ones (a browser version
            that moved them) reads as empty rather than raising, so one odd
            browser cannot fail a whole machine's pass.
    """
    row_limit = max(1, min(int(limit or DEFAULT_ROWS_PER_READ), MAXIMUM_ROWS_PER_READ))
    end = until or datetime.now(UTC) + timedelta(days=1)
    try:
        with opened_copy(profile.database_path) as connection:
            if profile.family == FAMILY_CHROMIUM:
                return _read_chromium_visits(connection, profile, since, end, row_limit)
            if profile.family == FAMILY_FIREFOX:
                return _read_firefox_visits(connection, profile, since, end, row_limit)
            return _read_safari_visits(connection, profile, since, end, row_limit)
    except LocalDatabaseError as database_error:
        raise BrowserHistoryError(str(database_error)) from database_error
    except sqlite3.Error as database_error:
        # A file that is not a database at all, or one a crash left damaged.
        # One unreadable profile must not fail the machine's whole pass, so
        # this is raised as the profile's own error and reported per profile.
        raise BrowserHistoryError(
            f"{profile.identifier} could not be read: {database_error}"
        ) from database_error


def _read_chromium_visits(connection, profile, since, until, row_limit):
    """Read ``urls`` joined to ``visits`` — Chrome, Brave, Edge, Opera, Vivaldi, Arc."""
    if not table_exists(connection, "visits") or not table_exists(connection, "urls"):
        return []
    rows = connection.execute(
        "SELECT visits.visit_time AS visit_time, urls.url AS url, urls.title AS title, "
        "urls.visit_count AS visit_count, visits.visit_duration AS visit_duration "
        "FROM visits JOIN urls ON urls.id = visits.url "
        "WHERE visits.visit_time > ? AND visits.visit_time <= ? "
        # Newest first with the limit applied, so a limit keeps the most
        # recent browsing rather than the oldest; reversed below.
        "ORDER BY visits.visit_time DESC LIMIT ?",
        (_to_chromium_time(since), _to_chromium_time(until), row_limit),
    ).fetchall()
    visits: list[dict[str, Any]] = []
    for row in rows:
        visited_at = _chromium_time(row["visit_time"])
        if visited_at is None:
            continue
        visits.append(
            _row(
                visited_at=visited_at,
                url=row["url"],
                title=row["title"],
                profile=profile,
                visit_count=row["visit_count"],
                # Chromium counts the time a page stayed open, in microseconds.
                duration_seconds=(row["visit_duration"] or 0) / 1_000_000,
            )
        )
    visits.reverse()
    return visits


def _read_firefox_visits(connection, profile, since, until, row_limit):
    """Read ``moz_places`` joined to ``moz_historyvisits`` — Firefox and its forks."""
    if not table_exists(connection, "moz_historyvisits") or not table_exists(
        connection, "moz_places"
    ):
        return []
    rows = connection.execute(
        "SELECT moz_historyvisits.visit_date AS visit_date, moz_places.url AS url, "
        "moz_places.title AS title, moz_places.visit_count AS visit_count "
        "FROM moz_historyvisits JOIN moz_places ON moz_places.id = moz_historyvisits.place_id "
        "WHERE moz_historyvisits.visit_date > ? AND moz_historyvisits.visit_date <= ? "
        "ORDER BY moz_historyvisits.visit_date DESC LIMIT ?",
        (_to_firefox_time(since), _to_firefox_time(until), row_limit),
    ).fetchall()
    visits: list[dict[str, Any]] = []
    for row in rows:
        visited_at = _firefox_time(row["visit_date"])
        if visited_at is None:
            continue
        visits.append(
            _row(
                visited_at=visited_at,
                url=row["url"],
                title=row["title"],
                profile=profile,
                visit_count=row["visit_count"],
            )
        )
    visits.reverse()
    return visits


def _read_safari_visits(connection, profile, since, until, row_limit):
    """Read ``history_items`` joined to ``history_visits`` — Safari on macOS."""
    if not table_exists(connection, "history_visits") or not table_exists(
        connection, "history_items"
    ):
        return []
    rows = connection.execute(
        "SELECT history_visits.visit_time AS visit_time, history_items.url AS url, "
        "history_visits.title AS title, history_items.visit_count AS visit_count "
        "FROM history_visits JOIN history_items "
        "ON history_items.id = history_visits.history_item "
        "WHERE history_visits.visit_time > ? AND history_visits.visit_time <= ? "
        "ORDER BY history_visits.visit_time DESC LIMIT ?",
        (_to_safari_time(since), _to_safari_time(until), row_limit),
    ).fetchall()
    visits: list[dict[str, Any]] = []
    for row in rows:
        visited_at = _safari_time(row["visit_time"])
        if visited_at is None:
            continue
        visits.append(
            _row(
                visited_at=visited_at,
                url=row["url"],
                title=row["title"],
                profile=profile,
                visit_count=row["visit_count"],
            )
        )
    visits.reverse()
    return visits


# ---------------------------------------------------------------------------
# Reading the machine
# ---------------------------------------------------------------------------


def read_history(
    *,
    since: str | None = None,
    until: str | None = None,
    limit: int = DEFAULT_ROWS_PER_READ,
    profile_identifier: str = "",
    default_days: int = 30,
    home_directory: Path | None = None,
    system_name: str | None = None,
) -> dict[str, Any]:
    """Every visit on this machine newer than ``since``, across every browser.

    Returns the visits oldest first, the watermark to pass as the next
    ``since``, and the profiles that could not be read with the reason — a
    locked Safari database must be visible to the person, not silently absent.
    """
    start = parse_since(since, default_days=default_days)
    end = parse_since(until, default_days=0) if until else None
    profiles = discover_history_profiles(home_directory, system_name)
    if profile_identifier:
        profiles = [
            profile for profile in profiles if profile.identifier == profile_identifier
        ]
        if not profiles:
            raise BrowserHistoryError(f"No browser profile named {profile_identifier!r}.")
    visits: list[dict[str, Any]] = []
    unreadable: list[dict[str, str]] = []
    for profile in profiles:
        try:
            visits.extend(
                read_profile_visits(profile, since=start, until=end, limit=limit)
            )
        except BrowserHistoryError as read_error:
            unreadable.append({"profile": profile.identifier, "reason": str(read_error)})
    visits.sort(key=lambda visit: visit["visited_at"])
    if len(visits) > limit:
        visits = visits[-int(limit):]
    latest = visits[-1]["visited_at"] if visits else start.isoformat()
    return {
        "visits": visits,
        "visit_count": len(visits),
        "since": start.isoformat(),
        "watermark": latest,
        "profiles_read": [profile.identifier for profile in profiles],
        "profiles_unreadable": unreadable,
        "platform": system_name or current_platform(),
    }


def summarize_activity(
    *,
    since: str | None = None,
    default_days: int = 30,
    home_directory: Path | None = None,
    system_name: str | None = None,
) -> dict[str, Any]:
    """How much browsing has happened since a watermark, without returning it.

    This is the question the avatar asks every few minutes while the person is
    working: it costs one indexed count per profile, no rows cross the network,
    and an answer of zero means the analysis does not run and nothing is spent.
    """
    start = parse_since(since, default_days=default_days)
    profiles = discover_history_profiles(home_directory, system_name)
    total = 0
    latest: str | None = None
    per_profile: list[dict[str, Any]] = []
    unreadable: list[dict[str, str]] = []
    for profile in profiles:
        try:
            counted, newest = _count_visits_since(profile, start)
        except BrowserHistoryError as read_error:
            unreadable.append({"profile": profile.identifier, "reason": str(read_error)})
            continue
        total += counted
        if newest and (latest is None or newest > latest):
            latest = newest
        per_profile.append(
            {"profile": profile.identifier, "visit_count": counted, "latest_visit": newest}
        )
    return {
        "visit_count": total,
        "since": start.isoformat(),
        "latest_visit": latest,
        "watermark": latest or start.isoformat(),
        "profiles": per_profile,
        "profiles_unreadable": unreadable,
        "platform": system_name or current_platform(),
    }


def _count_visits_since(profile: HistoryProfile, since: datetime) -> tuple[int, str | None]:
    """How many visits one profile holds after a moment, and the newest one."""
    queries = {
        FAMILY_CHROMIUM: (
            "SELECT COUNT(*) AS visits_counted, MAX(visit_time) AS newest FROM visits "
            "WHERE visit_time > ?",
            _to_chromium_time,
            _chromium_time,
            "visits",
        ),
        FAMILY_FIREFOX: (
            "SELECT COUNT(*) AS visits_counted, MAX(visit_date) AS newest "
            "FROM moz_historyvisits WHERE visit_date > ?",
            _to_firefox_time,
            _firefox_time,
            "moz_historyvisits",
        ),
        FAMILY_SAFARI: (
            "SELECT COUNT(*) AS visits_counted, MAX(visit_time) AS newest "
            "FROM history_visits WHERE visit_time > ?",
            _to_safari_time,
            _safari_time,
            "history_visits",
        ),
    }
    statement, to_browser_time, from_browser_time, table_name = queries[profile.family]
    try:
        with opened_copy(profile.database_path) as connection:
            if not table_exists(connection, table_name):
                return 0, None
            row = connection.execute(statement, (to_browser_time(since),)).fetchone()
    except LocalDatabaseError as database_error:
        raise BrowserHistoryError(str(database_error)) from database_error
    counted = int((row["visits_counted"] if row else 0) or 0)
    newest_time = from_browser_time(row["newest"]) if row else None
    return counted, newest_time.isoformat() if newest_time else None


def history_is_exposed() -> bool:
    """Whether the person has left browsing history readable by their avatar."""
    value = os.getenv("EXPOSE_BROWSER_HISTORY")
    if value is None or not value.strip():
        return True
    return value.strip().lower() in {"1", "true", "yes", "on"}
