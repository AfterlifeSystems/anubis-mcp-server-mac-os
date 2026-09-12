"""Read-only git inspection helpers behind the git progress-summary MCP tools.

The avatar calls these to answer "what changed in my repos lately?". Everything
here is strictly read-only: invocations are argv lists (never ``shell=True``),
always go through ``git -C <repo> --no-optional-locks``, and are limited to the
``rev-parse``, ``symbolic-ref``, ``rev-list``, ``log``, ``status``, ``diff``,
``show``, and ``hash-object`` subcommands.

Path allow-listing is *not* done here. The tool closures in ``src.server.app``
resolve user input through ``_resolve_allowed_dir`` first and hand this module an
already-validated ``Path``; keeping it that way is also why this module never
imports from ``app`` (which would be a cycle).
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from collections import deque
from pathlib import Path

from fastmcp.exceptions import ResourceError

GIT_TIMEOUT_SECONDS = 15
DISCOVERY_TIME_BUDGET_SECONDS = 45
MAX_SCAN_DEPTH = 3
MAX_REPOS = 50
MAX_COMMITS_LIMIT = 100
MAX_FILES_PER_COMMIT = 20
MAX_STATUS_FILES = 100
MAX_UNTRACKED_FILES = 50
DEFAULT_DIFF_BYTES = 100_000
MAX_DIFF_BYTES = 500_000

# ASCII record/unit separators: safe delimiters for `git log --pretty=format:`
# because neither can appear in a hash, author name, ISO date, or subject line.
_RECORD_SEP = "\x1e"
_FIELD_SEP = "\x1f"
_LOG_FORMAT = f"format:{_RECORD_SEP}%H{_FIELD_SEP}%an{_FIELD_SEP}%aI{_FIELD_SEP}%s"

# `git hash-object -t tree /dev/null` in any SHA-1 repo. Used only if that call
# itself fails, so a SHA-256 repo still gets a real answer from git.
_EMPTY_TREE_FALLBACK = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

_SHORTSTAT_RE = re.compile(
    r"(?P<files>\d+)\s+files?\s+changed"
    r"(?:,\s+(?P<insertions>\d+)\s+insertions?\(\+\))?"
    r"(?:,\s+(?P<deletions>\d+)\s+deletions?\(-\))?"
)


# --------------------------------------------------------------------------- #
# git invocation
# --------------------------------------------------------------------------- #


def _git(
    repo: Path, *args: str, timeout: int = GIT_TIMEOUT_SECONDS
) -> subprocess.CompletedProcess[str]:
    """Run a git command and return the completed process without checking rc.

    Raises ResourceError only for conditions that are never a normal git "no"
    answer: git missing entirely, or the command hanging.
    """
    argv = ["git", "-C", str(repo), "--no-optional-locks", *args]
    env = {
        **os.environ,
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
    }
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except FileNotFoundError as exc:
        raise ResourceError("git is not installed on this device.") from exc
    except subprocess.TimeoutExpired as exc:
        raise ResourceError(
            f"git {args[0]} timed out after {timeout}s in {repo}."
        ) from exc


def _run_git(repo: Path, *args: str, timeout: int = GIT_TIMEOUT_SECONDS) -> str:
    """Run a git command, raising ResourceError on a nonzero exit."""
    proc = _git(repo, *args, timeout=timeout)
    if proc.returncode != 0:
        # `safe.directory` / dubious-ownership failures surface here verbatim.
        # That is deliberate -- we never auto-add safe.directory on the user's
        # behalf, we just tell them what git said.
        stderr = (proc.stderr or "").strip() or f"exit status {proc.returncode}"
        raise ResourceError(f"git {args[0]} failed: {stderr[:300]}")
    return proc.stdout


def require_git_repo(path: Path) -> Path:
    """Assert that `path` is the top level of a git repository or worktree."""
    # .exists() rather than .is_dir(): linked worktrees and submodules carry a
    # .git *file* pointing at the real gitdir.
    if not (path / ".git").exists():
        raise ResourceError(f"Not a git repository: {path}")
    return path


def _reject_option_like(value: str, param: str) -> str:
    """Refuse values that git would parse as an option rather than a ref."""
    if not value or value.startswith("-"):
        raise ResourceError(f"Invalid {param}: {value!r} is not a valid git ref.")
    return value


def _verify_ref(repo: Path, ref: str, param: str) -> str:
    """Reject option-like refs, then resolve `ref` to a full commit hash."""
    _reject_option_like(ref, param)
    proc = _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    resolved = proc.stdout.strip()
    if proc.returncode != 0 or not resolved:
        raise ResourceError(f"Unknown {param} ref in {repo.name}: {ref}")
    return resolved


def _has_commits(repo: Path) -> bool:
    """True when HEAD resolves -- False for a freshly `git init`ed repo."""
    return _git(repo, "rev-parse", "--verify", "--quiet", "HEAD").returncode == 0


def _current_branch(repo: Path) -> str:
    """Branch name, or "HEAD" when detached."""
    proc = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if proc.returncode == 0:
        return proc.stdout.strip()
    # An unborn HEAD (repo with no commits) cannot be rev-parsed, but `git init`
    # has already pointed the symbolic ref at the default branch.
    return _run_git(repo, "symbolic-ref", "--short", "HEAD").strip()


def _empty_tree_hash(repo: Path) -> str:
    """The empty-tree object id, for diffing a repo's entire history."""
    proc = _git(repo, "hash-object", "-t", "tree", "/dev/null")
    resolved = proc.stdout.strip()
    if proc.returncode != 0 or not resolved:
        return _EMPTY_TREE_FALLBACK
    return resolved


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #


def _describe_repo(path: Path) -> dict:
    """Probe one repository, degrading to an `error` field instead of raising.

    A single unreadable repo (bad ownership, corrupt objects) must not abort a
    whole discovery scan.
    """
    entry: dict = {
        "path": str(path),
        "name": path.name,
        "branch": None,
        "is_worktree": (path / ".git").is_file(),
        "is_dirty": False,
        "last_commit": None,
        "error": None,
    }
    try:
        entry["branch"] = _current_branch(path)
        entry["is_dirty"] = bool(_run_git(path, "status", "--porcelain").strip())
        if _has_commits(path):
            raw = _run_git(
                path, "log", "-1", f"--pretty={_LOG_FORMAT}"
            ).strip(_RECORD_SEP + "\n")
            if raw:
                hash_, _author, date, subject = _split_header(raw)
                entry["last_commit"] = {
                    "hash": hash_,
                    "date": date,
                    "subject": subject,
                }
    except ResourceError as exc:
        entry["error"] = str(exc)
    return entry


def discover_repositories(
    allowed_roots,
    max_depth: int = MAX_SCAN_DEPTH,
    max_repos: int = MAX_REPOS,
) -> dict:
    """Find git repositories under the shared folders.

    Breadth-first from each root, never descending into a repository once found
    and never following symlinked directories. Bounded by `max_repos` and by
    DISCOVERY_TIME_BUDGET_SECONDS so a huge tree yields a truncated answer
    rather than a hung request.
    """
    deadline = time.monotonic() + DISCOVERY_TIME_BUDGET_SECONDS
    repositories: list[dict] = []
    scanned_roots: list[str] = []
    seen: set[str] = set()
    truncated = False

    for root in allowed_roots:
        root = Path(root)
        scanned_roots.append(str(root))
        if not root.is_dir():
            continue

        queue: deque[tuple[Path, int]] = deque([(root, 0)])
        while queue:
            if len(repositories) >= max_repos or time.monotonic() > deadline:
                truncated = True
                break

            current, depth = queue.popleft()
            key = str(current)
            if key in seen:
                continue
            seen.add(key)

            if (current / ".git").exists():
                repositories.append(_describe_repo(current))
                # Do not descend into a repository: its subdirectories are that
                # repo's content, not more repos to report.
                continue

            if depth >= max_depth:
                continue

            try:
                with os.scandir(current) as entries:
                    children = sorted(entries, key=lambda e: e.name)
            except OSError:
                continue

            for entry in children:
                if entry.name.startswith("."):
                    continue
                # follow_symlinks=False: a symlinked directory would let the
                # scan escape the watched root and can also cycle forever.
                if not entry.is_dir(follow_symlinks=False):
                    continue
                queue.append((Path(entry.path), depth + 1))

        if truncated:
            break

    return {
        "repositories": repositories,
        "truncated": truncated,
        "scanned_roots": scanned_roots,
    }


# --------------------------------------------------------------------------- #
# change summary
# --------------------------------------------------------------------------- #


def _split_header(line: str) -> tuple[str, str, str, str]:
    fields = line.split(_FIELD_SEP)
    while len(fields) < 4:
        fields.append("")
    return fields[0], fields[1], fields[2], _FIELD_SEP.join(fields[3:])


def _parse_numstat(line: str) -> dict | None:
    """Parse one `--numstat` row: ``<insertions>\\t<deletions>\\t<path>``."""
    parts = line.split("\t", 2)
    if len(parts) != 3:
        return None
    added, removed, path = parts
    return {
        "path": path,
        # Binary files report "-" for both counts.
        "insertions": None if added == "-" else _safe_int(added),
        "deletions": None if removed == "-" else _safe_int(removed),
    }


def _safe_int(value: str) -> int | None:
    try:
        return int(value)
    except ValueError:
        return None


def _parse_shortstat(text: str) -> dict:
    match = _SHORTSTAT_RE.search(text)
    if not match:
        return {"files_changed": 0, "insertions": 0, "deletions": 0}
    return {
        "files_changed": int(match.group("files")),
        "insertions": int(match.group("insertions") or 0),
        "deletions": int(match.group("deletions") or 0),
    }


def _parse_status(text: str, limit: int) -> tuple[list[dict], bool]:
    lines = [line for line in text.split("\n") if line.strip()]
    entries = [
        {"state": line[:2], "path": line[3:]} for line in lines[:limit]
    ]
    return entries, len(lines) > limit


def summarize_changes(
    repo: Path,
    since: str = "24 hours ago",
    until: str | None = None,
    max_commits: int = 30,
    include_uncommitted: bool = True,
) -> dict:
    """Summarize committed and uncommitted work in `repo` over a time window."""
    require_git_repo(repo)
    max_commits = max(1, min(int(max_commits), MAX_COMMITS_LIMIT))

    commits: list[dict] = []
    commits_truncated = False
    totals = {"commits": 0, "insertions": 0, "deletions": 0}
    branch = _current_branch(repo)

    if _has_commits(repo):
        args = [
            "log",
            # Always one argv token, so a hostile `since` cannot become a flag.
            f"--since={since}",
            "--date=iso-strict",
            # +1 so we can tell "exactly max_commits" from "more than that".
            f"--max-count={max_commits + 1}",
            f"--pretty={_LOG_FORMAT}",
            "--numstat",
        ]
        if until:
            args.insert(2, f"--until={until}")
        raw = _run_git(repo, *args)

        records = [rec for rec in raw.split(_RECORD_SEP) if rec.strip()]
        if len(records) > max_commits:
            records = records[:max_commits]
            commits_truncated = True

        for record in records:
            lines = record.split("\n")
            hash_, author, date, subject = _split_header(lines[0])
            files = [
                parsed
                for parsed in (_parse_numstat(line) for line in lines[1:] if line.strip())
                if parsed is not None
            ]
            insertions = sum(f["insertions"] or 0 for f in files)
            deletions = sum(f["deletions"] or 0 for f in files)
            commits.append(
                {
                    "hash": hash_,
                    "author": author,
                    "date": date,
                    "subject": subject,
                    "files_changed": len(files),
                    "insertions": insertions,
                    "deletions": deletions,
                    "files": files[:MAX_FILES_PER_COMMIT],
                    "files_truncated": len(files) > MAX_FILES_PER_COMMIT,
                }
            )
            totals["commits"] += 1
            totals["insertions"] += insertions
            totals["deletions"] += deletions

    uncommitted = None
    if include_uncommitted and _has_commits(repo):
        status_text = _run_git(repo, "status", "--porcelain")
        status, status_truncated = _parse_status(status_text, MAX_STATUS_FILES)
        uncommitted = {
            "status": status,
            "status_truncated": status_truncated,
            "diff_stat": _parse_shortstat(
                _run_git(repo, "diff", "--shortstat", "HEAD")
            ),
        }

    return {
        "repo_path": str(repo),
        "branch": branch,
        "since": since,
        "until": until,
        "commits": commits,
        "commits_truncated": commits_truncated,
        "total": totals,
        "uncommitted": uncommitted,
    }


# --------------------------------------------------------------------------- #
# raw diff
# --------------------------------------------------------------------------- #


def _truncate_diff(text: str, max_bytes: int) -> tuple[str, bool, int]:
    """Byte-cap diff text on a line boundary so the LLM never sees half a line."""
    raw = text.encode("utf-8")
    total_bytes = len(raw)
    if total_bytes <= max_bytes:
        return text, False, total_bytes
    clipped = raw[:max_bytes].decode("utf-8", errors="ignore")
    last_newline = clipped.rfind("\n")
    if last_newline != -1:
        clipped = clipped[: last_newline + 1]
    return clipped, True, total_bytes


def get_diff(
    repo: Path,
    since: str | None = None,
    commit: str | None = None,
    base: str | None = None,
    head: str | None = None,
    max_bytes: int = DEFAULT_DIFF_BYTES,
) -> dict:
    """Return capped raw diff text for `repo` in one of four modes."""
    require_git_repo(repo)
    max_bytes = max(1, min(int(max_bytes), MAX_DIFF_BYTES))

    selectors = [
        name
        for name, value in (("commit", commit), ("base", base), ("since", since))
        if value
    ]
    if len(selectors) > 1:
        raise ResourceError(
            "Use only one of commit, base, or since -- got: " + ", ".join(selectors)
        )
    if head and not base:
        raise ResourceError("head is only valid together with base.")

    untracked_files: list[str] = []
    untracked_truncated = False

    if commit:
        resolved = _verify_ref(repo, commit, "commit")
        mode = "commit"
        base_out, head_out = None, resolved
        # `--format=` drops the commit header so only the patch comes back.
        text = _run_git(repo, "show", "--format=", "--patch", resolved, "--")

    elif base:
        resolved_base = _verify_ref(repo, base, "base")
        resolved_head = _verify_ref(repo, head, "head") if head else _verify_ref(
            repo, "HEAD", "head"
        )
        mode = "range"
        base_out, head_out = resolved_base, resolved_head
        text = _run_git(repo, "diff", resolved_base, resolved_head, "--")

    elif since:
        if not _has_commits(repo):
            raise ResourceError("Repository has no commits yet.")
        resolved_head = _verify_ref(repo, "HEAD", "head")
        # The newest commit *older* than the window is the diff's starting point.
        boundary = _run_git(
            repo, "rev-list", "-1", f"--before={since}", "HEAD"
        ).strip()
        # No boundary means the whole history falls inside the window, so diff
        # against the empty tree to show every line as added.
        resolved_base = boundary or _empty_tree_hash(repo)
        mode = "since"
        base_out, head_out = resolved_base, resolved_head
        text = _run_git(repo, "diff", resolved_base, resolved_head, "--")

    else:
        if not _has_commits(repo):
            raise ResourceError("Repository has no commits yet.")
        mode = "working_tree"
        base_out, head_out = _verify_ref(repo, "HEAD", "head"), None
        text = _run_git(repo, "diff", "HEAD", "--")
        # `git diff HEAD` ignores untracked files entirely, so list their names
        # separately -- names only, since their full contents could be huge.
        status_text = _run_git(repo, "status", "--porcelain")
        names = [
            line[3:]
            for line in status_text.split("\n")
            if line.startswith("??")
        ]
        untracked_files = names[:MAX_UNTRACKED_FILES]
        untracked_truncated = len(names) > MAX_UNTRACKED_FILES

    diff_text, truncated, total_bytes = _truncate_diff(text, max_bytes)
    return {
        "repo_path": str(repo),
        "mode": mode,
        "base": base_out,
        "head": head_out,
        "diff": diff_text,
        "truncated": truncated,
        "total_bytes": total_bytes,
        "untracked_files": untracked_files,
        "untracked_truncated": untracked_truncated,
    }
