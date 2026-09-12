from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest
from fastmcp.exceptions import ResourceError

from src.server import git_tools
from src.server.app import _resolve_allowed_dir, build_server_settings, create_mcp_server

# Every test here drives a real git binary. The one exception -- "git is not
# installed" -- monkeypatches subprocess instead, so it stays outside the skip.
pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git is not installed"
)


def _git(repo: Path, *args: str, env: dict | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    return result.stdout


def _commit(repo: Path, message: str, when: str | None = None) -> str:
    """Commit everything staged/unstaged with a fixed identity.

    `when` sets GIT_COMMITTER_DATE as well as GIT_AUTHOR_DATE: `git log --since`
    filters on the *committer* date, so backdating the author alone would leave
    the commit inside the window and quietly break the time-window tests.
    """
    import os

    env = dict(os.environ)
    if when:
        env["GIT_AUTHOR_DATE"] = when
        env["GIT_COMMITTER_DATE"] = when
    _git(repo, "add", "-A")
    _git(
        repo,
        "-c",
        "user.email=test@example.com",
        "-c",
        "user.name=Test User",
        "commit",
        "-m",
        message,
        env=env,
    )
    return _git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture
def make_repo(tmp_path: Path):
    def _make(name: str = "repo", *, empty: bool = False) -> Path:
        repo = tmp_path / name
        repo.mkdir(parents=True)
        _git(repo, "init", "-b", "main", "-q", ".")
        if not empty:
            (repo / "README.md").write_text("hello\n")
            _commit(repo, "initial commit")
        return repo

    return _make


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #


def test_discover_finds_repo_at_root_and_nested(tmp_path: Path, make_repo) -> None:
    top = make_repo("top")
    nested_parent = tmp_path / "workspace" / "projects"
    nested_parent.mkdir(parents=True)
    nested = make_repo("workspace/projects/deep")

    result = git_tools.discover_repositories((tmp_path,))
    found = {entry["path"] for entry in result["repositories"]}

    assert str(top) in found
    assert str(nested) in found
    assert result["truncated"] is False
    assert result["scanned_roots"] == [str(tmp_path)]
    entry = next(e for e in result["repositories"] if e["path"] == str(top))
    assert entry["branch"] == "main"
    assert entry["is_dirty"] is False
    assert entry["is_worktree"] is False
    assert entry["last_commit"]["subject"] == "initial commit"
    assert entry["error"] is None


def test_discover_reports_dirty_state(make_repo) -> None:
    repo = make_repo()
    (repo / "README.md").write_text("changed\n")

    result = git_tools.discover_repositories((repo.parent,))
    entry = next(e for e in result["repositories"] if e["path"] == str(repo))
    assert entry["is_dirty"] is True


def test_discover_flags_linked_worktree(tmp_path: Path, make_repo) -> None:
    repo = make_repo("main-repo")
    worktree = tmp_path / "side-worktree"
    _git(repo, "worktree", "add", "-q", str(worktree), "-b", "side")

    result = git_tools.discover_repositories((tmp_path,))
    entry = next(e for e in result["repositories"] if e["path"] == str(worktree))
    assert entry["is_worktree"] is True
    assert entry["branch"] == "side"


def test_discover_skips_non_repos_hidden_dirs_and_symlinks(
    tmp_path: Path, make_repo
) -> None:
    make_repo("real")
    (tmp_path / "not-a-repo").mkdir()
    hidden = tmp_path / ".hidden"
    hidden.mkdir()
    _git_init_at(hidden / "secret")
    (tmp_path / "link").symlink_to(tmp_path / "real", target_is_directory=True)

    result = git_tools.discover_repositories((tmp_path,))
    paths = {entry["path"] for entry in result["repositories"]}

    assert paths == {str(tmp_path / "real")}


def test_discover_respects_max_depth(tmp_path: Path) -> None:
    deep = tmp_path / "a" / "b" / "c" / "too-deep"
    deep.mkdir(parents=True)
    _git_init_at(deep)

    assert git_tools.discover_repositories((tmp_path,))["repositories"] == []
    shallower = git_tools.discover_repositories((tmp_path,), max_depth=4)
    assert [e["path"] for e in shallower["repositories"]] == [str(deep)]


def test_discover_truncates_at_max_repos(tmp_path: Path, make_repo) -> None:
    make_repo("one")
    make_repo("two")

    result = git_tools.discover_repositories((tmp_path,), max_repos=1)
    assert len(result["repositories"]) == 1
    assert result["truncated"] is True


def test_discover_handles_empty_repo_without_error(make_repo) -> None:
    repo = make_repo("fresh", empty=True)

    result = git_tools.discover_repositories((repo.parent,))
    entry = next(e for e in result["repositories"] if e["path"] == str(repo))
    assert entry["last_commit"] is None
    assert entry["error"] is None
    assert entry["branch"] == "main"


def _git_init_at(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "-C", str(path), "init", "-b", "main", "-q", "."],
        check=True,
        capture_output=True,
    )


# --------------------------------------------------------------------------- #
# change summary
# --------------------------------------------------------------------------- #


def test_summary_excludes_commits_outside_the_window(make_repo) -> None:
    repo = make_repo()
    (repo / "old.txt").write_text("old\n")
    _commit(repo, "three days ago work", when="2026-08-09T09:00:00-04:00")
    (repo / "new.txt").write_text("new\n")
    recent = _commit(repo, "recent work")

    result = git_tools.summarize_changes(repo, since="24 hours ago")
    subjects = [c["subject"] for c in result["commits"]]

    assert "recent work" in subjects
    assert "three days ago work" not in subjects
    assert result["commits"][0]["hash"] == recent
    assert result["total"]["commits"] == len(result["commits"])
    assert result["since"] == "24 hours ago"
    assert result["branch"] == "main"


def test_summary_brackets_a_window_with_since_and_until(make_repo) -> None:
    repo = make_repo()
    (repo / "a.txt").write_text("a\n")
    _commit(repo, "before window", when="2026-08-01T09:00:00-04:00")
    (repo / "b.txt").write_text("b\n")
    _commit(repo, "inside window", when="2026-08-05T09:00:00-04:00")
    (repo / "c.txt").write_text("c\n")
    _commit(repo, "after window", when="2026-08-10T09:00:00-04:00")

    result = git_tools.summarize_changes(
        repo, since="2026-08-04T00:00:00-04:00", until="2026-08-06T00:00:00-04:00"
    )

    assert [c["subject"] for c in result["commits"]] == ["inside window"]
    assert result["commits"][0]["files"] == [
        {"path": "b.txt", "insertions": 1, "deletions": 0}
    ]
    assert result["until"] == "2026-08-06T00:00:00-04:00"


def test_summary_reports_uncommitted_state(make_repo) -> None:
    repo = make_repo()
    (repo / "README.md").write_text("hello\nmore\n")
    (repo / "scratch.txt").write_text("untracked\n")

    result = git_tools.summarize_changes(repo, since="24 hours ago")
    states = {entry["path"]: entry["state"] for entry in result["uncommitted"]["status"]}

    assert states["README.md"] == " M"
    assert states["scratch.txt"] == "??"
    assert result["uncommitted"]["status_truncated"] is False
    assert result["uncommitted"]["diff_stat"]["insertions"] == 1
    assert result["uncommitted"]["diff_stat"]["files_changed"] == 1


def test_summary_can_skip_uncommitted_state(make_repo) -> None:
    repo = make_repo()
    (repo / "README.md").write_text("changed\n")

    result = git_tools.summarize_changes(repo, include_uncommitted=False)
    assert result["uncommitted"] is None


def test_summary_truncates_commits_and_files(make_repo, monkeypatch) -> None:
    repo = make_repo()
    for index in range(4):
        (repo / f"file{index}.txt").write_text(f"{index}\n")
        _commit(repo, f"commit {index}")

    result = git_tools.summarize_changes(repo, max_commits=2)
    assert len(result["commits"]) == 2
    assert result["commits_truncated"] is True

    monkeypatch.setattr(git_tools, "MAX_FILES_PER_COMMIT", 1)
    (repo / "x.txt").write_text("x\n")
    (repo / "y.txt").write_text("y\n")
    _commit(repo, "two files at once")

    capped = git_tools.summarize_changes(repo, max_commits=1)
    assert capped["commits"][0]["files_changed"] == 2
    assert len(capped["commits"][0]["files"]) == 1
    assert capped["commits"][0]["files_truncated"] is True


def test_summary_on_empty_repo_returns_no_commits(make_repo) -> None:
    repo = make_repo("fresh", empty=True)

    result = git_tools.summarize_changes(repo)
    assert result["commits"] == []
    assert result["commits_truncated"] is False
    assert result["total"] == {"commits": 0, "insertions": 0, "deletions": 0}


# --------------------------------------------------------------------------- #
# diff
# --------------------------------------------------------------------------- #


def test_diff_working_tree_shows_hunk_and_untracked_names(make_repo) -> None:
    repo = make_repo()
    (repo / "README.md").write_text("hello\nadded line\n")
    (repo / "scratch.txt").write_text("secret contents\n")

    result = git_tools.get_diff(repo)

    assert result["mode"] == "working_tree"
    assert "+added line" in result["diff"]
    assert result["untracked_files"] == ["scratch.txt"]
    assert result["untracked_truncated"] is False
    # Names only -- untracked contents must not be inlined into the diff.
    assert "secret contents" not in result["diff"]
    assert result["truncated"] is False


def test_diff_commit_mode_returns_that_patch(make_repo) -> None:
    repo = make_repo()
    (repo / "feature.txt").write_text("feature\n")
    sha = _commit(repo, "add feature")

    result = git_tools.get_diff(repo, commit=sha)
    assert result["mode"] == "commit"
    assert result["head"] == sha
    assert "feature.txt" in result["diff"]
    assert "+feature" in result["diff"]


def test_diff_range_mode_returns_base_to_head(make_repo) -> None:
    repo = make_repo()
    base = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "later.txt").write_text("later\n")
    _commit(repo, "later work")

    result = git_tools.get_diff(repo, base=base)
    assert result["mode"] == "range"
    assert result["base"] == base
    assert "later.txt" in result["diff"]


def test_diff_since_mode_covers_the_window(make_repo) -> None:
    repo = make_repo()
    (repo / "old.txt").write_text("old\n")
    _commit(repo, "old work", when="2026-08-01T09:00:00-04:00")
    (repo / "fresh.txt").write_text("fresh\n")
    _commit(repo, "fresh work")

    result = git_tools.get_diff(repo, since="2026-08-05T00:00:00-04:00")
    assert result["mode"] == "since"
    assert "fresh.txt" in result["diff"]
    assert "old.txt" not in result["diff"]


def test_diff_truncates_to_max_bytes(make_repo) -> None:
    repo = make_repo()
    # The file has to be tracked and then modified: `git diff HEAD` never
    # includes untracked content, so an untracked file yields an empty diff.
    (repo / "big.txt").write_text("".join(f"line {n}\n" for n in range(2000)))
    _commit(repo, "add big file")
    (repo / "big.txt").write_text("".join(f"changed {n}\n" for n in range(2000)))

    result = git_tools.get_diff(repo, max_bytes=200)
    assert result["truncated"] is True
    assert len(result["diff"].encode("utf-8")) <= 200
    assert result["total_bytes"] > 200


@pytest.mark.parametrize("bad_ref", ["--output=/tmp/pwn", "-R"])
def test_diff_rejects_option_like_refs(make_repo, bad_ref: str) -> None:
    repo = make_repo()
    with pytest.raises(ResourceError, match="not a valid git ref"):
        git_tools.get_diff(repo, commit=bad_ref)
    with pytest.raises(ResourceError, match="not a valid git ref"):
        git_tools.get_diff(repo, base=bad_ref)


def test_diff_rejects_conflicting_modes(make_repo) -> None:
    repo = make_repo()
    with pytest.raises(ResourceError, match="only one of"):
        git_tools.get_diff(repo, commit="HEAD", since="24 hours ago")
    with pytest.raises(ResourceError, match="head is only valid"):
        git_tools.get_diff(repo, head="HEAD")


# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #


def test_non_repo_directory_is_rejected(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(ResourceError, match="Not a git repository"):
        git_tools.summarize_changes(plain)
    with pytest.raises(ResourceError, match="Not a git repository"):
        git_tools.get_diff(plain)


def test_missing_git_binary_is_reported(make_repo, monkeypatch) -> None:
    repo = make_repo()

    def _no_git(*_args, **_kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(git_tools.subprocess, "run", _no_git)
    with pytest.raises(ResourceError, match="git is not installed"):
        git_tools.summarize_changes(repo)


# --------------------------------------------------------------------------- #
# allow-list enforcement
# --------------------------------------------------------------------------- #


def test_repo_outside_allowed_roots_is_denied(tmp_path: Path, make_repo) -> None:
    watched = tmp_path / "watched"
    watched.mkdir()
    outside = make_repo("outside")

    with pytest.raises(ResourceError, match="Path not allowed"):
        _resolve_allowed_dir(str(outside), (watched.resolve(),))


def test_tool_call_denies_repo_outside_allowed_roots(tmp_path: Path, make_repo) -> None:
    """End-to-end through the MCP layer: the closure must enforce the allow-list."""
    from fastmcp import Client

    watched = tmp_path / "watched"
    watched.mkdir()
    outside = make_repo("outside")

    settings = build_server_settings(
        watched_roots=[str(watched)], require_device_auth=False
    )
    mcp = create_mcp_server(settings)

    async def _call():
        async with Client(mcp) as client:
            tool_names = {tool.name for tool in await client.list_tools()}
            assert {
                "list_git_repositories",
                "git_changes_summary",
                "git_diff",
            } <= tool_names
            return await client.call_tool(
                "git_changes_summary", {"repo_path": str(outside)}
            )

    with pytest.raises(Exception, match="Path not allowed"):
        asyncio.run(_call())
