# Git progress-summary MCP tools for anubis-mcp-server-mac-os

> Plan for the feature described in [`_FEATURE.md`](./_FEATURE.md).
> Implementation target: `/Users/evan/Developer/anubis-mcp-server-mac-os` (branch `test`).

## Context

The goal is to *query* the Neural Nexus avatar for intermittent status updates ("what changed in
my repos in the last N hours?") covering any git repository watched by the device MCP server. The
avatar's backend agent (`efwoods/anubis`) calls MCP tools on the device; the tools run **read-only**
git commands and return structured data that the avatar LLM summarizes conversationally.

This is the pull-based counterpart to `hourly_progress.py` in this repo, which pushes
`git diff HEAD` to the avatar on a cron schedule. Same underlying idea — avatar narrates repo
progress in first person — but the avatar decides *when* and *which repo*, instead of cron.

**Decisions:**

- **Device-side only.** The anubis backend hardcodes wrappers for exactly four MCP tool names in
  `src/anubis/utils/tools/data_analysis/analysis_tools.py`; new device tools are invisible to the
  LLM until matching wrappers are added there. That backend work is deferred — this plan ships the
  device tools plus a **backend handoff spec** (Step 6).
- **Target repo is `anubis-mcp-server-mac-os`**, layered on top of the completed macOS/launchd port.
  `anubis-mcp-server-ubuntu-desktop` stays reference-only and is never modified.
- **Watched repos are discovered under the existing `watched_roots` config** — no new config field.
  Watching a dev folder uses the existing `add-folder` flow; path security reuses `_resolve_allowed_dir`.

## Assumptions about the target repo

The macOS port has landed in the mac-os working tree (launchd `scripts/`, `neuralnexus-mcp.sh`,
`.venv`, `uv.lock` all present, currently uncommitted). Specifically this plan relies on:

- `src/server/app.py` matching the ubuntu structure — tool closures at `app.py:99-168`,
  `_resolve_allowed_*` helpers at `app.py:57-80` — with only the `server_name` default changed.
- `pytest` present in `requirements.dev.txt` and the existing tests green. There is **no
  pytest-asyncio**, so new tests are sync `def`s; any in-memory client check wraps `asyncio.run()`.
- The launchd plist `PATH` includes `/usr/bin`, so Apple Git resolves under launchd.

No new pip dependencies — stdlib `subprocess`/`os`/`shutil` plus the existing fastmcp.

## Design decisions

- **Logic lives in a new module `src/server/git_tools.py`** (module-level and therefore
  unit-testable; the existing closure-based tools are not). `create_mcp_server()` registers thin
  async closures over it. `git_tools` never imports from `app` (no cycle) and receives
  already-validated `Path`s — allow-listing stays in the closures via the existing
  `_resolve_allowed_dir` (`src/server/app.py:69`).
- **Sync `subprocess.run` wrapped in `asyncio.to_thread`**, matching the codebase's tool convention
  (`app.py:99-158`) and keeping tests sync.
- **Read-only guarantees:** argv lists only, never `shell=True`; every invocation is
  `git -C <repo> --no-optional-locks …` with env `GIT_OPTIONAL_LOCKS=0` and `GIT_TERMINAL_PROMPT=0`;
  the only subcommands ever used are `rev-parse`, `rev-list`, `log`, `status`, `diff`, `show`, and
  `hash-object -t tree /dev/null`.
- **Injection hardening:** user-supplied refs (`commit`, `base`, `head`) are rejected if empty or
  starting with `-`, then verified with `git rev-parse --verify <ref>^{commit}` before use;
  `since`/`until` are always embedded as single argv tokens `--since=<value>`; a `--` separator
  precedes any path position.
- **Worktrees:** a `.git` *file* (linked worktree or submodule) counts as a repo, flagged
  `is_worktree: true`. A worktree whose gitdir resolves outside the allowed roots is still readable —
  a deliberate, documented relaxation, since the entry path the user shared is inside a watched root.
- **Output is capped device-side.** The relay has a 120s per-request ceiling and responses land whole
  in the LLM context, so every list is capped with an explicit `truncated` flag and diff text is
  byte-capped.

## Step 1 — New `src/server/git_tools.py`

Constants: `GIT_TIMEOUT_SECONDS=15`, `DISCOVERY_TIME_BUDGET_SECONDS=45`, `MAX_SCAN_DEPTH=3`,
`MAX_REPOS=50`, `MAX_COMMITS_LIMIT=100`, `MAX_FILES_PER_COMMIT=20`, `MAX_STATUS_FILES=100`,
`MAX_UNTRACKED_FILES=50`, `DEFAULT_DIFF_BYTES=100_000`, `MAX_DIFF_BYTES=500_000`.

**1. `_run_git(repo: Path, *args, timeout=GIT_TIMEOUT_SECONDS) -> str`**

```python
subprocess.run(
    ["git", "-C", str(repo), "--no-optional-locks", *args],
    capture_output=True, text=True, timeout=timeout,
    env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"},
)
```

Maps `FileNotFoundError` → `ResourceError("git is not installed on this device.")`,
`TimeoutExpired` → `ResourceError`, and nonzero exit →
`ResourceError(f"git {args[0]} failed: {stderr[:300]}")`. That last case is where `safe.directory`
errors surface verbatim — do **not** auto-add `safe.directory`.

**2. `require_git_repo(path: Path) -> Path`** — `(path / ".git").exists()` (covers both directories
and files) else `ResourceError(f"Not a git repository: {path}")`.

**3. `_reject_option_like(value: str, param: str) -> str`** — `ResourceError` on empty or leading `-`.

**4. `discover_repositories(allowed_roots, max_depth=MAX_SCAN_DEPTH, max_repos=MAX_REPOS) -> dict`**

Per root: check the root itself, then breadth-first `os.scandir` to `max_depth`. Skip non-directories
and symlinked directories (`entry.is_dir(follow_symlinks=False)`) and hidden names. On finding a repo,
record it and do not descend into it. Stop with `truncated=True` at `max_repos` or once past the
`time.monotonic()` deadline.

Per repo — each probe wrapped so one broken repo yields an `error` field and the scan continues:
branch from `rev-parse --abbrev-ref HEAD` (`"HEAD"` when detached), dirty from a non-empty
`status --porcelain`, last commit from `log -1 --format=%H%x00%cI%x00%s` (an empty repo gives
`last_commit: None` with no error).

Returns:

```python
{
  "repositories": [
    {"path": str, "name": str, "branch": str, "is_worktree": bool, "is_dirty": bool,
     "last_commit": {"hash": str, "date": str, "subject": str} | None,
     "error": None | str},
  ],
  "truncated": bool,
  "scanned_roots": [str, ...],
}
```

**5. `summarize_changes(repo, since="24 hours ago", until=None, max_commits=30, include_uncommitted=True) -> dict`**

`require_git_repo` first; clamp `max_commits` to `[1, MAX_COMMITS_LIMIT]`.

Committed changes come from one log call:

```
log --since=<since> [--until=<until>] --date=iso-strict --max-count=<max_commits+1> \
    --pretty=format:%x1e%H%x1f%an%x1f%aI%x1f%s --numstat
```

Records split on `\x1e`, header fields on `\x1f`; numstat lines are `ins\tdel\tpath` (binary files
use `-` → `None`; rename paths like `old => new` are kept raw). Getting `max_commits+1` records means
drop the last and set `commits_truncated=True`. Each commit's `files` list is capped at
`MAX_FILES_PER_COMMIT` with a per-commit `files_truncated`. An empty repo (git says "does not have
any commits") returns `commits: []` rather than raising.

Uncommitted state, when enabled and HEAD exists: `status --porcelain` (capped at `MAX_STATUS_FILES`,
parsed into `{"state", "path"}`) plus `diff --shortstat HEAD` regex-parsed into
`{files_changed, insertions, deletions}`.

Returns:

```python
{
  "repo_path": str, "branch": str, "since": str, "until": str | None,
  "commits": [
    {"hash": str, "author": str, "date": str, "subject": str,
     "files_changed": int, "insertions": int, "deletions": int,
     "files": [{"path": str, "insertions": int | None, "deletions": int | None}],
     "files_truncated": bool},
  ],
  "commits_truncated": bool,
  "total": {"commits": int, "insertions": int, "deletions": int},
  "uncommitted": {"status": [...], "status_truncated": bool, "diff_stat": {...}} | None,
}
```

**6. `get_diff(repo, since=None, commit=None, base=None, head=None, max_bytes=DEFAULT_DIFF_BYTES) -> dict`**

`require_git_repo`; clamp `max_bytes` to `[1, MAX_DIFF_BYTES]`; more than one of `commit`/`base`/`since`
raises `ResourceError` (`head` is only valid with `base`). Modes:

| Mode | Behavior |
|---|---|
| `commit` | verify the ref, then `show --format= --patch <commit> --` |
| `range` | verify both refs, then `diff <base> <head\|HEAD> --` |
| `since` | `base = rev-list -1 --before=<since> HEAD`, then `diff <base> HEAD --`; if there is no base (whole history is inside the window) diff against the empty tree from `hash-object -t tree /dev/null` |
| `working_tree` (default) | `diff HEAD --` (empty repo → `ResourceError("Repository has no commits yet.")`), plus untracked file *names* from `status --porcelain` `??` lines, capped at `MAX_UNTRACKED_FILES` |

Truncation: encode UTF-8, slice to `max_bytes`, decode with `errors="ignore"`, cut back to the last
newline, set `truncated=True`.

Returns `{"repo_path", "mode", "base", "head", "diff", "truncated", "total_bytes", "untracked_files", "untracked_truncated"}`.

## Step 2 — Modify `src/server/app.py`

Add `from src.server import git_tools`, then register three closures inside `create_mcp_server()`
after `read_files_for_sandbox` (`app.py:168`) and before the `DirectoryResource` loop:

```python
@mcp.tool()
async def list_git_repositories() -> dict:
    """Discover git repositories under the shared folders with branch, dirty state, and last commit."""
    if not allowed_roots:
        raise ResourceError("No shared folders are configured on this device.")
    return await asyncio.to_thread(git_tools.discover_repositories, allowed_roots)


@mcp.tool()
async def git_changes_summary(repo_path: str, since: str = "24 hours ago",
                              until: str | None = None, max_commits: int = 30,
                              include_uncommitted: bool = True) -> dict:
    """Summarize committed and uncommitted changes in a git repository over a time window."""
    repo = _resolve_allowed_dir(repo_path, allowed_roots)
    return await asyncio.to_thread(git_tools.summarize_changes, repo, since, until,
                                   max_commits, include_uncommitted)


@mcp.tool()
async def git_diff(repo_path: str, since: str | None = None, commit: str | None = None,
                   base: str | None = None, head: str | None = None,
                   max_bytes: int = 100_000) -> dict:
    """Return capped raw git diff text: working tree, a single commit, a base..head range, or a time window."""
    repo = _resolve_allowed_dir(repo_path, allowed_roots)
    return await asyncio.to_thread(git_tools.get_diff, repo, since, commit, base, head, max_bytes)
```

Docstrings should recommend ISO-8601 or `"N hours ago"` for `since`/`until` (git approxidate).
No `/discovery`, `/health`, or settings changes.

## Step 3 — New `tests/test_git_tools.py`

Module-level `pytestmark = pytest.mark.skipif(shutil.which("git") is None, ...)`; the git-missing
test monkeypatches instead of needing real git.

Helpers: `_git(repo, *args, env=None)` via `subprocess.run(check=True)`, and
`_commit(repo, msg, when=None)` which sets **`GIT_COMMITTER_DATE`** (plus `GIT_AUTHOR_DATE`) —
`log --since` filters on committer date, so `--date` alone is not enough — and passes
`-c user.email=… -c user.name=…`. Fixture `make_repo(tmp_path)` does `git init -b main` plus an
initial commit.

Roughly 17 cases:

- **Discovery** — finds a normal repo, a repo nested at depth 2, and a linked worktree created with
  `git worktree add` (`is_worktree=True`); skips non-repos, hidden dirs, symlinks, and repos at
  depth 4; `max_repos=1` with two repos sets `truncated`; an empty repo yields `last_commit=None`
  with no error.
- **Summary** — a commit backdated three days is excluded by `since="24 hours ago"`; ISO `since` +
  `until` brackets exactly one commit; a modified tracked file plus an untracked file produce ` M`
  and `??` status entries with a nonzero `diff_stat`; `max_commits` and the per-commit files cap
  (monkeypatch `MAX_FILES_PER_COMMIT` to something small) set their truncated flags; an empty repo
  returns `commits == []`.
- **Diff** — working-tree mode contains the hunk and lists untracked names only; `since`, `commit`,
  and `base` modes return the expected patches; `max_bytes=200` sets `truncated` and keeps the
  payload within the cap; option-like refs (`"--output=/tmp/pwn"`, `"-R"`) raise `ResourceError`;
  conflicting modes raise `ResourceError`.
- **Errors** — a non-repo directory raises "Not a git repository"; a monkeypatched
  `FileNotFoundError` raises "git is not installed".
- **Denial** — `_resolve_allowed_dir(outside_repo, allowed_roots)` raises, plus one end-to-end
  `asyncio.run(...)` in-memory `fastmcp.Client` call of `git_changes_summary` against a repo outside
  the roots returning "Path not allowed".

The existing autouse `isolated_config_dir` fixture in `tests/conftest.py` applies unchanged.

## Step 4 — Docs

In the mac-os `README.md`: add the three tools to the tools / API-contract table, plus a short
"Git progress summaries" section covering that repos are discovered under shared folders only, that
all access is read-only (`--no-optional-locks`), the worktree-gitdir-outside-roots caveat, and that
`safe.directory` errors surface as tool errors (fixed with
`git config --global --add safe.directory <path>`). No `.env.example` changes.

## Step 5 — Verification

```bash
cd /Users/evan/Developer/anubis-mcp-server-mac-os
.venv/bin/python -m pytest tests/ -v          # existing tests + ~17 new, all green

# Foreground smoke against real repos:
MCP_REQUIRE_DEVICE_AUTH=false MCP_WATCHED_ROOTS="$HOME/Developer" .venv/bin/python -m src.server.app
```

In a second shell (streamable-HTTP needs `Accept: application/json, text/event-stream`; run
`initialize` first and reuse the `mcp-session-id` header if sessions are enforced):

- `tools/list` → 8 tools.
- `tools/call` `list_git_repositories` → finds real repos including worktrees.
- `git_changes_summary` with `since: "48 hours ago"` on a real repo.
- `git_diff` with `max_bytes: 5000` → confirms the truncation flag.
- Negative: `repo_path: "/etc"` → "Path not allowed"; a watched non-repo dir → "Not a git repository".

Then repeat under launchd (`./neuralnexus-mcp.sh restart`) and, if a real API key is configured,
end-to-end through the relay — responses should land well inside the 120s window.

## Step 6 — Backend handoff spec (`efwoods/anubis`, deferred)

- The device server (server_name `macOS-Filesystem`) exposes `list_git_repositories()`,
  `git_changes_summary(repo_path, since, until, max_commits, include_uncommitted)`, and
  `git_diff(repo_path, since, commit, base, head, max_bytes)` with the return shapes above. All lists
  carry `truncated` flags and diff text is `≤ max_bytes` (hard cap 500 KB).
- **The avatar will not see these automatically.**
  `src/anubis/utils/tools/data_analysis/analysis_tools.py` hardcodes wrappers for only
  `list_all_files`, `preview_data`, `get_file_info`, and `read_file_bytes`. LangChain wrappers must be
  added for the three new names — the intended landing spot is `src/anubis/utils/tools/git/git_tools.py`,
  already referenced by `research/data_analysis_slack_agent/reference_code/connectors/git_local.py`.
- Mention the capability in `DATA_ANALYSIS_CAPABILITY_PROMPT`
  (`src/anubis/utils/prompts/system_prompts.py:547`).
- `mcp_client.py` keeps a module-level tool cache that is never invalidated — the backend must be
  restarted after the device upgrades before the new tools appear.
- Suggested agent flow: `list_git_repositories` → `git_changes_summary(since=f"{N} hours ago")` for
  each interesting repo → `git_diff` only when the summary is insufficient, with a small `max_bytes`.

## Risks

- `git log --since` approxidate parsing is lenient — nonsense strings parse loosely rather than
  erroring. Mitigated by docstring guidance and by echoing `since`/`until` back in responses so the
  LLM can sanity-check the window.
- `safe.directory` / dubious-ownership failures surface as clean `ResourceError`s and are
  deliberately not auto-bypassed.
- Huge repos or wide windows are bounded by `--max-count`, the 15s per-command timeout, and the 45s
  discovery budget — the worst case is a clean error, never a hung relay request.
- Submodules are reported as repos (`is_worktree=True`) only when directly under a scanned path;
  they are never enumerated from a parent repo.
- A machine missing Command Line Tools gets the `/usr/bin/git` xcode-select error, surfaced through
  the `ResourceError` stderr snippet.
