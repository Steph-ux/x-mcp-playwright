# Changelog

## 1.2.0 — Rate limiter, keepalive, cleanup

### Added
- **Rate limiter** — token bucket for write actions (post/delete/DM/reply/engage). 1 write/min, 2 engage/min. Read actions unlimited. Prevents accidental spam from LLM overuse.
- **Session keepalive** — background task pings `/home` every 30 min. Logs warning on expiration.
- `.dockerignore` — excludes `.venv/`, `__pycache__/`, `tests/`, `.git/` from Docker builds.

### Removed
- Dead code: `ToolResult` class, `deque` import.

## 1.1.0 — Performance overhaul

### Changed
- **Page pool** — 3 pages reused across tool calls instead of open+close per call (~200-500ms saved per call).
- **Sleep reduction** — all `wait_for_timeout` values halved (1500→800, 2500→1000, 3000→1500).
- **Smart scroll** — early exit after 2 stale rounds. Scroll distance increased (2500→3000px). Settle wait reduced (1500→800ms).
- **`wait_stable`** helper for DOM-ready detection (unused yet, available for future tools).

## 1.0.0 — Tool consolidation (33 → 9)

### Changed
- **33 individual tools → 9 grouped tools** with `action` discriminator. Zero functionality lost.
  - `x_post` (6 actions), `x_engage` (4), `x_user` (6), `x_search` (2), `x_feed` (4), `x_tweet` (4), `x_list` (2), `x_dm` (3), `x_session` (2).
- All internal logic extracted to `_do_*` private functions. Gateway tools are thin dispatchers.

## 0.6.0 — Portability

### Added
- `utils.py` — shared `find_chrome()`, `USER_DATA_DIR`, `HEADLESS`, `SCREENSHOT_DIR`, `_log()`.
- `__main__.py` — `python -m x_mcp_server` support.
- `Dockerfile` — headless VPS container with Chromium + profile mount.
- `.dockerignore` for lean Docker builds.
- `pyproject.toml` entry points: `x-mcp-server`, `x-mcp-login`.

### Changed
- `login.py` and `fetch_tweets.py` now import from `utils` instead of duplicating `find_chrome()`.
- README rewritten with universal instructions (`pip install -e .`, Docker, multi-client config).
- `.gitignore` expanded with `.opencode/`, `dist/`, `build/`.

## 0.5.0 — Toggle fusion + multi-tab merge

### Changed
- **Tool surface shrunk 42 → 33** by collapsing redundant pairs/variants into
  single tools with discriminator params. LLMs no longer need to remember
  `like_tweet` vs `unlike_tweet`; one tool with `on: bool = True` does both.

#### Toggles fused (-5 tools)
Each pair below collapsed into a single tool taking `on: bool = True`:
- `like_tweet` / `unlike_tweet` → `like_tweet(tweet_url, on=True)`
- `retweet` / `unretweet` → `retweet(tweet_url, on=True)`
- `bookmark_tweet` / `unbookmark_tweet` → `bookmark_tweet(tweet_url, on=True)`
- `follow_user` / `unfollow_user` → `follow_user(handle, on=True)`
- `pin_tweet` / `unpin_tweet` → `pin_tweet(tweet_url, on=True)`

#### Multi-tab merges (-4 tools)
- `get_user_tweets` / `get_likes` / `get_media_from_user` →
  `get_user_timeline(handle, tab="tweets"|"likes"|"media", limit)`
- `get_user_followers` / `get_user_following` →
  `get_user_connections(handle, kind="followers"|"following", limit)`
- `get_who_liked` / `get_who_retweeted` →
  `get_tweet_engagers(tweet_url, kind="likes"|"retweets", limit)`

### Updated
- `TOOLS_BY_CATEGORY` rewritten with 33 entries (was 42). Category counts:
  posting(6) + engagement(3) + users(6) + feeds(8) + lists(2) + messaging(3) +
  pin_analytics(3) + misc(2).
- `EXPECTED_TOOLS` in `tests/test_smoke.py` rewritten to match.
- Module docstring updated to document toggle/tab/kind signatures.
- Test count unchanged at 30 — registry-vs-registration drift tests still
  guarantee no ghosts and no uncategorized tools.

## 0.4.0 — Validation backfill + tool taxonomy

### Added
- **`TOOLS_BY_CATEGORY`** — single source of truth grouping every tool into
  one of: `posting`, `engagement`, `users`, `feeds`, `lists`, `messaging`,
  `pin_analytics`, `misc`. Plus `category_for(name)` lookup helper.
- **`x://tools/categories`** MCP resource — discoverable grouped catalog
  that flags drift between the registry and live `@mcp.tool()` registrations.
- **3 new tests** verifying the taxonomy stays in sync with the registry
  (no uncategorized tools, no ghosts, no duplicates across categories).

### Changed
- **Validation backfilled** into 10 iter-2 tools that had been left on the
  old `normalize_handle` + `max(min(int(limit)…))` pattern:
  `get_user_followers`, `get_user_following`, `get_trending_topics`,
  `get_lists`, `get_list_tweets`, `get_conversations`, `get_messages`,
  `get_likes`, `get_media_from_user`, `advanced_search`. Bad input now
  short-circuits with a structured `err_result(...)` before the browser
  ever launches.
- `advanced_search` additionally validates `since`/`until` (ISO YYYY-MM-DD),
  `lang` (2-3 letter ISO), and `filter` (`latest`/`top`/`people`/`media`).
- Test count: 27 → 30.

## 0.3.0 — Validation, analytics, pin/unpin

### Added
- **Input validators**: `validate_handle`, `validate_tweet_url`, `validate_tweet_text`,
  `validate_limit`, `validate_list_id`. Each raises `ValidationError`; tools call
  them at the boundary and return structured error payloads before touching the browser.
- **5 new tools** (total: 42):
  - `pin_tweet`, `unpin_tweet`
  - `get_who_liked`, `get_who_retweeted`
  - `get_tweet_analytics`
- **2 new MCP resources**:
  - `x://version` — server version string
  - `x://tools/list` — plain-text inventory of every tool / resource / prompt
- `_open_tweet_menu` helper for overflow-menu navigation (Pin / Unpin).
- `__version__` constant + `TWEET_MAX_CHARS_FREE` for clarity.
- Test coverage doubled: 12 → 27 tests. Now covers every validator boundary.

### Fixed
- `_log` previously recursed into itself because `print` was rebound before
  `_log` referenced it. Captured `_builtin_print` first; recursion gone.

## 0.2.0 — DMs, followers, lists, trends

### Added
- 10 new tools: `get_user_followers`, `get_user_following`, `get_trending_topics`,
  `get_lists`, `get_list_tweets`, `get_conversations`, `get_messages`,
  `get_likes`, `get_media_from_user`, `advanced_search`.
- Pydantic v2 models: `Tweet`, `UserProfile`, `ToolResult`.
- `with_retry(max_attempts, base_delay, catch)` async decorator with exponential
  backoff for transient Playwright failures.
- `tests/test_smoke.py` (12 tests) — registration, Pydantic, retry, helpers.
- `Makefile` with `install / login / test / smoke / server / introspect / lint / clean`.

## 0.1.0 — Initial rewrite

### Added
- Shared persistent `_BrowserManager` (~10× faster than per-call launches).
- 27 MCP tools across posting / engagement / users / feeds / messaging / misc.
- `x://session/status` resource + `analyze_tweet` and `draft_reply` prompts.
- Anti-detection via `patchright` + `--disable-blink-features=AutomationControlled`.
- Multi-OS Chrome auto-detection (Win / mac / Linux).
- Debug screenshots auto-saved under `X_MCP_SCREENSHOT_DIR` on every failure.
- `login.py` interactive one-time login flow.
- `fetch_tweets.py` smoke test CLI.
- `README.md`, `pyproject.toml`, `requirements.txt`, `.env.example`, `.gitignore`.
