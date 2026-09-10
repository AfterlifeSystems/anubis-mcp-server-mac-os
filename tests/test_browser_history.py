"""Reading the person's browsing history, on every platform and browser family.

Real databases are built in ``tmp_path`` in each of the three layouts — the
Chromium ``History``, the Firefox ``places.sqlite``, and the Safari
``History.db`` — with each family's own epoch, so the reader is exercised end
to end with no browser installed and on whichever platform the tests run on.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.server.browser_history import (
    CHROMIUM_EPOCH_OFFSET_SECONDS,
    SAFARI_EPOCH_OFFSET_SECONDS,
    BrowserHistoryError,
    HistoryProfile,
    discover_history_profiles,
    hostname_of,
    parse_since,
    read_history,
    read_profile_visits,
    search_terms_of,
    summarize_activity,
)
from src.server.history_tools import browser_history, browsing_activity, history_profiles
from src.server.local_databases import LocalDatabaseError, opened_copy

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


def chromium_time(moment: datetime) -> int:
    return int((moment.timestamp() + CHROMIUM_EPOCH_OFFSET_SECONDS) * 1_000_000)


def firefox_time(moment: datetime) -> int:
    return int(moment.timestamp() * 1_000_000)


def safari_time(moment: datetime) -> float:
    return moment.timestamp() - SAFARI_EPOCH_OFFSET_SECONDS


def build_chromium_history(home: Path, visits, *, relative="  ", profile_name="Default"):
    """Write a Chromium history database; `visits` are (moment, url, title)."""
    directory = home / (relative.strip() or ".config/google-chrome") / profile_name
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "History"
    connection = sqlite3.connect(str(path))
    connection.execute(
        "CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT, title TEXT, visit_count INTEGER)"
    )
    connection.execute(
        "CREATE TABLE visits (id INTEGER PRIMARY KEY, url INTEGER, visit_time INTEGER, "
        "visit_duration INTEGER)"
    )
    for index, (moment, url, title) in enumerate(visits, start=1):
        connection.execute(
            "INSERT INTO urls (id, url, title, visit_count) VALUES (?,?,?,?)",
            (index, url, title, 1),
        )
        connection.execute(
            "INSERT INTO visits (id, url, visit_time, visit_duration) VALUES (?,?,?,?)",
            (index, index, chromium_time(moment), 30_000_000),
        )
    connection.commit()
    connection.close()
    return path


def build_firefox_history(home: Path, visits, *, relative=".mozilla/firefox"):
    directory = home / relative / "abc123.default-release"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "places.sqlite"
    connection = sqlite3.connect(str(path))
    connection.execute(
        "CREATE TABLE moz_places (id INTEGER PRIMARY KEY, url TEXT, title TEXT, visit_count INTEGER)"
    )
    connection.execute(
        "CREATE TABLE moz_historyvisits (id INTEGER PRIMARY KEY, place_id INTEGER, visit_date INTEGER)"
    )
    for index, (moment, url, title) in enumerate(visits, start=1):
        connection.execute(
            "INSERT INTO moz_places (id, url, title, visit_count) VALUES (?,?,?,?)",
            (index, url, title, 2),
        )
        connection.execute(
            "INSERT INTO moz_historyvisits (id, place_id, visit_date) VALUES (?,?,?)",
            (index, index, firefox_time(moment)),
        )
    connection.commit()
    connection.close()
    return path


def build_safari_history(home: Path, visits):
    directory = home / "Library/Safari"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "History.db"
    connection = sqlite3.connect(str(path))
    connection.execute(
        "CREATE TABLE history_items (id INTEGER PRIMARY KEY, url TEXT, visit_count INTEGER)"
    )
    connection.execute(
        "CREATE TABLE history_visits (id INTEGER PRIMARY KEY, history_item INTEGER, "
        "visit_time REAL, title TEXT)"
    )
    for index, (moment, url, title) in enumerate(visits, start=1):
        connection.execute(
            "INSERT INTO history_items (id, url, visit_count) VALUES (?,?,?)", (index, url, 3)
        )
        connection.execute(
            "INSERT INTO history_visits (id, history_item, visit_time, title) VALUES (?,?,?,?)",
            (index, index, safari_time(moment), title),
        )
    connection.commit()
    connection.close()
    return path


SAMPLE_VISITS = [
    (NOW - timedelta(hours=5), "https://news.ycombinator.com/", "Hacker News"),
    (
        NOW - timedelta(hours=3),
        "https://www.google.com/search?q=qlora+fine+tuning+llama&sourceid=chrome",
        "qlora fine tuning llama - Google Search",
    ),
    (NOW - timedelta(minutes=20), "https://github.com/langchain-ai/langgraph", "LangGraph"),
]


# ---------------------------------------------------------------------------
# Reading each family
# ---------------------------------------------------------------------------


def test_chromium_history_is_read_with_its_own_epoch(tmp_path):
    path = build_chromium_history(tmp_path, SAMPLE_VISITS)
    profile = HistoryProfile("Google Chrome", "Default", "chromium", path)
    visits = read_profile_visits(profile, since=NOW - timedelta(days=1))
    assert [visit["title"] for visit in visits] == [
        "Hacker News",
        "qlora fine tuning llama - Google Search",
        "LangGraph",
    ]
    assert visits[0]["visited_at"].startswith("2026-09-10T07:00")
    assert visits[0]["duration_seconds"] == 30.0
    assert visits[0]["browser_name"] == "Google Chrome"


def test_firefox_history_is_read_with_its_own_epoch(tmp_path):
    path = build_firefox_history(tmp_path, SAMPLE_VISITS)
    profile = HistoryProfile("Firefox", "abc123.default-release", "firefox", path)
    visits = read_profile_visits(profile, since=NOW - timedelta(days=1))
    assert len(visits) == 3
    assert visits[-1]["url"] == "https://github.com/langchain-ai/langgraph"
    assert visits[-1]["visited_at"].startswith("2026-09-10T11:40")


def test_safari_history_is_read_with_its_own_epoch(tmp_path):
    path = build_safari_history(tmp_path, SAMPLE_VISITS)
    profile = HistoryProfile("Safari", "Default", "safari", path)
    visits = read_profile_visits(profile, since=NOW - timedelta(days=1))
    assert len(visits) == 3
    assert visits[0]["title"] == "Hacker News"
    assert visits[0]["visited_at"].startswith("2026-09-10T07:00")


def test_a_browser_version_that_moved_its_tables_reads_as_empty_not_as_an_error(tmp_path):
    path = tmp_path / "History"
    connection = sqlite3.connect(str(path))
    connection.execute("CREATE TABLE something_else (id INTEGER)")
    connection.commit()
    connection.close()
    profile = HistoryProfile("Google Chrome", "Default", "chromium", path)
    assert read_profile_visits(profile, since=NOW - timedelta(days=1)) == []


def test_a_missing_database_is_refused_in_the_owners_terms(tmp_path):
    profile = HistoryProfile("Google Chrome", "Default", "chromium", tmp_path / "nothing")
    with pytest.raises(BrowserHistoryError, match="No database at"):
        read_profile_visits(profile, since=NOW - timedelta(days=1))


def test_a_database_a_running_browser_holds_open_is_still_read(tmp_path):
    path = build_chromium_history(tmp_path, SAMPLE_VISITS)
    holder = sqlite3.connect(str(path))
    holder.execute("BEGIN EXCLUSIVE")
    try:
        profile = HistoryProfile("Google Chrome", "Default", "chromium", path)
        assert len(read_profile_visits(profile, since=NOW - timedelta(days=1))) == 3
    finally:
        holder.rollback()
        holder.close()


# ---------------------------------------------------------------------------
# Finding the browsers, on each platform
# ---------------------------------------------------------------------------


def test_linux_profiles_are_found_including_snap_and_a_second_profile(tmp_path):
    build_chromium_history(tmp_path, SAMPLE_VISITS)
    build_chromium_history(tmp_path, SAMPLE_VISITS, profile_name="Profile 2")
    build_chromium_history(
        tmp_path, SAMPLE_VISITS, relative="snap/chromium/common/chromium"
    )
    build_firefox_history(tmp_path, SAMPLE_VISITS)
    build_firefox_history(tmp_path, SAMPLE_VISITS, relative="snap/firefox/common/.mozilla/firefox")
    identifiers = [
        profile.identifier
        for profile in discover_history_profiles(tmp_path, system_name="Linux")
    ]
    assert identifiers[0] == "Google Chrome:Default"
    assert "Google Chrome:Profile 2" in identifiers
    assert "Chromium:Default" in identifiers
    assert any(identifier.startswith("Firefox:") for identifier in identifiers)


def test_macos_profiles_are_found_including_safari(tmp_path):
    build_chromium_history(
        tmp_path, SAMPLE_VISITS, relative="Library/Application Support/Google/Chrome"
    )
    build_firefox_history(
        tmp_path, SAMPLE_VISITS, relative="Library/Application Support/Firefox/Profiles"
    )
    build_safari_history(tmp_path, SAMPLE_VISITS)
    profiles = discover_history_profiles(tmp_path, system_name="Darwin")
    identifiers = [profile.identifier for profile in profiles]
    assert "Google Chrome:Default" in identifiers
    assert "Safari:Default" in identifiers
    assert any(profile.family == "firefox" for profile in profiles)


def test_windows_profiles_are_found_under_the_application_data_layout(tmp_path):
    build_chromium_history(
        tmp_path, SAMPLE_VISITS, relative="AppData/Local/Google/Chrome/User Data"
    )
    build_firefox_history(
        tmp_path, SAMPLE_VISITS, relative="AppData/Roaming/Mozilla/Firefox/Profiles"
    )
    identifiers = [
        profile.identifier
        for profile in discover_history_profiles(tmp_path, system_name="Windows")
    ]
    assert "Google Chrome:Default" in identifiers
    assert any(identifier.startswith("Firefox:") for identifier in identifiers)


def test_safari_is_not_looked_for_off_macos(tmp_path):
    build_safari_history(tmp_path, SAMPLE_VISITS)
    profiles = discover_history_profiles(tmp_path, system_name="Linux")
    assert not any(profile.family == "safari" for profile in profiles)


def test_one_database_reachable_by_two_paths_is_listed_once(tmp_path):
    build_chromium_history(tmp_path, SAMPLE_VISITS)
    profiles = discover_history_profiles(tmp_path, system_name="Linux")
    paths = [str(profile.database_path) for profile in profiles]
    assert len(paths) == len(set(paths))


# ---------------------------------------------------------------------------
# What a visit says about itself
# ---------------------------------------------------------------------------


def test_the_words_typed_into_a_search_are_pulled_out_of_the_address():
    assert (
        search_terms_of("https://www.google.com/search?q=qlora+fine+tuning&hl=en")
        == "qlora fine tuning"
    )
    assert search_terms_of("https://duckduckgo.com/?q=best+espresso+grinder") == (
        "best espresso grinder"
    )
    assert search_terms_of("https://www.youtube.com/results?search_query=langgraph") == (
        "langgraph"
    )
    assert search_terms_of("https://github.com/langchain-ai/langgraph") == ""
    assert search_terms_of("not a url") == ""


def test_the_host_is_read_off_the_address():
    assert hostname_of("https://News.YCombinator.com:443/item?id=1") == "news.ycombinator.com"
    assert hostname_of("") == ""


# ---------------------------------------------------------------------------
# Watermarks: reading only what is new
# ---------------------------------------------------------------------------


def test_a_since_reads_as_a_timestamp_a_date_or_a_number_of_days():
    assert parse_since("2026-09-10T00:00:00+00:00").year == 2026
    assert parse_since("2026-09-10").month == 9
    assert round((datetime.now(UTC) - parse_since("7")).total_seconds() / 86400) == 7
    assert round((datetime.now(UTC) - parse_since("")).total_seconds() / 86400) == 30
    with pytest.raises(BrowserHistoryError, match="is not a time"):
        parse_since("last tuesday")


def test_only_visits_newer_than_the_watermark_come_back(tmp_path):
    build_chromium_history(tmp_path, SAMPLE_VISITS)
    everything = read_history(
        since=(NOW - timedelta(days=1)).isoformat(),
        home_directory=tmp_path,
        system_name="Linux",
    )
    assert everything["visit_count"] == 3
    newer = read_history(
        since=(NOW - timedelta(hours=1)).isoformat(),
        home_directory=tmp_path,
        system_name="Linux",
    )
    assert newer["visit_count"] == 1
    assert newer["visits"][0]["title"] == "LangGraph"


def test_the_watermark_returned_is_the_newest_visit_and_reads_nothing_twice(tmp_path):
    build_chromium_history(tmp_path, SAMPLE_VISITS)
    first = read_history(
        since=(NOW - timedelta(days=1)).isoformat(),
        home_directory=tmp_path,
        system_name="Linux",
    )
    assert first["watermark"] == first["visits"][-1]["visited_at"]
    second = read_history(
        since=first["watermark"], home_directory=tmp_path, system_name="Linux"
    )
    assert second["visit_count"] == 0
    assert second["watermark"] == first["watermark"]


def test_visits_from_several_browsers_are_merged_in_time_order(tmp_path):
    build_chromium_history(tmp_path, [SAMPLE_VISITS[0], SAMPLE_VISITS[2]])
    build_firefox_history(tmp_path, [SAMPLE_VISITS[1]])
    read = read_history(
        since=(NOW - timedelta(days=1)).isoformat(),
        home_directory=tmp_path,
        system_name="Linux",
    )
    assert [visit["visited_at"] for visit in read["visits"]] == sorted(
        visit["visited_at"] for visit in read["visits"]
    )
    assert {visit["browser_name"] for visit in read["visits"]} == {
        "Google Chrome",
        "Firefox",
    }


def test_a_limit_keeps_the_newest_visits_not_the_oldest(tmp_path):
    build_chromium_history(tmp_path, SAMPLE_VISITS)
    read = read_history(
        since=(NOW - timedelta(days=1)).isoformat(),
        limit=1,
        home_directory=tmp_path,
        system_name="Linux",
    )
    assert read["visit_count"] == 1
    assert read["visits"][0]["title"] == "LangGraph"


def test_the_cheap_summary_counts_without_returning_the_visits(tmp_path):
    build_chromium_history(tmp_path, SAMPLE_VISITS)
    summary = summarize_activity(
        since=(NOW - timedelta(days=1)).isoformat(),
        home_directory=tmp_path,
        system_name="Linux",
    )
    assert summary["visit_count"] == 3
    assert "visits" not in summary
    assert summary["latest_visit"].startswith("2026-09-10T11:40")
    assert summary["profiles"][0]["visit_count"] == 3


def test_a_quiet_period_counts_zero_so_nothing_is_analysed(tmp_path):
    build_chromium_history(tmp_path, SAMPLE_VISITS)
    summary = summarize_activity(
        since=NOW.isoformat(), home_directory=tmp_path, system_name="Linux"
    )
    assert summary["visit_count"] == 0
    assert summary["latest_visit"] is None


def test_a_profile_that_cannot_be_read_is_named_rather_than_silently_skipped(tmp_path):
    build_chromium_history(tmp_path, SAMPLE_VISITS)
    corrupt = tmp_path / ".mozilla/firefox/broken.default"
    corrupt.mkdir(parents=True)
    (corrupt / "places.sqlite").write_text("this is not a database")
    read = read_history(
        since=(NOW - timedelta(days=1)).isoformat(),
        home_directory=tmp_path,
        system_name="Linux",
    )
    assert read["visit_count"] == 3
    assert read["profiles_unreadable"]
    assert "broken.default" in read["profiles_unreadable"][0]["profile"]


def test_a_named_profile_is_the_only_one_read(tmp_path):
    build_chromium_history(tmp_path, SAMPLE_VISITS)
    build_firefox_history(tmp_path, SAMPLE_VISITS)
    read = read_history(
        since=(NOW - timedelta(days=1)).isoformat(),
        profile_identifier="Firefox:abc123.default-release",
        home_directory=tmp_path,
        system_name="Linux",
    )
    assert {visit["browser_name"] for visit in read["visits"]} == {"Firefox"}
    with pytest.raises(BrowserHistoryError, match="No browser profile named"):
        read_history(
            since="1", profile_identifier="Nothing:Here", home_directory=tmp_path,
            system_name="Linux",
        )


# ---------------------------------------------------------------------------
# The tools, and the switch that turns them off
# ---------------------------------------------------------------------------


def test_the_tools_answer_from_the_machine(tmp_path, monkeypatch):
    build_chromium_history(tmp_path, SAMPLE_VISITS)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("EXPOSE_BROWSER_HISTORY", raising=False)
    summary = asyncio.run(browsing_activity(since=(NOW - timedelta(days=1)).isoformat()))
    assert summary["visit_count"] == 3
    read = asyncio.run(browser_history(since=(NOW - timedelta(days=1)).isoformat()))
    assert read["visit_count"] == 3
    assert asyncio.run(history_profiles())[0]["browser_name"] == "Google Chrome"


def test_switching_history_off_answers_with_a_refusal_and_no_data(tmp_path, monkeypatch):
    build_chromium_history(tmp_path, SAMPLE_VISITS)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("EXPOSE_BROWSER_HISTORY", "false")
    summary = asyncio.run(browsing_activity())
    assert summary["disabled"] is True
    assert "EXPOSE_BROWSER_HISTORY" in summary["reason"]
    assert asyncio.run(browser_history())["disabled"] is True
    assert asyncio.run(history_profiles()) == []


# ---------------------------------------------------------------------------
# The shared snapshot helper
# ---------------------------------------------------------------------------


def test_a_database_is_copied_with_its_write_ahead_log(tmp_path):
    path = build_chromium_history(tmp_path, SAMPLE_VISITS)
    path.with_name("History-wal").write_bytes(b"")
    with opened_copy(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM visits").fetchone()[0] == 3


def test_the_copy_is_cleaned_up_even_when_the_reader_raises(tmp_path):
    path = build_chromium_history(tmp_path, SAMPLE_VISITS)
    with pytest.raises(ValueError):
        with opened_copy(path):
            raise ValueError("the reader failed")
    assert not list(Path("/tmp").glob("neuralnexus-db-*/History"))


def test_a_missing_file_is_refused_before_anything_is_copied(tmp_path):
    with pytest.raises(LocalDatabaseError, match="No database at"):
        with opened_copy(tmp_path / "absent"):
            pass
