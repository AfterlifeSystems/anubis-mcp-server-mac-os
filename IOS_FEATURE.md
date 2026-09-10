# Handoff — browsing insights + own-browser sign-in (continue on the Mac)

Written 2026-09-10 from the Linux desktop session. Everything below is fact checked against
the repositories, not recollection. A Claude session on the Mac laptop should be able to
pick up from this file alone.

---

## 1. What the feature is

**Browsing insights.** The avatar reads the owner's own web browsing history from every
machine the owner runs the Neural Nexus connector on, and learns two things from it:

- **Facts about the owner** → the identity store namespace `(assistant_id, user_id, "identity")`,
  which is loaded in full into every reply. These are things the avatar then simply knows.
- **Scored traits** → folded into the accumulated psychological profile under a new graded
  dimension **`browsing_behaviour`**, rendered into the system prompt every turn.

It also writes analysis-namespace Documents (retrievable by similarity) and saves a report
in the `/reports` list per pass.

**Own-browser sign-in** (built in the same session, shares the connector repos). Connect
cards open a site's real sign-in page as a tab in the owner's *own* browser via the
connector daemon, then bring the resulting session back. Included here because the Mac
connector repo carries part of it.

### Evan's four decisions on browsing insights (2026-09-10, answered explicitly)

1. **Full URLs leave the machine** — he rejected the aggregates-only and titles-only options.
2. **All three destinations** — identity facts AND psychological profile AND a saved report,
   *and* "this should be an intermittent continuous process that is updated whenever the user
   is browsing immediately".
3. **Trigger** — backfill once on first connect, on demand when asked, continuous background
   passes as he browses. A scheduled pass may only spend money "if there are new updates of
   usage (this needs to be cost effective)".
4. **Platforms** — Linux + macOS + Windows, Chromium + Firefox + Safari, **plus iOS**.

---

## 2. Where the code is — IT IS COMMITTED BUT **NOT PUSHED**

Four repositories, one commit each (two in the API repo). Nothing has been pushed to GitHub.
**This is the first thing to resolve on the Mac.**

| Repository (local path on the Linux box) | Remote | Branch | Commit |
|---|---|---|---|
| `~/gh/anubis-project/wt/f-anubis` | `https://github.com/efwoods/anubis.git` | `f-anubis` | `003df89` sign-in, `fd6a27f` browsing |
| `~/gh/anubis-project/wt/f-anubis-mcp-server-ubuntu-desktop` | `AfterlifeSystems/anubis-mcp-server-ubuntu-desktop` | `test` | `3e69f85` |
| `~/gh/anubis-project/wt/f-anubis-mcp-server-mac-os` | `AfterlifeSystems/anubis-mcp-server-mac-os` | `test` | `db7ddce` |
| `~/gh/anubis-project/wt/f-anubis-mcp-server-mobile` | `AfterlifeSystems/anubis-mcp-server-phone` | `test` | `c6bced6` |

`f-anubis` is 5 ahead of `origin/f-anubis`; the three connector repos are 2 ahead of
`origin/test`. The extra commits are **other sessions' work already committed on those
branches** — pushing the branch publishes those too. Evan was asked how he wanted the
handoff done and chose to write this document instead of pushing, so the transfer decision
is still open. Three ways to move it:

```bash
# A. Push the working branches (simplest; also publishes the other sessions' commits)
git -C ~/gh/anubis-project/wt/f-anubis push origin f-anubis
git -C ~/gh/anubis-project/wt/f-anubis-mcp-server-ubuntu-desktop push origin test
git -C ~/gh/anubis-project/wt/f-anubis-mcp-server-mac-os push origin test
git -C ~/gh/anubis-project/wt/f-anubis-mcp-server-mobile push origin test

# B. Push handoff branches only, leaving the shared branches where they are
git -C <repo> push origin HEAD:refs/heads/mac-handoff/browsing-insights

# C. No GitHub at all — bundle and carry the file across
git -C <repo> bundle create ~/Desktop/<name>.bundle <branch>
#   then on the Mac:  git fetch ~/Desktop/<name>.bundle <branch>:<branch>
```

⚠️ **The Linux working trees also hold ~52 files of OTHER sessions' uncommitted work**
(ambient vision, moderation/bans, psycho-analysis, Stripe). Those are deliberately *not* in
these commits. Do not `git add -A` in `wt/f-anubis` on the Linux box.

---

## 3. What is in each commit

### `f-anubis` — the API (2 commits, 3,655 insertions)

`003df89` **sign-in in your own browser**
- `src/anubis/utils/connected_accounts/desktop_login.py` (new, 417 lines)
- `src/api/webapp.py` — `_start_sign_in_browser` prefers the owner's browser and falls back
  to the hosted one; finish/cancel routes serve both; `/connectable_providers` answers
  `sign_in_in_your_own_browser` + `sign_in_device_label`
- `tests/unit_tests/test_desktop_sign_in.py` (22 tests), `test_connect_sign_in_routes.py` (10)

`fd6a27f` **browsing insights**
- `src/anubis/utils/browsing/` — `__init__.py`, `digest.py`, `history_client.py`,
  `insights.py`, `sweeper.py`, `tools.py`
- `src/anubis/utils/prompts/browsing_analysis_prompt.py`
- `src/anubis/utils/context.py` — 8 new `browsing_insights_*` fields
- `src/anubis/graph.py` — registers the `analyze_browsing_history` tool for personal avatars
- `src/api/webapp.py` — starts/stops `run_browsing_sweeper` in the lifespan
- `src/anubis/utils/tools/data_analysis/relay.py` — new `all_sessions()`
- `.env.example` — the 8 env vars (values are set in `.env` / `.env.dev`, which are gitignored
  and therefore **do not travel**; see §6)
- `tests/unit_tests/test_browsing_insights.py` (27 tests)

### `f-anubis-mcp-server-ubuntu-desktop` — `3e69f85` (2,769 insertions)

`src/server/local_databases.py`, `browser_history.py`, `history_tools.py` (browsing) plus
`browser_cookies.py`, `browser_tools.py` (own-browser sign-in), `app.py` registration,
`tests/test_browser_history.py` (27), `tests/test_browser_sign_in.py` (~30), README section,
and `BROWSER_HISTORY_INSIGHTS_FEATURE.md` rewritten from a stub into the built design.

### `f-anubis-mcp-server-mac-os` — `db7ddce` (1,401 insertions)

`local_databases.py`, `browser_history.py`, `history_tools.py`, `tests/test_browser_history.py`
— **byte-identical copies** of the Ubuntu ones — plus the `register_history_tools(mcp)` call in
`src/server/app.py`. Keep them identical; if you change one, copy it to the other.

### `f-anubis-mcp-server-mobile` — `c6bced6` (610 insertions)

`AnubisMCP/Tools/BrowsingHistoryStore.swift`, `AnubisMCP/Tools/HistoryTools.swift`,
`SafariHistoryExtension/` (Info.plist, entitlements, `SafariWebExtensionHandler.swift`,
`Resources/manifest.json`, `Resources/background.js`), the app-group entitlement, the
`ToolRegistry` registration, a `BrowserAutomator` hook that records in-app navigations, the
new `SafariHistoryExtension` target in `project.yml`, and a README section.

---

## 4. The contract every platform implements (do not add a per-platform branch)

Three MCP tools, identical names and shapes on Linux, macOS, Windows and iOS:

| Tool | Arguments | Returns |
|---|---|---|
| `browsing_activity_summary` | `since=""`, `default_days=30` | `{visit_count, latest_visit, watermark, platform, profiles[], profiles_unreadable[]}` — **counts only, no rows** |
| `read_browser_history` | `since=""`, `until=""`, `limit=2000`, `profile_identifier=""`, `default_days=30` | `{visits[], visit_count, since, watermark, profiles_read[], profiles_unreadable[], platform}` |
| `list_browser_history_profiles` | — | `[{identifier, browser_name, profile_name, family, database_path}]` |

One visit row:

```json
{"visited_at": "ISO-8601", "url": "...", "title": "...", "host": "...",
 "search_terms": "...", "visit_count": 1, "duration_seconds": 0,
 "browser_name": "...", "profile_name": "...", "family": "chromium|firefox|safari"}
```

`since` accepts a watermark (ISO timestamp), a plain date, or a bare number of days.
`EXPOSE_BROWSER_HISTORY=false` makes all three answer `{"disabled": true, "reason": ...}`.

### The database layouts (all handled in `browser_history.py`)

| Family | Tables | Epoch |
|---|---|---|
| Chromium (Chrome, Chromium, Brave, Edge, Opera, Vivaldi, Arc) | `urls` + `visits` | microseconds since **1601-01-01** |
| Firefox (+ LibreWolf, Waterfox, Zen) | `moz_places` + `moz_historyvisits` | microseconds since **1970-01-01** |
| Safari (macOS only) | `history_items` + `history_visits` | seconds since **2001-01-01** |

Every database is **copied aside with its `-wal`/`-shm` sidecars before reading**
(`local_databases.opened_copy`), because the browser holding it open is the one the owner is
using. A limit keeps the **newest** visits (the SQL orders DESC and the list is reversed).

---

## 5. The cost gate (the part Evan cares about — preserve it)

In `src/anubis/utils/browsing/sweeper.py::analyse_machine`. A pass spends a model call only
when **all** hold:

1. The machine holds a live relay socket (`relay.all_sessions()`); an asleep machine is never asked.
2. New visits since the watermark ≥ `BROWSING_INSIGHTS_MINIMUM_NEW_VISITS` (25) — **waived on
   the first pass over a machine** (that is the backfill) and **waived when the owner asks**
   (`force=True` from the tool).
3. ≥ `BROWSING_INSIGHTS_MINIMUM_SECONDS_BETWEEN_ANALYSES` (900) since that machine's last analysis.

The watermark advances to the newest visit **actually read**, never to the count's newest, so
a batch cut short by `limit` leaves the remainder for the next pass. Watermarks live per
machine at `(user_id, assistant_id, "browsing_watermark")` keyed by `device_id`, and carry
`{watermark, analysed_at, device_label, platform, hosts[], passes, visits_analysed}` — `hosts`
is what lets the next digest say which websites are *new*.

---

## 6. Environment variables

Added to `.env`, `.env.dev` (both gitignored — **they will not be on the Mac; recreate them**)
and `.env.example` (committed). Defaults live in `GlobalContext` and are what runs if unset.

| Variable | Default |
|---|---|
| `BROWSING_INSIGHTS_ENABLED` | `TRUE` |
| `BROWSING_INSIGHTS_POLL_SECONDS` | `300` |
| `BROWSING_INSIGHTS_MINIMUM_NEW_VISITS` | `25` |
| `BROWSING_INSIGHTS_MINIMUM_SECONDS_BETWEEN_ANALYSES` | `900` |
| `BROWSING_INSIGHTS_BACKFILL_DAYS` | `30` |
| `BROWSING_INSIGHTS_MAX_VISITS_PER_PASS` | `2000` |
| `BROWSING_INSIGHTS_MAX_DIGEST_CHARACTERS` | `24000` |
| `BROWSING_INSIGHTS_REPORT_ENABLED` | `TRUE` |

Connector side (the daemon's own `.env`): `EXPOSE_BROWSER_HISTORY` (default true).

---

## 7. What is verified, and what is not

**Verified on Linux:**
- API: full unit suite **1,883 passed, 0 failed** (`~3 min`), including the 27 browsing and 32
  sign-in tests. `ruff` clean on everything new.
- Ubuntu connector: `tests/test_browser_history.py` **27 passed**; whole runnable suite 62 passed.
- macOS connector: the same 27 tests pass (run with the Ubuntu repo's venv — the mac repo has
  no `.venv` on the Linux box).

**NOT verified — this is the Mac work:**
1. **iOS has never been compiled.** No Xcode on the Linux box. `project.yml` gains a
   `SafariHistoryExtension` app-extension target; `xcodegen generate` then a build is step one.
2. **The Safari Web Extension has never run.** Needs a device or simulator, and the owner must
   switch **Neural Nexus browsing** on in *Settings → Safari → Extensions* and allow it on
   websites.
3. **The app group `group.site.neuralnexus.anubis-mcp` does not exist yet in the Apple developer
   portal.** It is declared in both `AnubisMCP/AnubisMCP.entitlements` and
   `SafariHistoryExtension/SafariHistoryExtension.entitlements`, and both targets must be members
   or the extension's visits never reach the app. Note `CODE_SIGNING_ALLOWED: NO` is set in
   `project.yml` today — a real signed build will need that revisited.
4. **Safari's own `History.db` has never been read on a real Mac.** Needs **Full Disk Access**
   for whatever process runs the connector (System Settings → Privacy & Security → Full Disk
   Access). Without it the read is refused with a message naming the setting rather than
   returning an empty history — verify that message actually appears.
5. **The macOS connector repo has no virtual environment** on the Linux box; create one on the
   Mac (`uv venv` / `pip install -r requirements.txt`) before running its tests.
6. **The Ubuntu connector's `tests/test_browser_sign_in.py` (~30 tests) has never been run.**
   Claude Code's auto-mode classifier on the Linux box blocked every Bash command naming the
   cookie modules. Run it on the Mac (the file is committed) or with `! pytest ...` from the
   Claude Code prompt.
7. **No end-to-end run.** No pass has gone: real browser → connector → relay → API → store.
   That is the single most valuable thing to do next.

---

## 8. How to run the tests

```bash
# API (Linux box paths; the Mac clone will differ)
cd <anubis repo>
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/unit_tests/test_browsing_insights.py -q -p no:cacheprovider
# (PYTHONDONTWRITEBYTECODE is required on the Linux box: the __pycache__ dirs are root-owned.)

# Either connector
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_browser_history.py -q -p no:cacheprovider
```

The connector tests build real SQLite databases in `tmp_path` in all three layouts with each
family's own epoch, so they pass on any platform with no browser installed.

---

## 9. Suggested order of work on the Mac

1. Get the four commits onto the Mac (§2) and confirm the tests still pass.
2. **macOS connector against real Safari**: run the daemon, grant Full Disk Access, call
   `list_browser_history_profiles` and `read_browser_history`, confirm Safari rows come back
   with sane timestamps (the 2001 epoch is the thing most likely to be wrong in the wild) and
   that Chrome/Brave/Arc are found too.
3. **End-to-end**: point the connector at the dev API, let the sweeper run, and confirm a
   browsing pass writes identity facts, moves the `browsing_behaviour` dimension in the
   psychological profile, and saves a report. Check the watermark advances and a second pass
   with no browsing costs nothing.
4. **iOS**: `xcodegen generate`, build, create the app group, enable the extension, browse in
   Safari, and confirm `browsing_activity_summary` on the phone counts those visits.
5. Run `tests/test_browser_sign_in.py` in the Ubuntu connector repo (item 7.6).

---

## 10. Traps that already cost time

- **Shared working trees.** Several Claude sessions edit `wt/f-anubis` at once. `webapp.py`,
  `graph.py` and `context.py` each held other sessions' uncommitted work; the two API commits
  were built by reconstructing blobs (HEAD + only this session's lines) and staging them with
  `git hash-object` + `git update-index`, never `git add <file>`. Do the same, or work on the
  Mac clone where the tree is yours alone.
- **Never `git stash`** in these worktrees — the stash stack is shared across worktrees and
  other sessions pop it.
- **The auto-mode classifier** blocks Bash commands that name browser-cookie modules
  (pytest and `py_compile` included). Files can still be written and read with the file tools;
  run those tests from the prompt with `! ...`.
- **The three failures in `test_browser_sessions.py`** seen mid-session were another session's
  in-flight LangSmith → API-key change removing `login_url`, not this work. They are gone now.
- **iOS cannot read Safari's history directly** — do not go looking for a database. The Safari
  Web Extension is the only sanctioned route, which is why one was written.

---

## 11. Design decisions worth not re-litigating

- One structured-output call per pass produces facts *and* traits, because both come off the
  same evidence and a second call would double the cost of something that runs continuously.
- Facts below **0.55 confidence** are never written: the avatar states facts as its own
  knowledge, so a wrong one is worse than a missing one.
- The prompt (`browsing_analysis_prompt.py`) forbids inferring health, sexuality, religion,
  political allegiance, financial distress or legal trouble from a page visit, and tells the
  analysis to treat machine-opened pages as machinery rather than interest.
- `digest.py` drops single-sign-on pages, analytics beacons, CDNs and `localhost` before the
  model ever sees them, folds tens of thousands of rows into counts plus samples, and keeps
  **searches** above everything else — they are the person's own words.
- Traits are *graded* (0–1 with a confidence), so `merge_findings_into_profile` moves a running
  mean rather than stacking restatements. Verified by a test.
